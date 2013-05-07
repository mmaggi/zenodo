# -*- coding: utf-8 -*-
## This file is part of Invenio.
## Copyright (C) 2011, 2012, 2013 CERN.
##
## Invenio is free software; you can redistribute it and/or
## modify it under the terms of the GNU General Public License as
## published by the Free Software Foundation; either version 2 of the
## License, or (at your option) any later version.
##
## Invenio is distributed in the hope that it will be useful, but
## WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
## General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with Invenio; if not, write to the Free Software Foundation, Inc.,
## 59 Temple Place, Suite 330, Boston, MA 02111-1307, USA.

"""
OpenAIRE local customization of Flask application
"""

import time

from invenio.config import CFG_SITE_LANG, CFG_OPENAIRE_MAX_UPLOAD
from invenio.textutils import nice_size
from invenio.signalutils import webcoll_after_webpage_cache_update
from invenio.usercollection_signals import after_save_collection
from jinja2 import nodes
from jinja2.ext import Extension
from invenio.webuser_flask import current_user
from invenio.usercollection_model import UserCollection
from invenio.cache import cache
from invenio.search_engine import search_pattern_parenthesised

JINJA_CACHE_ATTR_NAME = '_template_fragment_cache'
TEMPLATE_FRAGMENT_KEY_TEMPLATE = '_template_fragment_cache_%s%s'


def customize_app(app):
    #from invenio.webinterface_handler_flask_utils import _
    from flask import current_app

    app.config['MAX_CONTENT_LENGTH'] = CFG_OPENAIRE_MAX_UPLOAD

    @app.context_processor
    def local_processor():
        """
        This will add variables to the Jinja2 to context containing the footer
        menus.
        """
        left = filter(lambda x: x.display(),
            current_app.config['menubuilder_map']['footermenu_left'].children.values())
        right = filter(lambda x: x.display(),
            current_app.config['menubuilder_map']['footermenu_right'].children.values())
        bottom = filter(lambda x: x.display(),
            current_app.config['menubuilder_map']['footermenu_bottom'].children.values())

        return dict(footermenu_left=left, footermenu_right=right,
            footermenu_bottom=bottom)

    @app.template_filter('filesizeformat')
    def filesizeformat_filter(value):
        """
        Jinja2 filesizeformat filters is broken in Jinja2 up to v2.7, so
        let's implement our own.
        """
        return nice_size(value)

    @app.template_filter('timefmt')
    def timefmt_filter(value, format="%d %b %Y, %H:%M"):
        return time.strftime(format, value)

    @app.template_filter('is_record_owner')
    def is_record_owner(bfo, tag="8560_f"):
        """
        Determine if current user is owner of a given record

        @param bfo: BibFormat Object
        @param tag: Tag to use for extracting the email from the record.
        """
        email = bfo.field(tag)
        return email and current_user.is_active and \
            current_user['email'] == email

    @app.template_filter('usercollection_id')
    def usercollection_id(coll):
        """
        Determine if current user is owner of a given record

        @param coll: Collection object
        """
        identifier = coll.name
        if identifier.startswith("provisional-user-"):
            return identifier[len("provisional-user-"):]
        elif identifier.startswith("user-"):
            return identifier[len("user-"):]
        else:
            return ""

    @app.template_filter('curation_action')
    def curation_action(recid, ucoll_id=None):
        """
        Determine if curation action is underway
        """
        return cache.get("usercoll_curate:%s_%s" % (ucoll_id, recid))

    @app.template_filter('zenodo_curated')
    def zenodo_curated(reclist, length=10, reverse=True, open_only=False):
        """
        Show only curated publications from reclist
        """
        if open_only:
            p = "980__a:curated AND (542__l:open OR 542__l:embargoed)"
        else:
            p = "980__a:curated"
        reclist = (reclist & search_pattern_parenthesised(p=p))
        if reverse:
            reclist = reclist[-length:]
            return reversed(reclist)
        else:
            return reclist[:length]

    @app.template_filter('usercollection_state')
    def usercollection_state(bfo, ucoll_id=None):
        """
        Determine if current user is owner of a given record

        @param coll: Collection object
        """
        coll_id_reject = "provisional-user-%s" % ucoll_id
        coll_id_accept = "user-%s" % ucoll_id

        for cid in bfo.fields('980__a'):
            if cid == coll_id_accept:
                return "accepted"
            elif cid == coll_id_reject:
                return "provisional"
        return "rejected"

    @app.template_filter('usercollections')
    def usercollections(bfo, is_owner=False, provisional=False, public=True):
        """
        Maps collection identifiers to community collection objects

        @param bfo: BibFormat Object
        @param is_owner: Set to true to only return user collections which the
                         current user owns.
        @oaram provisional: Return provisional collections (default to false)
        @oaram public: Return public collections (default to true)
        """
        colls = []
        if is_owner and current_user.is_guest:
            return colls

        for cid in bfo.fields('980__a'):
            if provisional and cid.startswith('provisional-'):
                colls.append(cid[len("provisional-user-"):])
            elif public and cid.startswith('user-'):
                colls.append(cid[len("user-"):])

        query = [UserCollection.id.in_(colls)]
        if is_owner:
            query.append(UserCollection.id_user == current_user.get_id())

        return UserCollection.query.filter(*query).all()

    #
    # Removed unwanted invenio menu items
    #
    del app.config['menubuilder_map']['main'].children['help']

    # Add {% zenodocache tag %} and connect webcoll signal handler to invalidate
    # cache.
    app.jinja_env.add_extension(ZenodoExtension)
    webcoll_after_webpage_cache_update.connect(invalidate_jinja2_cache)
    after_save_collection.connect(invalidate_jinja2_cache)


