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
import StringIO
import cStringIO
import time

from zope.interface import implements

from twisted.mail import imap4
from twisted.internet import defer

#from twisted import cred

#import u1db

from leap.common.check import leap_assert, leap_assert_type
from leap.soledad.backends.sqlcipher import SQLCipherDatabase

logger = logging.getLogger(__name__)


###################################
# SoledadAccount Index
###################################

class MissingIndexError(Exception):
    """raises when tried to access a non existent index document"""


class BadIndexError(Exception):
    """raises when index is malformed or has the wrong cardinality"""


class IndexedDB(object):
    """
    Methods dealing with the index
    """

    def initialize_db(self):
        """
        Initialize the database.
        """
        # Ask the database for currently existing indexes.
        db_indexes = dict(self._db.list_indexes())
        for name, expression in self.INDEXES.items():
            if name not in db_indexes:
                # The index does not yet exist.
                self._db.create_index(name, *expression)
                continue

            if expression == db_indexes[name]:
                # The index exists and is up to date.
                continue
            # The index exists but the definition is not what expected, so we
            # delete it and add the proper index expression.
            self._db.delete_index(name)
            self._db.create_index(name, *expression)


'''
EMPTY_INDEXDOC = {
    "is_index": True,
    "mailboxes": [],
    "subscriptions": [],
    "flags": {},
    "status": {}}
get_empty_indexdoc = lambda: copy.deepcopy(EMPTY_INDEXDOC)


class SoledadAccountIndex(IndexedDB):
    """
    Index for the Soledad Account
    keeps track of mailboxes and subscriptions
    """
    _index = None
    MBOX_CREATION_KEY = "created"

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
            print "No SoledadAccountIndex. Creating one."
            self._create_index_doc()

    # setters and getters for the index document

    # mailboxes

    def _get_mailboxes(self):
        """Get mailboxes associated with this account."""
        return self._index.content.setdefault('mailboxes', [])

    def _set_mailboxes(self, mailboxes):
        """Set mailboxes associated with this account."""
        self._index.content['mailboxes'] = list(set(mailboxes))
        self._update_index_doc()

    mailboxes = property(
        _get_mailboxes, _set_mailboxes, doc="Account mailboxes.")

    def addMailbox(self, name, creation_ts=None):
        """
        Add a mailbox to the mailboxes list.

        @param name: the mailbox name to add
        @type name: C{str}

        @param creation_ts: an optional timestamp integer that will be
            used as a permanent identificator for the mailbox. By default
            a value based on the system time will be used.
        @type creation_ts: C{int}
        """
        name = name.upper()
        if not creation_ts:
            # by default, we pass an int value
            # taken from the current time
            creation_ts = int(time.time() * 10E2)

        self.mailboxes.append(name)
        self.setMailboxCreationTimestamp(
            name,
            creation_ts)

        self._update_index_doc()

    def removeMailbox(self, name):
        """Remove a mailbox from the mailboxes list, and reset its
        entry in the mailbox properties dictionary."""
        name = name.upper()
        self.mailboxes.remove(name)
        self._mailbox_dict[name] = {}
        self._update_index_doc()

    # mailbox_dict (all info about mailboxes, should get flags and status
    # in here too)
    # TODO maybe use nested document instead?

    def _get_mailbox_dict(self):
        """Get mailbox dict associated with this account."""
        return self._index.content.setdefault('mailbox_dict', {})

    def _set_mailbox_dict(self, mailbox_dict):
        """Set mailbox_dict associated with this account."""
        self._index.content['mailbox_dict'] = mailbox_dict
        self._update_index_doc()

    _mailbox_dict = property(
        _get_mailbox_dict, _set_mailbox_dict, doc="Mailbox config dictionary.")

    def _getMailboxProperty(self, mbox, key):
        """Get a property from the mailbox dict"""
        mboxd = self._mailbox_dict.setdefault(mbox, {})
        return mboxd.get(key, None)

    def _setMailboxProperty(self, mbox, key, value):
        """Set a property in the mailbox dict"""
        mboxd = self._mailbox_dict.setdefault(mbox, {})
        mboxd[key] = value
        self._mailbox_dict[mbox] = mboxd
        self._update_index_doc()

    def setMailboxCreationTimestamp(self, mbox, value, force=False):
        """Sets the creation timestamp for a mailbox.

        If a value already exists for the `created` key for that mailbox,
        it will only be updated if `force` is True

        @param mbox: the mailbox to update
        @param value: the timestamp for this mailbox
        @type value: C{int}
        @rtype: C{bool}
        @return: True if the operation was successful
        """
        leap_assert_type(value, int)
        created = self._getMailboxProperty(mbox, self.MBOX_CREATION_KEY)
        if not created or force:
            self._setMailboxProperty(mbox, self.MBOX_CREATION_KEY, value)
            return True
        print "fail while setting timestamp"
        return False

    def getMailboxUIDValidity(self, mbox):
        """
        Returns the creation timestamp of the mailbox, which we use
        as a UIDVALIDITY value.
        """
        return self._getMailboxProperty(mbox, self.MBOX_CREATION_KEY)

    # subscriptions

    def _get_subscriptions(self):
        """Get subscriptions associated with this account."""
        return self._index.content.setdefault('subscriptions', [])

    def _set_subscriptions(self, subscriptions):
        """Set subscriptions associated with this account."""
        self._index.content['subscriptions'] = list(set(subscriptions))
        self._update_index_doc()

    subscriptions = property(
        _get_subscriptions, _set_subscriptions, doc="Account subscriptions.")

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

    # ------------------------------------------------------
    # flags and status are separate fields now,
    # but should move to mailbox dict

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

    #
    # status
    #

    # XXX move to mailbox dict ...

    def _get_mailbox_status(self):
        """Get status from index for the account."""
        return self._index.content.setdefault('status', {})

    def _set_mailbox_status(self, value):
        """Set status dict in the index for the account."""
        self._index.content['status'] = value
        self._update_index_doc()

    _mailbox_status = property(
        _get_mailbox_status, _set_mailbox_status, doc="Mailbox status.")

'''

