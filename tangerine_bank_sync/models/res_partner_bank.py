from odoo import _, models

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

    def _get_merchant_account_info(self):
        # l10n_vn feeds res.bank.bic into the VietQR payment network identifier
        # because upstream stores the NAPAS Bank Identification Number in that
        # field. This module keeps the real SWIFT code in `bic` and the BIN in
        # `bin` (both synced from the VietQR directory), so rebuild the block
        # with the BIN. Banks without a BIN keep the upstream behaviour.
        if (
            self.country_code != 'VN'
            or self.proxy_type not in PROXY_TYPE_SERVICE_CODES
            or not self.bank_id.bin
        ):
            return super()._get_merchant_account_info()
        payment_network = [
            (0, self.bank_id.bin),
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
        if qr_method == 'emv_qr' and self.country_code == 'VN' and self.bank_id.bin:
            if currency.name != 'VND':
                return _("Can't generate a Vietnamese QR banking code with a currency other than VND.")
            return None
        return super()._get_error_messages_for_qr(qr_method, debtor_partner, currency)
