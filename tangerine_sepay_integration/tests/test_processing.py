from unittest.mock import patch

from odoo import Command
from odoo.tests import tagged

from .common import SePayCommon
from odoo.addons.tangerine_sepay_integration.models import sepay_transaction

PROCESSING_LOGGER = 'odoo.addons.tangerine_sepay_integration.models.sepay_transaction'


@tagged('post_install', '-at_install')
class TestSePayProcessing(SePayCommon):
    def test_exact_invoice_match_records_and_reconciles_payment(self):
        invoice = self._create_sepay_invoice()
        transaction = self._ingest_webhook()

        transaction.action_process()

        self.assertEqual(transaction.state, 'processed')
        self.assertEqual(transaction.invoice_ids, invoice)
        self.assertTrue(transaction.payment_id)
        # Matched with its bank transaction, the payment is reconciled in 20.0.
        self.assertEqual(transaction.payment_id.state, 'reconciled')
        self.assertTrue(transaction.bank_statement_line_id.is_reconciled)
        self.assertEqual(transaction.bank_statement_line_id.amount, 500000)
        self.assertEqual(invoice.amount_residual, 0)

    def test_partial_invoice_payment_is_supported(self):
        invoice = self._create_sepay_invoice(amount=800000)
        transaction = self._ingest_webhook(transferAmount=300000)

        transaction.action_process()

        self.assertEqual(transaction.state, 'processed')
        self.assertEqual(invoice.amount_residual, 500000)
        self.assertTrue(transaction.bank_statement_line_id.is_reconciled)

    def test_pending_payment_transaction_uses_standard_post_processing(self):
        invoice = self._create_sepay_invoice()
        provider = self.env.ref('payment.payment_provider_transfer').sudo()
        provider.write(
            {
                'company_id': self.company.id,
                'journal_id': self.bank_journal.id,
            },
        )
        payment_transaction = self.env['payment.transaction'].create(
            {
                'provider_id': provider.id,
                'payment_method_id': self.env.ref(
                    'payment_custom.payment_method_wire_transfer',
                ).id,
                'reference': 'SEPAY-TX-001',
                'amount': 500000,
                'currency_id': self.company.currency_id.id,
                'partner_id': self.partner_a.id,
                'operation': 'online_redirect',
                'state': 'pending',
                'invoice_ids': [Command.set(invoice.ids)],
            },
        )
        transaction = self._ingest_webhook()

        transaction.action_process()

        self.assertEqual(transaction.state, 'processed')
        self.assertEqual(transaction.payment_transaction_id, payment_transaction)
        self.assertEqual(payment_transaction.state, 'done')
        self.assertEqual(transaction.payment_id, payment_transaction.payment_id)
        self.assertTrue(transaction.bank_statement_line_id.is_reconciled)
        self.assertEqual(invoice.amount_residual, 0)

    def test_pending_sale_order_transaction_confirms_and_records_payment(self):
        order = (
            self.env['sale.order']
            .sudo()
            .create(
                {
                    'partner_id': self.partner_a.id,
                    'company_id': self.company.id,
                    # Since 20.0 a payment no longer confirms an order that
                    # still waits for its online signature, as on eCommerce.
                    'require_signature': False,
                    'order_line': [
                        Command.create(
                            {
                                'product_id': self.product_a.id,
                                'product_uom_qty': 1,
                                'price_unit': 500000,
                                'tax_ids': [Command.clear()],
                            },
                        ),
                    ],
                },
            )
        )
        provider = self.env.ref('payment.payment_provider_transfer').sudo()
        provider.write(
            {
                'company_id': self.company.id,
                'journal_id': self.bank_journal.id,
            },
        )
        payment_transaction = (
            self.env['payment.transaction']
            .sudo()
            .create(
                {
                    'provider_id': provider.id,
                    'payment_method_id': self.env.ref(
                        'payment_custom.payment_method_wire_transfer',
                    ).id,
                    'reference': 'SEPAY-SO-TX-001',
                    'amount': 500000,
                    'currency_id': self.company.currency_id.id,
                    'partner_id': self.partner_a.id,
                    'operation': 'online_redirect',
                    'state': 'pending',
                    'sale_order_ids': [Command.set(order.ids)],
                },
            )
        )
        transaction = self._ingest_webhook(code=order.name, content=order.name)

        transaction.action_process()

        self.assertEqual(transaction.state, 'processed')
        self.assertEqual(transaction.sale_order_id, order)
        self.assertEqual(payment_transaction.state, 'done')
        self.assertEqual(order.state, 'sale')
        self.assertEqual(transaction.payment_id, payment_transaction.payment_id)
        self.assertTrue(transaction.bank_statement_line_id.is_reconciled)

    def test_unknown_code_creates_unreconciled_statement_line(self):
        transaction = self._ingest_webhook(code='UNKNOWN-REFERENCE')

        transaction.action_process()

        self.assertEqual(transaction.state, 'unmatched')
        self.assertTrue(transaction.bank_statement_line_id)
        self.assertFalse(transaction.bank_statement_line_id.is_reconciled)
        self.assertFalse(transaction.payment_id)

    def test_overpayment_is_left_for_review(self):
        invoice = self._create_sepay_invoice(amount=500000)
        transaction = self._ingest_webhook(transferAmount=500001)

        transaction.action_process()

        self.assertEqual(transaction.state, 'unmatched')
        self.assertEqual(transaction.invoice_ids, invoice)
        self.assertFalse(transaction.payment_id)
        self.assertFalse(transaction.bank_statement_line_id.is_reconciled)
        self.assertEqual(invoice.amount_residual, 500000)

    def test_outgoing_transaction_is_never_auto_allocated(self):
        transaction = self._ingest_webhook(
            transferType='out',
            transferAmount=200000,
            code=False,
        )

        transaction.action_process()

        self.assertEqual(transaction.state, 'unmatched')
        self.assertEqual(transaction.bank_statement_line_id.amount, -200000)
        self.assertFalse(transaction.payment_id)

    def test_reprocessing_does_not_duplicate_accounting_entries(self):
        self._create_sepay_invoice()
        transaction = self._ingest_webhook()
        transaction.action_process()
        payment = transaction.payment_id
        statement_line = transaction.bank_statement_line_id

        transaction.action_process()

        self.assertEqual(transaction.payment_id, payment)
        self.assertEqual(transaction.bank_statement_line_id, statement_line)

    def test_retry_closes_an_already_manually_reconciled_transaction(self):
        transaction = self._ingest_webhook(code='MANUAL-REVIEW')
        transaction.action_process()
        order = self.env['sale.order'].sudo().create(
            {
                'partner_id': self.partner_a.id,
                'company_id': self.company.id,
            },
        )
        payment = transaction.sudo()._create_order_payment(
            self.bank_journal,
            order,
            transaction.transaction_date.date(),
        )
        outstanding_lines = payment._seek_for_lines()[0].filtered(
            lambda line: not line.reconciled,
        )
        transaction.bank_statement_line_id._sepay_reconcile_with_lines(outstanding_lines)

        transaction.action_retry()

        self.assertEqual(transaction.state, 'processed')
        self.assertTrue(transaction.bank_statement_line_id.is_reconciled)
        self.assertFalse(transaction.payment_id)

    def test_partially_paid_invoice_reuses_the_manual_payment(self):
        invoice = self._create_sepay_invoice(amount=800000)
        manual_payment = self._register_manual_payment(invoice, 300000)
        transaction = self._ingest_webhook(transferAmount=300000)

        transaction.action_process()

        self.assertEqual(transaction.state, 'processed')
        self.assertEqual(transaction.payment_id, manual_payment)
        self.assertEqual(invoice.amount_residual, 500000)
        self.assertTrue(transaction.bank_statement_line_id.is_reconciled)
        self.assertEqual(
            self.env['account.payment'].search_count(
                [('journal_id', '=', self.bank_journal.id)],
            ),
            1,
        )

    def test_fully_paid_invoice_is_closed_by_the_existing_payment(self):
        invoice = self._create_sepay_invoice()
        manual_payment = self._register_manual_payment(invoice, 500000)
        transaction = self._ingest_webhook()

        transaction.action_process()

        self.assertEqual(transaction.state, 'processed')
        self.assertEqual(transaction.payment_id, manual_payment)
        self.assertEqual(invoice.amount_residual, 0)
        self.assertTrue(transaction.bank_statement_line_id.is_reconciled)

    def test_fully_paid_invoice_without_a_matching_payment_is_reviewed(self):
        invoice = self._create_sepay_invoice()
        # Paid from another journal, so nothing on the SePay journal records
        # this transfer.
        self.env['account.payment.register'].with_context(
            active_model='account.move',
            active_ids=invoice.ids,
        ).create(
            {'journal_id': self.company_data['default_journal_cash'].id},
        )._create_payments()
        transaction = self._ingest_webhook()

        transaction.action_process()

        self.assertEqual(transaction.state, 'unmatched')
        self.assertFalse(transaction.payment_id)
        self.assertEqual(invoice.amount_residual, 0)

    def test_code_with_separators_still_matches_the_invoice(self):
        invoice = self._create_sepay_invoice(payment_reference='INV/2026/00123')
        transaction = self._ingest_webhook(
            code='INV202600123',
            content='INV202600123',
        )

        transaction.action_process()

        self.assertEqual(transaction.state, 'processed')
        self.assertEqual(transaction.invoice_ids, invoice)
        self.assertEqual(invoice.amount_residual, 0)

    def test_content_matches_the_invoice_when_sepay_sends_no_code(self):
        invoice = self._create_sepay_invoice(payment_reference='INV/2026/00777')
        transaction = self._ingest_webhook(
            code=False,
            content='CT tu NGUYEN VAN A INV/2026/00777 thanh toan don hang',
        )

        transaction.action_process()

        self.assertEqual(transaction.state, 'processed')
        self.assertEqual(transaction.invoice_ids, invoice)

    def test_content_naming_two_invoices_is_left_for_review(self):
        self._create_sepay_invoice(payment_reference='INV/2026/00901')
        self._create_sepay_invoice(payment_reference='INV/2026/00902')
        transaction = self._ingest_webhook(
            code=False,
            content='TT INV/2026/00901 va INV/2026/00902',
        )

        transaction.action_process()

        self.assertEqual(transaction.state, 'unmatched')
        self.assertFalse(transaction.invoice_ids)
        self.assertFalse(transaction.payment_id)

    def test_cron_retries_errored_transactions_up_to_the_limit(self):
        transaction = self._ingest_webhook()
        self.bank_journal.bank_account_id = False
        transactions = self.env['sepay.transaction']

        with self.assertLogs(PROCESSING_LOGGER, level='ERROR'):
            transaction.action_process()
            self.assertEqual(transaction.state, 'error')
            self.assertEqual(transaction.retry_count, 1)

            with patch.object(sepay_transaction, 'AUTOMATIC_RETRY_DELAY_MINUTES', 0):
                transactions._cron_process_transactions()
            self.assertEqual(transaction.retry_count, 2)

        transaction.sudo().write(
            {'retry_count': sepay_transaction.MAX_AUTOMATIC_RETRIES},
        )
        with patch.object(sepay_transaction, 'AUTOMATIC_RETRY_DELAY_MINUTES', 0):
            transactions._cron_process_transactions()

        self.assertEqual(
            transaction.retry_count,
            sepay_transaction.MAX_AUTOMATIC_RETRIES,
        )

    def test_review_activity_goes_to_the_configured_responsible(self):
        self.company.sepay_activity_user_id = self.env.user
        transaction = self._ingest_webhook(
            code='UNKNOWN-REFERENCE',
            content='UNKNOWN-REFERENCE',
        )

        transaction.action_process()

        self.assertEqual(transaction.state, 'unmatched')
        self.assertEqual(transaction.activity_ids.user_id, self.env.user)

        transaction.action_retry()

        self.assertEqual(len(transaction.activity_ids), 1)

    def test_no_review_activity_without_a_responsible(self):
        transaction = self._ingest_webhook(
            code='UNKNOWN-REFERENCE',
            content='UNKNOWN-REFERENCE',
        )

        transaction.action_process()

        self.assertEqual(transaction.state, 'unmatched')
        self.assertFalse(transaction.activity_ids)

    def test_content_matching_can_be_turned_off(self):
        self.company.sepay_match_content = False
        self._create_sepay_invoice(payment_reference='INV/2026/00777')
        transaction = self._ingest_webhook(
            code=False,
            content='CT tu NGUYEN VAN A INV/2026/00777 thanh toan don hang',
        )

        transaction.action_process()

        self.assertEqual(transaction.state, 'unmatched')
        self.assertFalse(transaction.invoice_ids)

    def test_reference_folding_matches_what_the_bank_reports(self):
        fold = self.env['sepay.transaction']._normalise_reference

        self.assertEqual(fold('HĐ-2026-0459'), 'HD20260459')
        self.assertEqual(fold('hđ 2026 0459'), 'HD20260459')
        self.assertEqual(fold('INV/2026/00014'), 'INV202600014')
        self.assertEqual(fold('Sổ MB'), 'SOMB')

    def test_vietnamese_invoice_number_matches_the_folded_content(self):
        invoice = self._create_sepay_invoice(payment_reference='HĐ-2026-0459')
        # SePay strips the accent and the hyphen before it reaches Odoo.
        transaction = self._ingest_webhook(
            code='HD20260459',
            content='HD20260459',
        )

        transaction.action_process()

        self.assertEqual(transaction.state, 'processed')
        self.assertEqual(transaction.invoice_ids, invoice)
        self.assertEqual(invoice.amount_residual, 0)
