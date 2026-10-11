from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import requests

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import SePayCommon


@tagged('post_install', '-at_install')
class TestSePayQrAndSecurity(SePayCommon):
    def test_qr_url_is_encoded_from_existing_bank_directory(self):
        qr_url = self.bank_account._sepay_qr_url(
            500000,
            'INV/2026/001 & customer',
            self.company.currency_id,
            silent_errors=False,
        )
        parsed = urlparse(qr_url)
        query = parse_qs(parsed.query)

        self.assertEqual(
            f'{parsed.scheme}://{parsed.netloc}{parsed.path}',
            'https://qr.sepay.vn/img',
        )
        self.assertEqual(query['acc'], [self.bank_account.acc_number])
        self.assertEqual(query['bank'], [self.bank.short_name])
        self.assertEqual(query['amount'], ['500000'])
        self.assertEqual(query['des'], ['INV/2026/001 & customer'])

    def test_invoice_uses_sepay_qr_method(self):
        invoice = self._create_sepay_invoice()
        invoice.partner_bank_id = self.bank_account
        invoice.display_qr_code = True

        qr_url = invoice._generate_qr_code(silent_errors=False)

        self.assertTrue(qr_url.startswith('https://qr.sepay.vn/img?'))
        self.assertEqual(invoice.qr_code_method, 'sepay_qr')

    def test_non_manager_cannot_process_or_modify_source_data(self):
        transaction = self._ingest_webhook(code='UNKNOWN-REFERENCE')
        user = self.env['res.users'].create(
            {
                'name': 'SePay Accounting User',
                'login': 'sepay-accounting-user',
                'groups_id': [
                    Command.set(
                        [
                            self.env.ref('base.group_user').id,
                            self.env.ref('account.group_account_invoice').id,
                        ],
                    ),
                ],
            },
        )

        with self.assertRaises(AccessError):
            transaction.with_user(user).action_process()
        with self.assertRaises(AccessError):
            transaction.with_user(user).write({'amount': 1})

    def test_qr_url_and_base64_hooks_agree(self):
        arguments = (
            'sepay_qr',
            500000,
            self.company.currency_id,
            self.env['res.partner'],
            'INV-SEPAY-001',
            None,
        )

        url = self.bank_account._get_qr_code_url(*arguments)

        self.assertEqual(url, self.bank_account._get_qr_code_base64(*arguments))
        self.assertTrue(url.startswith('https://qr.sepay.vn/img?'))

    def test_printing_never_reroutes_the_invoice_bank_account(self):
        other_account = self.env['res.partner.bank'].create(
            {
                'acc_number': '9998887776',
                'bank_id': self.bank.id,
                'partner_id': self.company.partner_id.id,
                'company_id': self.company.id,
            },
        )
        invoice = self._create_sepay_invoice()
        invoice.partner_bank_id = other_account

        self.assertIsNone(invoice._generate_qr_code(silent_errors=True))
        self.assertEqual(invoice.partner_bank_id, other_account)
        with self.assertRaises(UserError):
            invoice._generate_qr_code(silent_errors=False)

    # Filling a blank recipient bank while printing now depends on the invoice
    # state, so both directions live in `test_invoice_qr_defaults.py`.

    def test_cached_qr_image_is_downloaded_once(self):
        self.company.sepay_cache_qr_images = True
        response = Mock(
            content=b'\x89PNG\r\n\x1a\n' + b'0' * 64,
            headers={'Content-Type': 'image/png'},
        )
        response.raise_for_status.return_value = None

        with patch(
            'odoo.addons.tangerine_sepay_integration.models.res_partner_bank.requests.get',
            return_value=response,
        ) as download:
            first = self.bank_account._sepay_qr_url(
                500000,
                'INV-SEPAY-001',
                self.company.currency_id,
            )
            second = self.bank_account._sepay_qr_url(
                500000,
                'INV-SEPAY-001',
                self.company.currency_id,
            )

        self.assertEqual(download.call_count, 1)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith('data:image/png;base64,'))

    def test_unreachable_qr_service_falls_back_to_the_sepay_url(self):
        self.company.sepay_cache_qr_images = True

        with patch(
            'odoo.addons.tangerine_sepay_integration.models.res_partner_bank.requests.get',
            side_effect=requests.exceptions.ConnectTimeout(),
        ):
            qr_url = self.bank_account._sepay_qr_url(
                500000,
                'INV-SEPAY-001',
                self.company.currency_id,
            )

        self.assertTrue(qr_url.startswith('https://qr.sepay.vn/img?'))
