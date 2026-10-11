from odoo import Command, _, api, models
from odoo.exceptions import UserError


class AccountBankStatementLine(models.Model):
    _inherit = 'account.bank.statement.line'

    @api.private
    def _sepay_reconcile_with_lines(self, amls):
        """Reconcile this statement line against existing journal items.

        Community has no bank reconciliation widget, so this applies the
        standard mechanism directly: the statement line's counterpart item is
        moved from the journal suspense account onto the account of ``amls``
        (as core ``action_undo_reconciliation`` rewrites the statement move),
        then both sides go through core ``reconcile()``, which keeps the full
        and partial reconciliation logic where it belongs.

        :param amls: ``account.move.line`` records sharing one reconcilable
            account, e.g. the outstanding receipt line of a payment.
        """
        self.ensure_one()
        amls = amls.exists()
        if not amls:
            raise UserError(_('The journal items to reconcile no longer exist.'))
        account = amls.account_id
        if len(account) != 1:
            raise UserError(
                _('The journal items must share one account to be reconciled with a bank transaction.'),
            )
        if account == self.journal_id.default_account_id:
            raise UserError(
                _(
                    'Cannot reconcile a bank transaction against the bank account itself (%(account)s).',
                    account=account.display_name,
                ),
            )
        if not self.partner_id and len(amls.partner_id) == 1:
            self.partner_id = amls.partner_id
        if not account.reconcile:
            account.reconcile = True
        self.move_id.with_context(force_delete=True, skip_readonly_check=True).write(
            {
                'line_ids': [Command.clear()] + [
                    Command.create(values)
                    for values in self._prepare_move_line_default_vals(
                        counterpart_account_id=account.id,
                    )
                ],
            },
        )
        counterpart = self._seek_for_lines()[2].filtered(
            lambda line: line.account_id == account,
        )
        amls = amls.exists()
        if not counterpart or not amls:
            raise UserError(
                _('The bank transaction or the journal items changed during reconciliation. Please try again.'),
            )
        (counterpart + amls).filtered(lambda line: not line.reconciled).reconcile()
        return True