#######################################
# Soledad Account
#######################################


class SoledadBackedAccount(IndexedDB):
    """
    An implementation of IAccount and INamespacePresenteer
    that is backed by Soledad Encrypted Documents.
    """

    implements(imap4.IAccount, imap4.INamespacePresenter)

    _soledad = None
    _db = None
    selected = None

    TYPE_IDX = 'by-type'
    TYPE_MBOX_IDX = 'by-type-and-mbox'
    TYPE_MBOX_UID_IDX = 'by-type-and-mbox-and-uid'
    TYPE_SUBS_IDX = 'by-type-and-subscribed'
    TYPE_MBOX_SEEN_IDX = 'by-type-and-mbox-and-seen'
    TYPE_MBOX_RECT_IDX = 'by-type-and-mbox-and-recent'

    INDEXES = {
        # generic
        TYPE_IDX: ['type'],
        TYPE_MBOX_IDX: ['type', 'mbox'],
        TYPE_MBOX_UID_IDX: ['type', 'mbox', 'uid'],
        # mailboxes
        TYPE_SUBS_IDX: ['type', 'bool(subscribed)'],
        # messages
        TYPE_MBOX_SEEN_IDX: ['type', 'mbox', 'bool(seen)'],
        TYPE_MBOX_RECT_IDX: ['type', 'mbox', 'bool(recent)'],
    }

    EMPTY_MBOX = {
        "type": "mbox",
        "mbox": "INBOX",
        "subject": "",
        "flags": [],
        "closed": False,
        "subscribed": False,
        "rw": 1,
    }

    def __init__(self, name, soledad=None):
        """
        SoledadBackedAccount constructor
        creates a SoledadAccountIndex that keeps track of the
        mailboxes and subscriptions handled by this account.

        @param name: the name of the account (user id)
        @type name: C{str}

        @param soledad: a Soledad instance
        @param soledad: C{Soledad}
        """
        leap_assert(soledad, "Need a soledad instance to initialize")
        # XXX check isinstance ...
        # XXX SHOULD assert too that the name matches the user with which
        # soledad has been intialized.

        self.name = name.upper()
        self._soledad = soledad

        self._db = soledad._db
        self.initialize_db()

        # every user should see an inbox folder
        # at least

        #if not self.mailboxes:
            #self.addMailbox('inbox')

    def _get_empty_mailbox(self):
        """
        Returns an empty mailbox.

        @rtype: dict
        """
        return copy.deepcopy(self.EMPTY_MBOX)

    def _get_mailbox_by_name(self, name):
        """
        Returns an mbox by name.

        @rtype: C{LeapDocument}
        """
        name = name.upper()
        doc = self._db.get_from_index(self.TYPE_MBOX_IDX, 'mbox', name)
        return doc[0] if doc else None

    @property
    def mailboxes(self):
        """
        A list of the current mailboxes for this account.
        """
        return [str(doc.content['mbox'])
                for doc in self._db.get_from_index(self.TYPE_IDX, 'mbox')]

    @property
    def subscriptions(self):
        """
        A list of the current subscriptions for this account.
        """
        return [str(doc.content['mbox'])
                for doc in self._db.get_from_index(
                    self.TYPE_SUBS_IDX, 'mbox', '1')]

    def getMailbox(self, name):
        """
        Returns Mailbox with that name, without selecting it.

        @param name: name of the mailbox
        @type name: C{str}

        @returns: a a SoledadMailbox instance
        """
        name = name.upper()
        if name not in self.mailboxes:
            raise imap4.MailboxException("No such mailbox")

        return SoledadMailbox(name, soledad=self._soledad)

    ##
    ## IAccount
    ##

    def addMailbox(self, name, creation_ts=None):
        """
        Adds a mailbox to the account.

        @param name: the name of the mailbox
        @type name: str

        @param creation_ts: a optional creation timestamp to be used as
            mailbox id. A timestamp will be used if no one is provided.
        @type creation_ts: C{int}

        @returns: True if successful
        @rtype: bool
        """
        name = name.upper()
        # XXX should check mailbox name for RFC-compliant form

        if name in self.mailboxes:
            raise imap4.MailboxCollision, name

        if not creation_ts:
            # by default, we pass an int value
            # taken from the current time
            creation_ts = int(time.time() * 10E2)

        mbox = self._get_empty_mailbox()
        mbox['mbox'] = name
        mbox['created'] = creation_ts

        doc = self._db.create_doc(mbox)
        return bool(doc)

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
        name = name.upper()

        if name not in self.mailboxes:
            return None

        self.selected = str(name)

        return SoledadMailbox(
            name, rw=readwrite,
            soledad=self._soledad)

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

        mbox = self.getMailbox(name)

        if force is False:
            # See if this box is flagged \Noselect
            # XXX use mbox.flags instead?
            if r'\Noselect' in mbox.getFlags():
                # Check for hierarchically inferior mailboxes with this one
                # as part of their root.
                for others in self.mailboxes:
                    if others != name and others.startswith(name):
                        raise imap4.MailboxException, (
                            "Hierarchically inferior mailboxes "
                            "exist and \\Noselect is set")
        mbox.destroy()

        # XXX FIXME --- not honoring the inferior names...

        # if there are no hierarchically inferior names, we will
        # delete it from our ken.
        #if self._inferiorNames(name) > 1:
            # ??! -- can this be rite?
            #self._index.removeMailbox(name)

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
            mbox = self._get_mailbox_by_name(old)
            mbox.content['mbox'] = new
            self._db.put_doc(mbox)

        # XXX ---- FIXME!!!! ------------------------------------
        # until here we just renamed the index...
        # We have to rename also the occurrence of this
        # mailbox on ALL the messages that are contained in it!!!
        # ... we maybe could use a reference to the doc_id
        # in each msg, instead of the "mbox" field in msgs
        # -------------------------------------------------------

    def _inferiorNames(self, name):
        """
        Return hierarchically inferior mailboxes
        @param name: the mailbox
        @rtype: list
        """
        # XXX use wildcard query instead
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
        mbox = self._get_mailbox_by_name(name)
        return mbox.content.get('subscribed', False)

    def _set_subscription(self, name, value):
        """
        Sets the subscription value for a given mailbox

        @param name: the mailbox
        @type name: C{str}

        @param value: the boolean value
        @type value: C{bool}
        """
        # maybe we should store subscriptions in another
        # document...
        if not name in self.mailboxes:
            print "not this mbox"
            self.addMailbox(name)
        mbox = self._get_mailbox_by_name(name)

        if mbox:
            mbox.content['subscribed'] = value
            self._db.put_doc(mbox)

    def subscribe(self, name):
        """
        Subscribe to this mailbox

        @param name: the mailbox
        @type name: C{str}
        """
        name = name.upper()
        if name not in self.subscriptions:
            self._set_subscription(name, True)

    def unsubscribe(self, name):
        """
        Unsubscribe from this mailbox

        @param name: the mailbox
        @type name: C{str}
        """
        name = name.upper()
        if name not in self.subscriptions:
            raise imap4.MailboxException, "Not currently subscribed to " + name
        self._set_subscription(name, False)

    def listMailboxes(self, ref, wildcard):
        """
        List the mailboxes.

        from rfc 3501:
        returns a subset of names from the complete set
        of all names available to the client.  Zero or more untagged LIST
        replies are returned, containing the name attributes, hierarchy
        delimiter, and name.

        @param ref: reference name
        @param wildcard: mailbox name with possible wildcards
        """
        # XXX use wildcard in index query
        ref = self._inferiorNames(ref.upper())
        wildcard = imap4.wildcardToRegexp(wildcard, '/')
        return [(i, self.getMailbox(i)) for i in ref if wildcard.match(i)]

    ##
    ## INamespacePresenter
    ##

    def getPersonalNamespaces(self):
        return [["", "/"]]

    def getSharedNamespaces(self):
        return None

    def getOtherNamespaces(self):
        return None

    # extra, for convenience

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


