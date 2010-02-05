# Part of Objavi2, which turns html manuals into books.
# This contains classes representing books and coordinates their processing.
#
# Copyright (C) 2009 Douglas Bagnall
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

"""Library module representing a complete FM book being turned into a
PDF"""

import os, sys
import tempfile
import re, time
import random
from subprocess import Popen, check_call, PIPE
from cStringIO import StringIO
from urllib2 import urlopen, HTTPError
import zipfile
import traceback
from string import ascii_letters
from pprint import pformat

try:
    import simplejson as json
except ImportError:
    import json

import lxml, lxml.html
from lxml import etree

from objavi import config, epub_utils
from objavi.cgi_utils import log, run, shift_file, make_book_name, guess_lang, guess_text_dir
from objavi.pdf import PageSettings, count_pdf_pages, concat_pdfs, rotate_pdf, parse_outline
from objavi.epub import add_guts, _find_tag

from iarchive import epub as ia_epub
from booki.xhtml_utils import EpubChapter
from booki.bookizip import get_metadata, add_metadata, clear_metadata, get_metadata_schemes

TMPDIR = os.path.abspath(config.TMPDIR)
DOC_ROOT = os.environ.get('DOCUMENT_ROOT', '.')
HTTP_HOST = os.environ.get('HTTP_HOST', '')
PUBLISH_PATH = "%s/books/" % DOC_ROOT


def _get_best_title(tocpoint):
    if 'html_title' in tocpoint:
        return tocpoint['html_title']
    if 'title' in tocpoint:
        return tocpoint['title']
    return 'Untitled'


def _add_initial_number(e, n):
    """Put a styled chapter number n at the beginning of element e."""
    initial = e.makeelement("strong", Class="initial")
    e.insert(0, initial)
    initial.tail = ' '
    if e.text is not None:
        initial.tail += e.text
    e.text = ''
    initial.text = "%s." % n

def expand_toc(toc, depth=1, index=0):
    """Reformat toc slightly for convenience"""
    for item in toc:
        url = item['url'].lstrip('/')
        bits = url.split('#', 1)
        filename = bits[0]
        fragment = (bits[1] if len(bits) == 2 else None)
        item['depth'] = depth
        item["filename"] = filename
        item["fragment"] = fragment
        item["index"] = index
        index += 1
        if 'children' in item:
            index = expand_toc(item['children'], depth + 1, index)
    return index

def _serialise(rtoc, stoc, depth):
    for item in rtoc:
        url = item['url'].lstrip('/')
        bits = url.split('#', 1)
        filename = bits[0]
        fragment = (bits[1] if len(bits) == 2 else None)
        stoc.append({"depth": depth,
                     "title": item['title'],
                     "url": url,
                     "filename": filename,
                     "fragment": fragment,
                     "type": item['type']
                     })
        if 'children' in item:
            _serialise(item['children'], stoc, depth + 1)


def serialise_toc(rtoc):
    """Take the recursive TOC structure and turn it into a list of
    serial points.  Reformat some things for convenience."""
    stoc = []
    _serialise(rtoc, stoc, 1)
    for i, x in enumerate(stoc):
        x['position'] = i
    return stoc

def filename_toc_map(rtoc):
    tocmap = {}
    log(rtoc)
    def traverse(toc):
        for point in toc:
            log(point.keys())
            tocmap.setdefault(point['filename'], []).append(point)
            if 'children' in point:
                traverse(point['children'])
    traverse(rtoc)
    return tocmap


