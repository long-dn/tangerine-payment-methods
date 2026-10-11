from odoo import api, models


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    @api.private
    def _sepay_qr_url(self):
        self.ensure_one()
        if (
            not self.company_id.sepay_enabled
            or self.provider_code != 'custom'
            or not self.provider_id.journal_id.bank_account_id
        ):
            return None
        return self.provider_id.journal_id.bank_account_id._sepay_qr_url(
            self.amount,
            self._get_communication(),
            self.currency_id,
        )
