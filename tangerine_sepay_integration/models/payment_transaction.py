from odoo import api, models


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    @api.private
    def _sepay_qr_url(self):
        """Return the SePay QR of this wire transfer, or None.

        The account shown on the payment instructions is used, so the QR and
        the text beside it always agree; the provider's journal account is the
        fallback. It should be the same account: SePay transfers are matched
        back through the journal that owns the receiving account.
        """
        self.ensure_one()
        if not self.company_id.sepay_enabled or self.provider_code != 'custom':
            return None
        provider = self.provider_id.sudo()
        bank_account = provider._get_custom_bank_account() or provider.journal_id.bank_account_id
        return bank_account._sepay_qr_url(
            self.amount,
            self._get_communication(),
            self.currency_id,
        )

    def _get_custom_qr_code(self):
        # Both the payment status page and the eCommerce confirmation render
        # the wire-transfer instructions through this hook.
        self.ensure_one()
        if self.company_id.sepay_enabled:
            return self._sepay_qr_url() or ''
        return super()._get_custom_qr_code()
