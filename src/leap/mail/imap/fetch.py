import logging
import json
import ssl

from twisted.python import log
from twisted.internet import defer
from twisted.internet.task import LoopingCall
from twisted.internet.threads import deferToThread

from leap.common.check import leap_assert, leap_assert_type
from leap.keymanager import openpgp
from leap.soledad import Soledad

logger = logging.getLogger(__name__)


class LeapIncomingMail(object):
    """
    Fetches mail from the incoming queue.
    """

    ENC_SCHEME_KEY = "_enc_scheme"
    ENC_JSON_KEY = "_enc_json"

    RECENT_FLAG = "\\Recent"

    INCOMING_KEY = "incoming"
    CONTENT_KEY = "content"

    def __init__(self, keymanager, soledad, imap_account,
                 check_period):

        """
        Initialize LeapIMAP.

        :param keymanager: a keymanager instance
        :type keymanager: keymanager.KeyManager

        :param soledad: a soledad instance
        :type soledad: Soledad

        :param imap_account: the account to fetch periodically
        :type imap_account: SoledadBackedAccount

        :param check_period: the period to fetch new mail, in seconds.
        :type check_period: int
        """

        leap_assert(keymanager, "need a keymanager to initialize")
        leap_assert_type(soledad, Soledad)
        leap_assert(check_period, "need a period to check incoming mail")
        leap_assert_type(check_period, int)

        self._keymanager = keymanager
        self._soledad = soledad
        self.imapAccount = imap_account
        self._inbox = self.imapAccount.getMailbox('inbox')

        self._pkey = self._keymanager.get_all_keys_in_local_db(
            private=True).pop()
        self._loop = None
        self._check_period = check_period

        self._create_soledad_indexes()

    def _create_soledad_indexes(self):
        """
        Create needed indexes on soledad.
        """
        self._soledad.create_index("just-mail", "incoming")

    def fetch(self):
        """
        Fetch incoming mail, to be called periodically.

        Calls a deferred that will execute the fetch callback
        in a separate thread
        """
        logger.debug('fetching mail...')
        d = deferToThread(self._sync_soledad)
        d.addCallbacks(self._process_doclist, self._sync_soledad_err)
        return d

    def start_loop(self):
        """
        Starts a loop to fetch mail.
        """
        self._loop = LoopingCall(self.fetch)
        self._loop.start(self._check_period)

    def stop(self):
        """
        Stops the loop that fetches mail.
        """
        if self._loop:
            try:
                self._loop.stop()
            except AssertionError:
                logger.debug("It looks like we tried to stop a "
                             "loop that was not running.")

    def _sync_soledad(self):
        log.msg('syncing soledad...')
        logger.debug('in soledad sync')

        try:
            self._soledad.sync()
            doclist = self._soledad.get_from_index("just-mail", "*")
            log.msg("there are %s mails" % (len(doclist),))
            return doclist
        except ssl.SSLError as exc:
            logger.warning('SSL Error while syncing soledad: %r' % (exc,))
        except Exception as exc:
            logger.warning('Error while syncing soledad: %r' % (exc,))

    def _sync_soledad_err(self, f):
        log.err("error syncing soledad: %s" % (f.value,))
        return f

    def _process_doclist(self, doclist):
        log.msg('processing doclist')
        if not doclist:
            logger.debug("no docs found")
            return
        for doc in doclist:
            logger.debug("processing doc: %s" % doc)
            keys = doc.content.keys()
            if self.ENC_SCHEME_KEY in keys and self.ENC_JSON_KEY in keys:

                # XXX should check for _enc_scheme == "pubkey" || "none"
                # that is what incoming mail uses.
                encdata = doc.content[self.ENC_JSON_KEY]
                d = defer.Deferred(self._decrypt_msg(doc, encdata))
                d.addCallbacks(self._process_decrypted, log.msg)
            else:
                logger.debug('this SHIT does not look like a proper msg')

    def _decrypt_msg(self, doc, encdata):
        log.msg('decrypting msg')
        key = self._pkey
        decrdata = (openpgp.decrypt_asym(
            encdata, key,
            # XXX get from public method instead
            passphrase=self._soledad._passphrase))

        # XXX DEBUG ME --------------------
        #log.msg('decrdata: --------------')
        #log.msg(decrdata)

        # XXX TODO: defer this properly
        return self._process_decrypted(doc, decrdata)

    def _process_decrypted(self, doc, data):
        """
        Process a successfully decrypted message.

        :param doc: a SoledadDocument instance containing the incoming message
        :type doc: SoledadDocument

        :param data: the json-encoded, decrypted content of the incoming
                     message
        :type data: str

        :param inbox: a open SoledadMailbox instance where this message is
                      to be saved
        :type inbox: SoledadMailbox
        """
        log.msg("processing incoming message!")
        msg = json.loads(data)
        if not isinstance(msg, dict):
            return False
        if not msg.get(self.INCOMING_KEY, False):
            return False
        # ok, this is an incoming message
        rawmsg = msg.get(self.CONTENT_KEY, None)
        if not rawmsg:
            return False
        logger.debug('got incoming message: %s' % (rawmsg,))

        # add to inbox and delete from soledad
        self._inbox.addMessage(rawmsg, (self.RECENT_FLAG,))
        doc_id = doc.doc_id
        self._soledad.delete_doc(doc)
        log.msg("deleted doc %s from incoming" % doc_id)
