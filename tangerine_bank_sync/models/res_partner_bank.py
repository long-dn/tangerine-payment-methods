from odoo import _, api, fields, models

# NAPAS Application Identifier carried by every VietQR payload.
NAPAS_AID = 'A000000727'

# Service codes l10n_vn maps each proxy type to inside the merchant account
# information block.
PROXY_TYPE_SERVICE_CODES = {
    'merchant_id': 'QRPUSH',
    'payment_service': 'QRPUSH',
    'atm_card': 'QRIBFTTC',
    'bank_acc': 'QRIBFTTA',
}


class ResPartnerBank(models.Model):
    _inherit = 'res.partner.bank'

    vn_bank_id = fields.Many2one(
        comodel_name='vn.bank',
        string='Vietnamese Bank',
        index='btree_not_null',
        help="Bank of the VietQR directory. It fills the bank name and BIC, and "
             "provides the NAPAS BIN used in VietQR payment codes.",
    )
    bank_name = fields.Char(
        compute='_compute_bank_from_vn_bank',
        store=True,
        readonly=False,
        precompute=True,
    )
    bank_bic = fields.Char(
        compute='_compute_bank_from_vn_bank',
        store=True,
        readonly=False,
        precompute=True,
    )

    @api.depends('vn_bank_id')
    def _compute_bank_from_vn_bank(self):
        for account in self:
            bank = account.vn_bank_id
            account.bank_name = bank.name or account.bank_name
            account.bank_bic = bank.bic or account.bank_bic

    def _get_merchant_account_info(self):
        # l10n_vn feeds `bank_bic` into the VietQR payment network identifier
        # because upstream expects the NAPAS Bank Identification Number in that
        # field. This module keeps the real SWIFT code in `bank_bic` and takes
        # the BIN from the VietQR directory, so rebuild the block with the BIN.
        # Accounts without a directory bank keep the upstream behaviour.
        if (
            self.country_code != 'VN'
            or self.proxy_type not in PROXY_TYPE_SERVICE_CODES
            or not self.vn_bank_id.bin
        ):
            return super()._get_merchant_account_info()
        payment_network = [
            (0, self.vn_bank_id.bin),
            (1, self.proxy_value),
        ]
        vals = [
            (0, NAPAS_AID),
            (1, ''.join(self._serialize(*val) for val in payment_network)),
            (2, PROXY_TYPE_SERVICE_CODES[self.proxy_type]),
        ]
        return (38, ''.join(self._serialize(*val) for val in vals))

    def _get_error_messages_for_qr(self, qr_method, debtor_partner, currency):
        # l10n_vn refuses accounts without a BIC, but VietQR never transports the
        # SWIFT code: the BIN alone identifies the receiving bank.
        if qr_method == 'emv_qr' and self.country_code == 'VN' and self.vn_bank_id.bin:
            if currency.name != 'VND':
                return _("Can't generate a Vietnamese QR banking code with a currency other than VND.")
            return None
        return super()._get_error_messages_for_qr(qr_method, debtor_partner, currency)
