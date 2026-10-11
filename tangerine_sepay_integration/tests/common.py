import json

from odoo import Command

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

WEBHOOK_TRANSACTION_DATE = '2026-08-27 10:15:20'


class SePayCommon(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country('vn')
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.company_data['company']
        cls.company.sepay_enabled = True
        # Let `account.move.display_qr_code` compute to True on its own.
        cls.company.qr_code = True
        cls.env.user.group_ids |= cls.env.ref('account.group_account_manager')

        cls.bank = cls.env['vn.bank'].create(
            {
                'name': 'Test SePay Bank',
                'code': 'SEPAYBANK',
                'short_name': 'SPB',
                'bin': '970999',
            },
        )
        cls.bank_account = cls.env['res.partner.bank'].create(
            {
                'account_number': '0123456789',
                'holder_name': 'ABC FOOD COMPANY',
                'vn_bank_id': cls.bank.id,
                'partner_id': cls.company.partner_id.id,
                'company_id': cls.company.id,
            },
        )
        cls.bank_journal = cls.company_data['default_journal_bank']
        cls.bank_journal.bank_account_id = cls.bank_account

        cls.outstanding_account = cls.env['account.account'].create(
            {
                'name': 'SePay Outstanding Receipts',
                'code': '1126SP',
                'account_type': 'asset_current',
                'reconcile': True,
                'company_ids': [Command.link(cls.company.id)],
            },
        )
        inbound_manual = cls.bank_journal.inbound_payment_method_line_ids.filtered(
            lambda line: line.code == 'manual',
        )[:1]
        inbound_manual.payment_account_id = cls.outstanding_account

    def _webhook_payload(self, **overrides):
        payload = {
            'id': 10001,
            'gateway': 'SPB',
            'transactionDate': WEBHOOK_TRANSACTION_DATE,
            'accountNumber': self.bank_account.account_number,
            'subAccount': None,
            'code': 'INV-SEPAY-001',
            'content': 'INV-SEPAY-001',
            'transferType': 'in',
            'description': 'Incoming transfer',
            'transferAmount': 500000,
            'accumulated': 1500000,
            'referenceCode': 'FT26239ABC',
        }
        payload.update(overrides)
        return payload

    def _ingest_webhook(self, **overrides):
        payload = self._webhook_payload(**overrides)
        raw_body = json.dumps(payload, separators=(',', ':')).encode()
        return (
            self.env['sepay.transaction'].sudo()._ingest_webhook(payload, raw_body)
        )

    def _create_sepay_invoice(self, amount=500000, payment_reference='INV-SEPAY-001'):
        return self._create_invoice_one_line(
            price_unit=amount,
            tax_ids=self.env['account.tax'],
            partner_id=self.partner_a.id,
            company_id=self.company.id,
            currency_id=self.company.currency_id.id,
            payment_reference=payment_reference,
            post=True,
        )

    def _register_manual_payment(self, invoice, amount):
        """Record the payment an accountant would encode by hand."""
        return (
            self.env['account.payment.register']
            .with_context(active_model='account.move', active_ids=invoice.ids)
            .create(
                {
                    'amount': amount,
                    'journal_id': self.bank_journal.id,
                    'payment_date': WEBHOOK_TRANSACTION_DATE[:10],
                },
            )
            ._create_payments()
        )
