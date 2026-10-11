from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import SePayCommon


@tagged('post_install', '-at_install')
class TestSePayIgnore(SePayCommon):
    def _unmatched_transaction(self):
        transaction = self._ingest_webhook(code='NOTADOC-99', content='NOTADOC-99')
        transaction.action_process()
        self.assertEqual(transaction.state, 'unmatched')
        return transaction

    def test_ignore_closes_a_transfer_that_needs_no_entry(self):
        """Without this there is no way out: deletion is forbidden by the ACL."""
        self.company.sepay_activity_user_id = self.env.user
        transaction = self._unmatched_transaction()
        self.assertTrue(transaction.sudo().activity_ids)

        transaction.action_ignore()

        self.assertEqual(transaction.state, 'ignored')
        self.assertFalse(transaction.sudo().activity_ids)
        # The money did arrive, so its bank transaction has to survive.
        self.assertTrue(transaction.bank_statement_line_id)

    def test_ignore_is_reversible_through_retry(self):
        transaction = self._unmatched_transaction()
        transaction.action_ignore()

        invoice = self._create_sepay_invoice(payment_reference='NOTADOC-99')
        transaction.action_retry()

        self.assertEqual(transaction.state, 'processed')
        self.assertEqual(transaction.invoice_ids, invoice)

    def test_ignore_refuses_a_processed_transaction(self):
        self._create_sepay_invoice()
        transaction = self._ingest_webhook()
        transaction.action_process()
        self.assertEqual(transaction.state, 'processed')

        with self.assertRaises(UserError):
            transaction.action_ignore()

    def test_cron_leaves_ignored_transactions_alone(self):
        transaction = self._unmatched_transaction()
        transaction.action_ignore()

        self.env['sepay.transaction']._cron_process_transactions()

        self.assertEqual(transaction.state, 'ignored')
