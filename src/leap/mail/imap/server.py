# -*- coding: utf-8 -*-
# server.py
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
Soledad-backed IMAP Server.
"""
import copy
import logging

from zope.interface import implements

from twisted.mail import imap4
from twisted.internet import defer

#from twisted import cred

import u1db

from leap.common.check import leap_assert
from leap.soledad.backends.sqlcipher import SQLCipherDatabase

logger = logging.getLogger(__name__)


###################################
# SoledadAccount Index
###################################

class MissingIndexError(Exception):
    """raises when tried to access a non existent index document"""


class BadIndexError(Exception):
    """raises when index is malformed or has the wrong cardinality"""


EMPTY_INDEXDOC = {
    "is_index": True,
    "mailboxes": [],
    "subscriptions": [],
    "flags": {}}
get_empty_indexdoc = lambda: copy.deepcopy(EMPTY_INDEXDOC)


class IndexedDB(object):

    def _create_index_doc(self):
        """creates an empty index document"""
        indexdoc = get_empty_indexdoc()
        self._index = self._soledad.create_doc(
            indexdoc)

    def _get_index_doc(self):
        """gets index document"""
        indexdoc = self._db.get_from_index("isindex", "*")
        if not indexdoc:
            raise MissingIndexError
        if len(indexdoc) > 1:
            raise BadIndexError
        return indexdoc[0]

    def _update_index_doc(self):
        """updates index document"""
        self._db.put_doc(self._index)


class SoledadAccountIndex(IndexedDB):
    """
    Index for the Soledad Account
    keeps track of mailboxes and subscriptions
    """
    _index = None

    def __init__(self, soledad=None):
        """
        Constructor for the SoledadAccountIndex.
        Needs a soledad intance to be initialized
        """
        leap_assert(soledad, "Need a soledad instance to initialize")
        # XXX instance check

        self._soledad = soledad
        self._db = soledad._db
        self._initialize_db()

    def _initialize_db(self):
        """initialize the database"""
        db_indexes = dict(self._soledad._db.list_indexes())
        name, expression = "isindex", ["bool(is_index)"]
        if name not in db_indexes:
            self._soledad._db.create_index(name, *expression)
        try:
            self._index = self._get_index_doc()
        except MissingIndexError:
            print "no index!!! creating..."
            self._create_index_doc()

    # setters and getters for the index document

    def _get_mailboxes(self):
        """Get mailboxes associated with this account."""
        return self._index.content.setdefault('mailboxes', [])

    def _set_mailboxes(self, mailboxes):
        """Set mailboxes associated with this account."""
        self._index.content['mailboxes'] = list(set(mailboxes))
        self._update_index_doc()

    mailboxes = property(
        _get_mailboxes, _set_mailboxes, doc="Account mailboxes.")

    def _get_subscriptions(self):
        """Get subscriptions associated with this account."""
        return self._index.content.setdefault('subscriptions', [])

    def _set_subscriptions(self, subscriptions):
        """Set subscriptions associated with this account."""
        self._index.content['subscriptions'] = list(set(subscriptions))
        self._update_index_doc()

    subscriptions = property(
        _get_subscriptions, _set_subscriptions, doc="Account subscriptions.")

    def addMailbox(self, name):
        """add a mailbox to the mailboxes list."""
        name = name.upper()
        self.mailboxes.append(name)
        self._update_index_doc()

    def removeMailbox(self, name):
        """remove a mailbox from the mailboxes list."""
        self.mailboxes.remove(name)
        self._update_index_doc()

    def addSubscription(self, name):
        """add a subscription to the subscriptions list."""
        name = name.upper()
        self.subscriptions.append(name)
        self._update_index_doc()

    def removeSubscription(self, name):
        """
        Remove a subscription from the subscriptions list.
        """
        self.subscriptions.remove(name)
        self._update_index_doc()

    #
    # flags
    #

    def _get_flags(self):
        """Get flags from index for the account."""
        return self._index.content.setdefault('flags', {})

    def _set_flags(self, flags):
        """Set flags dict in the index for the account."""
        self._index.content['flags'] = flags
        self._update_index_doc()

    _flags = property(
        _get_flags, _set_flags, doc="Mailboxes flags.")


#######################################
# Soledad Account
#######################################

class SoledadBackedAccount(object):
    """
    An implementation of IAccount and INamespacePresenteer
    that is backed by Soledad Encrypted Documents.
    """

    implements(imap4.IAccount, imap4.INamespacePresenter)

    #top_id = 0  # XXX move top_id to _index

    _soledad = None
    _db = None

    selected = None

    def __init__(self, name, soledad=None):
        """
        SoledadBackedAccount constructor
        creates a SoledadAccountIndex that keeps track of the
        mailboxes and subscriptions handled by this account.

        @param name: the name of the account (user id)
        @param soledad: a Soledad instance
        """
        leap_assert(soledad, "Need a soledad instance to initialize")
        # XXX check isinstance ...
        # XXX SHOULD assert too that the name matches the user with which
        # soledad has been initialized.

        self.name = name.upper()
        self._index = SoledadAccountIndex(soledad=soledad)
        self._soledad = soledad

        self._db = soledad._db

    # XXX - this was allocating sequential IDs for the mailbox,
    # but since we're storing that in soledad index doc
    # we can get the document ID from there, no need for
    # an ID anymore.
    #def allocateID(self):
        #"""
        #"""
        #id = self.top_id
        #self.top_id += 1
        #return id

    @property
    def mailboxes(self):
        return self._index.mailboxes

    @property
    def subscriptions(self):
        return self._index.subscriptions

    def getMailbox(self, name):
        """
        Returns Mailbox with that name, without selecting it.
        @param: name
        """
        name = name.upper()
        return SoledadMailbox(name, soledad=self._soledad,
                              index=self._index)

    ##
    ## IAccount
    ##

    def addMailbox(self, name):
        """
        Adds a mailbox to the account

        @param name: the name of the mailbox
        @type name: str
        @rtype: bool
        """
        name = name.upper()
        if name in self.mailboxes:
            raise imap4.MailboxCollision, name
        #if mbox is None:
        #mbox = self._emptyMailbox(name, self.allocateID())
        self._index.addMailbox(name)
        return True

    def create(self, pathspec):
        # XXX What _exactly_ is the difference with addMailbox?
        # We accept here a path specification, which can contain
        # many levels, but look for the appropriate documentation
        # pointer.
        """
        Create a mailbox
        Return True if successfully created

        @param pathspec: XXX ??? -----------------
        @rtype: bool
        """
        paths = filter(None, pathspec.split('/'))
        for accum in range(1, len(paths)):
            try:
                self.addMailbox('/'.join(paths[:accum]))
            except imap4.MailboxCollision:
                pass
        try:
            self.addMailbox('/'.join(paths))
        except imap4.MailboxCollision:
            if not pathspec.endswith('/'):
                return False
        return True

    def select(self, name, readwrite=1):
        """
        Select a mailbox.
        @param name: the mailbox to select
        @param readwrite: 1 for readwrite permissions.
        @rtype: bool
        """
        self.selected = name
        return SoledadMailbox(name, rw=readwrite, soledad=self._soledad,
                              index=self._index)

    def delete(self, name, force=False):
        """
        Deletes a mailbox.
        Right now it does not purge the messages, but just removes the mailbox
        name from the mailboxes list!!!

        @param name: the mailbox to be deleted
        """
        name = name.upper()
        if not name in self.mailboxes:
            raise imap4.MailboxException("No such mailbox")

        #self._index.removeMailbox(name)

        mbox = self.getMailbox(name)

        #import ipdb; ipdb.set_trace()

        if force is False:
            # See if this box is flagged \Noselect
            if r'\Noselect' in mbox.getFlags():
                # Check for hierarchically inferior mailboxes with this one
                # as part of their root.
                for others in self.mailboxes:
                    if others != name and others.startswith(name):
                        raise imap4.MailboxException, (
                            "Hierarchically inferior mailboxes "
                            "exist and \\Noselect is set")
        mbox.destroy()

        # if there are no hierarchically inferior names, we will
        # delete it from our ken.
        if self._inferiorNames(name) > 1:
            #del self.mailboxes[name]
            self._index.removeMailbox(name)

    def rename(self, oldname, newname):
        """
        Renames a mailbox
        @param oldname: old name of the mailbox
        @param newname: new name of the mailbox
        """
        oldname = oldname.upper()
        newname = newname.upper()
        if oldname not in self.mailboxes:
            raise imap4.NoSuchMailbox, oldname

        inferiors = self._inferiorNames(oldname)
        inferiors = [(o, o.replace(oldname, newname, 1)) for o in inferiors]

        for (old, new) in inferiors:
            if new in self.mailboxes:
                raise imap4.MailboxCollision, new

        for (old, new) in inferiors:
            self.mailboxes[self.mailboxes.index(old)] = new

        # XXX ---- FIXME!!!! ------------------------------------
        # until here we just renamed the index...
        # We have to rename also the occurrence of this
        # mailbox on ALL the messages that are contained in it!!!
        # -------------------------------------------------------

    def _inferiorNames(self, name):
        """
        Return hierarchically inferior mailboxes
        @param name: the mailbox
        @rtype: list
        """
        inferiors = []
        for infname in self.mailboxes:
            if infname.startswith(name):
                inferiors.append(infname)
        return inferiors

    def isSubscribed(self, name):
        """
        Returns True if user is subscribed to this mailbox.
        @param name: the mailbox to be checked.
        @rtype: bool
        """
        return name.upper() in self.subscriptions

    def subscribe(self, name):
        """
        Subscribe to this mailbox
        @param name: the mailbox
        @type name: str
        """
        name = name.upper()
        if name not in self.subscriptions:
            self._index.addSubscription(name)

    def unsubscribe(self, name):
        """
        Unsubscribe from this mailbox
        @param name: the mailbox
        @type name: str
        """
        name = name.upper()
        if name not in self.subscriptions:
            raise imap4.MailboxException, "Not currently subscribed to " + name
        self._index.removeSubscription(name)

    def listMailboxes(self, ref, wildcard):
        """
        List the mailboxes.
        @param ref: XXX ---------------
        @param wildcard: XXX ----------
        """
        # XXX fill docstring ----------
        ref = self._inferiorNames(ref.upper())
        wildcard = imap4.wildcardToRegexp(wildcard, '/')
        return [(i, self.mailboxes[i]) for i in ref if wildcard.match(i)]

    def deleteAllMessages(self, iknowhatiamdoing=False):
        """
        Deletes all messages from all mailboxes.
        Danger! high voltage!

        @param iknowhatiamdoing: confirmation parameter, needs to be True
            to proceed.
        """
        if iknowhatiamdoing is True:
            for mbox in self.mailboxes:
                self.delete(mbox, force=True)

    ##
    ## INamespacePresenter
    ##

    def getPersonalNamespaces(self):
        return [["", "/"]]

    def getSharedNamespaces(self):
        return None

    def getOtherNamespaces(self):
        return None

