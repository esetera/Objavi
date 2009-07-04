#!/usr/bin/python
#
# Part of Objavi2, which turns html manuals into books
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

"""Make a pdf from the specified book."""
import os, sys
import cgi
import re, time
from urllib2 import urlopen
from getopt import gnu_getopt

from fmbook import log, Book
from fmbook import PAGE_SETTINGS, SERVER_DEFAULTS, DEFAULT_SERVER

import config
from config import BOOK_LIST_CACHE, BOOK_LIST_CACHE_DIR

FORM_TEMPLATE = os.path.abspath('templates/form.html')
PROGRESS_TEMPLATE = os.path.abspath('templates/progress.html')

# ARG_VALIDATORS is a mapping between the expected cgi arguments and
# functions to validate their values. (None means no validation).

ARG_VALIDATORS = {
    "webName": re.compile(r'^(\w+/?)*\w+$').match, # can be: BlahBlah/Blah_Blah
    "css": None, # an url, empty (for default), or css content
    "title": lambda x: len(x) < 999,
    "header": None, # header text, UNUSED
    "isbn": lambda x: x.isdigit() and len(x) == 13,
    "license": lambda x: len(x) < 999, #should be a codename?
    "server": SERVER_DEFAULTS.__contains__,
    "engine": config.ENGINES.__contains__,
    "booksize": PAGE_SETTINGS.__contains__,
    "cgi-context": lambda x: x.lower() in '1true0false',
    "mode": str.isalnum,
    "rotate": u"rotate".__eq__,
}

__doc__ += '\nValid arguments are: %s.\n' % ', '.join(ARG_VALIDATORS.keys())

def parse_args():
    """Read and validate CGI or commandline arguments, putting the
    good ones into the returned dictionary.  Command line arguments
    should be in the form --title='A Book'.
    """
    query = cgi.FieldStorage()
    options, args = gnu_getopt(sys.argv[1:], '', [x + '=' for x in ARG_VALIDATORS])
    options = dict(options)
    log(options)
    data = {}
    for key, validator in ARG_VALIDATORS.items():
        value = query.getfirst(key, options.get('--' + key, None))
        log('%s: %s' % (key, value), debug='STARTUP')
        if value is not None:
            if validator is not None and not validator(value):
                log("argument '%s' is not valid ('%s')" % (key, value))
                continue
            data[key] = value

    log(data, debug='STARTUP')
    return data

def get_server_list():
    return sorted(SERVER_DEFAULTS.keys())


def get_book_list(server):
    """Ask the server for a list of books.  Floss Manual TWikis keep such a list at
    /bin/view/TWiki/WebLeftBarWebsList?skin=text but it needs a bit of processing

    If BOOK_LIST_CACHE is non-zero, the book list won't be re-fetched
    in that many seconds, rather it will be read from disk.
    """
    if BOOK_LIST_CACHE:
       cache_name = os.path.join(BOOK_LIST_CACHE_DIR, '%s.booklist' % server)
       if (os.path.exists(cache_name) and
           os.stat(cache_name).st_mtime + BOOK_LIST_CACHE > time.time()):
           f = open(cache_name)
           s = f.read()
           f.close()
           return s.split()

    url = 'http://%s/bin/view/TWiki/WebLeftBarWebsList?skin=text' % server
    #XXX should use lxml
    log(url)
    f = urlopen(url)
    s = f.read()
    f.close()
    items = sorted(re.findall(r'/bin/view/([\w/]+)/WebHome', s))
    if BOOK_LIST_CACHE:
        f = open(cache_name, 'w')
        f.write('\n'.join(items))
        f.close()
    return items

def get_size_list():
    #XXX PAGE_SETTINGS instances are only constructed for this list.
    # the area and mmsizes could be calculated seperately here from PAGE_SIZE_DATA

    #order by increasing areal size.
    ordered = [x[1] for x in
               sorted((v.area, v) for v in PAGE_SETTINGS.values())]
    return [(v.name, '%s (%dmm x %dmm)' % (v.name, v.mmsize[0], v.mmsize[1]))
            for v in ordered]


