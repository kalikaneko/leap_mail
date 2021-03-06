# *- coding: utf-8 -*-
# mailbox.py
# Copyright (C) 2013 LEAP
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
Soledad Mailbox.
"""
import copy
import threading
import logging
import StringIO
import cStringIO
import os

from collections import defaultdict

from twisted.internet import defer
from twisted.internet.task import deferLater
from twisted.python import log

from twisted.mail import imap4
from zope.interface import implements

from leap.common import events as leap_events
from leap.common.events.events_pb2 import IMAP_UNREAD_MAIL
from leap.common.check import leap_assert, leap_assert_type
from leap.mail.decorators import deferred_to_thread
from leap.mail.utils import empty
from leap.mail.imap.fields import WithMsgFields, fields
from leap.mail.imap.messages import MessageCollection
from leap.mail.imap.messageparts import MessageWrapper
from leap.mail.imap.parser import MBoxParser

logger = logging.getLogger(__name__)

"""
If the environment variable `LEAP_SKIPNOTIFY` is set, we avoid
notifying clients of new messages. Use during stress tests.
"""
NOTIFY_NEW = not os.environ.get('LEAP_SKIPNOTIFY', False)


class SoledadMailbox(WithMsgFields, MBoxParser):
    """
    A Soledad-backed IMAP mailbox.

    Implements the high-level method needed for the Mailbox interfaces.
    The low-level database methods are contained in MessageCollection class,
    which we instantiate and make accessible in the `messages` attribute.
    """
    implements(
        imap4.IMailbox,
        imap4.IMailboxInfo,
        imap4.ICloseableMailbox,
        imap4.ISearchableMailbox,
        imap4.IMessageCopier)

    # XXX should finish the implementation of IMailboxListener
    # XXX should completely implement ISearchableMailbox too

    messages = None
    _closed = False

    INIT_FLAGS = (WithMsgFields.SEEN_FLAG, WithMsgFields.ANSWERED_FLAG,
                  WithMsgFields.FLAGGED_FLAG, WithMsgFields.DELETED_FLAG,
                  WithMsgFields.DRAFT_FLAG, WithMsgFields.RECENT_FLAG,
                  WithMsgFields.LIST_FLAG)
    flags = None

    CMD_MSG = "MESSAGES"
    CMD_RECENT = "RECENT"
    CMD_UIDNEXT = "UIDNEXT"
    CMD_UIDVALIDITY = "UIDVALIDITY"
    CMD_UNSEEN = "UNSEEN"

    # FIXME we should turn this into a datastructure with limited capacity
    _listeners = defaultdict(set)

    next_uid_lock = threading.Lock()

    def __init__(self, mbox, soledad, memstore, rw=1):
        """
        SoledadMailbox constructor. Needs to get passed a name, plus a
        Soledad instance.

        :param mbox: the mailbox name
        :type mbox: str

        :param soledad: a Soledad instance.
        :type soledad: Soledad

        :param memstore: a MemoryStore instance
        :type memstore: MemoryStore

        :param rw: read-and-write flag for this mailbox
        :type rw: int
        """
        leap_assert(mbox, "Need a mailbox name to initialize")
        leap_assert(soledad, "Need a soledad instance to initialize")

        # XXX should move to wrapper
        #leap_assert(isinstance(soledad._db, SQLCipherDatabase),
                    #"soledad._db must be an instance of SQLCipherDatabase")

        self.mbox = self._parse_mailbox_name(mbox)
        self.rw = rw

        self._soledad = soledad
        self._memstore = memstore

        self.messages = MessageCollection(
            mbox=mbox, soledad=self._soledad, memstore=self._memstore)

        if not self.getFlags():
            self.setFlags(self.INIT_FLAGS)

        if self._memstore:
            self.prime_known_uids_to_memstore()
            self.prime_last_uid_to_memstore()

    @property
    def listeners(self):
        """
        Returns listeners for this mbox.

        The server itself is a listener to the mailbox.
        so we can notify it (and should!) after changes in flags
        and number of messages.

        :rtype: set
        """
        return self._listeners[self.mbox]

    # TODO this grows too crazily when many instances are fired, like
    # during imaptest stress testing. Should have a queue of limited size
    # instead.
    def addListener(self, listener):
        """
        Add a listener to the listeners queue.
        The server adds itself as a listener when there is a SELECT,
        so it can send EXIST commands.

        :param listener: listener to add
        :type listener: an object that implements IMailboxListener
        """
        if not NOTIFY_NEW:
            return

        logger.debug('adding mailbox listener: %s' % listener)
        self.listeners.add(listener)

    def removeListener(self, listener):
        """
        Remove a listener from the listeners queue.

        :param listener: listener to remove
        :type listener: an object that implements IMailboxListener
        """
        self.listeners.remove(listener)

    # TODO move completely to soledadstore, under memstore reponsibility.
    def _get_mbox(self):
        """
        Return mailbox document.

        :return: A SoledadDocument containing this mailbox, or None if
                 the query failed.
        :rtype: SoledadDocument or None.
        """
        try:
            query = self._soledad.get_from_index(
                fields.TYPE_MBOX_IDX,
                fields.TYPE_MBOX_VAL, self.mbox)
            if query:
                return query.pop()
        except Exception as exc:
            logger.exception("Unhandled error %r" % exc)

    def getFlags(self):
        """
        Returns the flags defined for this mailbox.

        :returns: tuple of flags for this mailbox
        :rtype: tuple of str
        """
        mbox = self._get_mbox()
        if not mbox:
            return None
        flags = mbox.content.get(self.FLAGS_KEY, [])
        return map(str, flags)

    def setFlags(self, flags):
        """
        Sets flags for this mailbox.

        :param flags: a tuple with the flags
        :type flags: tuple of str
        """
        leap_assert(isinstance(flags, tuple),
                    "flags expected to be a tuple")
        mbox = self._get_mbox()
        if not mbox:
            return None
        mbox.content[self.FLAGS_KEY] = map(str, flags)
        self._soledad.put_doc(mbox)

    # XXX SHOULD BETTER IMPLEMENT ADD_FLAG, REMOVE_FLAG.

    def _get_closed(self):
        """
        Return the closed attribute for this mailbox.

        :return: True if the mailbox is closed
        :rtype: bool
        """
        mbox = self._get_mbox()
        return mbox.content.get(self.CLOSED_KEY, False)

    def _set_closed(self, closed):
        """
        Set the closed attribute for this mailbox.

        :param closed: the state to be set
        :type closed: bool
        """
        leap_assert(isinstance(closed, bool), "closed needs to be boolean")
        mbox = self._get_mbox()
        mbox.content[self.CLOSED_KEY] = closed
        self._soledad.put_doc(mbox)

    closed = property(
        _get_closed, _set_closed, doc="Closed attribute.")

    def _get_last_uid(self):
        """
        Return the last uid for this mailbox.
        If we have a memory store, the last UID will be the highest
        recorded UID in the message store, or a counter cached from
        the mailbox document in soledad if this is higher.

        :return: the last uid for messages in this mailbox
        :rtype: int
        """
        last = self._memstore.get_last_uid(self.mbox)
        logger.debug("last uid for %s: %s (from memstore)" % (
            repr(self.mbox), last))
        return last

    last_uid = property(
        _get_last_uid, doc="Last_UID attribute.")

    def prime_last_uid_to_memstore(self):
        """
        Prime memstore with last_uid value
        """
        set_exist = set(self.messages.all_uid_iter())
        last = max(set_exist) if set_exist else 0
        logger.info("Priming Soledad last_uid to %s" % (last,))
        self._memstore.set_last_soledad_uid(self.mbox, last)

    def prime_known_uids_to_memstore(self):
        """
        Prime memstore with the set of all known uids.

        We do this to be able to filter the requests efficiently.
        """
        known_uids = self.messages.all_soledad_uid_iter()
        self._memstore.set_known_uids(self.mbox, known_uids)

    def getUIDValidity(self):
        """
        Return the unique validity identifier for this mailbox.

        :return: unique validity identifier
        :rtype: int
        """
        mbox = self._get_mbox()
        return mbox.content.get(self.CREATED_KEY, 1)

    def getUID(self, message):
        """
        Return the UID of a message in the mailbox

        .. note:: this implementation does not make much sense RIGHT NOW,
        but in the future will be useful to get absolute UIDs from
        message sequence numbers.

        :param message: the message uid
        :type message: int

        :rtype: int
        """
        msg = self.messages.get_msg_by_uid(message)
        return msg.getUID()

    def getUIDNext(self):
        """
        Return the likely UID for the next message added to this
        mailbox. Currently it returns the higher UID incremented by
        one.

        We increment the next uid *each* time this function gets called.
        In this way, there will be gaps if the message with the allocated
        uid cannot be saved. But that is preferable to having race conditions
        if we get to parallel message adding.

        :rtype: int
        """
        with self.next_uid_lock:
            if self._memstore:
                return self.last_uid + 1
            else:
                # XXX after lock, it should be safe to
                # return just the increment here, and
                # have a different method that actually increments
                # the counter when really adding.
                self.last_uid += 1
                return self.last_uid

    def getMessageCount(self):
        """
        Returns the total count of messages in this mailbox.

        :rtype: int
        """
        return self.messages.count()

    def getUnseenCount(self):
        """
        Returns the number of messages with the 'Unseen' flag.

        :return: count of messages flagged `unseen`
        :rtype: int
        """
        return self.messages.count_unseen()

    def getRecentCount(self):
        """
        Returns the number of messages with the 'Recent' flag.

        :return: count of messages flagged `recent`
        :rtype: int
        """
        return self.messages.count_recent()

    def isWriteable(self):
        """
        Get the read/write status of the mailbox.

        :return: 1 if mailbox is read-writeable, 0 otherwise.
        :rtype: int
        """
        return self.rw

    def getHierarchicalDelimiter(self):
        """
        Returns the character used to delimite hierarchies in mailboxes.

        :rtype: str
        """
        return '/'

    def requestStatus(self, names):
        """
        Handles a status request by gathering the output of the different
        status commands.

        :param names: a list of strings containing the status commands
        :type names: iter
        """
        r = {}
        if self.CMD_MSG in names:
            r[self.CMD_MSG] = self.getMessageCount()
        if self.CMD_RECENT in names:
            r[self.CMD_RECENT] = self.getRecentCount()
        if self.CMD_UIDNEXT in names:
            r[self.CMD_UIDNEXT] = self.last_uid + 1
        if self.CMD_UIDVALIDITY in names:
            r[self.CMD_UIDVALIDITY] = self.getUIDValidity()
        if self.CMD_UNSEEN in names:
            r[self.CMD_UNSEEN] = self.getUnseenCount()
        return defer.succeed(r)

    def addMessage(self, message, flags, date=None):
        """
        Adds a message to this mailbox.

        :param message: the raw message
        :type message: str

        :param flags: flag list
        :type flags: list of str

        :param date: timestamp
        :type date: str

        :return: a deferred that evals to None
        """
        # TODO have a look at the cases for internal date in the rfc
        if isinstance(message, (cStringIO.OutputType, StringIO.StringIO)):
            message = message.getvalue()

        # XXX we could treat the message as an IMessage from here
        leap_assert_type(message, basestring)
        if flags is None:
            flags = tuple()
        else:
            flags = tuple(str(flag) for flag in flags)

        d = self._do_add_message(message, flags=flags, date=date)
        return d

    def _do_add_message(self, message, flags, date):
        """
        Calls to the messageCollection add_msg method.
        Invoked from addMessage.
        """
        d = self.messages.add_msg(message, flags=flags, date=date)
        # XXX Removing notify temporarily.
        # This is interfering with imaptest results. I'm not clear if it's
        # because we clutter the logging or because the set of listeners is
        # ever-growing. We should come up with some smart way of dealing with
        # it, or maybe just disabling it using an environmental variable since
        # we will only have just a few listeners in the regular desktop case.
        #d.addCallback(self.notify_new)
        return d

    def notify_new(self, *args):
        """
        Notify of new messages to all the listeners.

        :param args: ignored.
        """
        if not NOTIFY_NEW:
            return
        exists = self.getMessageCount()
        recent = self.getRecentCount()
        logger.debug("NOTIFY: there are %s messages, %s recent" % (
            exists,
            recent))

        for l in self.listeners:
            logger.debug('notifying...')
            l.newMessages(exists, recent)

    # commands, do not rename methods

    def destroy(self):
        """
        Called before this mailbox is permanently deleted.

        Should cleanup resources, and set the \\Noselect flag
        on the mailbox.
        """
        self.setFlags((self.NOSELECT_FLAG,))
        self.deleteAllDocs()

        # XXX removing the mailbox in situ for now,
        # we should postpone the removal

        # XXX move to memory store??
        self._soledad.delete_doc(self._get_mbox())

    def _close_cb(self, result):
        self.closed = True

    def close(self):
        """
        Expunge and mark as closed
        """
        d = self.expunge()
        d.addCallback(self._close_cb)
        return d

    def expunge(self):
        """
        Remove all messages flagged \\Deleted
        """
        if not self.isWriteable():
            raise imap4.ReadOnlyMailbox
        d = defer.Deferred()
        self._memstore.expunge(self.mbox, d)
        return d

    def _bound_seq(self, messages_asked):
        """
        Put an upper bound to a messages sequence if this is open.

        :param messages_asked: IDs of the messages.
        :type messages_asked: MessageSet
        :rtype: MessageSet
        """
        if not messages_asked.last:
            try:
                iter(messages_asked)
            except TypeError:
                # looks like we cannot iterate
                try:
                    messages_asked.last = self.last_uid
                except ValueError:
                    pass
        return messages_asked

    def _filter_msg_seq(self, messages_asked):
        """
        Filter a message sequence returning only the ones that do exist in the
        collection.

        :param messages_asked: IDs of the messages.
        :type messages_asked: MessageSet
        :rtype: set
        """
        set_asked = set(messages_asked)
        set_exist = set(self.messages.all_uid_iter())
        seq_messg = set_asked.intersection(set_exist)
        return seq_messg

    @deferred_to_thread
    #@profile
    def fetch(self, messages_asked, uid):
        """
        Retrieve one or more messages in this mailbox.

        from rfc 3501: The data items to be fetched can be either a single atom
        or a parenthesized list.

        :param messages_asked: IDs of the messages to retrieve information
                               about
        :type messages_asked: MessageSet

        :param uid: If true, the IDs are UIDs. They are message sequence IDs
                    otherwise.
        :type uid: bool

        :rtype: A tuple of two-tuples of message sequence numbers and
                LeapMessage
        """
        # For the moment our UID is sequential, so we
        # can treat them all the same.
        # Change this to the flag that twisted expects when we
        # switch to content-hash based index + local UID table.

        sequence = False
        #sequence = True if uid == 0 else False

        messages_asked = self._bound_seq(messages_asked)
        seq_messg = self._filter_msg_seq(messages_asked)
        getmsg = lambda uid: self.messages.get_msg_by_uid(uid)

        # for sequence numbers (uid = 0)
        if sequence:
            logger.debug("Getting msg by index: INEFFICIENT call!")
            raise NotImplementedError
        else:
            result = ((msgid, getmsg(msgid)) for msgid in seq_messg)
        return result

    @deferred_to_thread
    def fetch_flags(self, messages_asked, uid):
        """
        A fast method to fetch all flags, tricking just the
        needed subset of the MIME interface that's needed to satisfy
        a generic FLAGS query.

        Given how LEAP Mail is supposed to work without local cache,
        this query is going to be quite common, and also we expect
        it to be in the form 1:* at the beginning of a session, so
        it's not bad to fetch all the FLAGS docs at once.

        :param messages_asked: IDs of the messages to retrieve information
                               about
        :type messages_asked: MessageSet

        :param uid: If true, the IDs are UIDs. They are message sequence IDs
                    otherwise.
        :type uid: bool

        :return: A tuple of two-tuples of message sequence numbers and
                flagsPart, which is a only a partial implementation of
                MessagePart.
        :rtype: tuple
        """
        class flagsPart(object):
            def __init__(self, uid, flags):
                self.uid = uid
                self.flags = flags

            def getUID(self):
                return self.uid

            def getFlags(self):
                return map(str, self.flags)

        messages_asked = self._bound_seq(messages_asked)
        seq_messg = self._filter_msg_seq(messages_asked)

        all_flags = self.messages.all_flags()
        result = ((msgid, flagsPart(
            msgid, all_flags.get(msgid, tuple()))) for msgid in seq_messg)
        return result

    @deferred_to_thread
    def fetch_headers(self, messages_asked, uid):
        """
        A fast method to fetch all headers, tricking just the
        needed subset of the MIME interface that's needed to satisfy
        a generic HEADERS query.

        Given how LEAP Mail is supposed to work without local cache,
        this query is going to be quite common, and also we expect
        it to be in the form 1:* at the beginning of a session, so
        **MAYBE** it's not too bad to fetch all the HEADERS docs at once.

        :param messages_asked: IDs of the messages to retrieve information
                               about
        :type messages_asked: MessageSet

        :param uid: If true, the IDs are UIDs. They are message sequence IDs
                    otherwise.
        :type uid: bool

        :return: A tuple of two-tuples of message sequence numbers and
                headersPart, which is a only a partial implementation of
                MessagePart.
        :rtype: tuple
        """
        class headersPart(object):
            def __init__(self, uid, headers):
                self.uid = uid
                self.headers = headers

            def getUID(self):
                return self.uid

            def getHeaders(self, _):
                return dict(
                    (str(key), str(value))
                    for key, value in
                    self.headers.items())

        messages_asked = self._bound_seq(messages_asked)
        seq_messg = self._filter_msg_seq(messages_asked)

        all_chash = self.messages.all_flags_chash()
        all_headers = self.messages.all_headers()
        result = ((msgid, headersPart(
            msgid, all_headers.get(all_chash.get(msgid, 'nil'), {})))
            for msgid in seq_messg)
        return result

    def signal_unread_to_ui(self, *args, **kwargs):
        """
        Sends unread event to ui.

        :param args: ignored
        :param kwargs: ignored
        """
        unseen = self.getUnseenCount()
        leap_events.signal(IMAP_UNREAD_MAIL, str(unseen))

    def store(self, messages_asked, flags, mode, uid):
        """
        Sets the flags of one or more messages.

        :param messages: The identifiers of the messages to set the flags
        :type messages: A MessageSet object with the list of messages requested

        :param flags: The flags to set, unset, or add.
        :type flags: sequence of str

        :param mode: If mode is -1, these flags should be removed from the
                     specified messages.  If mode is 1, these flags should be
                     added to the specified messages.  If mode is 0, all
                     existing flags should be cleared and these flags should be
                     added.
        :type mode: -1, 0, or 1

        :param uid: If true, the IDs specified in the query are UIDs;
                    otherwise they are message sequence IDs.
        :type uid: bool

        :return: A deferred, that will be called with a dict mapping message
                 sequence numbers to sequences of str representing the flags
                 set on the message after this operation has been performed.
        :rtype: deferred

        :raise ReadOnlyMailbox: Raised if this mailbox is not open for
                                read-write.
        """
        from twisted.internet import reactor
        if not self.isWriteable():
            log.msg('read only mailbox!')
            raise imap4.ReadOnlyMailbox

        d = defer.Deferred()
        deferLater(reactor, 0, self._do_store, messages_asked, flags,
                   mode, uid, d)
        return d

    def _do_store(self, messages_asked, flags, mode, uid, observer):
        """
        Helper method, invoke set_flags method in the MessageCollection.

        See the documentation for the `store` method for the parameters.

        :param observer: a deferred that will be called with the dictionary
                         mapping UIDs to flags after the operation has been
                         done.
        :type observer: deferred
        """
        # XXX implement also sequence (uid = 0)
        # XXX we should prevent cclient from setting Recent flag?
        leap_assert(not isinstance(flags, basestring),
                    "flags cannot be a string")
        flags = tuple(flags)
        messages_asked = self._bound_seq(messages_asked)
        seq_messg = self._filter_msg_seq(messages_asked)
        self.messages.set_flags(self.mbox, seq_messg, flags, mode, observer)

    # ISearchableMailbox

    def search(self, query, uid):
        """
        Search for messages that meet the given query criteria.

        Warning: this is half-baked, and it might give problems since
        it offers the SearchableInterface.
        We'll be implementing it asap.

        :param query: The search criteria
        :type query: list

        :param uid: If true, the IDs specified in the query are UIDs;
                    otherwise they are message sequence IDs.
        :type uid: bool

        :return: A list of message sequence numbers or message UIDs which
                 match the search criteria or a C{Deferred} whose callback
                 will be invoked with such a list.
        :rtype: C{list} or C{Deferred}
        """
        # TODO see if we can raise w/o interrupting flow
        #:raise IllegalQueryError: Raised when query is not valid.
        # example query:
        #  ['UNDELETED', 'HEADER', 'Message-ID',
        #   '52D44F11.9060107@dev.bitmask.net']

        # TODO hardcoding for now! -- we'll support generic queries later on
        # but doing a quickfix for avoiding duplicat saves in the draft folder.
        # See issue #4209

        if len(query) > 2:
            if query[1] == 'HEADER' and query[2].lower() == "message-id":
                msgid = str(query[3]).strip()
                d = self.messages._get_uid_from_msgid(str(msgid))
                d1 = defer.gatherResults([d])
                # we want a list, so return it all the same
                return d1

        # nothing implemented for any other query
        logger.warning("Cannot process query: %s" % (query,))
        return []

    # IMessageCopier

    def copy(self, message):
        """
        Copy the given message object into this mailbox.

        :param message: an IMessage implementor
        :type message: LeapMessage
        :return: a deferred that will be fired with the message
                 uid when the copy succeed.
        :rtype: Deferred
        """
        from twisted.internet import reactor

        d = defer.Deferred()
        # XXX this should not happen ... track it down,
        # probably to FETCH...
        if message is None:
            log.msg("BUG: COPY found a None in passed message")
            d.callback(None)
        deferLater(reactor, 0, self._do_copy, message, d)
        return d

    def _do_copy(self, message, observer):
        """
        Call invoked from the deferLater in `copy`. This will
        copy the flags and header documents, and pass them to the
        `create_message` method in the MemoryStore, together with
        the observer deferred that we've been passed along.

        :param message: an IMessage implementor
        :type message: LeapMessage
        :param observer: the deferred that will fire with the
                         UID of the message
        :type observer: Deferred
        """
        # XXX  for clarity, this could be delegated to a
        # MessageCollection mixin that implements copy too, and
        # moved out of here.
        msg = message
        memstore = self._memstore

        # XXX should use a public api instead
        fdoc = msg._fdoc
        hdoc = msg._hdoc
        if not fdoc:
            logger.warning("Tried to copy a MSG with no fdoc")
            return
        new_fdoc = copy.deepcopy(fdoc.content)

        fdoc_chash = new_fdoc[fields.CONTENT_HASH_KEY]

        # XXX is this hitting the db??? --- probably.
        # We should profile after the pre-fetch.
        dest_fdoc = memstore.get_fdoc_from_chash(
            fdoc_chash, self.mbox)
        exist = dest_fdoc and not empty(dest_fdoc.content)

        if exist:
            # Should we signal error on the callback?
            logger.warning("Destination message already exists!")

            # XXX I'm still not clear if we should raise the
            # errback. This actually rases an ugly warning
            # in some muas like thunderbird. I guess the user does
            # not deserve that.
            observer.callback(True)
        else:
            mbox = self.mbox
            uid_next = memstore.increment_last_soledad_uid(mbox)
            new_fdoc[self.UID_KEY] = uid_next
            new_fdoc[self.MBOX_KEY] = mbox

            # FIXME set recent!

            self._memstore.create_message(
                self.mbox, uid_next,
                MessageWrapper(
                    new_fdoc, hdoc.content),
                observer=observer,
                notify_on_disk=False)

    # convenience fun

    def deleteAllDocs(self):
        """
        Delete all docs in this mailbox
        """
        docs = self.messages.get_all_docs()
        for doc in docs:
            self.messages._soledad.delete_doc(doc)

    def unset_recent_flags(self, uids):
        """
        Unset Recent flag for a sequence of UIDs.
        """
        seq_messg = self._bound_seq(uids)
        self.messages.unset_recent_flags(seq_messg)

    def __repr__(self):
        """
        Representation string for this mailbox.
        """
        return u"<SoledadMailbox: mbox '%s' (%s)>" % (
            self.mbox, self.messages.count())
