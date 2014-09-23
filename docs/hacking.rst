========
Hacking
========

Some hints oriented to `leap.mail` hackers.

Don't panic! Just manhole into it
=================================

If you want to inspect the objects living in your application memory, in
realtime, you can manhole into it.

First of all, check that the modules ``PyCrypto`` and ``pyasn1`` are installed
into your system, they are needed for it to work.

You just have to pass the ``LEAP_MAIL_MANHOLE=1`` enviroment variable while
launching the client::

  LEAP_MAIL_MANHOLE=1 bitmask --debug

And then you can ssh into your application! (password is "leap")::

  ssh boss@localhost -p 2222

Did I mention how *awesome* twisted is?? ``:)``


Profiling
=========

Use the ``LEAP_PROFILE_IMAPCMD`` to get profiling of certain IMAP commands::

 LEAP_PROFILE_IMAPCMD=1 bitmask --debug

Offline mode
============

The client has an ``--offline`` flag that will make the Mail services (imap,
currently) not try to sync with remote replicas. Very useful during development,
although you need to login with the remote server at least once before being
able to use it.

testing the service with twistd
===============================

In order to run the mail service (currently, the imap server only), you will
need a config with this info::

  [leap_mail]
  userid = "user@provider"
  uuid = "deadbeefdeadabad"
  passwd = "foobar" # Optional

In the ``LEAP_MAIL_CONF`` enviroment variable. If you do not specify a password
parameter, you'll be prompted for it.

In order to get the user uid (uuid), look into the
``~/.config/leap/leap-backend.conf`` file after you have logged in into your
provider at least once.

Run the twisted service::

  LEAP_IMAP_CONFIG=~/.leapmailrc twistd -n -y imap-server.tac

Now you can telnet into your local IMAP server::

  % telnet localhost 1984
  Trying 127.0.0.1...
  Connected to localhost.
  Escape character is '^]'.
  * OK [CAPABILITY IMAP4rev1 LITERAL+ IDLE NAMESPACE] Twisted IMAP4rev1 Ready

Although you probably prefer to use ``offlineimap`` for tests:: 

  offlineimap -c LEAPofflineimapRC-tests


Minimal offlineimap configuration
---------------------------------

You can use this as a sample offlineimap config file::

 [general]
  accounts = leap-local

  [Account leap-local]
  localrepository = LocalLeap
  remoterepository = RemoteLeap

  [Repository LocalLeap]
  type = Maildir
  localfolders = ~/LEAPMail/Mail

  [Repository RemoteLeap]
  type = IMAP
  ssl = no
  remotehost = localhost
  remoteport = 9930
  remoteuser = user
  remotepass = pass

Debugging IMAP commands
=======================

Use ``ngrep`` to obtain logs of the sequences::

  sudo ngrep -d lo -W byline port 1984