def optionise(items, default=None):
    options = []
    for x in items:
        if isinstance(x, str):
            if x == default:
                options.append('<option selected="selected">%s</option>' % x)
            else:
                options.append('<option>%s</option>' % x)
        else:
            log(x, x[0])
            # couple: value, name
            if x[0] == default:
                options.append('<option selected="selected" value="%s">%s</option>' % x)
            else:
                options.append('<option value="%s">%s</option>' % x)

    return '\n'.join(options)

def get_default_css(server=DEFAULT_SERVER):
    log(server)
    cssfile = SERVER_DEFAULTS[server]['css']
    log(cssfile)
    f = open(cssfile)
    s = f.read()
    f.close()
    #log(s)
    return s



def show_form(args, server, webname, size='COMICBOOK', engine='webkit'):
    f = open(FORM_TEMPLATE)
    template = f.read()
    f.close()
    f = open(config.FONT_LIST_INCLUDE)
    font_list = f.read()
    f.close()
    d = {
        'server_options': optionise(get_server_list(), default=server),
        'book_options': optionise(get_book_list(server), default=webname),
        'size_options': optionise(get_size_list(), default=size),
        'engines': optionise(config.ENGINES.keys(), default=engine),
        'css': get_default_css(server),
        'font_link': config.FONT_LIST_URL,
        'font_list': font_list,
    }
    print template % d


def make_progress_page(webname, bookname):
    f = open(PROGRESS_TEMPLATE)
    template = f.read()
    f.close()
    d = {
        'webname': webname,
        'bookname': bookname,
    }
    print template % d
    def progress_notifier(message):
        print ('<script type="text/javascript">\n'
               'objavi_show_progress("%s");\n'
               '</script>' % message
               )
        if message == 'finished':
            print '</body></html>'
        sys.stdout.flush()
    return progress_notifier

def print_progress(message):
    print '******* got message "%s"' %message

def make_book_name(webname, server):
    lang = SERVER_DEFAULTS.get(server, SERVER_DEFAULTS[DEFAULT_SERVER])['lang']
    webname = ''.join(x for x in webname if x.isalnum())
    return '%s-%s-%s.pdf' % (webname, lang,
                             time.strftime('%Y.%m.%d-%H.%M.%S'))

if __name__ == '__main__':
    args = parse_args()
    webname = args.get('webName')
    server = args.get('server', config.DEFAULT_SERVER)
    size = args.get('booksize', config.DEFAULT_SIZE)
    engine = args.get('engine', config.DEFAULT_ENGINE)
    mode = args.get('mode')

    cgi_context = 'SERVER_NAME' in os.environ or args.get('cgi-context', 'NO').lower() in '1true'
    if cgi_context:
        print "Content-type: text/html; charset=utf-8\n"

    if mode == 'booklist':
        print optionise(get_book_list(server), default=webname)
        sys.exit()
    if mode == 'css':
        #XX sending as text/html, but it doesn't really matter
        print get_default_css(server=server)
        sys.exit()

    if not webname or not server:
        if cgi_context:
            show_form(args, server, webname, size)
        else:
            print __doc__
        sys.exit()

    # so we're making a book.
    bookname = make_book_name(webname, server)
    if cgi_context:
        progress_bar = make_progress_page(webname, bookname)
    else:
        progress_bar = print_progress

    #XXX could use 'with Book() as book': to makesure cleanup happens
    # (or try ... finally)

    book = Book(webname, server, bookname, pagesize=size, engine=engine,
                watcher=progress_bar
                )

    if cgi_context:
        book.spawn_x()

    book.load()

    book.set_title(args.get('title'))
    book.add_css(args.get('css'))
    
    book.compose_inside_cover(args.get('license'), args.get('isbn'))

    book.add_section_titles()

    book.make_pdf()

    if "rotate" in args:
        book.rotate180()

    book.publish_pdf()

    book.cleanup()

    book.notify_watcher('finished')
