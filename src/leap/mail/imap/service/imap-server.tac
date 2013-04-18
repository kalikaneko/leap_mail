import logging

from twisted.application import internet, service
from twisted.internet.protocol import ServerFactory
from twisted.mail import imap4

from leap.common.check import leap_assert
from leap.mail.imap.server import SoledadBackedAccount
from leap.soledad import Soledad


logger = logging.getLogger(__name__)


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
        theAccount = SoledadBackedAccount(
            user, soledad=soledad)
        self.theAccount = theAccount

    def lineReceived(self, line):
        logger.debug('rcv: %s' % line)
        imap4.IMAP4Server.lineReceived(self, line)

    def authenticateLogin(self, username, password):
        # all is allowed so far
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

    def buildProtocol(self, addr):
        "Return a protocol suitable for the job."
        imapProtocol = LeapIMAPServer(
            user=self._user,
            soledad=self._soledad,
            gpg=self._gpg)
        imapProtocol.factory = self
        return imapProtocol

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
#
# Let's rock...
#
# XXX initialize gpg

from leap.mail.imap.tests import PUBLIC_KEY
from leap.mail.imap.tests import PRIVATE_KEY
from leap.soledad.util import GPGWrapper


def initialize_soledad(email, gnupg_home, tempdir):
    """
    Initializes soledad by hand

    @param email: ID for the user
    @param gnupg_home: path to home used by gnupg
    @param tempdir: path to temporal dir
    @rtype: Soledad instance
    """
    _soledad = Soledad(email, gnupg_home=gnupg_home,
                       bootstrap=False,
                       prefix=tempdir)
    _soledad._init_dirs()
    _soledad._gpg = GPGWrapper(gnupghome=gnupg_home,
                               verbose=False)

    if not _soledad._has_privkey():
        _soledad._set_privkey(PRIVATE_KEY)
    if not _soledad._has_symkey():
        _soledad._gen_symkey()
    _soledad._load_symkey()
    _soledad._init_db()

    return _soledad

userID = 'user@leap.se'  # TODO: get real USER from configs...

#soledad = Soledad(userID)
soledad = initialize_soledad(userID, '/tmp', '/tmp')
gpg = None

IMAP_PORT = 9930
factory = LeapIMAPFactory(userID, soledad, gpg)

# this is the important bit
application = service.Application("LEAP IMAP4 Local Service")
imapService = internet.TCPServer(IMAP_PORT, factory)
imapService.setServiceParent(application)