#######################################
# Soledad Message, MessageCollection
# and Mailbox
#######################################


class LeapMessage(object):

    implements(imap4.IMessage, imap4.IMessageFile)

    def __init__(self, doc):
        """
        Initializes a LeapMessage.

        @type doc: C{LeapDocument}
        @param doc: A LeapDocument containing the internal
        representation of the message
        """
        self._doc = doc

    def getUID(self):
        """
        Retrieve the unique identifier associated with this message

        @rtype: C{int}
        """
        return self._doc.content['uid']

    def getFlags(self):
        """
        Retrieve the flags associated with this message

        @rtype: C{iterable}
        @return: The flags, represented as strings
        """
        flags = self._doc.content.get('flags', None)
        if flags:
            flags = map(str, flags)
        return flags

    def getInternalDate(self):
        """
        Retrieve the date internally associated with this message

        @rtype: C{str}
        @retur: An RFC822-formatted date string.
        """
        return str(self._doc.content.get('date', ''))

    #
    # IMessageFile
    #

    """
    Optional message interface for representing messages as files.

    If provided by message objects, this interface will be used instead
    the more complex MIME-based interface.
    """

    def open(self):
        """
        Return an file-like object opened for reading.

        Reading from the returned file will return all the bytes
        of which this message consists.
        """
        fd = cStringIO.StringIO()
        fd.write(str(self._doc.content.get('raw', '')))
        fd.seek(0)
        return fd

    #
    # IMessagePart
    #

    # XXX should implement the rest of IMessagePart interface:
    # (and do not use the open above)

    def getBodyFile(self):
        """
        Retrieve a file object containing only the body of this message.

        @rtype: C{StringIO}
        """
        fd = StringIO.StringIO()
        fd.write(str(self._doc.content.get('raw', '')))
        # SHOULD use a separate BODY FIELD ...
        fd.seek(0)
        return fd

    def getSize(self):
        """
        Return the total size, in octets, of this message

        @rtype: C{int}
        """
        return self.getBodyFile().len

    def getHeaders(negate, *names):
        # XXX implement headers
        # as separate fields in the LeapDoc
        return {}

    # --- no multipart for now

    def isMultipart(self):
        return False

    def getSubPart(part):
        return None