#######################################
# Soledad Message, MessageCollection
# and Mailbox
#######################################


class Message(u1db.Document):

    """A rfc822 message item."""
    # XXX TODO use email module

    def _get_subject(self):
        """Get the message title."""
        return self.content.get('subject')

    def _set_subject(self, subject):
        """Set the message title."""
        self.content['subject'] = subject

    subject = property(_get_subject, _set_subject,
                       doc="Subject of the message.")

    def _get_seen(self):
        """Get the seen status of the message."""
        return self.content.get('seen', False)

    def _set_seen(self, value):
        """Set the seen status."""
        self.content['seen'] = value

    seen = property(_get_seen, _set_seen, doc="Seen flag.")

    def _get_flags(self):
        """Get flags associated with the message."""
        return self.content.setdefault('flags', [])

    def _set_flags(self, flags):
        """Set flags associated with the message."""
        self.content['flags'] = list(set(flags))

    flags = property(_get_flags, _set_flags, doc="Message flags.")


class MessageCollection(object):
    """
    A collection of messages, surprisingly.

    It is tied to a selected mailbox name that is passed to constructor.
    Implements a filter query over the messages contained in a soledad
    database, and deals with the initialization of the soledad database.
    """
    SEEN_INDEX = 'seen'
    FLAGS_INDEX = 'flags'
    MAILBOX_INDEX = 'mailbox'

    INDEXES = {
        FLAGS_INDEX: ['flags'],
        SEEN_INDEX: ['bool(seen)'],
        MAILBOX_INDEX: ['mailbox']
        # XXX Can we add some magic
        # for making more complex queries
        # when selecting by mailbox / flags?
    }

    EMPTY_MSG = {
        "subject": "",
        "seen": False,
        "flags": [],
        "mailbox": "inbox",
    }

    def __init__(self, mbox=None, db=None):
        """
        Constructor for MessageCollection

        @param mbox: the name of the mailbox. It is the name
                     with which we filter the query over the
                     messages database
        @type mbox: str
        @param db: SQLCipher database (contained in soledad)
        @type db: SQLCipher instance
        """
        leap_assert(mbox, "Need a mailbox name to initialize")
        leap_assert(mbox.strip() != "", "mbox cannot be blank space")
        leap_assert(isinstance(mbox, str), "mbox needs to be a string")
        leap_assert(db, "Need a db instance to initialize")
        leap_assert(isinstance(db, SQLCipherDatabase),
                    "db must be an instance of SQLCipherDatabase")

        # okay, all in order, keep going...

        self.mbox = mbox.upper()
        self.db = db
        self.initialize_db()

    def initialize_db(self):
        """
        Initialize the database.
        """
        # Ask the database for currently existing indexes.
        db_indexes = dict(self.db.list_indexes())
        # Loop through the indexes we expect to find.
        for name, expression in self.INDEXES.items():
            print 'name is', name
            if name not in db_indexes:
                # The index does not yet exist.
                print 'creating index'
                self.db.create_index(name, *expression)
                continue

            if expression == db_indexes[name]:
                print 'expression up to date'
                # The index exists and is up to date.
                continue
            # The index exists but the definition is not what expected, so we
            # delete it and add the proper index expression.
            print 'deleting index'
            self.db.delete_index(name)
            self.db.create_index(name, *expression)

            # XXX create index by MAILBOX -------------
            # FIXME -----------------------------------

    def get_empty_msg(self):
        """
        Return an empty message
        @rtype: dict
        """
        return copy.deepcopy(self.EMPTY_MSG)

    def add_msg(self, subject=None, flags=None):
        # XXX add raw message here
        """
        Create a new message document.
        @param mbox: name of the mbox to place this message in.
        @param subject: subject of the message.
        @param flags: flags
        """
        if flags is None:
            flags = []

        content = self.get_empty_msg()
        content['mailbox'] = self.mbox

        if subject or flags:
            content['subject'] = subject
            content['flags'] = flags
        # Store the document in the database. Since we did not set a document
        # id, the database will store it as a new document, and generate
        # a valid id.
        return self.db.create_doc(content)

    def get_all(self):
        """
        Get all messages for the selected mailbox

        @rtype: list
        """
        # XXX FILTER BY MAILBOX!!!
        #return self.db.get_from_index(self.SEEN_INDEX, "*")
        return self.db.get_from_index(self.MAILBOX_INDEX, self.mbox)

    def unseen_iter(self):
        """
        Get only unseen messages for the selected mailbox

        @rtype: iter
        """
        # we should be able to join the query-by-mailbox
        # and query-by-flag...
        # make list comprenhension by now ...
        #return self.db.get_from_index(self.SEEN_INDEX, "0")
        return (doc for doc in self.get_all()
                if doc.content['seen'] is False)

    def count(self):
        """
        Return the count of messages
        """
        return len(self.get_all())