def parse_filesize(s):
    """
    Convert a human readable filesize into number of bytes
    """
    sizes = [('', 1), ('kb', 1024), ('mb', 1024*1024), ('gb', 1024*1024*1024),
             ('g', 1024*1024*1024), ]
    s = s.lower()
    for size_str, val in sizes:
        intval = s.replace(size_str, '')
        try:
            return int(intval)*val
        except ValueError:
            pass
    raise ValueError("Could not parse '%s' into bytes" % s)


def make_template_fragment_key(fragment_name, vary_on=[]):
    """
    Make a cache key for a specific fragment name
    """
    if vary_on:
        fragment_name = "%s_" % fragment_name
    return TEMPLATE_FRAGMENT_KEY_TEMPLATE % (fragment_name, "_".join(vary_on))


def invalidate_jinja2_cache(sender, collection=None, lang=None, **extra):
    """
    Invalidate collection cache
    """
    from invenio.cache import cache
    if lang is None:
        lang = CFG_SITE_LANG
    cache.delete(make_template_fragment_key(collection.name, vary_on=[lang]))


class ZenodoExtension(Extension):
    """
    Temporary extension (let's see how long it will stay ;-).

    This extension is made until a pull-request has been integrated in the
    main Flask-Cache branch, so that generated cache keys are stable and
    predictable instead of based on filename and line numbers.
    """

    tags = set(['zenodocache'])

    def parse(self, parser):
        lineno = parser.stream.next().lineno

        # Parse timeout
        args = [parser.parse_expression()]

        # Parse fragment name
        parser.stream.skip_if('comma')
        args.append(parser.parse_expression())

        # Parse vary_on parameters
        vary_on = []
        while parser.stream.skip_if('comma'):
            vary_on.append(parser.parse_expression())

        if vary_on:
            args.append(nodes.List(vary_on))
        else:
            args.append(nodes.Const([]))

        body = parser.parse_statements(['name:endcache'], drop_needle=True)
        return nodes.CallBlock(self.call_method('_cache', args),
                               [], [], body).set_lineno(lineno)

    def _cache(self, timeout, fragment_name, vary_on,  caller):
        try:
            cache = getattr(self.environment, JINJA_CACHE_ATTR_NAME)
        except AttributeError, e:
            raise e

        key = make_template_fragment_key(fragment_name, vary_on=vary_on)

        # Delete key if timeout is 'del'
        if timeout == "del":
            cache.delete(key)
            return caller()

        rv = cache.get(key)
        if rv is None:
            rv = caller()
            cache.set(key, rv, timeout)
        return rv