class MessageCollection(object):
    """
    A collection of messages, surprisingly.

    It is tied to a selected mailbox name that is passed to constructor.
    Implements a filter query over the messages contained in a soledad
    database.
    """
    # XXX this should be able to produce a MessageSet methinks

    EMPTY_MSG = {
        "type": "msg",
        "uid": 1,
        "mbox": "inbox",
        "subject": "",
        "date": "",
        "seen": False,
        "recent": True,
        "flags": [],
        "raw": "",
    }

    def __init__(self, mbox=None, db=None):
        """
        Constructor for MessageCollection.

        @param mbox: the name of the mailbox. It is the name
                     with which we filter the query over the
                     messages database
        @type mbox: C{str}

        @param db: SQLCipher database (contained in soledad)
        @type db: SQLCipher instance
        """
        leap_assert(mbox, "Need a mailbox name to initialize")
        leap_assert(mbox.strip() != "", "mbox cannot be blank space")
        leap_assert(isinstance(mbox, (str, unicode)),
                    "mbox needs to be a string")
        leap_assert(db, "Need a db instance to initialize")
        leap_assert(isinstance(db, SQLCipherDatabase),
                    "db must be an instance of SQLCipherDatabase")

        # okay, all in order, keep going...

        self.mbox = mbox.upper()
        self.db = db

    def _get_empty_msg(self):
        """
        Returns an empty message.

        @rtype: dict
        """
        return copy.deepcopy(self.EMPTY_MSG)

    def add_msg(self, raw, subject=None, flags=None, date=None, uid=1):
        """
        Creates a new message document.

        @param raw: the raw message
        @type raw: C{str}

        @param subject: subject of the message.
        @type subject: C{str}

        @param flags: flags
        @type flags: C{list}

        @param date: the received date for the message
        @type date: C{str}

        @param uid: the message uid for this mailbox
        @type uid: C{int}
        """
        if flags is None:
            flags = []
        leap_assert_type(flags, list)

        def stringify(o):
            if isinstance(o, (cStringIO.OutputType, StringIO.StringIO)):
                return o.getvalue()
            else:
                return o

        content = self._get_empty_msg()
        content['mbox'] = self.mbox

        if subject:
            content['subject'] = stringify(subject)
        if flags:
            content['flags'] = map(stringify, flags)
            content['seen'] = "\\Seen" in flags
        # XXX if not subject, extract it from raw...
        # XXX extract other headers to do searches...
        content['raw'] = stringify(raw)
        content['date'] = date
        # ...should get a sanity check here.
        content['uid'] = uid

        return self.db.create_doc(content)

    def remove(self, msg):
        """
        Removes a message.

        @param msg: a u1db doc containing the message
        """
        self.db.delete_doc(msg)

    # getters

    def get_by_uid(self, uid):
        """
        Retrieves a message document by UID
        """
        docs = self.db.get_from_index(
            SoledadBackedAccount.TYPE_MBOX_UID_IDX, 'msg', self.mbox, str(uid))
        return docs[0] if docs else None

    def get_all(self):
        """
        Get all messages for the selected mailbox
        Returns a list of u1db documents.
        If you want acess to the content, use __iter__ instead

        @rtype: list
        """
        # XXX this should return LeapMessage instances
        return self.db.get_from_index(
            SoledadBackedAccount.TYPE_MBOX_IDX, 'msg', self.mbox)

    def unseen_iter(self):
        """
        Get an iterator for the message docs with no `seen` flag

        @rtype: C{iterable}
        """
        return (doc for doc in
                self.db.get_from_index(
                    SoledadBackedAccount.TYPE_MBOX_RECT_IDX,
                    'msg', self.mbox, '1'))

    def get_unseen(self):
        """
        Get all messages with the `Unseen` flag

        @rtype: C{list}
        @returns: a list of LeapMessages
        """
        return [LeapMessage(doc) for doc in self.unseen_iter()]

    def recent_iter(self):
        """
        Get an iterator for the message docs with recent flag.

        @rtype: C{iterable}
        """
        return (doc for doc in
                self.db.get_from_index(
                    SoledadBackedAccount.TYPE_MBOX_RECT_IDX,
                    'msg', self.mbox, '1'))

    def get_recent(self):
        """
        Get all messages with the `Recent` flag.

        @type: C{list}
        @returns: a list of LeapMessages
        """
        return [LeapMessage(doc) for doc in self.recent_iter()]

    def count(self):
        """
        Return the count of messages for this mailbox.

        @rtype: C{int}
        """
        return len(self.get_all())

    def __len__(self):
        """
        Returns the number of messages on this mailbox

        @rtype: C{int}
        """
        return self.count()

    def __iter__(self):
        """
        Returns an iterator over all messages.

        @rtype: C{iterable}
        @returns: iterator of dicts with content for all messages.
        """
        return (m.content for m in self.get_all())

    def __getitem__(self, uid):
        """
        Allows indexing as a list, with msg uid as the index.

        @type key: C{int}
        @param key: an integer index
        """
        # XXX this should return LeapMessage instances
        try:
            # this would work for sequence, but let's
            # try to keep it simple with mailbox uid instead
            #msg_doc = self.get_all()[key]
            msg_doc = self.get_by_uid(uid)
        except IndexError:
            return None
        if msg_doc:
            return msg_doc.content
        else:
            return None

    def __repr__(self):
        return u"<MessageCollection: mbox '%s' (%s)>" % (
            self.mbox, self.count())

    # XXX should implement __eq__ also


