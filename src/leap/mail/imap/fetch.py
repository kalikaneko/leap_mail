import hmac

from leap.common.check import leap_assert
from leap.soledad import Soledad
from leap.soledad.backends.couch import CouchDatabase


class LeapIncomingMail(object):
    """
    Fetches mail from the incoming queue in CouchDB database
    """

    def __init__(self, user, soledad_pass, couch_url, imap_account,
                 **kwargs):
        """
        Initialize LeapIMAP.

        @param user: The user adress in the form C{user@provider}.
        @type user: str

        @param soledad_pass: The password for the local database replica.
        @type soledad_pass: str

        @param couch_url: The URL of the CouchDB where email data will be
            saved.
        @type couch_url: str

        @param soledad_imap_account: a SoledadBackedAccount instance to which
            the incoming mail will be saved to

        @param **kwargs: Used to pass arguments to Soledad instance. Maybe
            Soledad instantiation could be factored out from here, and maybe
            we should have a standard for all client code.
        """
        leap_assert(user, "need an user to initialize")

        self._user = user
        self._couch_url = couch_url
        print kwargs['local_db_path']
        self._soledad = Soledad(
            user, soledad_pass,
            gnupg_home=kwargs['gnupg_home'],
            local_db_path=kwargs['local_db_path'],
            secret_path=kwargs['secret_path'])
        self.imapAccount = imap_account

    def _get_couchdb_for_user(self):
        """
        Returns the appropriate URI for a given user
        """
        db_url = self._couch_url + '/user-%s' % self._get_user_id()
        return CouchDatabase.open_database(db_url, create=True)

    def _get_user_id(self):
        """
        Returns uuid for a given user
        """
        # TODO: implement this method properly when webapi is available.
        query = 'users/_design/User/_view/by_email_or_alias/'
        query += '?key="%s"&reduce=false' % self._user
        #response = json.loads(_get('https://webapi/uid/%s/' % query))
        #uid = response['rows'][0]['id']
        uid = hmac.new('uuid', self._user).hexdigest()  # remove this!
        return uid

    def fetch(self):
        """
        Get new mail from CouchDB database, store it in the INBOX for the user
        account, and remove from the remote db.
        """
        db = self._get_couchdb_for_user(self._couch_url, self._user)
        gen, doclist = db.get_all_docs()

        if doclist:
            inbox = self.imapAccount.getMailbox('inbox')

        for doc in doclist:
            inbox.addMessage(doc.content, ("\\Recent",))
            db.delete_doc(doc)

        # XXX here we could make soledad sync with its remote db.
