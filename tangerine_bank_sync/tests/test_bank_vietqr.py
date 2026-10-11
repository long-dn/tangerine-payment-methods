from odoo.tests import TransactionCase, tagged

# Payload produced for a 1,500,000 VND transfer to Vietcombank (BIN 970436),
# account 1234567890, reference INV/2026/0001.
QR_WITH_BIN = (
    '00020101021238540010A00000072701240006970436011012345678900208QRIBFTTA'
    '520400005303704540715000005802VN5908ABC Food6011Ho Chi Minh'
    '62150811INV20260001630406A9'
)
# Same payload built by upstream l10n_vn, which puts the SWIFT code where the
# NAPAS BIN belongs - banking apps cannot route it.
QR_WITH_BIC = (
    '00020101021238560010A00000072701260008BFTVVNVX011012345678900208QRIBFTTA'
    '520400005303704540715000005802VN5908ABC Food6011Ho Chi Minh'
    '62150811INV202600016304E7A0'
)


@tagged('post_install', '-at_install')
class TestBankVietQr(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bank = cls.env['res.bank'].create({
            'name': 'Vietcombank',
            'code': 'VCB',
            'bin': '970436',
            'bic': 'BFTVVNVX',
        })
        cls.partner = cls.env['res.partner'].create({
            'name': 'ABC Food',
            'city': 'Ho Chi Minh',
            'country_id': cls.env.ref('base.vn').id,
        })
        cls.bank_account = cls.env['res.partner.bank'].create({
            'acc_number': '1234567890',
            'bank_id': cls.bank.id,
            'partner_id': cls.partner.id,
            'proxy_type': 'bank_acc',
            'proxy_value': '1234567890',
            'include_reference': True,
        })
        cls.vnd = cls.env.ref('base.VND')
        cls.usd = cls.env.ref('base.USD')

    def _qr_vals(self):
        return self.bank_account._get_qr_vals(
            'emv_qr', 1500000.0, self.vnd, self.partner, 'INV/2026/0001', None,
        )

    def test_qr_carries_napas_bin(self):
        self.assertEqual(self._qr_vals(), QR_WITH_BIN)

    def test_qr_falls_back_to_bic_without_bin(self):
        self.bank.bin = False
        self.assertEqual(self._qr_vals(), QR_WITH_BIC)

    def test_qr_allowed_without_bic(self):
        self.bank.bic = False
        self.assertFalse(self.bank_account._get_error_messages_for_qr('emv_qr', self.partner, self.vnd))
        self.assertEqual(self._qr_vals(), QR_WITH_BIN)

    def test_qr_rejects_currency_other_than_vnd(self):
        self.assertTrue(self.bank_account._get_error_messages_for_qr('emv_qr', self.partner, self.usd))

    def test_qr_left_to_upstream_without_proxy_type(self):
        self.bank_account.proxy_type = 'none'
        self.assertEqual(self.bank_account._get_merchant_account_info(), (None, None))