class Book(object):
    page_numbers = 'latin'
    preamble_page_numbers = 'roman'

    def notify_watcher(self, message=None):
        if self.watcher:
            if  message is None:
                #message is the name of the caller
                message = traceback.extract_stack(None, 2)[0][2]
            log("notify_watcher called with '%s'" % message)
            self.watcher(message)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.notify_watcher('finished')
        self.cleanup()
        #could deal with exceptions here and return true


    def __init__(self, book, server, bookname, project=None,
                 page_settings=None, watcher=None, isbn=None,
                 license=config.DEFAULT_LICENSE, title=None,
                 max_age=0):
        log("*** Starting new book %s ***" % bookname,
            "starting zipbook with", server, book, project)
        self.watcher = watcher
        self.notify_watcher('start')
        self.bookname = bookname
        self.book = book
        self.server = server
        self.project = project
        self.cookie = ''.join(random.sample(ascii_letters, 10))
        try:
            blob = fetch_zip(server, book, project, save=True, max_age=max_age)
        except HTTPError, e:
            #log(e.url)
            traceback.print_exc()
            self.notify_watcher("ERROR:\n Couldn't get %r\n %s %s" % (e.url, e.code, e.msg))
            #not much to do?
            sys.exit()
        f = StringIO(blob)
        self.notify_watcher('fetch_zip')
        self.store = zipfile.ZipFile(f, 'r')
        self.info = json.loads(self.store.read('info.json'))
        for k in ('manifest', 'metadata', 'spine', 'TOC'):
            if k not in self.info:
                raise ObjaviError('info.json of %s lacks vital element "%s"' %
                                  (bookname, k))
            #check types also?

        self.metadata = self.info['metadata']
        self.spine = self.info['spine']
        self.manifest = self.info['manifest']

        if server == config.LOCALHOST: # [DEPRECATED]
            server = get_metadata(self.metadata, 'server', ns=config.FM, default=[server])[0]
            book = get_metadata(self.metadata, 'book', ns=config.FM, default=[book])[0]

        log(pformat(self.metadata))
        self.lang = get_metadata(self.metadata, 'language', default=[None])[0]
        if not self.lang:
            self.lang = guess_lang(server, book)
            log('guessed lang as %s' % self.lang)

        self.toc_header = get_metadata(self.metadata, 'toc_header', ns=config.FM, default=[None])[0]
        if not self.toc_header:
            self.toc_header = config.SERVER_DEFAULTS[server]['toc_header']

        self.dir = get_metadata(self.metadata, 'dir', ns=config.FM, default=[None])[0]
        if not self.dir:
            self.dir = guess_text_dir(server, book)


        #Patch in the extra metadata. (lang and dir may be set from config)
        #these should be read from zip -- so should go into zip?
        for var, key, scheme, ns in (
            (isbn, 'id', 'ISBN', config.DC),
            (license, 'rights', 'License', config.DC),
            (title, 'title', '', config.DC),
            (self.lang, 'language', '', config.DC),
            (self.dir, 'dir', '', config.FM),
            ):
            if var is not None:
                add_metadata(self.metadata, key, var, scheme=scheme, ns=ns)

        self.isbn = get_metadata(self.metadata, 'id', scheme='ISBN', default=[None])[0]
        self.license = get_metadata(self.metadata, 'rights', scheme='License', default=[None])[0]

        self.toc = self.info['TOC']
        expand_toc(self.toc)

        self.workdir = tempfile.mkdtemp(prefix=bookname, dir=TMPDIR)
        os.chmod(self.workdir, 0755)

        self.body_html_file = self.filepath('body.html')
        self.body_pdf_file = self.filepath('body.pdf')
        self.preamble_html_file = self.filepath('preamble.html')
        self.preamble_pdf_file = self.filepath('preamble.pdf')
        self.tail_html_file = self.filepath('tail.html')
        self.tail_pdf_file = self.filepath('tail.pdf')
        self.isbn_pdf_file = None
        self.pdf_file = self.filepath('final.pdf')
        self.body_odt_file = self.filepath('body.odt')

        self.publish_file = os.path.join(PUBLISH_PATH, bookname)
        self.publish_url = os.path.join(config.PUBLISH_URL, bookname)

        if page_settings is not None:
            self.maker = PageSettings(**page_settings)

        titles = get_metadata(self.metadata, 'title')
        if titles:
            self.title = titles[0]
        else:
            self.title = 'A Manual About ' + self.book

        self.notify_watcher()


    if config.TRY_BOOK_CLEANUP_ON_DEL:
        #Dont even define __del__ if it is not used.
        _try_cleanup_on_del = True
        def __del__(self):
            if self._try_cleanup_on_del and os.path.exists(self.workdir):
                self._try_cleanup_on_del = False #or else you can get in bad cycles
                self.cleanup()

    def get_tree_by_id(self, id):
        """get an HTML tree from the given manifest ID"""
        name = self.manifest[id]['url']
        mimetype = self.manifest[id]['mimetype']
        s = self.store.read(name)
        f = StringIO(s)
        if mimetype == 'text/html':
            try:
                tree = lxml.html.parse(f)
            except etree.XMLSyntaxError, e:
                log('Could not parse html ID %r, filename %r, string %r... exception %s' %
                    (id, name, s[:20], e))
                tree = lxml.html.document_fromstring('<html><body></body></html>').getroottree()
        elif 'xml' in mimetype: #XXX or is this just asking for trouble?
            tree = etree.parse(f)
        else:
            tree = f.read()
        f.close()
        return tree

    def filepath(self, fn):
        return os.path.join(self.workdir, fn)

    def save_data(self, fn, data):
        """Save without tripping up on unicode"""
        if isinstance(data, unicode):
            data = data.encode('utf8', 'ignore')
        f = open(fn, 'w')
        f.write(data)
        f.close()

    def save_tempfile(self, fn, data):
        """Save the data in a temporary directory that will be cleaned
        up when all is done.  Return the absolute file path."""
        fn = self.filepath(fn)
        self.save_data(fn, data)
        return fn

    def make_oo_doc(self):
        """Make an openoffice document, using the html2odt script."""
        self.wait_for_xvfb()
        html_text = etree.tostring(self.tree, method="html")
        self.save_data(self.body_html_file, html_text)
        run([config.HTML2ODT, self.workdir, self.body_html_file, self.body_odt_file])
        log("Publishing %r as %r" % (self.body_odt_file, self.publish_file))
        os.rename(self.body_odt_file, self.publish_file)
        self.notify_watcher()

    def extract_pdf_outline(self):
        #self.outline_contents, self.outline_text, number_of_pages = parse_outline(self.body_pdf_file, 1)
        debugf = self.filepath('outline.txt')
        self.outline_contents, self.outline_text, number_of_pages = \
                parse_outline(self.body_pdf_file, 1, debugf)

        if not self.outline_contents:
            #probably problems with international text. need a horrible hack
            log('no outline: trying again with ascii headings')
            import copy
            tree = copy.deepcopy(self.tree)
            titlemap = {}
            for tag in ('h1', 'h2', 'h3', 'h4'):
                for i, e in enumerate(tree.getiterator(tag)):
                    key = "%s_%s" % (tag, i)
                    titlemap[key] = e.text_content().strip(config.WHITESPACE_AND_NULL)
                    del e[:]
                    if tag == 'h1':
                        e = lxml.etree.SubElement(e, "strong", Class="initial")
                    e.text = key
                    log("key: %r, text: %r, value: %r" %(key, e.text, titlemap[key]))

            ascii_html_file = self.filepath('body-ascii-headings.html')
            ascii_pdf_file = self.filepath('body-ascii-headings.pdf')
            html_text = lxml.etree.tostring(tree, method="html")
            self.save_data(ascii_html_file, html_text)
            self.maker.make_raw_pdf(ascii_html_file, ascii_pdf_file, outline=True)
            debugf = self.filepath('ascii_outline.txt')
            ascii_contents, ascii_text, number_of_ascii_pages = \
                parse_outline(ascii_pdf_file, 1, debugf)
            self.outline_contents = []
            log ("number of pages: %s, post ascii: %s" %
                 (number_of_pages, number_of_ascii_pages))
            for ascii_title, depth, pageno in ascii_contents:
                if ascii_title[-4:] == '&#0;': #stupid [something] puts this in
                    ascii_title = ascii_title[:-4]
                if ' ' in ascii_title:
                    ascii_title = ascii_title.rsplit(' ', 1)[1]
                title = titlemap.get(ascii_title, '')
                log((ascii_title, title, depth, pageno))

                self.outline_contents.append((title, depth, pageno))
        else:
            for x in self.outline_contents:
                log(x)

        self.notify_watcher()
        return number_of_pages

    def make_body_pdf(self):
        """Make a pdf of the HTML, using webkit"""
        #1. Save the html
        html_text = etree.tostring(self.tree, method="html")
        self.save_data(self.body_html_file, html_text)

        #2. Make a pdf of it
        self.maker.make_raw_pdf(self.body_html_file, self.body_pdf_file, outline=True)
        self.notify_watcher('generate_pdf')

        n_pages = self.extract_pdf_outline()

        log ("found %s pages in pdf" % n_pages)
        #4. resize pages, shift gutters, even pages
        self.maker.reshape_pdf(self.body_pdf_file, self.dir, centre_end=True)
        self.notify_watcher('reshape_pdf')

        #5 add page numbers
        self.maker.number_pdf(self.body_pdf_file, n_pages, dir=self.dir,
                              numbers=self.page_numbers)
        self.notify_watcher("number_pdf")
        self.notify_watcher()

    def make_preamble_pdf(self):
        contents = self.make_contents()
        inside_cover_html = self.compose_inside_cover()
        log(self.dir, self.css_url, self.title, inside_cover_html,
            self.toc_header, contents, self.title)

        html = ('<html dir="%s"><head>\n'
                '<meta http-equiv="Content-Type" content="text/html;charset=utf-8" />\n'
                '<link rel="stylesheet" href="%s" />\n'
                '</head>\n<body>\n'
                '<h1 class="frontpage">%s</h1>'
                '%s\n'
                '<div class="contents"><h1>%s</h1>\n%s</div>\n'
                '<div style="page-break-after: always; color:#fff" class="unseen">.'
                '<!--%s--></div></body></html>'
                ) % (self.dir, self.css_url, self.title, inside_cover_html.decode('utf-8'),
                     self.toc_header, contents, self.title)
        self.save_data(self.preamble_html_file, html)

        self.maker.make_raw_pdf(self.preamble_html_file, self.preamble_pdf_file)

        self.maker.reshape_pdf(self.preamble_pdf_file, self.dir, centre_start=True)

        self.maker.number_pdf(self.preamble_pdf_file, None, dir=self.dir,
                            numbers=self.preamble_page_numbers,
                            number_start=-2)

        self.notify_watcher()

    def make_end_matter_pdf(self):
        """Make an inside back cover and a back cover.  If there is an
        isbn number its barcode will be put on the back cover."""
        if self.isbn:
            self.isbn_pdf_file = self.filepath('isbn.pdf')
            self.maker.make_barcode_pdf(self.isbn, self.isbn_pdf_file)
            self.notify_watcher('make_barcode_pdf')

        end_matter = self.compose_end_matter()
        log(end_matter)
        self.save_data(self.tail_html_file, end_matter.decode('utf-8'))
        self.maker.make_raw_pdf(self.tail_html_file, self.tail_pdf_file)

        self.maker.reshape_pdf(self.tail_pdf_file, self.dir, centre_start=True,
                               centre_end=True, even_pages=False)
        self.notify_watcher()

    def make_book_pdf(self):
        """A convenient wrapper of a few necessary steps"""
        # now the Xvfb server is needed. make sure it has had long enough to get going
        self.wait_for_xvfb()
        self.make_body_pdf()
        self.make_preamble_pdf()
        self.make_end_matter_pdf()

        concat_pdfs(self.pdf_file, self.preamble_pdf_file,
                    self.body_pdf_file, self.tail_pdf_file,
                    self.isbn_pdf_file)

        self.notify_watcher('concatenated_pdfs')


    def make_simple_pdf(self, mode):
        """Make a simple pdf document without contents or separate
        title page.  This is used for multicolumn newspapers and for
        web-destined pdfs."""
        self.wait_for_xvfb()
        #0. Add heading to begining of html
        body = list(self.tree.cssselect('body'))[0]
        e = body.makeelement('h1', {'id': 'book-title'})
        e.text = self.title
        body.insert(0, e)
        intro = lxml.html.fragment_fromstring(self.compose_inside_cover())
        e.addnext(intro)

        #0.5 adjust parameters to suit the particular kind of output
        if mode == 'web':
            self.maker.gutter = 0

        #1. Save the html
        html_text = etree.tostring(self.tree, method="html")
        self.save_data(self.body_html_file, html_text)

        #2. Make a pdf of it (direct to to final pdf)
        self.maker.make_raw_pdf(self.body_html_file, self.pdf_file, outline=True)
        self.notify_watcher('generate_pdf')
        n_pages = count_pdf_pages(self.pdf_file)

        if mode != 'web':
            #3. resize pages and shift gutters.
            self.maker.reshape_pdf(self.pdf_file, self.dir, centre_end=True)
            self.notify_watcher('reshape_pdf')

            #4. add page numbers
            self.maker.number_pdf(self.pdf_file, n_pages,
                                  dir=self.dir, numbers=self.page_numbers)
            self.notify_watcher("number_pdf")
        self.notify_watcher()


    def rotate180(self):
        """Rotate the pdf 180 degrees so an RTL book can print on LTR
        presses."""
        rotated = self.filepath('final-rotate.pdf')
        unrotated = self.filepath('final-pre-rotate.pdf')
        #leave the unrotated pdf intact at first, in case of error.
        rotate_pdf(self.pdf_file, rotated)
        os.rename(self.pdf_file, unrotated)
        os.rename(rotated, self.pdf_file)
        self.notify_watcher()

    def publish_pdf(self):
        """Move the finished PDF to its final resting place"""
        log("Publishing %r as %r" % (self.pdf_file, self.publish_file))
        os.rename(self.pdf_file, self.publish_file)
        self.notify_watcher()


    def concat_html(self):
        """Join all the chapters together into one tree.  Keep the TOC
        up-to-date along the way."""

        #each manifest item looks like:
        #{'contributors': []
        #'license': [],
        #'mimetype': '',
        #'rightsholders': []
        #'url': ''}
        doc = lxml.html.document_fromstring('<html><body></body></html>')
        tocmap = filename_toc_map(self.toc)
        for ID in self.spine:
            details = self.manifest[ID]
            log(ID, pformat(details))
            # ACO MIJENJAO
            try:
                root = self.get_tree_by_id(ID).getroot()
            except:
                continue
            #handle any TOC points in this file
            for point in tocmap[details['url']]:
                #if the url has a #identifier, use it. Otherwise, make
                #one up, using a hidden element at the beginning of
                #the inserted document.
                #XXX this will break if different files use the same ids
                #XXX should either replace all, or replace selectively.
                if point['fragment']:
                    fragment = point['fragment']
                else:
                    body = _find_tag(root, 'body')
                    fragment = '%s_%s' % (self.cookie, point['index'])
                    #reuse first tag if it is suitable.
                    if (len(body) and
                        body[0].tag in ('h1', 'h2', 'h3', 'h4', 'p', 'div')):
                        if body[0].get('id') is None:
                            body[0].set('id', fragment)
                        else:
                            fragment = body[0].get('id')
                        #the chapter starts with a heading. that heading should be the chapter name.
                        if body[0].tag in ('h1', 'h2', 'h3'):
                            log('chapter has title "%s", found html title "%s"' %
                                (point['title'], body[0].text_content()))
                            point['html_title'] = body[0].text_content()
                    else:
                        marker = body.makeelement('div', style="display:none",
                                                  id=fragment)
                        body.insert(0, marker)
                point['html_id'] = fragment

            add_guts(root, doc)
        return doc

    def unpack_static(self):
        """Extract static files from the zip for the html to refer to."""
        static_files = [x['url'] for x in self.manifest.values()
                        if x['url'].startswith('static')]
        if static_files:
            os.mkdir(self.filepath('static'))

        for name in static_files:
            s = self.store.read(name)
            f = open(self.filepath(name), 'w')
            f.write(s)
            f.close()
        self.notify_watcher()

    def load_book(self):
        """"""
        #XXX concatenate the HTML to match how TWiki version worked.
        # This is perhaps foolishly early -- throwing away useful boundaries.
        self.unpack_static()
        self.tree = self.concat_html()
        self.save_tempfile('raw.html', etree.tostring(self.tree, method='html'))

        self.headings = [x for x in self.tree.cssselect('h1')]
        if self.headings:
            self.headings[0].set('class', "first-heading")
        for h1 in self.headings:
            h1.title = h1.text_content().strip()
        self.notify_watcher()

    def make_contents(self):
        """Generate HTML containing the table of contents.  This can
        only be done after the main PDF has been made, because the
        page numbers are contained in the PDF outline."""
        header = '<h1>Table of Contents</h1><table class="toc">\n'
        row_tmpl = ('<tr><td class="chapter">%s</td><td class="title">%s</td>'
                    '<td class="pagenumber">%s</td></tr>\n')
        empty_section_tmpl = ('<tr><td class="empty-section" colspan="3">%s</td></tr>\n')
        section_tmpl = ('<tr><td class="section" colspan="3">%s</td></tr>\n')
        footer = '\n</table>'

        contents = []

        chapter = 1
        page_num = 1
        subsections = [] # for the subsection heading pages.

        outline_contents = iter(self.outline_contents)
        headings = iter(self.headings)

        for section in self.toc:
            if not section.get('children'):
                contents.append(empty_section_tmpl % section['title'])
                continue
            contents.append(section_tmpl % section['title'])

            for point in section['children']:
                try:
                    h1_text, level, page_num = outline_contents.next()
                except StopIteration:
                    log("contents data not found for %s. Stopping" % (point,))
                    break
                contents.append(row_tmpl % (chapter, _get_best_title(point), page_num))
                chapter += 1

        doc = header + '\n'.join(contents) + footer
        self.notify_watcher()
        return doc

    def add_section_titles(self):
        """Add any section heading pages that the TOC.txt file
        specifies.  These are sub-book, super-chapter groupings.

        Also add initial numbers to chapters.
        """
        headings = iter(self.headings)
        chapter = 1
        section = None
        log(self.toc)
        for t in self.toc:
            #only top level sections get a subsection page,
            #and only if they have children.
            if t.get('children'):
                section = self.tree.makeelement('div', Class="objavi-subsection")
                heading = etree.SubElement(section, 'div', Class="objavi-subsection-heading")
                heading.text = t['title']
                for child in t['children']:
                    item = etree.SubElement(section, 'div', Class="objavi-chapter")
                    if 'html_title' in child:
                        item.text = child['html_title']
                        heading = self.tree.cssselect('#'+ child['html_id'])
                        if heading:
                            _add_initial_number(heading[0], chapter)
                    else:
                        item.text = child['title']
                    _add_initial_number(item, chapter)
                    log(item.text, debug='HTMLGEN')
                    chapter += 1
                log("#%s is %s" % (t['html_id'], self.tree.cssselect('#'+ t['html_id'])))
                location = self.tree.cssselect('#'+ t['html_id'])[0]
                location.addprevious(section)


        self.notify_watcher()


    def add_css(self, css=None, mode='book'):
        """If css looks like a url, use it as a stylesheet link.
        Otherwise it is the CSS itself, which is saved to a temporary file
        and linked to."""
        log("css is %r" % css)
        htmltree = self.tree
        if css is None or not css.strip():
            css_default = config.SERVER_DEFAULTS[self.server]['css-%s' % mode]
            if css_default is None:
                #guess from language -- this should come first
                css_modes = config.LANGUAGE_CSS.get(self.lang,
                                                    config.LANGUAGE_CSS['en'])
                css_default = css_modes.get(mode, css_modes[None])
            url = 'file://' + os.path.abspath(css_default)
        elif not re.match(r'^http://\S+$', css):
            fn = self.save_tempfile('objavi.css', css)
            url = 'file://' + fn
        else:
            url = css
        #XXX for debugging and perhaps sensible anyway
        #url = url.replace('file:///home/douglas/objavi2', '')


        #find the head -- it's probably first child but lets not assume.
        for child in htmltree:
            if child.tag == 'head':
                head = child
                break
        else:
            head = htmltree.makeelement('head')
            htmltree.insert(0, head)

        link = etree.SubElement(head, 'link', rel='stylesheet', type='text/css', href=url)
        self.css_url = url
        self.notify_watcher()
        return url


    def _read_localised_template(self, template, fallbacks=['en']):
        """Try to get the template in the approriate language, otherwise in english."""
        for lang in [self.lang] + fallbacks:
            try:
                fn = template % (lang)
                f = open(fn)
                break
            except IOError, e:
                log("couldn't open inside front cover for lang %s (filename %s)" % (lang, fn))
                log(e)
        template = f.read()
        f.close()
        return template

    def compose_inside_cover(self):
        """create the markup for the preamble inside cover."""
        template = self._read_localised_template(config.INSIDE_FRONT_COVER_TEMPLATE)

        if self.isbn:
            isbn_text = '<b>ISBN :</b> %s <br>' % self.isbn
        else:
            isbn_text = ''

        return template % {'date': time.strftime('%Y-%m-%d'),
                           'isbn': isbn_text,
                           'license': self.license,
                           }


    def compose_end_matter(self):
        """create the markup for the end_matter inside cover.  If
        self.isbn is not set, the html will result in a pdf that
        spills onto two pages.
        """
        template = self._read_localised_template(config.END_MATTER_TEMPLATE)

        d = {'css_url': self.css_url,
             'title': self.title
             }

        if self.isbn:
            d['inside_cover_style'] = ''
        else:
            d['inside_cover_style'] = 'page-break-after: always'

        return template % d


    def make_epub(self, use_cache=False):
        """Make an epub version of the book, using Mike McCabe's
        epub module for the Internet Archive."""
        ebook = ia_epub.Book(self.publish_file, content_dir='')
        def add_file(ID, filename, mediatype, content):
            ebook.add_content({'media-type': mediatype.encode('utf-8'),
                               'id': ID.encode('utf-8'),
                               'href': filename.encode('utf-8'),
                               }, content)

        toc = self.info['TOC']

        #manifest
        filemap = {} #map html to corresponding xhtml
        spinemap = {} #map IDs to multi-file chapters
        for ID in self.manifest:
            details = self.manifest[ID]
            log(ID, pformat(details))
            fn, mediatype = details['url'], details['mimetype']
            content = self.store.read(fn)
            if mediatype == 'text/html':
                #convert to application/xhtml+xml, and perhaps split
                c = EpubChapter(self.server, self.book, ID, content,
                                use_cache=use_cache)
                c.remove_bad_tags()
                if fn[-5:] == '.html':
                    fnbase = fn[:-5]
                else:
                    fnbase = fn
                fnx = fnbase + '.xhtml'
                mediatype = 'application/xhtml+xml'

                fragments = split_html(c.as_xhtml(),
                                       compressed_size=self.store.getinfo(fn).compress_size)

                #add the first one as if it is the whole thing (as it often is)
                add_file(ID, fnx, mediatype, fragments[0])
                filemap[fn] = fnx
                if len(fragments) > 1:
                    spine_ids = [ID]
                    spinemap[ID] = spine_ids
                    #add any extras
                    for i in range(1, len(fragments)):
                        # XXX it is possible for duplicates if another
                        # file happens to have this name. Ignore for now
                        _id = '%s_SONY_WORKAROUND_%s' % (ID, i)
                        spine_ids.append(_id)
                        add_file(_id,
                                 '%s_SONY_WORKAROUND_%s.xhtml' % (fnbase, i),
                                 mediatype, fragments[i])

            else:
                add_file(ID, fn, mediatype, content)

        #toc
        ncx = epub_utils.make_ncx(toc, self.metadata, filemap)
        ebook.add(ebook.content_dir + 'toc.ncx', ncx)

        #spine
        for ID in self.spine:
            if ID in spinemap:
                for x in spinemap[ID]:
                    ebook.add_spine_item({'idref': x})
            else:
                ebook.add_spine_item({'idref': ID})

        #metadata -- no use of attributes (yet)
        # and fm: metadata disappears for now
        DCNS = config.DCNS
        DC = config.DC
        meta_info_items = []
        for ns, namespace in self.metadata.items():
            for keyword, schemes in namespace.items():
                if ns:
                    keyword = '{%s}%s' % (ns, keyword)
                for scheme, values in schemes.items():
                    for value in values:
                        item = {
                            'item': keyword,
                            'text': value,
                            }
                        if scheme:
                            if keyword in (DCNS + 'creator', DCNS + 'contributor'):
                                item['atts'] = {'role': scheme}
                            else:
                                item['atts'] = {'scheme': scheme}

        has_authors = 'creator' in self.metadata[DC]
        if not has_authors and config.CLAIM_UNAUTHORED:
            meta_info_items.append({'item': DCNS + 'creator',
                                    'text': 'The Contributors'})

            meta_info_items.append({'item': DCNS + 'rights',
                                    'text': 'This book is free. Copyright %s' % (', '.join(authors))}
                                   )

        tree_str = ia_epub.make_opf(meta_info_items,
                                    ebook.manifest_items,
                                    ebook.spine_items,
                                    ebook.guide_items,
                                    ebook.cover_id)
        ebook.add(ebook.content_dir + 'content.opf', tree_str)
        ebook.z.close()
        self.notify_watcher()


    def publish_s3(self):
        """Push the book's epub to archive.org, using S3."""
        #XXX why only epub?
        secrets = {}
        for x in ('S3_SECRET', 'S3_ACCESSKEY'):
            fn = getattr(config, x)
            f = open(fn)
            secrets[x] = f.read().strip()
            f.close()

        log(secrets)
        now = time.strftime('%F')
        s3output = self.filepath('s3-output.txt')
        s3url = 'http://s3.us.archive.org/booki-%s/%s' % (self.book, self.bookname)
        detailsurl = 'http://archive.org/details/booki-%s' % (self.book,)
        headers = [
            'x-amz-auto-make-bucket:1',
            "authorization: LOW %(S3_ACCESSKEY)s:%(S3_SECRET)s" % secrets,
            'x-archive-meta-mediatype:texts',
            'x-archive-meta-collection:opensource',
            'x-archive-meta-title:%s' %(self.book,),
            'x-archive-meta-date:%s' % (now,),
            'x-archive-meta-creator:FLOSS Manuals Contributors',
            ]

        if self.license in config.LICENSES:
            headers.append('x-archive-meta-licenseurl:%s' % config.LICENSES[self.license])

        argv = ['curl', '--location', '-s', '-o', s3output]
        for h in headers:
            argv.extend(('--header', h))
        argv.extend(('--upload-file', self.publish_file, s3url,))

        log(' '.join(repr(x) for x in argv))
        check_call(argv, stdout=sys.stderr)
        self.notify_watcher()
        return detailsurl, s3url


    def spawn_x(self):
        """Start an Xvfb instance, using a new server number.  A
        reference to it is stored in self.xvfb, which is used to kill
        it when the pdf is done.

        Note that Xvfb doesn't interact well with dbus which is
        present on modern desktops.
        """
        #Find an unused server number (in case two cgis are running at once)
        while True:
            servernum = random.randrange(50, 500)
            if not os.path.exists('/tmp/.X%s-lock' % servernum):
                break

        self.xserver_no = ':%s' % servernum

        authfile = self.filepath('Xauthority')
        os.environ['XAUTHORITY'] = authfile

        #mcookie(1) eats into /dev/random, so avoid that
        from hashlib import md5
        m = md5("%r %r %r %r %r" % (self, os.environ, os.getpid(), time.time(), os.urandom(32)))
        mcookie = m.hexdigest()

        check_call(['xauth', 'add', self.xserver_no, '.', mcookie])

        self.xvfb = Popen(['Xvfb', self.xserver_no,
                           '-screen', '0', '1024x768x24',
                           '-pixdepths', '32',
                           #'-blackpixel', '0',
                           #'-whitepixel', str(2 ** 24 -1),
                           #'+extension', 'Composite',
                           '-dpi', '96',
                           '-kb',
                           '-nolisten', 'tcp',
                           ])

        # We need to wait a bit before the Xvfb is ready.  but the
        # downloads are so slow that that probably doesn't matter

        self.xvfb_ready_time = time.time() + 2

        os.environ['DISPLAY'] = self.xserver_no
        log(self.xserver_no)

    def wait_for_xvfb(self):
        """wait until a previously set time before continuing.  This
        is so Xvfb has time to properly start."""
        if hasattr(self, 'xvfb'):
            d = self.xvfb_ready_time - time.time()
            if d > 0:
                time.sleep(d)
                self.notify_watcher()

    def cleanup_x(self):
        """Try very hard to kill off Xvfb.  In addition to killing
        this instance's xvfb, occasionally (randomly) search for
        escaped Xvfb instances and kill those too."""
        if not hasattr(self, 'xvfb'):
            return
        check_call(['xauth', 'remove', self.xserver_no])
        p = self.xvfb
        log("trying to kill Xvfb %s" % p.pid)
        os.kill(p.pid, 15)
        for i in range(10):
            if p.poll() is not None:
                log("%s died with %s" % (p.pid, p.poll()))
                break
            log("%s not dead yet" % p.pid)
            time.sleep(0.2)
        else:
            log("Xvfb would not die! kill -9! kill -9!")
            os.kill(p.pid, 9)

        if random.random() < 0.1:
            # occasionally kill old xvfbs and soffices, if there are any.
            self.kill_old_processes()

    def kill_old_processes(self):
        """Sometimes, despite everything, Xvfb or soffice instances
        hang around well after they are wanted -- for example if the
        cgi process dies particularly badly. So kill them if they have
        been running for a long time."""
        log("running kill_old_processes")
        p = Popen(['ps', '-C' 'Xvfb soffice soffice.bin html2odt ooffice wkhtmltopdf',
                   '-o', 'pid,etime', '--no-headers'], stdout=PIPE)
        data = p.communicate()[0].strip()
        if data:
            lines = data.split('\n')
            pids = []
            for line in lines:
                log('dealing with ps output "%s"' % line)
                try:
                    pid, days, hours, minutes, seconds \
                         = re.match(r'^\s*(\d+)\s+(\d+-)?(\d{2})?:?(\d{2}):(\d+)\s*$', line).groups()
                except AttributeError:
                    log("Couldn't parse that line!")
                # 50 minutes should be enough xvfb time for anyone
                if days or hours or int(minutes) > 50:
                    pid = int(pid)
                    log("going to kill pid %s" % pid)
                    os.kill(pid, 15)
                    pids.append(pid)

            time.sleep(1.0)
            for pid in pids:
                #try again in case any are lingerers
                try:
                    os.kill(int(pid), 9)
                except OSError, e:
                    log('PID %s seems dead (re-kill gives %s)' % (pid, e))
                    continue
                log('killing %s with -9' % pid)
        self.notify_watcher()

    def cleanup(self):
        self.cleanup_x()
        if not config.KEEP_TEMP_FILES:
            for fn in os.listdir(self.workdir):
                os.remove(os.path.join(self.workdir, fn))
            os.rmdir(self.workdir)
        else:
            log("NOT removing '%s', containing the following files:" % self.workdir)
            log(*os.listdir(self.workdir))

        self.notify_watcher()


