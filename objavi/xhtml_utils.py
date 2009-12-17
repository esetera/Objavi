"""Various things to do with [x]html that might be useful in more than
one place."""

import lxml, lxml.html, lxml.html.clean
from lxml import etree

import os, sys
import re, copy
from cStringIO import StringIO

from urlparse import urlparse, urlsplit, urljoin
from urllib2 import urlopen, HTTPError

from config import XHTMLNS, XHTML, IMG_CACHE, MARKER_CLASS_SPLIT
from cgi_utils import log

ADJUST_HEADING_WEIGHT = False

OK_TAGS = set([
    "body", "head", "html", "title", "abbr", "acronym", "address",
    "blockquote", "br", "cite", "code", "dfn", "div", "em", "h1", "h2",
    "h3", "h4", "h5", "h6", "kbd", "p", "pre", "q", "samp", "span",
    "strong", "var", "a", "dl", "dt", "dd", "ol", "ul", "li", "object",
    "param", "b", "big", "hr", "i", "small", "sub", "sup", "tt", "del",
    "ins", "bdo", "caption", "col", "colgroup", "table", "tbody", "td",
    "tfoot", "th", "thead", "tr", "img", "area", "map", "meta", "style",
    "link", "base",
    etree.Comment,
    ])


XHTML11_DOCTYPE = '''<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"
    "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
'''
XML_DEC = '<?xml version="1.0" encoding="UTF-8"?>\n'

IMG_PREFIX = 'static/'

def convert_tags(root, elmap):
    for el in root.iterdescendants():
        if el.tag in elmap:
            el.tag = elmap[el.tag]


class ImageCache(object):
    def __init__(self, cache_dir=IMG_CACHE, prefix=IMG_PREFIX):
        self._fetched = {}
        self.cache_dir = cache_dir
        self.prefix = prefix
        if not os.path.exists(cache_dir + prefix):
            os.makedirs(cache_dir + prefix)

    def read_local_url(self, path):
        f = open(self.cache_dir + path)
        s = f.read()
        f.close()
        return s

    def _save_local_url(self, path, data):
        f = open(self.cache_dir + path, 'w')
        f.write(data)
        f.close()
        #os.chmod(path, 0444)

    def fetch_if_necessary(self, url, target=None, use_cache=True):
        if url in self._fetched:
            return self._fetched[url]

        if target is None:
            target = url_to_filename(url, self.prefix)

        if use_cache and os.path.exists(self.cache_dir + target):
            log("used cache for %s" % target)
            return target

        try:
            f = urlopen(url)
            data = f.read()
            f.close()
        except HTTPError, e:
            # if it is missing, assume it will be missing every time
            # after, otherwise, you can get into endless waiting
            self._fetched[url] = None
            log("Wanting '%s', got error %s" %(url, e))
            return None

        self._save_local_url(target, data)
        self._fetched[url] = target
        log("got %s as %s" % (url, target))
        return target


class BaseChapter(object):
    def as_html(self):
        """Serialise the tree as html."""
        return etree.tostring(self.tree, method='html')

    def as_xhtml(self):
        """Convert to xhtml and serialise."""
        try:
            root = self.tree.getroot()
        except AttributeError:
            root = self.tree

        nsmap = {None: XHTML}
        xroot = etree.Element(XHTMLNS + "html", nsmap=nsmap)

        def xhtml_copy(el, xel):
            xel.text = el.text
            for k, v in el.items():
                xel.set(k, v)
            for child in el.iterchildren():
                xchild = xel.makeelement(XHTMLNS + child.tag)
                xel.append(xchild)
                xhtml_copy(child, xchild)
            xel.tail = el.tail

        xhtml_copy(root, xroot)

        return XML_DEC + XHTML11_DOCTYPE + etree.tostring(xroot)

    cleaner = lxml.html.clean.Cleaner(scripts=True,
                                      javascript=True,
                                      comments=False,
                                      style=True,
                                      links=True,
                                      meta=True,
                                      page_structure=False,
                                      processing_instructions=True,
                                      embedded=True,
                                      frames=True,
                                      forms=True,
                                      annoying_tags=True,
                                      allow_tags=OK_TAGS,
                                      remove_unknown_tags=False,
                                      safe_attrs_only=True,
                                      add_nofollow=False
                                      )

    def remove_bad_tags(self):
        #for e in self.tree.iter():
        #    if not e.tag in OK_TAGS:
        #        log('found bad tag %s' % e.tag)
        self.cleaner(self.tree)

    def _loadtree(self, html):
        try:
            self.tree = lxml.html.document_fromstring(html)
        except etree.XMLSyntaxError, e:
            log('Could not parse html file %r, string %r... exception %s' %
                (self.name, html[:40], e))
            self.tree = lxml.html.document_fromstring('<html><body></body></html>').getroottree()


