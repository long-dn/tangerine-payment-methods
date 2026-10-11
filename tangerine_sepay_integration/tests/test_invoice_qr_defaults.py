from odoo.tests import tagged

from .common import SePayCommon

SEPAY_QR_METHOD = 'sepay_qr'


@tagged('post_install', '-at_install')
class TestSePayInvoiceQrDefaults(SePayCommon):
    def _draft_invoice(self):
        return self._create_invoice_one_line(
            price_unit=500000,
            tax_ids=self.env['account.tax'],
            partner_id=self.partner_a.id,
            company_id=self.company.id,
            currency_id=self.company.currency_id.id,
            post=False,
        )

    def test_new_invoice_preselects_the_sepay_qr_method(self):
        """The method used to stay empty until somebody printed the invoice."""
        invoice = self._draft_invoice()

        self.assertEqual(invoice.qr_code_method, SEPAY_QR_METHOD)
        self.assertEqual(invoice.partner_bank_id, self.bank_account)

    def test_recompute_prefers_a_bank_account_owned_by_a_journal(self):
        """Core sorts by currency only, so it can land on an unusable account."""
        other = self.env['res.partner.bank'].create(
            {
                'acc_number': '9999999999',
                'partner_id': self.company.partner_id.id,
                'company_id': self.company.id,
            },
        )
        invoice = self._draft_invoice()
        invoice.partner_bank_id = other

        invoice._compute_partner_bank_id()

        self.assertEqual(invoice.partner_bank_id, self.bank_account)

    def test_a_vendor_bill_keeps_the_core_behaviour(self):
        bill = self._create_invoice_one_line(
            move_type='in_invoice',
            price_unit=500000,
            tax_ids=self.env['account.tax'],
            partner_id=self.partner_a.id,
            company_id=self.company.id,
            currency_id=self.company.currency_id.id,
            post=False,
        )

        self.assertNotEqual(bill.qr_code_method, SEPAY_QR_METHOD)

    def test_printing_a_posted_invoice_does_not_touch_the_recipient_bank(self):
        """`partner_bank_id` is tracked; a report render must not file chatter."""
        invoice = self._create_sepay_invoice()
        invoice.sudo().partner_bank_id = False

        qr_code = invoice._generate_qr_code(silent_errors=True)

        self.assertTrue(qr_code)
        self.assertFalse(invoice.partner_bank_id)

    def test_printing_a_draft_invoice_still_fills_the_recipient_bank(self):
        invoice = self._draft_invoice()
        invoice.partner_bank_id = False

        qr_code = invoice._generate_qr_code(silent_errors=True)

        self.assertTrue(qr_code)
        self.assertEqual(invoice.partner_bank_id, self.bank_account)
