# -*- coding: utf-8 -*-
# account.py
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
Soledad Backed Account.
"""
import copy
import logging
import os
import time

from twisted.internet import defer
from twisted.mail import imap4
from twisted.python import log
from zope.interface import implements

from leap.common.check import leap_assert, leap_assert_type
from leap.mail.imap.index import IndexedDB
from leap.mail.imap.fields import WithMsgFields
from leap.mail.imap.parser import MBoxParser
from leap.mail.imap.mailbox import SoledadMailbox
from leap.soledad.client import Soledad

logger = logging.getLogger(__name__)

PROFILE_CMD = os.environ.get('LEAP_PROFILE_IMAPCMD', False)

if PROFILE_CMD:

    def _debugProfiling(result, cmdname, start):
        took = (time.time() - start) * 1000
        log.msg("CMD " + cmdname + " TOOK: " + str(took) + " msec")
        return result


#######################################
# Soledad Account
#######################################


# TODO change name to LeapIMAPAccount, since we're using
# the memstore.
# IndexedDB should also not be here anymore.

class SoledadBackedAccount(WithMsgFields, IndexedDB, MBoxParser):
    """
    An implementation of IAccount and INamespacePresenteer
    that is backed by Soledad Encrypted Documents.
    """

    implements(imap4.IAccount, imap4.INamespacePresenter)

    _soledad = None
    selected = None
    closed = False
    _initialized = False

    def __init__(self, account_name, soledad, memstore=None):
        """
        Creates a SoledadAccountIndex that keeps track of the mailboxes
        and subscriptions handled by this account.

        :param acct_name: The name of the account (user id).
        :type acct_name: str

        :param soledad: a Soledad instance.
        :type soledad: Soledad
        :param memstore: a MemoryStore instance.
        :type memstore: MemoryStore
        """
        leap_assert(soledad, "Need a soledad instance to initialize")
        leap_assert_type(soledad, Soledad)

        # XXX SHOULD assert too that the name matches the user/uuid with which
        # soledad has been initialized.

        # XXX ??? why is this parsing mailbox name??? it's account...
        # userid? homogenize.
        self._account_name = self._parse_mailbox_name(account_name)
        self._soledad = soledad
        self._memstore = memstore

        self.__mailboxes = set([])

        self._deferred_initialization = defer.Deferred()
        self._initialize_storage()

    def _initialize_storage(self):

        def add_mailbox_if_none(result):
            # every user should have the right to an inbox folder
            # at least, so let's make one!
            if not self.mailboxes:
                self.addMailbox(self.INBOX_NAME)

        def finish_initialization(result):
            self._initialized = True
            self._deferred_initialization.callback(None)

        d = self.initialize_db()

        d.addCallback(lambda r: self._load_mailboxes())
        d.addCallback(add_mailbox_if_none)
        d.addCallback(finish_initialization)

    def callWhenReady(self, cb):
        if self._initialized:
            cb(self)
            return defer.succeed(None)
        else:
            self._deferred_initialization.addCallback(cb)
            return self._deferred_initialization

    def _get_empty_mailbox(self):
        """
        Returns an empty mailbox.

        :rtype: dict
        """
        return copy.deepcopy(self.EMPTY_MBOX)

    def _get_mailbox_by_name(self, name):
        """
        Return an mbox document by name.

        :param name: the name of the mailbox
        :type name: str

        :rtype: SoledadDocument
        """
        # XXX use soledadstore instead ...;
        def get_first_if_any(docs):
            return docs[0] if docs else None

        d = self._soledad.get_from_index(
            self.TYPE_MBOX_IDX, self.MBOX_KEY,
            self._parse_mailbox_name(name))
        d.addCallback(get_first_if_any)
        return d

    @property
    def mailboxes(self):
        """
        A list of the current mailboxes for this account.
        :rtype: set
        """
        return sorted(self.__mailboxes)

    def _load_mailboxes(self):
        def update_mailboxes(db_indexes):
            self.__mailboxes.update(
                [doc.content[self.MBOX_KEY] for doc in db_indexes])
        d = self._soledad.get_from_index(self.TYPE_IDX, self.MBOX_KEY)
        d.addCallback(update_mailboxes)
        return d

    def getMailbox(self, name):
        """
        Return a Mailbox with that name, without selecting it.

        :param name: name of the mailbox
        :type name: str

        :returns: a a SoledadMailbox instance
        :rtype: SoledadMailbox
        """
        name = self._parse_mailbox_name(name)

        if name not in self.mailboxes:
            raise imap4.MailboxException("No such mailbox: %r" % name)

        return SoledadMailbox(name, self._soledad,
                              memstore=self._memstore)

    #
    # IAccount
    #

    def addMailbox(self, name, creation_ts=None):
        """
        Add a mailbox to the account.

        :param name: the name of the mailbox
        :type name: str

        :param creation_ts: an optional creation timestamp to be used as
                            mailbox id. A timestamp will be used if no
                            one is provided.
        :type creation_ts: int

        :returns: a Deferred that will contain the document if successful.
        :rtype: bool
        """
        name = self._parse_mailbox_name(name)

        leap_assert(name, "Need a mailbox name to create a mailbox")

        if name in self.mailboxes:
            raise imap4.MailboxCollision(repr(name))

        if creation_ts is None:
            # by default, we pass an int value
            # taken from the current time
            # we make sure to take enough decimals to get a unique
            # mailbox-uidvalidity.
            creation_ts = int(time.time() * 10E2)

        mbox = self._get_empty_mailbox()
        mbox[self.MBOX_KEY] = name
        mbox[self.CREATED_KEY] = creation_ts

        d = self._soledad.create_doc(mbox)
        d.addCallback(lambda r: self._load_mailboxes())
        return d

    def create(self, pathspec):
        """
        Create a new mailbox from the given hierarchical name.

        :param pathspec: The full hierarchical name of a new mailbox to create.
                         If any of the inferior hierarchical names to this one
                         do not exist, they are created as well.
        :type pathspec: str

        :return: A true value if the creation succeeds.
        :rtype: bool

        :raise MailboxException: Raised if this mailbox cannot be added.
        """
        # TODO raise MailboxException
        paths = filter(
            None,
            self._parse_mailbox_name(pathspec).split('/'))
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
        self._load_mailboxes()
        return True

    def select(self, name, readwrite=1):
        """
        Selects a mailbox.

        :param name: the mailbox to select
        :type name: str

        :param readwrite: 1 for readwrite permissions.
        :type readwrite: int

        :rtype: SoledadMailbox
        """
        if PROFILE_CMD:
            start = time.time()

        name = self._parse_mailbox_name(name)
        if name not in self.mailboxes:
            logger.warning("No such mailbox!")
            return None
        self.selected = name

        sm = SoledadMailbox(
            name, self._soledad, self._memstore, readwrite)
        if PROFILE_CMD:
            _debugProfiling(None, "SELECT", start)
        return sm

    def delete(self, name, force=False):
        """
        Deletes a mailbox.

        Right now it does not purge the messages, but just removes the mailbox
        name from the mailboxes list!!!

        :param name: the mailbox to be deleted
        :type name: str

        :param force: if True, it will not check for noselect flag or inferior
                      names. use with care.
        :type force: bool
        """
        name = self._parse_mailbox_name(name)

        if name not in self.mailboxes:
            raise imap4.MailboxException("No such mailbox: %r" % name)
        mbox = self.getMailbox(name)

        if force is False:
            # See if this box is flagged \Noselect
            # XXX use mbox.flags instead?
            mbox_flags = mbox.getFlags()
            if self.NOSELECT_FLAG in mbox_flags:
                # Check for hierarchically inferior mailboxes with this one
                # as part of their root.
                for others in self.mailboxes:
                    if others != name and others.startswith(name):
                        raise imap4.MailboxException, (
                            "Hierarchically inferior mailboxes "
                            "exist and \\Noselect is set")
        self.__mailboxes.discard(name)
        mbox.destroy()

        # XXX FIXME --- not honoring the inferior names...

        # if there are no hierarchically inferior names, we will
        # delete it from our ken.
        # if self._inferiorNames(name) > 1:
        #  ??! -- can this be rite?
        # self._index.removeMailbox(name)

    def rename(self, oldname, newname):
        """
        Renames a mailbox.

        :param oldname: old name of the mailbox
        :type oldname: str

        :param newname: new name of the mailbox
        :type newname: str
        """
        oldname = self._parse_mailbox_name(oldname)
        newname = self._parse_mailbox_name(newname)

        if oldname not in self.mailboxes:
            raise imap4.NoSuchMailbox(repr(oldname))

        inferiors = self._inferiorNames(oldname)
        inferiors = [(o, o.replace(oldname, newname, 1)) for o in inferiors]

        for (old, new) in inferiors:
            if new in self.mailboxes:
                raise imap4.MailboxCollision(repr(new))

        def update_mbox_doc_name(old, mbox):
            mbox.content[self.MBOX_KEY] = new
            self.__mailboxes.discard(old)
            self._soledad.put_doc(mbox)

        for (old, new) in inferiors:
            self._memstore.rename_fdocs_mailbox(old, new)
            d = self._get_mailbox_by_name(old)
            d.addCallback(lambda mbox_name: update_mbox_doc_name(
                old, mbox_name))

        self._load_mailboxes()

    def _inferiorNames(self, name):
        """
        Return hierarchically inferior mailboxes.

        :param name: name of the mailbox
        :rtype: list
        """
        # XXX use wildcard query instead
        inferiors = []
        for infname in self.mailboxes:
            if infname.startswith(name):
                inferiors.append(infname)
        return inferiors

    # TODO ------------------ can we preserve the attr?
    # maybe add to memory store.
    def isSubscribed(self, name):
        """
        Returns True if user is subscribed to this mailbox.

        :param name: the mailbox to be checked.
        :type name: str

        :rtype: Deferred (will fire with bool)
        """
        subscribed = self.SUBSCRIBED_KEY

        def is_subscribed(mbox):
            subs_bool = bool(mbox.content.get(subscribed, False))
            return subs_bool

        d = self._get_mailbox_by_name(name)
        d.addCallback(is_subscribed)
        return d

    # TODO ------------------ can we preserve the property?
    # maybe add to memory store.

    def _get_subscriptions(self):
        """
        Return a list of the current subscriptions for this account.

        :returns: A deferred that will fire with the subscriptions.
        :rtype: Deferred
        """
        def get_docs_content(docs):
            return [doc.content[self.MBOX_KEY] for doc in docs]

        d = self._soledad.get_from_index(
            self.TYPE_SUBS_IDX, self.MBOX_KEY, '1')
        d.addCallback(get_docs_content)
        return d

    def _set_subscription(self, name, value):
        """
        Sets the subscription value for a given mailbox

        :param name: the mailbox
        :type name: str

        :param value: the boolean value
        :type value: bool
        """
        # XXX Note that this kind of operation has
        # no guarantees of atomicity. We should not be accessing mbox
        # documents concurrently.

        subscribed = self.SUBSCRIBED_KEY

        def update_subscribed_value(mbox):
            mbox.content[subscribed] = value
            return self._soledad.put_doc(mbox)

        # maybe we should store subscriptions in another
        # document...
        if name not in self.mailboxes:
            d = self.addMailbox(name)
            d.addCallback(lambda v: self._get_mailbox_by_name(name))
        else:
            d = self._get_mailbox_by_name(name)
        d.addCallback(update_subscribed_value)
        return d

    def subscribe(self, name):
        """
        Subscribe to this mailbox if not already subscribed.

        :param name: name of the mailbox
        :type name: str
        """
        name = self._parse_mailbox_name(name)

        def check_and_subscribe(subscriptions):
            if name not in subscriptions:
                return self._set_subscription(name, True)
        d = self._get_subscriptions()
        d.addCallback(check_and_subscribe)
        return d

    def unsubscribe(self, name):
        """
        Unsubscribe from this mailbox

        :param name: name of the mailbox
        :type name: str
        """
        name = self._parse_mailbox_name(name)

        def check_and_unsubscribe(subscriptions):
            if name not in self.subscriptions:
                raise imap4.MailboxException(
                    "Not currently subscribed to %r" % name)
            self._set_subscription(name, False)
        d = self._get_subscriptions()
        d.addCallback(check_and_unsubscribe)
        return d

    def getSubscriptions(self):
        return self._get_subscriptions()

    def listMailboxes(self, ref, wildcard):
        """
        List the mailboxes.

        from rfc 3501:
        returns a subset of names from the complete set
        of all names available to the client.  Zero or more untagged LIST
        replies are returned, containing the name attributes, hierarchy
        delimiter, and name.

        :param ref: reference name
        :type ref: str

        :param wildcard: mailbox name with possible wildcards
        :type wildcard: str
        """
        # XXX use wildcard in index query
        ref = self._inferiorNames(
            self._parse_mailbox_name(ref))
        wildcard = imap4.wildcardToRegexp(wildcard, '/')
        return [(i, self.getMailbox(i)) for i in ref if wildcard.match(i)]

    #
    # INamespacePresenter
    #

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

        :param iknowhatiamdoing: confirmation parameter, needs to be True
                                 to proceed.
        """
        if iknowhatiamdoing is True:
            for mbox in self.mailboxes:
                self.delete(mbox, force=True)

    def __repr__(self):
        """
        Representation string for this object.
        """
        return "<SoledadBackedAccount (%s)>" % self._account_name
