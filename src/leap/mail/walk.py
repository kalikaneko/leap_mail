# -*- coding: utf-8 -*-
# walk.py
# Copyright (C) 2013-2015 LEAP
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
Utilities for walking along a message tree.
"""
from copy import deepcopy
from email.parser import Parser

from cryptography.hazmat.backends.multibackend import MultiBackend
from cryptography.hazmat.backends.openssl.backend import (
    Backend as OpenSSLBackend)
from cryptography.hazmat.primitives import hashes

from leap.mail.utils import first

crypto_backend = MultiBackend([OpenSSLBackend()])

_parser = Parser()


def get_hash(s):
    digest = hashes.Hash(hashes.SHA256(), crypto_backend)
    digest.update(s)
    return digest.finalize().encode("hex").upper()


def get_parts(msg):
    """
    Gather attributes about each of the message parts.
    Returns a list with dictionaries containing the relevant information about
    each of the parts in the recursive descent of the mime tree.
    """
    list_of_parts = []
    parts = list(msg.walk())
    for idx, part in enumerate(parts):
        part_dict = {
            'multi': part.is_multipart(),
            'ctype': part.get_content_type(),
            'size': len(part.as_string()),
            'parts':
                len(part.get_payload())
                if isinstance(part.get_payload(), list)
                else 1,
            'headers': part.items(),
            'phash':
                get_hash(part.get_payload())
                if not part.is_multipart()
                else None}

        # special case
        if part['content-type'] in ('message/delivery-status',):
            part_dict['parts'] = 1
            parts.pop(idx + 1)
        list_of_parts.append(part_dict)

    return list_of_parts


"""
Utility functions for getting the parts vector and the
payloads from the original message.
"""


def get_parts_vector(parts):
    """Returns a list of integers that show how many subparts each part has"""
    return (x.get('parts', 1) for x in parts)


def get_payloads(msg):
    return ((x.get_payload(),
            dict(((str.lower(k), v) for k, v in (x.items()))))
            for x in msg.walk())


def get_body_phash(msg):
    """
    Find the body payload-hash for this message.
    """
    for part in msg.walk():
        # XXX what other ctypes should be considered body?
        if part.get_content_type() in ("text/plain", "text/html"):
            # XXX avoid hashing again
            return get_hash(part.get_payload())

"""
On getting the raw docs, we get also some of the headers to be able to
index the content. Here we remove any mutable part, as the the filename
in the content disposition.
"""


def get_raw_docs(msg, parts):
    return (
        {'type': 'cnt',  # type content they'll be
         'raw': payload,
         'phash': get_hash(payload),
         'content-disposition': first(headers.get(
             'content-disposition', '').split(';')),
         'content-type': headers.get(
             'content-type', ''),
         'content-transfer-encoding': headers.get(
             'content-transfer-encoding', '')
         } for payload, headers in get_payloads(msg)
        if not isinstance(payload, list))


"""
Groucho Marx: Now pay particular attention to this first clause, because it's
              most important. There's the party of the first part shall be
              known in this contract as the party of the first part. How do you
              like that, that's pretty neat eh?

Chico Marx: No, that's no good.
Groucho Marx: What's the matter with it?

Chico Marx: I don't know, let's hear it again.
Groucho Marx: So the party of the first part shall be known in this contract as
              the party of the first part.

Chico Marx: Well it sounds a little better this time.
Groucho Marx: Well, it grows on you. Would you like to hear it once more?

Chico Marx: Just the first part.
Groucho Marx: All right. It says the first part of the party of the first part
              shall be known in this contract as the first part of the party of
              the first part, shall be known in this contract - look, why
              should we quarrel about a thing like this, we'll take it right
              out, eh?

Chico Marx: Yes, it's too long anyhow. Now what have we got left?
Groucho Marx: Well I've got about a foot and a half. Now what's the matter?

Chico Marx: I don't like the second party either.
"""


def get_subparts_tree(messagestr):
    msg = _parser.parsestr(messagestr)
    return walk_msg_tree(get_parts(msg))


def walk_msg_tree(parts, body_phash=None):
    """
    Take a list of interesting items of a message subparts structure,
    and return a dict of dicts almost ready to be written to the content
    documents that will be stored in Soledad.

    It walks down the subparts in the parsed message tree, and collapses
    the leaf documents into a wrapper document until no multipart submessages
    are left. To achieve this, it iteratively calculates a wrapper vector of
    all documents in the sequence that have more than one part and have unitary
    documents to their right. To collapse a multipart, take as many
    unitary documents as parts the submessage contains, and replace the object
    in the sequence with the new wrapper document.

    :param parts: A list of dicts containing the interesting properties for
                  the message structure. Normally this has been generated by
                  doing a message walk.
    :type parts: list of dicts.
    :param body_phash: the payload hash of the body part, to be included
                       in the outer content doc for convenience.
    :type body_phash: basestring or None
    """
    _parts = deepcopy(parts)

    # a list of integers representing how many subparts each part has
    num_parts_vector = list(get_parts_vector(_parts))

    # a list of bools stating whether a subpart is a wrapper
    is_wrapper_vector = _get_wrappers_vector(num_parts_vector)

    # do until no wrapper document is left
    while any(is_wrapper_vector):
        wrapper_idx = is_wrapper_vector.index(True)

        # how many subparts to pick
        subp_n = num_parts_vector[wrapper_idx]

        # slice with subparts
        subparts = _parts[wrapper_idx + 1:wrapper_idx + 1 + subp_n]

        content_wrapper = {
            'multi': True,
            'part_map': dict((index + 1, part)
                           for index, part in enumerate(subparts)),
            'headers': dict(_parts[wrapper_idx]['headers'])
        }

        # remove subparts and substitute wrapper
        map(lambda i: _parts.remove(i), subparts)
        _parts[wrapper_idx] = content_wrapper

        # refresh vectors for this iteration
        num_parts_vector = list(get_parts_vector(_parts))
        is_wrapper_vector = _get_wrappers_vector(num_parts_vector)

    def is_rightmost_item(num_parts_vector):
        return all(x == 1 for x in num_parts_vector)

    if is_rightmost_item(num_parts_vector):
        # special case
        main_pmap = _parts[0].get('part_map', None)
        if main_pmap is not None:
            last_part = max(main_pmap.keys())
            main_pmap[last_part]['part_map'] = {}
            for part_idx in range(len(num_parts_vector) - 1):
                main_pmap[last_part]['part_map'][part_idx + 1] = _parts[part_idx + 1]

    outer = first(_parts)
    outer.pop('headers')

    if 'part_map' in outer:
        parts_dict = outer
    else:
        # we have a multipart with 1 part only, so kind of fix it
        # although it would be prettier if I take this special case at
        # the beginning of the walk.
        try:
            inner = second(_parts)
            is_multipart = True
        except IndexError:
            inner = outer
            is_multipart = False

        parts_dict = {'multi': is_multipart, 'part_map': {1: inner}}
        parts_dict['part_map'][1]['multi'] = False
        if not parts_dict['part_map'][1].get('phash', None):
            parts_dict['part_map'][1][PHASH] = body_phash

        inner_headers = _parts[1].get('headers', None) if (
            len(_parts) == 2) else None
        if inner_headers:
            second(parts_dict['part_map'])['headers'] = inner_headers

    parts_dict['body'] = body_phash
    return parts_dict


def _get_wrappers_vector(vparts):
    return [True if vparts[i] != 1 and vparts[i + 1] == 1
            else False
            for i in range(len(vparts) - 1)]

def second(thing):
    return thing[1]
