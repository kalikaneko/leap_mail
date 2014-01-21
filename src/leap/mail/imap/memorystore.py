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

from zope.interface import Interface, implements

from leap.mail import size

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

# Interfaces


class IMessageContainer(Interface):
    """
    I am a container around the different documents that a message
    is split into.
    """

    @property
    def fdoc(self):
        """
        Return the flags document for this message.

        :rtype: dict
        """

    @property
    def hdoc(self):
        """
        Return the headers document for this message.

        :rtype: dict
        """

    @property
    def cdocs(self):
        """
        Return the content docs for this message.

        :return: a list of dicts, or empty list.
        :rtype: list
        """

    def all_docs_iter(self):
        """
        Return an iterator to the docs for all the parts.

        :rtype: iterator
        """


class IMessageStore(Interface):
    """
    I represent a generic storage for LEAP Messages.
    """

    def put(self, mbox, uid, message):
        """
        Put the passed message into this IMessageStore.

        :param mbox: the mbox this message belongs.
        :param uid: the UID that identifies this message in this mailbox.
        :param message: a IMessageContainer implementor.
        """

    def get(self, mbox, uid):
        """
        Get a IMessageContainer for the given mbox and uid combination.

        :param mbox: the mbox this message belongs.
        :param uid: the UID that identifies this message in this mailbox.
        """

    def write(self, store):
        """
        Write the documents in this IMessageStore to a different
        storage. Usually this will be done from a MemoryStorage to a DbStorage.

        :param store: another IMessageStore implementor.
        """

#
# Implementors
#


class MessageDict(object):
    """
    A simple dictionary container around the different message subparts.
    """
    implements(IMessageContainer)

    FDOC = "fdoc"
    HDOC = "hdoc"
    CDOCS = "cdocs"

    def __init__(self, from_dict, fdoc=None, hdoc=None, cdocs=None):
        self._dict = {}

        if from_dict is not None:
            self.from_dict(from_dict)
        else:
            if fdoc is not None:
                self._dict[self.FDOC] = fdoc
            if hdoc is not None:
                self._dict[self.HDOC] = hdoc
            if cdocs is not None:
                self._dict[self.CDOCS] = cdocs

    # IMessageContainer

    @property
    def fdoc(self):
        return self._dict.get(self.FDOC, {})

    @property
    def hdoc(self):
        return self._dict.get(self.HDOC, {})

    @property
    def cdocs(self):
        cdocs_dict = self._dict.get(self.CDOCS, {})
        return cdocs_dict.values()

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
        Populate parts from a dictionary.
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
    implements(IMessageStore)

    # TODO We will want to index by chash when we transition to local-only
    # UIDs.

    WRITING_FLAG = "_writing"

    def __init__(self, *args, **kwargs):
        """
        Initialize a MemoryStore.
        """
        self._store = {}
        setattr(self, self.WRITING_FLAG, False)

    # IMessageStore

    def put(self, mbox, uid, message):
        """
        Put the passed message into this MemoryStore.
        """
        key = mbox, uid
        self._store[key] = message.as_dict()

    def get(self, mbox, uid):
        """
        Get a MessageDict for the given mbox and uid combination.

        :return: MessageDict or None
        """
        key = mbox, uid
        msg_dict = self._store.get(key, None)
        if msg_dict:
            return MessageDict(msg_dict)
        else:
            return None

    def write(self, store):
        """
        Write the documents in this MemoryStore to a different store.
        """
        raise NotImplementedError("ouch!")

        # XXX store should implement a IMessageStore
        with set_bool_flag(self, self.WRITING_FLAG):
            # XXX do stuff ...........................
            # for foo in bar: store.write
            pass

    # MemoryStore specific methods.

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
        return size.get_size(self._store)
