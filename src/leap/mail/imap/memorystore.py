# -*- coding: utf-8 -*-
# memorystore.py
# Copyright (C) 2014 LEAP
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
In-memory transient store for a LEAPIMAPServer.
"""
import contextlib
import logging
import weakref

from collections import namedtuple

from zope.interface import implements

from leap.mail import size
from leap.mail.imap import interfaces
from leap.mail.imap.fields import fields

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def set_bool_flag(obj, att):
    """
    Set a boolean flag to True while we're doing our thing.
    Just to let the world know.
    """
    setattr(obj, att, True)
    try:
        yield True
    except RuntimeError as exc:
        logger.exception(exc)
    finally:
        setattr(obj, att, False)


class ReferenciableDict(dict):
    """
    A dict that can be weak-referenced.

    Some builtin objects are not weak-referenciable unless
    subclassed. So we do.

    Used to return pointers to the items in the MemoryStore.
    """


class ReferenciableList(list):
    """
    ibidem.
    """

MessagePartTuple = namedtuple(
    'MessagePartTuple', ['new', 'dirty', 'store', 'content'])


class MessageDict(object):
    """
    A simple dictionary container around the different message subparts.
    """
    # TODO use __slots__ to limit memory footprint?

    implements(interfaces.IMessageContainer)

    FDOC = "fdoc"
    HDOC = "hdoc"
    CDOCS = "cdocs"

    def __init__(self, fdoc=None, hdoc=None, cdocs=None,
                 from_dict=None, memstore=None,
                 new=True, dirty=False):
        self._dict = {}
        self.new = new
        # XXX add set_dirty method
        self.dirty = dirty
        self.memstore = memstore

        if from_dict is not None:
            self.from_dict(from_dict)
        else:
            if fdoc is not None:
                self._dict[self.FDOC] = ReferenciableDict(fdoc)
            if hdoc is not None:
                self._dict[self.HDOC] = ReferenciableDict(hdoc)
            if cdocs is not None:
                self._dict[self.CDOCS] = ReferenciableDict(cdocs)

    # IMessageContainer

    @property
    def fdoc(self):
        _fdoc = self._dict.get(self.FDOC, None)
        if _fdoc:
            content_ref = weakref.proxy(_fdoc)
        else:
            print "NO FDOC!!!"
            print "_dict:", self._dict
            content_ref = {}
        return MessagePartTuple(new=self.new, dirty=self.dirty, store="mem",
                                content=content_ref)

    @property
    def hdoc(self):
        _hdoc = self._dict.get(self.HDOC, None)
        if _hdoc:
            content_ref = weakref.proxy(_hdoc)
        else:
            print "NO HDOC!!!!"
            print "_dict:", self._dict
            content_ref = {}
        return MessagePartTuple(new=self.new, dirty=self.dirty, store="mem",
                                content=content_ref)

    # XXX check interface for this.
    # Should return a dict Zero-indexed with content docs.
    @property
    def cdocs(self):
        _cdocs = self._dict.get(self.CDOCS, None)
        if _cdocs:
            return weakref.proxy(_cdocs)
        else:
            return {}

    def all_docs_iter(self):
        return self._dict.itervalues()

    # i/o

    def as_dict(self):
        """
        Return a dict representation of the parts contained.
        """
        return self._dict

    def from_dict(self, msg_dict):
        """
        Populate MessageDict parts from a dictionary.
        It expects the same format that we use in a
        MessageDict.
        """
        fdoc, hdoc, cdocs = map(
            lambda part: msg_dict.get(part, None),
            [self.FDOC, self.HDOC, self.CDOCS])
        self._dict[self.FDOC] = fdoc
        self._dict[self.HDOC] = hdoc
        self._dict[self.CDOCS] = cdocs


class MemoryStore(object):
    """
    An in-memory store to where we can write the different parts that
    we split the messages into and buffer them until we write them to the
    permanent storage.

    It uses MessageDicts to store the message-parts, which are indexed
    by mailbox name and UID.
    """
    implements(interfaces.IMessageStore)

    # TODO We will want to index by chash when we transition to local-only
    # UIDs.
    # TODO should store RECENT-FLAGS too
    # TODO should store HDOCSET too (use weakrefs!) -- will need to subclass
    # TODO use dirty flag (maybe use namedtuples for that) so we can use it
    # also as a read-cache.

    WRITING_FLAG = "_writing"

    def __init__(self, *args, **kwargs):
        """
        Initialize a MemoryStore.
        """
        self._msg_store = {}
        self._phash_store = {}

        self._new = set([])
        self._dirty = set([])
        setattr(self, self.WRITING_FLAG, False)

    # IMessageStore

    # XXX change interface to put_msg etc??

    def put(self, mbox, uid, message):
        """
        Put the passed message into this MemoryStore.
        """
        print "putting doc %s (%s)" % (mbox, uid)
        key = mbox, uid
        self._new.add(key)
        msg_dict = message.as_dict()

        self._msg_store[key] = msg_dict

        cdocs = message.cdocs
        #import ipdb; ipdb.set_trace()

        dirty = True
        new = True

        # XXX should capture this in log...

        for cdoc_key in cdocs.keys():
            print "saving cdoc"
            cdoc = self._msg_store[key]['cdocs'][cdoc_key]
            #import ipdb; ipdb.set_trace()

            # make it weak-referenciable!
            referenciable_cdoc = ReferenciableDict(cdoc)
            # XXX get new attr???
            self._msg_store[key]['cdocs'][cdoc_key] = MessagePartTuple(
                new=new, dirty=dirty, store="mem",
                content=referenciable_cdoc)
            print "cdoc ----", cdoc
            phash = cdoc.get(fields.PAYLOAD_HASH_KEY, None)

            if not phash:
                continue
            self._phash_store[phash] = weakref.proxy(referenciable_cdoc)

    def get(self, mbox, uid):
        """
        Get a MessageDict for the given mbox and uid combination.

        :return: MessageDict or None
        """
        key = mbox, uid
        msg_dict = self._msg_store.get(key, None)
        if msg_dict:
            # XXX check if msg in new, dirty !
            return MessageDict(from_dict=msg_dict,
                               memstore=weakref.proxy(self))
        else:
            return None

    # XXX add write_msgs, write_rflags etc?
    # or do everything at once?

    def write(self, store):
        """
        Write the documents in this MemoryStore to a different store.
        """
        raise NotImplementedError("ouch!")

        # XXX should check for elements with the dirty state
        # XXX if new == True, create_doc
        # XXX if new == False, put_doc
        # XXX should delete the original message from incoming after
        # the writes are done.

        # XXX store should implement a IMessageStore too. assert.
        with set_bool_flag(self, self.WRITING_FLAG):
            # XXX do stuff ...........................
            # for foo in bar: store.write
            pass

    # MemoryStore specific methods.

    def get_uids(self, mbox):
        """
        Get all uids for a given mbox.
        """
        all_keys = self._msg_store.keys()
        return [uid for m, uid in all_keys if m == mbox]

    def get_last_uid(self, mbox):
        """
        Get the highest UID for a given mbox.
        """
        # XXX should get from msg_store keys instead!
        if not self._new:
            return 0
        return max([uid for m, uid in self._new if mbox == mbox])

    def count_new_mbox(self, mbox):
        """
        Count the new messages by inbox.
        """
        return len([(m, uid) for m, uid in self._new if mbox == mbox])

    def count_new(self):
        """
        Count all the new messages in the MemoryStore.
        """
        return len(self._new)

    def get_by_phash(self, phash):
        """
        Return a content-document by its payload-hash.
        """
        doc = self._phash_store.get(phash, None)
        # XXX get new, dirty properly???
        new = True
        dirty = True
        return MessagePartTuple(
            new=new, dirty=dirty, store="mem",
            content=doc)

    @property
    def is_writing(self):
        """
        Property that returns whether the store is currently writing its
        internal state to a permanent storage.

        Used to evaluate whether the CHECK command can inform that the field
        is clear to proceed, or waiting for the write operations to complete
        is needed instead.

        :rtype: bool
        """
        # XXX this should probably return a deferred !!!
        return getattr(self, self.WRITING_FLAG)

    def put_part(self, part_type, value):
        """
        Put the passed part into this IMessageStore.
        `part` should be one of: fdoc, hdoc, cdoc
        """
        # XXX turn that into a enum

    # Memory management.

    def get_size(self):
        """
        Return the size of the internal storage.
        Use for calculating the limit beyond which we should flush the store.
        """
        return size.get_size(self._msg_store)