class SoledadMailbox(object):
    """
    A Soledad-backed IMAP mailbox.

    Implements the high-level method needed for the Mailbox interfaces.
    The low-level database methods are contained in MessageCollection class,
    which we instantiate and make accessible in the `messages` attribute.
    """
    implements(imap4.IMailboxInfo, imap4.IMailbox, imap4.ICloseableMailbox)

    messages = None
    _closed = False

    INIT_FLAGS = ('\\Seen', '\\Answered', '\\Flagged',
                  '\\Deleted', '\\Draft', '\\Recent', 'List')
    DELETED_FLAG = '\\Deleted'
    flags = None

    def __init__(self, mbox, soledad=None, rw=1):
        """
        SoledadMailbox constructor
        Needs to get passed a name, plus a soledad instance and
        the soledad account index, where it stores the flags for this
        mailbox.

        @param mbox: the mailbox name
        @type mbox: C{str}

        @param soledad: a Soledad instance.
        @type soledad: C{Soledad}

        @param rw: read-and-write flags
        @type rw: C{int}
        """
        leap_assert(mbox, "Need a mailbox name to initialize")
        leap_assert(soledad, "Need a soledad instance to initialize")
        leap_assert(isinstance(soledad._db, SQLCipherDatabase),
                    "soledad._db must be an instance of SQLCipherDatabase")

        self.mbox = mbox
        self.rw = rw

        self._soledad = soledad
        self._db = soledad._db

        self.messages = MessageCollection(
            mbox=mbox, db=soledad._db)

        if not self.getFlags():
            self.setFlags(self.INIT_FLAGS)

        # XXX what is/was this used for? --------
        # ---> mail/imap4.py +1155,
        #      _cbSelectWork makes use of this
        # probably should implement hooks here
        # using leap.common.events
        self.listeners = []
        self.addListener = self.listeners.append
        self.removeListener = self.listeners.remove
        #------------------------------------------

    def _get_mbox(self):
        """Returns mailbox document"""
        return self._db.get_from_index(
            SoledadBackedAccount.TYPE_MBOX_IDX, 'mbox', self.mbox)[0]

    def getFlags(self):
        """
        Returns the possible flags of this mailbox
        @rtype: tuple
        """
        mbox = self._get_mbox()
        flags = mbox.content.get('flags', [])
        return map(str, flags)

    def setFlags(self, flags):
        """
        Sets flags for this mailbox
        @param flags: a tuple with the flags
        """
        leap_assert(isinstance(flags, tuple),
                    "flags expected to be a tuple")
        mbox = self._get_mbox()
        mbox.content['flags'] = map(str, flags)
        self._db.put_doc(mbox)

    # XXX SHOULD BETTER IMPLEMENT ADD_FLAG, REMOVE_FLAG.

    def _get_closed(self):
        mbox = self._get_mbox()
        return mbox.content.get('closed', False)

    def _set_closed(self, closed):
        leap_assert(isinstance(closed, bool), "closed needs to be boolean")
        mbox = self._get_mbox()
        mbox.content['closed'] = closed
        self._db.put_doc(mbox)

    closed = property(
        _get_closed, _set_closed, doc="Closed attribute.")

    def getUIDValidity(self):
        """
        Return the unique validity identifier for this mailbox.

        @rtype: C{int}
        """
        mbox = self._get_mbox()
        return mbox.content.get('created', 1)

    # TODO --------------------------------------------------

    def getUID(self, message):
        """
        Return the UID of a message in the mailbox

        @rtype: C{int}
        """
        # XXX what is this used for?
        # XXX what type is message?
        # until now, not needed in the test suite...
        return 0

    # XXX ----------------------  ^^^ -----------------------

    def getRecentCount(self):
        """
        Returns the number of messages with the 'Recent' flag

        @rtype: C{int}
        """
        return len(self.messages.get_recent())

    def getUIDNext(self):
        """
        Return the likely UID for the next message added to this
        mailbox

        @rtype: C{int}
        """
        # XXX reimplement with proper index
        return self.messages.count() + 1

    def getMessageCount(self):
        """
        Returns the total count of messages in this mailbox
        """
        return self.messages.count()

    def getUnseenCount(self):
        """
        Returns the total count of unseen messages in this mailbox
        """
        return len(self.messages.get_unseen())

    def isWriteable(self):
        """
        Get the read/write status of the mailbox
        @rtype: C{int}
        """
        return self.rw

    def getHierarchicalDelimiter(self):
        """
        Returns the character used to delimite hierarchies in mailboxes

        @rtype: C{str}
        """
        return '/'

    def requestStatus(self, names):
        """
        Handles a status request by gathering the output of the different
        status commands

        @param names: a list of strings containing the status commands
        @type names: iter
        """
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
        """
        Adds a message to this mailbox
        @param message: the raw message
        @flags: flag list
        @date: timestamp
        """
        # XXX we should treat the message as an IMessage from here
        uid_next = self.getUIDNext()
        self.messages.add_msg(message, flags=flags, date=date,
                              uid=uid_next)
        return defer.succeed(None)

    # commands, do not rename methods

    def destroy(self):
        """
        Called before this mailbox is permanently deleted.

        Should cleanup resources, and set the \\Noselect flag
        on the mailbox.
        """
        self.setFlags(('\\Noselect',))
        self.deleteAllDocs()

        # XXX removing the mailbox in situ for now,
        # we should postpone the removal
        self._db.delete_doc(self._get_mbox())

    def expunge(self):
        """
        Remove all messages flagged \\Deleted
        """
        # XXX if this is not open for R-W, this
        # should raise ReadOnlyMailbox

        delete = []
        deleted = []
        for m in self.messages.get_all():
            if self.DELETED_FLAG in m.content['flags']:
                delete.append(m)
        for m in delete:
            deleted.append(m.content)
            self.messages.remove(m)

        # XXX should return the UIDs of the deleted messages
        # more generically
        return [x for x in range(len(deleted))]

    def fetch(self, messages, uid):
        """
        Retrieve one or more messages in this mailbox.

        from rfc 3501: The data items to be fetched can be either a single atom
        or a parenthesized list.

        @type messages: C{MessageSet}
        @param messages: IDs of the messages to retrieve information about

        @type uid: C{bool}
        @param uid: If true, the IDs are UIDs. They are message sequence IDs
        otherwise.

        @rtype: A tuple of two-tuples of message sequence numbers and
        C{LeapMessage}
        """
        result = []
        if not uid:
            for last, first in messages.ranges:
                if not last:
                    last = self.messages.count()
                for _id in range(first, last):
                    msg_doc = self.messages.get_by_uid(_id)[0]
                    msg = LeapMessage(msg_doc)
                    result.append((_id, msg))
        else:
            for _, msgid in messages.ranges:
                msg_doc = self.messages.get_by_uid(msgid)[0]
                msg = LeapMessage(msg_doc)
                result.append((msgid, msg))
        return tuple(result)

    def store(messages, flags, mode, uid):
        """
        Sets the flags of one or more messages.
        """
        # XXX implement-me
        print "Not implemented!"
        raise NotImplemented("Store method not implemented")

    def close(self):
        """
        Expunge and mark as closed
        """
        self.expunge()
        self.closed = True

    # convenience fun

    def deleteAllDocs(self):
        """
        Deletes all docs in this mailbox
        """
        docs = self.messages.get_all()
        for doc in docs:
            self.messages.db.delete_doc(doc)

    def __repr__(self):
        return u"<SoledadMailbox: mbox '%s' (%s)>" % (
            self.mbox, self.messages.count())
