import datetime
import os
from functools import partial

from twisted.application import internet, service
from twisted.internet.protocol import ServerFactory
from twisted.mail import imap4
from twisted.python import log

from leap.common.check import leap_assert
from leap.mail.imap.server import SoledadBackedAccount
from leap.soledad import Soledad
from leap.soledad import SoledadCrypto

# Some constants
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# The port in which imap service will run
IMAP_PORT = 9930

# The period between succesive checks of the incoming mail
# queue (in seconds)
INCOMING_CHECK_PERIOD = 10
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~


class LeapIMAPServer(imap4.IMAP4Server):
    """
    An IMAP4 Server with mailboxes backed by soledad
    """
    def __init__(self, *args, **kwargs):
        # pop extraneous arguments
        soledad = kwargs.pop('soledad', None)
        user = kwargs.pop('user', None)
        gpg = kwargs.pop('gpg', None)
        leap_assert(soledad, "need a soledad instance")
        leap_assert(user, "need a user in the initialization")

        # initialize imap server!
        imap4.IMAP4Server.__init__(self, *args, **kwargs)

	# we should initialize the account here,
	# but we move it to the factory so we can
	# populate the test account properly (and only once
	# per session)

        # theAccount = SoledadBackedAccount(
        #     user, soledad=soledad)

        # ---------------------------------
        # XXX pre-populate acct for tests!!
        # populate_test_account(theAccount)
        # ---------------------------------
        #self.theAccount = theAccount

    def lineReceived(self, line):
        log.msg('rcv: %s' % line)
        imap4.IMAP4Server.lineReceived(self, line)

    def authenticateLogin(self, username, password):
        # all is allowed so far. use realm instead
        return imap4.IAccount, self.theAccount, lambda: None


class IMAPAuthRealm(object):
    """
    dummy authentication realm
    """
    theAccount = None

    def requestAvatar(self, avatarId, mind, *interfaces):
        return imap4.IAccount, self.theAccount, lambda: None


class LeapIMAPFactory(ServerFactory):
    """
    Factory for a IMAP4 server with soledad remote sync and gpg-decryption
    capabilities.
    """

    def __init__(self, user, soledad, gpg=None):
        self._user = user
        self._soledad = soledad
        self._gpg = gpg

        theAccount = SoledadBackedAccount(
            user, soledad=soledad)

        # ---------------------------------
        # XXX pre-populate acct for tests!!
        populate_test_account(theAccount)
        # ---------------------------------
        self.theAccount = theAccount

    def buildProtocol(self, addr):
        "Return a protocol suitable for the job."
        imapProtocol = LeapIMAPServer(
            user=self._user,
            soledad=self._soledad,
            gpg=self._gpg)
        imapProtocol.theAccount = self.theAccount
        imapProtocol.factory = self
        return imapProtocol

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#
# Let's rock...
#
# XXX initialize gpg

#from leap.mail.imap.tests import PUBLIC_KEY
#from leap.mail.imap.tests import PRIVATE_KEY
#from leap.soledad.util import GPGWrapper


def initialize_soledad(uuid, passphrase, tempdir):
    """
    Initializes soledad by hand

    @param uuid: uuid for the user
    @param passphrase: ...
    @param tempdir: path to temporal dir
    @rtype: Soledad instance
    """
    uuid = "foobar-uuid"
    passphrase = "verysecretpassphrase"
    secret_path = os.path.join(tempdir, "secret.gpg")
    local_db_path = os.path.join(tempdir, "soledad.u1db")
    server_url = "http://provider"
    cert_file = ""

    _soledad = Soledad(
        uuid,  # user's uuid, obtained through signal events
        passphrase,  # how to get this?
        secret_path,  # how to get this?
        local_db_path,  # how to get this?
        server_url,  # can be None for now
        cert_file,
        bootstrap=False)
    _soledad._init_dirs()
    _soledad._crypto = SoledadCrypto(_soledad)
    _soledad._shared_db = None
    _soledad._init_keys()
    _soledad._init_db()

    return _soledad


mail_sample = open('rfc822.message').read()


def populate_test_account(acct):
    """
    Populates inbox for testing purposes
    """
    print "populating test account!"
    inbox = acct.getMailbox('inbox')
    inbox.addMessage(mail_sample, ("\\Foo", "\\Recent",), date="Right now2")


def incoming_check(acct):
    """
    Check incoming queue. To be called periodically.
    """
    # FIXME -------------------------------------
    # XXX should instantiate LeapIncomingMail
    # properly, and just call its `fetch` method...
    # --------------------------------------------

    log.msg("checking incoming queue...")

    inbox = acct.getMailbox('inbox')
    ts = datetime.datetime.ctime(datetime.datetime.utcnow())
    inbox.addMessage("test!", ("\\Foo", "\\Recent", ), date=ts)


userID = 'user@leap.se'  # TODO: get real USER from configs...

# This initialization form is failing:
#soledad = Soledad(userID)

soledad = initialize_soledad(userID, '/tmp', '/tmp')
gpg = None

factory = LeapIMAPFactory(userID, soledad, gpg)

application = service.Application("LEAP IMAP4 Local Service")
imapService = internet.TCPServer(IMAP_PORT, factory)
imapService.setServiceParent(application)

incoming_check_for_acct = partial(incoming_check, factory.theAccount)
internet.TimerService(INCOMING_CHECK_PERIOD, incoming_check_for_acct).setServiceParent(application)
