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

from cryptography.hazmat.backends.multibackend import MultiBackend
from cryptography.hazmat.backends.openssl.backend import (
    Backend as OpenSSLBackend)
from cryptography.hazmat.primitives import hashes

from leap.mail.utils import first

crypto_backend = MultiBackend([OpenSSLBackend()])


PART_MAP = 'part_map'
HEADERS = 'headers'
PHASH = 'phash'
BODY = 'body'


def get_hash(s):
    digest = hashes.Hash(hashes.SHA256(), crypto_backend)
    digest.update(s)
    return digest.finalize().encode("hex").upper()


def get_parts(msg):
    """
    Get interesting message parts
    """
    return [
        {'multi': part.is_multipart(),
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
             else None} for part in msg.walk()]


"""
Utility functions for getting the parts vector and the
payloads from the original message.
"""


def get_parts_vector(parts):
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
    parts_vector = list(get_parts_vector(_parts))
    wv = _get_wrappers_vector(parts_vector)

    # do until no wrapper document is left
    while any(wv):
        wrapper_idx = wv.index(True)

        # how many subparts to pick
        subp_n = parts_vector[wrapper_idx]

        # slice with subparts
        subparts = _parts[wrapper_idx + 1:wrapper_idx + 1 + subp_n]

        content_wrapper = {
            'multi': True,
            PART_MAP: dict((index + 1, part)
                           for index, part in enumerate(subparts)),
            HEADERS: dict(_parts[wrapper_idx][HEADERS])
        }

        # remove subparts and substitute wrapper
        map(lambda i: _parts.remove(i), subparts)
        _parts[wrapper_idx] = content_wrapper

        # refresh vectors for this iteration
        parts_vector = list(get_parts_vector(_parts))
        wv = _get_wrappers_vector(parts_vector)

    def is_rightmost_item(parts_vector):
        return all(x == 1 for x in parts_vector)

    if is_rightmost_item(parts_vector):
        # special case
        main_pmap = _parts[0].get(PART_MAP, None)
        if main_pmap is not None:
            last_part = max(main_pmap.keys())
            main_pmap[last_part][PART_MAP] = {}
            for part_idx in range(len(parts_vector) - 1):
                main_pmap[last_part][PART_MAP][part_idx] = _parts[part_idx + 1]

    outer = first(_parts)
    outer.pop(HEADERS)

    if PART_MAP not in outer:
        try:
            inner = _parts[1]
            is_multipart = True
        except IndexError:
            inner = outer
            is_multipart = False
        # we have a multipart with 1 part only, so kind of fix it
        # although it would be prettier if I take this special case at
        # the beginning of the walk.
        pdoc = {'multi': is_multipart, PART_MAP: {1: inner}}
        pdoc[PART_MAP][1]['multi'] = False
        if not pdoc[PART_MAP][1].get(PHASH, None):
            pdoc[PART_MAP][1][PHASH] = body_phash

        inner_headers = _parts[1].get(HEADERS, None) if (
            len(_parts) == 2) else None
        if inner_headers:
            pdoc[PART_MAP][1][HEADERS] = inner_headers
    else:
        pdoc = outer
    pdoc[BODY] = body_phash
    return pdoc


def _get_wrappers_vector(vparts):
    return [True if vparts[i] != 1 and vparts[i + 1] == 1
            else False
            for i in range(len(vparts) - 1)]