def use_cache():
    return (os.environ.get('HTTP_HOST') in config.USE_ZIP_CACHE_ALWAYS_HOSTS)

def _read_cached_zip(server, book, max_age):
    #find a recent zip if possible
    prefix = '%s/%s' % (config.BOOKI_BOOK_DIR, make_book_name(book, server, '').split('-20', 1)[0])
    from glob import glob
    zips = sorted(glob(prefix + '*.zip'))
    if not zips:
        log("no cached booki-zips matching %s*.zip" % (prefix,))
        return None
    zipname = zips[-1]
    cutoff = time.time() - max_age * 60
    log(repr(zipname))
    try:
        date = time.mktime(time.strptime(zipname, prefix + '-%Y.%m.%d-%H.%M.%S.zip'))
        if date > cutoff:
            f = open(zipname)
            blob = f.read()
            f.close()
            return blob
        log("%s is too old, must reload" % zipname)
        return None
    except (IOError, IndexError, ValueError), e:
        log('could not make sense of %s: got exception %s' % (zipname, e))
        return None



def fetch_zip(server, book, project, save=False, max_age=-1):
    interface = config.SERVER_DEFAULTS[server]['interface']
    if interface not in ('Booki', 'TWiki'):
        raise NotImplementedError("Can't handle '%s' interface" % interface)
    if interface == 'Booki':
        url = config.BOOKI_ZIP_URL  % {'server': server, 'project': project, 'book':book}
    else:
        url = config.TWIKI_GATEWAY_URL % (HTTP_HOST, server, book)

    if use_cache() and max_age < 0:
        #default to 12 hours cache on objavi.halo.gen.nz
        max_age = 12 * 60

    if max_age:
        log('WARNING: trying to use cached booki-zip',
            'If you are debugging booki-zip creation, you will go CRAZY'
            ' unless you switch this off')
        blob = _read_cached_zip(server, book, max_age)
        if blob is not None:
            return blob

    log('fetching zip from %s'% url)
    f = urlopen(url)
    blob = f.read()
    f.close()
    if save:
        zipname = make_book_name(book, server, '.zip')
        f = open('%s/%s' % (config.BOOKI_BOOK_DIR, zipname), 'w')
        f.write(blob)
        f.close()
    return blob



