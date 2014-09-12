Hacking
========
Some hints oriented to `leap.mail` hackers.

Don't panic! Just manhole into it
---------------------------------

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
---------

Use the ``LEAP_PROFILE_IMAPCMD`` to get profiling of certain IMAP commands::

 LEAP_PROFILE_IMAPCMD=1 bitmask --debug

Offline mode
------------

The client has an ``--offline`` flag that will make the Mail services (imap,
currently) not try to sync with remote replicas. Very useful during development,
although you need to login with the remote server at least once before being
able to use it.
