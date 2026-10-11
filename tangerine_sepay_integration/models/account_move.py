import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .res_partner_bank import SEPAY_QR_METHOD

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'

    qr_code_method = fields.Selection(
        compute='_compute_qr_code_method',
        store=True,
        readonly=False,
    )

    @api.private
    def _sepay_journal_bank_accounts(self):
        """Return the company bank accounts SePay can actually report on."""
        self.ensure_one()
        return (
            self.env['account.journal']
            .search(
                [
                    ('company_id', '=', self.company_id.id),
                    ('type', '=', 'bank'),
                    ('bank_account_id', '!=', False),
                ],
            )
            .bank_account_id
        )

    @api.private
    def _sepay_wants_qr(self):
        """Whether this document should carry a SePay QR by default."""
        self.ensure_one()
        return bool(
            self.company_id.sepay_enabled
            and self.is_sale_document(include_receipts=True),
        )

    @api.depends(
        'bank_partner_id',
        'currency_id',
        'preferred_payment_method_line_id',
        'company_id',
    )
    def _compute_partner_bank_id(self):
        """Prefer a bank account SePay can report on.

        Core picks the first company bank account sorted by currency, with no
        idea whether a bank journal owns it. SePay only ever reports transfers
        landing on a journal's own account, so a customer invoice pointed at a
        journal-less account can never produce a QR code and the transfer that
        pays it can never be matched back.
        """
        super()._compute_partner_bank_id()
        candidates = self.filtered(lambda move: move._sepay_wants_qr())
        # One journal lookup per company, not per move: this compute runs on
        # every invoice import and on any upgrade that retriggers it.
        for company, moves in candidates.grouped('company_id').items():
            journal_accounts = (
                self.env['account.journal']
                .search(
                    [
                        ('company_id', '=', company.id),
                        ('type', '=', 'bank'),
                        ('bank_account_id', '!=', False),
                    ],
                )
                .bank_account_id
            )
            if len(journal_accounts) != 1:
                continue
            for move in moves:
                if move.partner_bank_id not in journal_accounts:
                    move.partner_bank_id = journal_accounts

    @api.depends('company_id', 'move_type', 'partner_bank_id', 'currency_id')
    def _compute_qr_code_method(self):
        """Pre-select the SePay QR method so the invoice states it up front.

        Core leaves the field empty and resolves a method only while rendering,
        which both hides the choice from whoever prepares the invoice and makes
        printing the moment the field is first written. Assignment is skipped
        when SePay cannot serve this document, so a method chosen by hand for a
        non-SePay invoice survives.
        """
        for move in self:
            eligible = move._sepay_wants_qr() and not move.partner_bank_id._get_error_messages_for_qr(
                SEPAY_QR_METHOD,
                move.partner_id,
                move.currency_id,
            )
            move.qr_code_method = SEPAY_QR_METHOD if eligible else move.qr_code_method

    @api.private
    def _sepay_partner_bank(self, silent_errors=True):
        """Return the company bank account the SePay QR code must credit.

        SePay only reports transfers landing on a bank journal's own account,
        so the QR has to point at one of those. An account already chosen on
        the invoice is kept when it qualifies; it is never replaced, because
        the document itself states where the customer is asked to pay.
        """
        self.ensure_one()
        journal_bank_accounts = self._sepay_journal_bank_accounts()
        if self.partner_bank_id:
            if self.partner_bank_id in journal_bank_accounts:
                return self.partner_bank_id
            message = _(
                'The recipient account of invoice %(invoice)s is not a SePay '
                'bank journal account, so no SePay QR code can be generated '
                'for it.',
                invoice=self.name,
            )
            if not silent_errors:
                raise UserError(message)
            _logger.warning(message)
            return self.env['res.partner.bank']
        if len(journal_bank_accounts) != 1:
            return self.env['res.partner.bank']
        return journal_bank_accounts

    def _generate_qr_code(self, silent_errors=False):
        self.ensure_one()
        if not self.company_id.sepay_enabled:
            return super()._generate_qr_code(silent_errors=silent_errors)
        if not self.display_qr_code or not self.amount_residual:
            return None
        partner_bank = self._sepay_partner_bank(silent_errors=silent_errors)
        qr_code = partner_bank._sepay_qr_url(
            self.amount_residual,
            self.payment_reference or self.name,
            self.currency_id,
            silent_errors=silent_errors,
        )
        if qr_code:
            # Rendering a report must not amend a posted accounting document:
            # `partner_bank_id` is tracked, so writing it here would file a
            # chatter entry every time somebody prints the invoice. Core only
            # ever writes the untracked `qr_code_method` from this method, and
            # `_compute_partner_bank_id` has already filled the account while
            # the invoice was still a draft.
            if not self.partner_bank_id and self.state != 'posted':
                self.partner_bank_id = partner_bank
            self.qr_code_method = SEPAY_QR_METHOD
        return qr_code
