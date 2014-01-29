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
import threading
import weakref

from collections import defaultdict
from copy import copy

from twisted.internet import defer
from twisted.internet.task import LoopingCall
from twisted.python import log
from zope.interface import implements

from leap.common.check import leap_assert_type
from leap.mail import size
from leap.mail.decorators import deferred
from leap.mail.utils import empty
from leap.mail.messageflow import MessageProducer
from leap.mail.imap import interfaces
from leap.mail.imap.fields import fields
from leap.mail.imap.messageparts import MessagePartType, MessagePartDoc
from leap.mail.imap.messageparts import RecentFlagsDoc
from leap.mail.imap.messageparts import MessageWrapper
from leap.mail.imap.messageparts import ReferenciableDict

logger = logging.getLogger(__name__)


# The default period to do writebacks to the permanent
# soledad storage, in seconds.
SOLEDAD_WRITE_PERIOD = 10


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


class MemoryStore(object):
    """
    An in-memory store to where we can write the different parts that
    we split the messages into and buffer them until we write them to the
    permanent storage.

    It uses MessageWrapper instances to represent the message-parts, which are
    indexed by mailbox name and UID.

    It also can be passed a permanent storage as a paremeter (any implementor
    of IMessageStore, in this case a SoledadStore). In this case, a periodic
    dump of the messages stored in memory will be done. The period of the
    writes to the permanent storage is controled by the write_period parameter
    in the constructor.
    """
    implements(interfaces.IMessageStore,
               interfaces.IMessageStoreWriter)

    # TODO We will want to index by chash when we transition to local-only
    # UIDs.

    WRITING_FLAG = "_writing"
    _last_uid_lock = threading.Lock()

    def __init__(self, permanent_store=None,
                 write_period=SOLEDAD_WRITE_PERIOD):
        """
        Initialize a MemoryStore.

        :param permanent_store: a IMessageStore implementor to dump
                                messages to.
        :type permanent_store: IMessageStore
        :param write_period: the interval to dump messages to disk, in seconds.
        :type write_period: int
        """
        self._permanent_store = permanent_store
        self._write_period = write_period

        # Internal Storage: messages
        self._msg_store = {}

        # Internal Storage: payload-hash
        """
        {'phash': weakreaf.proxy(dict)}
        """
        self._phash_store = {}

        # Internal Storage: content-hash:fdoc
        """
        chash-fdoc-store keeps references to
        the flag-documents indexed by content-hash.

        {'chash': {'mbox-a': weakref.proxy(dict),
                   'mbox-b': weakref.proxy(dict)}
        }
        """
        self._chash_fdoc_store = {}

        # Internal Storage: recent-flags store
        """
        recent-flags store keeps one dict per mailbox,
        with the document-id of the u1db document
        and the set of the UIDs that have the recent flag.

        {'mbox-a': {'doc_id': 'deadbeef',
                    'set': {1,2,3,4}
                    }
        }
        """
        # TODO this will have to transition to content-hash
        # indexes after we move to local-only UIDs.

        self._rflags_store = defaultdict(
            lambda: {'doc_id': None, 'set': set([])})

        """
        last-uid store keeps the count of the highest UID
        per mailbox.

        {'mbox-a': 42,
         'mbox-b': 23}
        """
        self._last_uid = {}

        # New and dirty flags, to set MessageWrapper State.
        self._new = set([])
        self._new_deferreds = {}
        self._dirty = set([])
        self._rflags_dirty = set([])
        self._dirty_deferreds = {}

        # Flag for signaling we're busy writing to the disk storage.
        setattr(self, self.WRITING_FLAG, False)

        if self._permanent_store is not None:
            # this producer spits its messages to the permanent store
            # consumer using a queue. We will use that to put
            # our messages to be written.
            self.producer = MessageProducer(permanent_store,
                                            period=0.1)
            # looping call for dumping to SoledadStore
            self._write_loop = LoopingCall(self.write_messages,
                                           permanent_store)

            # We can start the write loop right now, why wait?
            self._start_write_loop()

    def _start_write_loop(self):
        """
        Start loop for writing to disk database.
        """
        if not self._write_loop.running:
            self._write_loop.start(self._write_period, now=True)

    def _stop_write_loop(self):
        """
        Stop loop for writing to disk database.
        """
        if self._write_loop.running:
            self._write_loop.stop()

    # IMessageStore

    # XXX this would work well for whole message operations.
    # We would have to add a put_flags operation to modify only
    # the flags doc (and set the dirty flag accordingly)

    def create_message(self, mbox, uid, message, notify_on_disk=True):
        """
        Create the passed message into this MemoryStore.

        By default we consider that any message is a new message.

        :param mbox: the mailbox
        :type mbox: str or unicode
        :param uid: the UID for the message
        :type uid: int
        :param message: a message to be added
        :type message: MessageWrapper
        :param notify_on_disk: whether the deferred that is returned should
                               wait until the message is written to disk to
                               be fired.
        :type notify_on_disk: bool

        :return: a Deferred. if notify_on_disk is True, will be fired
                 when written to the db on disk.
                 Otherwise will fire inmediately
        :rtype: Deferred
        """
        log.msg("adding new doc to memstore %r (%r)" % (mbox, uid))
        key = mbox, uid

        self._add_message(mbox, uid, message, notify_on_disk)

        d = defer.Deferred()
        d.addCallback(lambda result: log.msg("message save: %s" % result))
        self._new.add(key)

        # We store this deferred so we can keep track of the pending
        # operations internally.
        self._new_deferreds[key] = d

        if notify_on_disk:
            # Caller wants to be notified when the message is on disk
            # so we pass the deferred that will be fired when the message
            # has been written.
            return d
        else:
            # Caller does not care, just fired and forgot, so we pass
            # a defer that will inmediately have its callback triggered.
            return defer.succeed('fire-and-forget:%s' % str(key))

    def put_message(self, mbox, uid, message, notify_on_disk=True):
        """
        Put an existing message.

        This will set the dirty flag on the MemoryStore.

        :param mbox: the mailbox
        :type mbox: str or unicode
        :param uid: the UID for the message
        :type uid: int
        :param message: a message to be added
        :type message: MessageWrapper
        :param notify_on_disk: whether the deferred that is returned should
                               wait until the message is written to disk to
                               be fired.
        :type notify_on_disk: bool

        :return: a Deferred. if notify_on_disk is True, will be fired
                 when written to the db on disk.
                 Otherwise will fire inmediately
        :rtype: Deferred
        """
        key = mbox, uid
        d = defer.Deferred()
        d.addCallback(lambda result: log.msg("message PUT save: %s" % result))

        self._dirty.add(key)
        self._dirty_deferreds[key] = d
        self._add_message(mbox, uid, message, notify_on_disk)
        return d

    def _add_message(self, mbox, uid, message, notify_on_disk=True):
        # XXX have to differentiate between notify_new and notify_dirty
        # TODO defaultdict the hell outa here...

        key = mbox, uid
        msg_dict = message.as_dict()

        FDOC = MessagePartType.fdoc.key
        HDOC = MessagePartType.hdoc.key
        CDOCS = MessagePartType.cdocs.key
        DOCS_ID = MessagePartType.docs_id.key

        try:
            store = self._msg_store[key]
        except KeyError:
            self._msg_store[key] = {FDOC: {},
                                    HDOC: {},
                                    CDOCS: {},
                                    DOCS_ID: {}}
            store = self._msg_store[key]

        fdoc = msg_dict.get(FDOC, None)
        if fdoc:
            if not store.get(FDOC, None):
                store[FDOC] = ReferenciableDict({})
            store[FDOC].update(fdoc)

            # content-hash indexing
            chash = fdoc.get(fields.CONTENT_HASH_KEY)
            chash_fdoc_store = self._chash_fdoc_store
            if not chash in chash_fdoc_store:
                chash_fdoc_store[chash] = {}

            chash_fdoc_store[chash][mbox] = weakref.proxy(
                store[FDOC])

        hdoc = msg_dict.get(HDOC, None)
        if hdoc:
            if not store.get(HDOC, None):
                store[HDOC] = ReferenciableDict({})
            store[HDOC].update(hdoc)

        docs_id = msg_dict.get(DOCS_ID, None)
        if docs_id:
            if not store.get(DOCS_ID, None):
                store[DOCS_ID] = {}
            store[DOCS_ID].update(docs_id)

        cdocs = message.cdocs
        for cdoc_key in cdocs.keys():
            if not store.get(CDOCS, None):
                store[CDOCS] = {}

            cdoc = cdocs[cdoc_key]
            # first we make it weak-referenciable
            referenciable_cdoc = ReferenciableDict(cdoc)
            store[CDOCS][cdoc_key] = referenciable_cdoc
            phash = cdoc.get(fields.PAYLOAD_HASH_KEY, None)
            if not phash:
                continue
            self._phash_store[phash] = weakref.proxy(referenciable_cdoc)

        def prune(seq, store):
            for key in seq:
                if key in store and empty(store.get(key)):
                    store.pop(key)
        prune((FDOC, HDOC, CDOCS, DOCS_ID), store)

    def get_docid_for_fdoc(self, mbox, uid):
        """
        Get Soledad document id for the flags-doc for a given mbox and uid.

        :param mbox: the mailbox
        :type mbox: str or unicode
        :param uid: the message UID
        :type uid: int
        """
        fdoc = self._permanent_store.get_flags_doc(mbox, uid)
        if empty(fdoc):
            return None
        doc_id = fdoc.doc_id
        return doc_id

    def get_message(self, mbox, uid, flags_only=False):
        """
        Get a MessageWrapper for the given mbox and uid combination.

        :param mbox: the mailbox
        :type mbox: str or unicode
        :param uid: the message UID
        :type uid: int

        :return: MessageWrapper or None
        """
        key = mbox, uid
        FDOC = MessagePartType.fdoc.key

        msg_dict = self._msg_store.get(key, None)
        if empty(msg_dict):
            return None
        new, dirty = self._get_new_dirty_state(key)
        if flags_only:
            return MessageWrapper(fdoc=msg_dict[FDOC],
                                  new=new, dirty=dirty,
                                  memstore=weakref.proxy(self))
        else:
            return MessageWrapper(from_dict=msg_dict,
                                  new=new, dirty=dirty,
                                  memstore=weakref.proxy(self))

    def remove_message(self, mbox, uid):
        """
        Remove a Message from this MemoryStore.

        :param mbox: the mailbox
        :type mbox: str or unicode
        :param uid: the message UID
        :type uid: int
        """
        # XXX For the moment we are only removing the flags and headers
        # docs. The rest we leave there polluting your hard disk,
        # until we think about a good way of deorphaning.

        # XXX implement elijah's idea of using a PUT document as a
        # token to ensure consistency in the removal.

        try:
            key = mbox, uid
            self._new.discard(key)
            self._dirty.discard(key)
            self._msg_store.pop(key, None)
        except Exception as exc:
            logger.exception(exc)

    # IMessageStoreWriter

    def write_messages(self, store):
        """
        Write the message documents in this MemoryStore to a different store.

        :param store: the IMessageStore to write to
        """
        # For now, we pass if the queue is not empty, to avoid duplicate
        # queuing.
        # We would better use a flag to know when we've already enqueued an
        # item.

        # XXX this could return the deferred for all the enqueued operations

        if not self.producer.is_queue_empty():
            return

        logger.info("Writing messages to Soledad...")

        # TODO change for lock, and make the property access
        # is accquired
        with set_bool_flag(self, self.WRITING_FLAG):
            for rflags_doc_wrapper in self.all_rdocs_iter():
                self.producer.push(rflags_doc_wrapper)
            for msg_wrapper in self.all_new_dirty_msg_iter():
                self.producer.push(msg_wrapper)

    # MemoryStore specific methods.

    def get_uids(self, mbox):
        """
        Get all uids for a given mbox.

        :param mbox: the mailbox
        :type mbox: str or unicode
        """
        all_keys = self._msg_store.keys()
        return [uid for m, uid in all_keys if m == mbox]

    # last_uid

    def get_last_uid(self, mbox):
        """
        Get the highest UID for a given mbox.
        It will be the highest between the highest uid in the message store for
        the mailbox, and the soledad integer cache.

        :param mbox: the mailbox
        :type mbox: str or unicode
        """
        uids = self.get_uids(mbox)
        last_mem_uid = uids and max(uids) or 0
        last_soledad_uid = self.get_last_soledad_uid(mbox)
        return max(last_mem_uid, last_soledad_uid)

    def get_last_soledad_uid(self, mbox):
        """
        Get last uid for a given mbox from the soledad integer cache.

        :param mbox: the mailbox
        :type mbox: str or unicode
        """
        return self._last_uid.get(mbox, 0)

    def set_last_soledad_uid(self, mbox, value):
        """
        Set last uid for a given mbox in the soledad integer cache.
        SoledadMailbox should prime this value during initialization.
        Other methods (during message adding) SHOULD call
        `increment_last_soledad_uid` instead.

        :param mbox: the mailbox
        :type mbox: str or unicode
        :param value: the value to set
        :type value: int
        """
        leap_assert_type(value, int)
        logger.info("setting last soledad uid for %s to %s" %
                    (mbox, value))
        # if we already have a value here, don't do anything
        with self._last_uid_lock:
            if not self._last_uid.get(mbox, None):
                self._last_uid[mbox] = value

    def increment_last_soledad_uid(self, mbox):
        """
        Increment by one the soledad integer cache for the last_uid for
        this mbox, and fire a defer-to-thread to update the soledad value.
        The caller should lock the call tho this method.

        :param mbox: the mailbox
        :type mbox: str or unicode
        """
        with self._last_uid_lock:
            self._last_uid[mbox] += 1
            value = self._last_uid[mbox]
            self.write_last_uid(mbox, value)
            return value

    @deferred
    def write_last_uid(self, mbox, value):
        """
        Increment the soledad integer cache for the highest uid value.

        :param mbox: the mailbox
        :type mbox: str or unicode
        :param value: the value to set
        :type value: int
        """
        leap_assert_type(value, int)
        if self._permanent_store:
            self._permanent_store.write_last_uid(mbox, value)

    # Counting sheeps...

    def count_new_mbox(self, mbox):
        """
        Count the new messages by inbox.

        :param mbox: the mailbox
        :type mbox: str or unicode
        :return: number of new messages
        :rtype: int
        """
        return len([(m, uid) for m, uid in self._new if mbox == mbox])

    # XXX used at all?
    def count_new(self):
        """
        Count all the new messages in the MemoryStore.

        :rtype: int
        """
        return len(self._new)

    def get_cdoc_from_phash(self, phash):
        """
        Return a content-document by its payload-hash.

        :param phash: the payload hash to check against
        :type phash: str or unicode
        :rtype: MessagePartDoc
        """
        doc = self._phash_store.get(phash, None)

        # XXX return None for consistency?

        # XXX have to keep a mapping between phash and its linkage
        # info, to know if this payload is been already saved or not.
        # We will be able to get this from the linkage-docs,
        # not yet implemented.
        new = True
        dirty = False
        return MessagePartDoc(
            new=new, dirty=dirty, store="mem",
            part=MessagePartType.cdoc,
            content=doc,
            doc_id=None)

    def get_fdoc_from_chash(self, chash, mbox):
        """
        Return a flags-document by its content-hash and a given mailbox.
        Used during content-duplication detection while copying or adding a
        message.

        :param chash: the content hash to check against
        :type chash: str or unicode
        :param mbox: the mailbox
        :type mbox: str or unicode

        :return: MessagePartDoc, or None.
        """
        docs_dict = self._chash_fdoc_store.get(chash, None)
        fdoc = docs_dict.get(mbox, None) if docs_dict else None

        # a couple of special cases.
        # 1. We might have a doc with empty content...
        if empty(fdoc):
            return None

        # 2. ...Or the message could exist, but being flagged for deletion.
        # We want to create a new one in this case.
        # Hmmm what if the deletion is un-done?? We would end with a
        # duplicate...
        if fdoc and fields.DELETED_FLAG in fdoc[fields.FLAGS_KEY]:
            return None

        uid = fdoc[fields.UID_KEY]
        key = mbox, uid
        new = key in self._new
        dirty = key in self._dirty
        return MessagePartDoc(
            new=new, dirty=dirty, store="mem",
            part=MessagePartType.fdoc,
            content=fdoc,
            doc_id=None)

    def all_msg_iter(self):
        """
        Return generator that iterates through all messages in the store.

        :rtype: generator
        """
        return (self.get_message(*key)
                for key in sorted(self._msg_store.keys()))

    def all_new_dirty_msg_iter(self):
        """
        Return generator that iterates through all new and dirty messages.

        :rtype: generator
        """
        return (self.get_message(*key)
                for key in sorted(self._msg_store.keys())
                if key in self._new or key in self._dirty)

    def all_msg_dict_for_mbox(self, mbox):
        """
        Return all the message dicts for a given mbox.

        :param mbox: the mailbox
        :type mbox: str or unicode
        :rtype: list
        """
        # This *needs* to return a fixed sequence. Otherwise the dictionary len
        # will change during iteration, when we modify it
        return [self._msg_store[(mb, uid)]
                for mb, uid in self._msg_store if mb == mbox]

    def all_deleted_uid_iter(self, mbox):
        """
        Return a list with the UIDs for all messags
        with deleted flag in a given mailbox.

        :param mbox: the mailbox
        :type mbox: str or unicode
        :rtype: list
        """
        # This *needs* to return a fixed sequence. Otherwise the dictionary len
        # will change during iteration, when we modify it
        all_deleted = [
            msg['fdoc']['uid'] for msg in self.all_msg_dict_for_mbox(mbox)
            if msg.get('fdoc', None)
            and fields.DELETED_FLAG in msg['fdoc']['flags']]
        return all_deleted

    # new, dirty flags

    def _get_new_dirty_state(self, key):
        """
        Return `new` and `dirty` flags for a given message.

        :param key: the key for the message, in the form mbox, uid
        :type key: tuple
        :rtype: tuple of bools
        """
        # XXX should return *first* the news, and *then* the dirty...
        return map(lambda _set: key in _set, (self._new, self._dirty))

    def set_new(self, key):
        """
        Add the key value to the `new` set.

        :param key: the key for the message, in the form mbox, uid
        :type key: tuple
        """
        self._new.add(key)

    def unset_new(self, key):
        """
        Remove the key value from the `new` set.

        :param key: the key for the message, in the form mbox, uid
        :type key: tuple
        """
        self._new.discard(key)
        deferreds = self._new_deferreds
        d = deferreds.get(key, None)
        if d:
            # XXX use a namedtuple for passing the result
            # when we check it in the other side.
            d.callback('%s, ok' % str(key))
            deferreds.pop(key)

    def set_dirty(self, key):
        """
        Add the key value to the `dirty` set.

        :param key: the key for the message, in the form mbox, uid
        :type key: tuple
        """
        self._dirty.add(key)

    def unset_dirty(self, key):
        """
        Remove the key value from the `dirty` set.

        :param key: the key for the message, in the form mbox, uid
        :type key: tuple
        """
        self._dirty.discard(key)
        deferreds = self._dirty_deferreds
        d = deferreds.get(key, None)
        if d:
            # XXX use a namedtuple for passing the result
            # when we check it in the other side.
            d.callback('%s, ok' % str(key))
            deferreds.pop(key)

    # Recent Flags

    # TODO --- nice but unused
    def set_recent_flag(self, mbox, uid):
        """
        Set the `Recent` flag for a given mailbox and UID.

        :param mbox: the mailbox
        :type mbox: str or unicode
        :param uid: the message UID
        :type uid: int
        """
        self._rflags_dirty.add(mbox)
        self._rflags_store[mbox]['set'].add(uid)

    # TODO --- nice but unused
    def unset_recent_flag(self, mbox, uid):
        """
        Unset the `Recent` flag for a given mailbox and UID.

        :param mbox: the mailbox
        :type mbox: str or unicode
        :param uid: the message UID
        :type uid: int
        """
        self._rflags_store[mbox]['set'].discard(uid)

    def set_recent_flags(self, mbox, value):
        """
        Set the value for the set of the recent flags.
        Used from the property in the MessageCollection.

        :param mbox: the mailbox
        :type mbox: str or unicode
        :param value: a sequence of flags to set
        :type value: sequence
        """
        self._rflags_dirty.add(mbox)
        self._rflags_store[mbox]['set'] = set(value)

    def load_recent_flags(self, mbox, flags_doc):
        """
        Load the passed flags document in the recent flags store, for a given
        mailbox.

        :param mbox: the mailbox
        :type mbox: str or unicode
        :param flags_doc: A dictionary containing the `doc_id` of the Soledad
                          flags-document for this mailbox, and the `set`
                          of uids marked with that flag.
        """
        self._rflags_store[mbox] = flags_doc

    def get_recent_flags(self, mbox):
        """
        Return the set of UIDs with the `Recent` flag for this mailbox.

        :param mbox: the mailbox
        :type mbox: str or unicode
        :rtype: set, or None
        """
        rflag_for_mbox = self._rflags_store.get(mbox, None)
        if not rflag_for_mbox:
            return None
        return self._rflags_store[mbox]['set']

    def all_rdocs_iter(self):
        """
        Return an iterator through all in-memory recent flag dicts, wrapped
        under a RecentFlagsDoc namedtuple.
        Used for saving to disk.

        :rtype: generator
        """
        # XXX use enums
        DOC_ID = "doc_id"
        SET = "set"

        rflags_store = self._rflags_store

        def get_rdoc(mbox, rdict):
            mbox_rflag_set = rdict[SET]
            recent_set = copy(mbox_rflag_set)
            # zero it!
            mbox_rflag_set.difference_update(mbox_rflag_set)
            return RecentFlagsDoc(
                doc_id=rflags_store[mbox][DOC_ID],
                content={
                    fields.TYPE_KEY: fields.TYPE_RECENT_VAL,
                    fields.MBOX_KEY: mbox,
                    fields.RECENTFLAGS_KEY: list(recent_set)
                })

        return (get_rdoc(mbox, rdict) for mbox, rdict in rflags_store.items()
                if not empty(rdict[SET]))

    # Methods that mirror the IMailbox interface

    def remove_all_deleted(self, mbox):
        """
        Remove all messages flagged \\Deleted from this Memory Store only.
        Called from `expunge`

        :param mbox: the mailbox
        :type mbox: str or unicode
        """
        mem_deleted = self.all_deleted_uid_iter(mbox)
        for uid in mem_deleted:
            self.remove_message(mbox, uid)
        return mem_deleted

    def expunge(self, mbox):
        """
        Remove all messages flagged \\Deleted, from the Memory Store
        and from the permanent store also.

        :param mbox: the mailbox
        :type mbox: str or unicode
        """
        # TODO expunge should add itself as a callback to the ongoing
        # writes.
        soledad_store = self._permanent_store

        try:
            # 1. Stop the writing call
            self._stop_write_loop()
            # 2. Enqueue a last write.
            #self.write_messages(soledad_store)
            # 3. Should wait on the writebacks to finish ???
            # FIXME wait for this, and add all the rest of the method
            # as a callback!!!
        except Exception as exc:
            logger.exception(exc)

        # Now, we...:

        try:
            # 1. Delete all messages marked as deleted in soledad.

            # XXX this could be deferred for faster operation.
            if soledad_store:
                sol_deleted = soledad_store.remove_all_deleted(mbox)
            else:
                sol_deleted = []

            # 2. Delete all messages marked as deleted in memory.
            mem_deleted = self.remove_all_deleted(mbox)

            all_deleted = set(mem_deleted).union(set(sol_deleted))
            logger.debug("deleted %r" % all_deleted)
        except Exception as exc:
            logger.exception(exc)
        finally:
            self._start_write_loop()
        return all_deleted

    # Dump-to-disk controls.

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
        # FIXME this should return a deferred !!!
        # XXX ----- can fire when all new + dirty deferreds
        # are done (gatherResults)
        return getattr(self, self.WRITING_FLAG)

    # Memory management.

    def get_size(self):
        """
        Return the size of the internal storage.
        Use for calculating the limit beyond which we should flush the store.

        :rtype: bool
        """
        return size.get_size(self._msg_store)