class SoledadMailbox(object):
    """
    A Soledad-backed IMAP mailbox.

    Implements the high-level method needed for the Mailbox interfaces.
    The low-level database methods are contained in MessageCollection class,
    which we instantiate and make accessible in the `messages` attribute.
    """
    implements(imap4.IMailboxInfo, imap4.IMailbox, imap4.ICloseableMailbox)

    messages = None
    closed = False

    INIT_FLAGS = ('\\Seen', '\\Answered', '\\Flagged',
                  '\\Deleted', '\\Draft', '\\Recent', 'List')
    flags = None

    def __init__(self, mbox, soledad=None, rw=1, index=None):
        """
        SoledadMailbox constructor
        @param mbox: the mailbox name
        @param soledad: a Soledad instance.
        @param rw: read-and-write flags
        @type rw: bool
        """
        leap_assert(mbox, "Need a mailbox name to initialize")
        leap_assert(soledad, "Need a soledad instance to initialize")
        leap_assert(isinstance(soledad._db, SQLCipherDatabase),
                    "soledad._db must be an instance of SQLCipherDatabase")

        self.mbox = mbox
        self.rw = rw

        self._soledad = soledad
        self._db = soledad._db
        self._index = index

        self.messages = MessageCollection(
            mbox=mbox, db=soledad._db)

        if not self.getFlags():
            self.setFlags(self.INIT_FLAGS)

        # XXX what is/was this used for??? --------
        # probably should implement
        # using leap.common.events
        #self.listeners = []
        #self.addListener = self.listeners.append
        #self.removeListener = self.listeners.remove
        #------------------------------------------

    def getFlags(self):
        """
        Returns the possible flags of this mailbox
        @rtype: tuple
        """
        if self._index:
            return self._index._flags.get(self.mbox, None)
        else:
            logger.debug('mailbox without access to the index')
            return self.flags or self.INIT_FLAGS

    def setFlags(self, flags):
        """
        Sets flags for this mailbox
        @param flags: a tuple with the flags
        """
        leap_assert(isinstance(flags, tuple),
                    "flags expected to be a tuple")
        #import ipdb; ipdb.set_trace()

        if self._index:
            self._index._flags[self.mbox] = flags
            self._index._update_index_doc()
        else:
            #logger.debug('mailbox without access to the index')
            print 'NO INDEX'
            self.flags = flags

    # XXX FIXME --------------- IMPLEMENT THIS ------------

    def getUIDValidity(self):
        return 42

    def getUIDNext(self):
        return self.messages.count() + 1

    def getMessageCount(self):
        return self.messages.count()

    def getUnseenCount(self):
        return len(self.messages.get_unseen())

    def getRecentCount(self):
        # XXX
        return 3

    # XXX ----------------------  ^^^ -----------------------

    def isWriteable(self):
        """
        Returns True if this mailbox is writable
        @rtype: int
        """
        return self.rw

    def getHierarchicalDelimiter(self):
        return '/'

    def requestStatus(self, names):
        r = {}
        if 'MESSAGES' in names:
            r['MESSAGES'] = self.getMessageCount()
        if 'RECENT' in names:
            r['RECENT'] = self.getRecentCount()
        if 'UIDNEXT' in names:
            r['UIDNEXT'] = self.getMessageCount() + 1
        if 'UIDVALIDITY' in names:
            r['UIDVALIDITY'] = self.getUID()
        if 'UNSEEN' in names:
            r['UNSEEN'] = self.getUnseenCount()
        return defer.succeed(r)

    def addMessage(self, message, flags, date=None):
        # self.messages.add_msg((msg, flags, date, self.mUID))
        #self.messages.append((message, flags, date, self.mUID))
        self.messages.add_msg(subject=message, flags=flags, date=date)
        return defer.succeed(None)

    def destroy(self):
        # XXX should remove also the mailbox from index
        self.deleteAllDocs()

    def deleteAllDocs(self):
        """
        Deletes all docs in this mailbox
        """
        docs = self.messages.get_all()
        for doc in docs:
            self.messages.db.delete_doc(doc)

    def expunge(self):
        """deletes all messages flagged \\Deleted"""
        # XXX FIXME!
        delete = []
        for i in self.messages:
            if '\\Deleted' in i[1]:
                delete.append(i)
        for i in delete:
            self.messages.remove(i)
        return [i[3] for i in delete]

    def close(self):
        self.closed = True
