testing the service
===================

Run the twisted service::

        twistd -n -y imap-server.tac

And use offlineimap for tests::

        offlineimap -c LEAPofflineimapRC-tests

debugging
---------

Use ngrep to obtain logs of the sequences::

        sudo ngrep -d lo -W byline port 9930
