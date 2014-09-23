# -*- coding: utf-8 -*-
# imap-server.tac
# Copyright (C) 2013,2014 LEAP
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
TAC file for initialization of the imap service using twistd.

Use this for debugging and testing the imap server using a native reactor.

For now, and for debugging/testing purposes, you need
to pass a config file with the following structure:

[leap_mail]
userid = "user@provider"
uuid = "deadbeefdeadabad"
passwd = "supersecret" # optional, will get prompted if not found.
"""
import ConfigParser
import getpass
import os
import sys

from leap.keymanager import KeyManager
from leap.mail.imap.service import imap
from leap.soledad.client import Soledad

from twisted.application import service, internet


# TODO should get this initializers from some authoritative mocked source
# We might want to put them the soledad itself.

def initialize_soledad(uuid, email, passwd,
                       secrets, localdb,
                       gnupg_home, tempdir):
    """
    Initializes soledad by hand

    :param email: ID for the user
    :param gnupg_home: path to home used by gnupg
    :param tempdir: path to temporal dir
    :rtype: Soledad instance
    """
    # XXX TODO unify with an authoritative source of mocks
    # for soledad (or partial initializations).
    # This is copied from the imap tests.

    server_url = "http://provider"
    cert_file = ""

    class Mock(object):
        def __init__(self, return_value=None):
            self._return = return_value

        def __call__(self, *args, **kwargs):
            return self._return

    class MockSharedDB(object):

        get_doc = Mock()
        put_doc = Mock()
        lock = Mock(return_value=('atoken', 300))
        unlock = Mock(return_value=True)

        def __call__(self):
            return self

    Soledad._shared_db = MockSharedDB()
    soledad = Soledad(
        uuid,
        passwd,
        secrets,
        localdb,
        server_url,
        cert_file)

    return soledad

######################################################################
# Remember to set your config files, see module documentation above!
######################################################################

print "[+] Running LEAP IMAP Service"


bmconf = os.environ.get("LEAP_MAIL_CONF", "")
if not bmconf:
    print ("[-] Please set LEAP_MAIL_CONF environment variable "
           "pointing to your config.")
    sys.exit(1)

SECTION = "leap_mail"
cp = ConfigParser.ConfigParser()
cp.read(bmconf)

userid = cp.get(SECTION, "userid")
uuid = cp.get(SECTION, "uuid")

try:
    passwd = unicode(cp.get(SECTION, "passwd"))
except Exception:
    passwd = ""

if not passwd:
    passwd = unicode(getpass.getpass("Soledad passphrase: "))

# XXX get this right from the environment variable !!!
port = 1984

if not userid or not uuid:
    print "[-] Config file missing userid or uuid field"
    sys.exit(1)



secrets = os.path.expanduser("~/.config/leap/soledad/%s.secret" % (uuid,))
localdb = os.path.expanduser("~/.config/leap/soledad/%s.db" % (uuid,))

# XXX Is this really used? Should point it to user var dirs defined in xdg?
gnupg_home = "/tmp/"
tempdir = "/tmp/"

###################################################

# Ad-hoc soledad/keymanager initialization.

soledad = initialize_soledad(uuid, userid, passwd, secrets,
                             localdb, gnupg_home, tempdir)
km_args = (userid, "https://localhost", soledad)
km_kwargs = {
    "token": "",
    "ca_cert_path": "",
    "api_uri":  "",
    "api_version": "",
    "uid": uuid,
    "gpgbinary": "/usr/bin/gpg"
}
keymanager = KeyManager(*km_args, **km_kwargs)

##################################################

# Ok, let's expose the application object for the twistd application
# framework to pick up from here...


def getIMAPService():
    factory = imap.LeapIMAPFactory(uuid, userid, soledad)
    return internet.TCPServer(port, factory, interface="localhost")


application = service.Application("LEAP IMAP Application")
service = getIMAPService()
service.setServiceParent(application)
