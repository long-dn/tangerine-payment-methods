from urllib.parse import parse_qs, urlparse

from odoo.exceptions import ValidationError
from odoo.tests import tagged

from .common import SePayCommon

VIRTUAL_ACCOUNT = 'QRPSEP1ZZZZ54900752'


@tagged('post_install', '-at_install')
class TestSePayVirtualAccount(SePayCommon):
    def _qr_account_number(self):
        qr_url = self.bank_account._sepay_qr_url(
            500000,
            'INV-SEPAY-001',
            self.company.currency_id,
            silent_errors=False,
        )
        return parse_qs(urlparse(qr_url).query)['acc']

    def test_qr_uses_the_virtual_account_when_enabled(self):
        self.bank_account.write(
            {
                'sepay_va_number': VIRTUAL_ACCOUNT,
                'sepay_use_va': True,
            },
        )

        self.assertEqual(self._qr_account_number(), [VIRTUAL_ACCOUNT])

    def test_qr_keeps_the_main_account_while_the_flag_is_off(self):
        self.bank_account.sepay_va_number = VIRTUAL_ACCOUNT

        self.assertEqual(self._qr_account_number(), [self.bank_account.account_number])

    def test_qr_strips_separators_from_the_virtual_account(self):
        self.bank_account.write(
            {
                'sepay_va_number': ' QRPSEP1ZZZZ-54900752 ',
                'sepay_use_va': True,
            },
        )

        self.assertEqual(self._qr_account_number(), [VIRTUAL_ACCOUNT])

    def test_invoice_qr_uses_the_virtual_account(self):
        self.bank_account.write(
            {
                'sepay_va_number': VIRTUAL_ACCOUNT,
                'sepay_use_va': True,
            },
        )
        invoice = self._create_sepay_invoice()
        invoice.partner_bank_id = self.bank_account

        qr_url = invoice._generate_qr_code(silent_errors=False)

        self.assertEqual(parse_qs(urlparse(qr_url).query)['acc'], [VIRTUAL_ACCOUNT])

    def test_enabling_the_flag_requires_a_virtual_account(self):
        with self.assertRaises(ValidationError):
            self.bank_account.sepay_use_va = True

    def test_webhook_on_the_main_account_still_resolves_the_journal(self):
        self.bank_account.write(
            {
                'sepay_va_number': VIRTUAL_ACCOUNT,
                'sepay_use_va': True,
            },
        )

        transaction = self._ingest_webhook(subAccount=VIRTUAL_ACCOUNT)

        self.assertEqual(transaction.company_id, self.company)
        self.assertEqual(transaction.sub_account, VIRTUAL_ACCOUNT)
