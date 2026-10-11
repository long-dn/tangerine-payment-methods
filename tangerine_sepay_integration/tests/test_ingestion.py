import hashlib
import hmac
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tools import config

from .common import SePayCommon
from odoo.addons.tangerine_sepay_integration.controllers.main import SePayWebhookController

SYNC_LOGGER = 'odoo.addons.tangerine_sepay_integration.models.res_company'


@tagged('post_install', '-at_install')
class TestSePayIngestion(SePayCommon):
    def test_webhook_route_uses_the_public_v1_path(self):
        # The second path is the URL published by version 1.x of the module.
        self.assertEqual(
            SePayWebhookController.sepay_transaction_webhook.original_routing[
                'routes'
            ],
            ['/webhook/sepay/v1/transactions', '/webhook/v1/payment/sepay'],
        )

    def test_webhook_replay_is_idempotent(self):
        first = self._ingest_webhook()
        second = self._ingest_webhook()

        self.assertEqual(first, second)
        self.assertEqual(
            self.env['sepay.transaction'].search_count(
                [
                    ('webhook_id', '=', '10001'),
                ],
            ),
            1,
        )

    def test_webhook_and_api_merge_by_canonical_identity(self):
        webhook_transaction = self._ingest_webhook()
        api_transaction = (
            self.env['sepay.transaction']
            .sudo()
            ._ingest_api(
                self.company,
                {
                    'id': 'a1b2c3d4-e5f6-7890-abcd-ef1234567890',
                    'transaction_date': '2026-08-27T10:15:20+07:00',
                    'account_number': self.bank_account.acc_number,
                    'va': None,
                    'transfer_type': 'in',
                    'amount_in': 500000,
                    'amount_out': 0,
                    'accumulated': 1500000,
                    'transaction_content': 'INV-SEPAY-001',
                    'reference_number': 'FT26239ABC',
                    'code': 'INV-SEPAY-001',
                    'bank_brand_name': 'SPB',
                },
            )
        )

        self.assertEqual(api_transaction, webhook_transaction)
        self.assertEqual(api_transaction.source, 'both')
        self.assertEqual(api_transaction.api_id, 'a1b2c3d4-e5f6-7890-abcd-ef1234567890')
        self.assertTrue(api_transaction.webhook_payload)
        self.assertTrue(api_transaction.api_payload)

    def test_api_sync_uses_cursor_and_pagination(self):
        pages = [
            {
                'status': 'success',
                'data': [
                    {
                        'id': 'api-1',
                        'transaction_date': '2026-08-27T10:15:20+07:00',
                        'account_number': self.bank_account.acc_number,
                        'transfer_type': 'in',
                        'amount_in': 100000,
                        'amount_out': 0,
                        'accumulated': 100000,
                        'transaction_content': 'First',
                        'reference_number': 'REF-1',
                        'code': None,
                        'bank_brand_name': 'SPB',
                    },
                ],
                'meta': {'pagination': {'has_more': True}},
            },
            {
                'status': 'success',
                'data': [
                    {
                        'id': 'api-2',
                        'transaction_date': '2026-08-27T10:16:20+07:00',
                        'account_number': self.bank_account.acc_number,
                        'transfer_type': 'in',
                        'amount_in': 200000,
                        'amount_out': 0,
                        'accumulated': 300000,
                        'transaction_content': 'Second',
                        'reference_number': 'REF-2',
                        'code': None,
                        'bank_brand_name': 'SPB',
                    },
                ],
                'meta': {'pagination': {'has_more': False}},
            },
        ]
        company_class = type(self.company)
        with patch.object(
            company_class,
            '_sepay_request_page',
            autospec=True,
            side_effect=pages,
        ) as request_page:
            transactions = self.company.sudo()._sepay_sync_transactions()

        self.assertEqual(len(transactions), 2)
        self.assertEqual(self.company.sepay_last_api_id, 'api-2')
        self.assertEqual(request_page.call_count, 2)
        self.assertEqual(request_page.call_args_list[1].args[1]['page'], 2)

    def test_hmac_verification_rejects_tampering_and_stale_timestamp(self):
        raw_body = b'{"id":10001}'
        timestamp = '1787800000'
        secret = 'test-secret'
        signature = (
            'sha256='
            + hmac.new(
                secret.encode(),
                timestamp.encode() + b'.' + raw_body,
                hashlib.sha256,
            ).hexdigest()
        )
        with patch(
            'odoo.addons.tangerine_sepay_integration.controllers.main.time.time',
            return_value=1787800000,
        ):
            self.assertTrue(
                SePayWebhookController._verify_signature(
                    raw_body,
                    signature,
                    timestamp,
                    secret,
                ),
            )
            self.assertFalse(
                SePayWebhookController._verify_signature(
                    raw_body + b' ',
                    signature,
                    timestamp,
                    secret,
                ),
            )
            self.assertFalse(
                SePayWebhookController._verify_signature(
                    raw_body,
                    signature,
                    '1787799000',
                    secret,
                ),
            )

    # The processing cron ships ENABLED since 19.0.4.1.0, so the shipped state
    # of both crons is asserted in `test_settings_crons.py` instead.

    def test_any_configured_secret_opens_the_first_gate(self):
        raw_body = b'{"id":10001}'
        timestamp = '1787800000'
        signature = (
            'sha256='
            + hmac.new(
                b'second-secret',
                timestamp.encode() + b'.' + raw_body,
                hashlib.sha256,
            ).hexdigest()
        )
        with patch(
            'odoo.addons.tangerine_sepay_integration.controllers.main.time.time',
            return_value=1787800000,
        ):
            self.assertTrue(
                SePayWebhookController._matches_any_secret(
                    raw_body,
                    signature,
                    timestamp,
                    ['first-secret', 'second-secret'],
                ),
            )
            self.assertFalse(
                SePayWebhookController._matches_any_secret(
                    raw_body,
                    signature,
                    timestamp,
                    ['first-secret'],
                ),
            )

    def test_configured_secrets_cover_per_company_overrides(self):
        options = {
            'sepay_webhook_secret': 'global-secret',
            'sepay_webhook_secret_7': 'company-secret',
            'sepay_webhook_secret_9': False,
            'sepay_api_token': 'not-a-webhook-secret',
        }

        with patch.object(config, 'options', options):
            secrets = SePayWebhookController._configured_secrets()

        self.assertEqual(sorted(secrets), ['company-secret', 'global-secret'])

    def test_company_secret_takes_precedence_over_the_global_one(self):
        company_option = f'sepay_webhook_secret_{self.company.id}'

        with patch.object(
            config,
            'options',
            {
                'sepay_webhook_secret': 'global-secret',
                company_option: 'company-secret',
            },
        ):
            self.assertEqual(
                self.company._sepay_webhook_secret(),
                'company-secret',
            )
        with patch.object(
            config,
            'options',
            {'sepay_webhook_secret': 'global-secret'},
        ):
            self.assertEqual(
                self.company._sepay_webhook_secret(),
                'global-secret',
            )

    def test_api_sync_skips_a_bad_payload_and_advances_the_cursor(self):
        def api_payload(identifier, amount, content):
            return {
                'id': identifier,
                'transaction_date': '2026-08-27T10:15:20+07:00',
                'account_number': self.bank_account.acc_number,
                'transfer_type': 'in',
                'amount_in': amount,
                'amount_out': 0,
                'accumulated': amount,
                'transaction_content': content,
                'reference_number': f'REF-{identifier}',
                'code': None,
                'bank_brand_name': 'SPB',
            }

        pages = [
            {
                'status': 'success',
                'data': [
                    api_payload('api-1', 100000, 'First'),
                    # SePay never sends this, but a single malformed record
                    # must not stall the cursor for every later run.
                    api_payload('api-2', 0, 'Broken'),
                    api_payload('api-3', 300000, 'Third'),
                ],
                'meta': {'pagination': {'has_more': False}},
            },
        ]
        company_class = type(self.company)
        with (
            patch.object(
                company_class,
                '_sepay_request_page',
                autospec=True,
                side_effect=pages,
            ),
            self.assertLogs(SYNC_LOGGER, level='ERROR'),
        ):
            transactions = self.company.sudo()._sepay_sync_transactions()

        self.assertEqual(len(transactions), 2)
        self.assertEqual(self.company.sepay_last_api_id, 'api-3')
        self.assertIn('api-2', self.company.sepay_last_sync_error)
