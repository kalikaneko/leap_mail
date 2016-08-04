"""
Tests for leap.mail.walk module
"""
import os.path
from email.parser import Parser

from leap.mail import walk

CORPUS = {
    'simple': 'rfc822.message',
    'multimin': 'rfc822.multi-minimal.message',
    'multisigned': 'rfc822.multi-signed.message',
    'bounced': 'rfc822.bounce.message',
}

_here = os.path.dirname(__file__)
_parser = Parser()


# tests

def test_walk_no_side_effects():
    msg = _parse('simple')
    parts = walk.get_parts(msg)
    tree = walk.walk_msg_tree(parts)
    tree2 = walk.walk_msg_tree(parts)
    assert tree == tree2


def test_simple_mail():
    msg = _parse('simple')
    parts = walk.get_parts(msg)
    tree = walk.walk_msg_tree(parts)
    assert len(tree['part_map']) == 1
    assert tree['part_map'][1]['ctype'] == 'text/plain'
    assert tree['multi'] is False


def test_multipart_minimal():
    msg = _parse('multimin')
    parts = walk.get_parts(msg)
    assert parts[0]['ctype'] == 'multipart/mixed'
    assert parts[1]['ctype'] == 'text/plain'

    tree = walk.walk_msg_tree(parts)
    assert tree['multi'] is True
    assert len(tree['part_map']) == 1
    first = tree['part_map'][1]
    assert first['multi'] is False
    assert first['ctype'] == 'text/plain'


def test_multi_signed():
    msg = _parse('multisigned')
    parts = walk.get_parts(msg)

    ctypes = [part['ctype'] for part in parts]
    # TODO this respects order?
    assert ctypes == [
        'multipart/signed',
        'multipart/mixed',
        'text/plain',
        'text/plain',
        'application/octet-stream',
        'application/pgp-signature']

    tree = walk.walk_msg_tree(parts)
    assert tree['multi'] is True
    assert len(tree['part_map']) == 2

    _first = tree['part_map'][1]
    _second = tree['part_map'][2]
    assert len(_first['part_map']) == 3
    assert(_second['multi'] is False)


def test_bounce_mime():
    msg = _parse('bounced')
    parts = walk.get_parts(msg)
    tree = walk.walk_msg_tree(parts)

    ctypes = [tree['part_map'][index]['ctype']
              for index in sorted(tree['part_map'].keys())]
    import pprint
    pprint.pprint(tree['part_map'])
    third = tree['part_map'][3]

    three_one_ctype = third['part_map'][1]['headers'][
        'Content-Type'].split(';')[0]
    assert three_one_ctype == 'multipart/signed'

    assert ctypes == [
        'text/plain',
        'message/delivery-status',
        'message/rfc822']

    # XXX BUG -- error while parsing...
    """
    >>> msg.get_payload()[2].items()
    [('Content-Description', 'Undelivered Message'), ('Content-Type',
        'message/rfc822')]
    >>> msg.get_payload()[2].get_payload()
    [<email.message.Message instance at 0x7f5bfba743f8>]
    """
    # XXX check that there's no 0-based subpart, that's a  BUG


# utils

def _parse(name):
    _str = _get_string_for_message(name)
    return _parser.parsestr(_str)


def _get_string_for_message(name):
    filename = os.path.join(_here, CORPUS[name])
    with open(filename) as f:
        msgstr = f.read()
    return msgstr