class EpubChapter(BaseChapter):
    def __init__(self, server, book, chapter_name, html, use_cache=False,
                 cache_dir=None):
        self.server = server
        self.book = book
        self.name = chapter_name
        self._loadtree(html)

    def prepare_for_epub(self):
        """Shift all headings down 2 places."""
        if ADJUST_HEADING_WEIGHT:
            # a question to resolve:
            # is it better (quicker) to have multiple, filtered iterations
            # converting in order (h4->h5, h3->h4, etc) or to do a single,
            # unfiltered pass and convert from a dict?

            hmap = dict(('h%s' % x, 'h%s' % (x + 2)) for x in range(4, 0, -1))
            hmap['h5'] = 'h6'
            convert_tags(self.root, hmap)




###################################################


class Section(object):
    def __init__(self, tree, ID=None, title=None):
        self.ID = ID
        self.tree = tree
        self.title = title

def split_tree(tree):
    """If a document has special marker elements (hr tags with class
    of config.MARKER_CLASS_SPLIT) it will be broken into smaller
    documents using the markers as boundaries.  Each element in the
    new documents will be nested and ordered as before, though those
    on the new edges will obviously lack siblings they once may have
    had.

    The new documents are returned as a list of Section objects (see
    above), which bundles the new tree with an ID and title if the
    marker elements contain those attributes.

    The original tree will be destroyed or reused.
    """
    try:
        root = tree.getroot()
    except AttributeError:
        root = tree

    # find the node lineages along which to split the document.
    # anything outside these lines (i.e., side branches) can be copied
    # wholesale, which speeds things up considerably.
    stacks = []
    for hr in root.iter(tag='hr'):
        if hr.get('class') == MARKER_CLASS_SPLIT:
            stack = [hr]
            stack.extend(x for x in hr.iterancestors())
            stack.reverse()
            stacks.append(stack)

    iterstacks = iter(stacks)

    src = root
    dest = lxml.html.Element(root.tag, **dict(root.items()))
    doc = dest
    stack = iterstacks.next()
    marker = stack[-1]

    chapters = []
    ID = 'unidentified-front-matter'
    title = None
    try:
        while True:
            for e in src:
                if e not in stack:
                    #cut and paste branch
                    dest.append(e)
                elif e is marker:
                    #got one.
                    chapters.append(Section(doc, ID, title))
                    #The ID and title are for the *next* section, so
                    #collect them before deleting the marker.
                    ID = e.get('id')
                    title = e.get('title')
                    src.remove(e)
                    src = root
                    dest = lxml.html.Element(root.tag, **dict(root.items()))
                    doc = dest
                    stack = iterstacks.next()
                    marker = stack[-1]
                    break
                else:
                    #next level.
                    #It is safe to descend without leaving a trail,
                    #because side branches are no descended.
                    dest = etree.SubElement(dest, e.tag, **dict(e.items()))
                    dest.text = e.text
                    e.text = None
                    src = e
                    break
    except StopIteration:
        #stacks have run out -- the rest of the tree is the last section
        chapters.append(Section(src, ID, title))
    return chapters