def split_html(html, compressed_size=None, xhtmlise=False):
    if compressed_size is None:
        import zlib
        compressed_size = len(zlib.compress(html))

    splits = max(compressed_size // config.EPUB_COMPRESSED_SIZE_MAX,
                 len(html) // config.EPUB_FILE_SIZE_MAX)
    log("uncompressed: %s, compressed: %s, splits: %s" % (len(html), compressed_size, splits))

    if not splits:
        return [html]

    if xhtmlise:
        #xhtmlisation removes '<' in attributes etc, which makes the
        #marker insertion more reliable
        html = etree.tostring(lxml.html.fromstring(html),
                              encoding='UTF-8',
                              #method='html'
                              )

    target = len(html) // (splits + 1)
    s = 0
    fragments = []
    for i in range(splits):
        e = html.find('<', target * (i + 1))
        fragments.append(html[s:e])
        fragments.append('<hr class="%s" id="split_%s" />' % (config.MARKER_CLASS, i))
        s = e
    fragments.append(html[s:])
    root = lxml.html.fromstring(''.join(fragments))

    # find the node lineages along which to split the document.
    # anything outside these lines (i.e., branches) can be copied
    # wholesale.

    stacks = []
    for hr in root.iter(tag='hr'):
        if hr.get('class') == config.MARKER_CLASS:
            stack = [hr]
            stack.extend(x for x in hr.iterancestors())
            stack.reverse()
            stacks.append(stack)

    iterstacks = iter(stacks)

    src = root
    log('root is', root, root.attrib, type(root.attrib))
    dest = lxml.html.Element(root.tag, **dict(root.items()))
    doc = dest
    stack = iterstacks.next()
    marker = stack[-1]

    chapters = []
    try:
        while True:
            for e in src:
                if e not in stack:
                    #cut and paste branch
                    dest.append(e)
                elif e is marker:
                    #got one
                    src.remove(e)
                    chapters.append(doc)
                    src = root
                    dest = lxml.html.Element(root.tag, **dict(root.items()))
                    doc = dest
                    stack = iterstacks.next()
                    marker = stack[-1]
                    break
                else:
                    #next level
                    dest = etree.SubElement(dest, e.tag, **dict(e.items()))
                    dest.text = e.text
                    e.text = None
                    src = e
                    break
    except StopIteration:
        #stacks have run out -- the rest of the tree is the last section
        chapters.append(src)

    #return chapters
    return [etree.tostring(c, encoding='UTF-8', method='html') for c in chapters]

