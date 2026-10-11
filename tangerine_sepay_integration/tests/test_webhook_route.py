import hashlib
import hmac
import json
import time

from odoo.tests import HttpCase, tagged
from odoo.tools import config

from .common import SePayCommon

WEBHOOK_PATH = '/webhook/sepay/v1/transactions'
LEGACY_WEBHOOK_PATH = '/webhook/v1/payment/sepay'
WEBHOOK_SECRET = 'route-test-secret'


@tagged('post_install', '-at_install')
class TestSePayWebhookRoute(SePayCommon, HttpCase):
    """Drive the public route end to end, as SePay actually calls it.

    The other suites call `_ingest_webhook` directly, which runs as the test
    user. Only a real request exercises the public user, and that is where an
    unelevated read blows up with a 403.
    """

    def setUp(self):
        super().setUp()
        # `config.options` is a ChainMap, so replacing it wholesale (what
        # patch.object did here before) strips every other option and the
        # request thread dies on `config['test_enable']`. Shadowing the single
        # key in the runtime layer leaves the rest of the mapping intact.
        config.options['sepay_webhook_secret'] = WEBHOOK_SECRET
        self.addCleanup(config.options.pop, 'sepay_webhook_secret', None)

    def _post_webhook(self, payload, secret=WEBHOOK_SECRET, timestamp=None, path=WEBHOOK_PATH):
        raw_body = json.dumps(payload, separators=(',', ':')).encode()
        timestamp = timestamp or str(int(time.time()))
        signature = 'sha256=' + hmac.new(
            secret.encode(),
            timestamp.encode() + b'.' + raw_body,
            hashlib.sha256,
        ).hexdigest()
        return self.url_open(
            path,
            data=raw_body,
            headers={
                'Content-Type': 'application/json',
                'X-SePay-Signature': signature,
                'X-SePay-Timestamp': timestamp,
            },
        )

    def test_public_route_ingests_a_signed_webhook(self):
        payload = self._webhook_payload(
            id=990001,
            code='ROUTE-TEST-001',
            content='ROUTE-TEST-001',
            referenceCode='ROUTE-TEST-REF-001',
        )

        response = self._post_webhook(payload)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        transaction = self.env['sepay.transaction'].search(
            [('webhook_id', '=', '990001')],
        )
        self.assertEqual(len(transaction), 1)
        self.assertEqual(transaction.company_id, self.company)
        self.assertEqual(transaction.state, 'received')

    def test_legacy_url_still_accepts_a_signed_webhook(self):
        payload = self._webhook_payload(
            id=990005,
            code='ROUTE-TEST-005',
            content='ROUTE-TEST-005',
            referenceCode='ROUTE-TEST-REF-005',
        )

        response = self._post_webhook(payload, path=LEGACY_WEBHOOK_PATH)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.env['sepay.transaction'].search_count(
                [('webhook_id', '=', '990005')],
            ),
            1,
        )

    def test_public_route_is_idempotent(self):
        payload = self._webhook_payload(
            id=990002,
            code='ROUTE-TEST-002',
            content='ROUTE-TEST-002',
            referenceCode='ROUTE-TEST-REF-002',
        )

        self.assertEqual(self._post_webhook(payload).status_code, 200)
        self.assertEqual(self._post_webhook(payload).status_code, 200)

        self.assertEqual(
            self.env['sepay.transaction'].search_count(
                [('webhook_id', '=', '990002')],
            ),
            1,
        )

    def test_public_route_rejects_a_wrong_signature(self):
        payload = self._webhook_payload(id=990003)

        response = self._post_webhook(payload, secret='another-secret')

        self.assertEqual(response.status_code, 401)
        self.assertFalse(
            self.env['sepay.transaction'].search_count(
                [('webhook_id', '=', '990003')],
            ),
        )

    def test_public_route_rejects_an_unknown_account(self):
        payload = self._webhook_payload(id=990004, accountNumber='9999999999')

        response = self._post_webhook(payload)

        self.assertEqual(response.status_code, 422)
        self.assertFalse(
            self.env['sepay.transaction'].search_count(
                [('webhook_id', '=', '990004')],
            ),
        )
