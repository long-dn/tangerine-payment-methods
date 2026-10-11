from unittest.mock import patch

import requests

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger


class _FakeResponse:
    def __init__(self, data):
        self._data = data
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {'data': self._data}


@tagged('post_install', '-at_install')
class TestBankInfo(TransactionCase):

    def test_payload_mapping(self):
        payload = self.env['vn.bank']._payload_bank({
            'name': 'Vietcombank',
            'bin': '970436',
            'shortName': 'VCB',
            'logo': 'http://example/vcb.png',
            'code': 'VCB',
            'swift_code': 'BFTVVNVX',
        })
        self.assertEqual(payload['short_name'], 'VCB')
        self.assertEqual(payload['bic'], 'BFTVVNVX')
        self.assertEqual(payload['code'], 'VCB')

    def test_display_name_with_code(self):
        bank = self.env['vn.bank'].create({'name': 'My Bank', 'code': 'MBX'})
        self.assertEqual(bank.display_name, '[MBX] My Bank')

    def test_display_name_without_code(self):
        bank = self.env['vn.bank'].create({'name': 'BNP Paribas'})
        self.assertEqual(bank.display_name, 'BNP Paribas')

    def test_search_display_name_by_code(self):
        # m2o autocomplete must find a bank by its VietQR code even when
        # neither the name nor the BIC contains the term.
        bank = self.env['vn.bank'].create({
            'name': 'Foreign Trade Bank',
            'code': 'VCBZ',
            'bic': 'BFTVVNVX',
        })
        ids = [r[0] for r in self.env['vn.bank'].name_search('vcbz')]
        self.assertIn(bank.id, ids)

    def test_search_display_name_by_bic_substring(self):
        bank = self.env['vn.bank'].create({'name': 'Some Bank', 'bic': 'ZZBFTVVN'})
        ids = [r[0] for r in self.env['vn.bank'].name_search('bftv')]
        self.assertIn(bank.id, ids)

    def test_bank_account_takes_name_and_bic_from_the_directory(self):
        bank = self.env['vn.bank'].create({
            'name': 'Vietcombank', 'code': 'VCBY', 'bin': '970436', 'bic': 'BFTVVNVX',
        })
        account = self.env['res.partner.bank'].create({
            'account_number': '0011002233',
            'partner_id': self.env.ref('base.main_partner').id,
            'vn_bank_id': bank.id,
        })
        self.assertEqual(account.bank_name, 'Vietcombank')
        self.assertEqual(account.bank_bic, 'BFTVVNVX')

    def test_bank_account_without_directory_bank_keeps_its_values(self):
        account = self.env['res.partner.bank'].create({
            'account_number': '0011002244',
            'partner_id': self.env.ref('base.main_partner').id,
            'bank_name': 'Foreign Bank',
            'bank_bic': 'FOREIGNXX',
        })
        self.assertEqual(account.bank_name, 'Foreign Bank')
        self.assertEqual(account.bank_bic, 'FOREIGNXX')

    def test_sync_creates_then_updates(self):
        first = [{
            'name': 'Bank One', 'bin': '111', 'shortName': 'B1',
            'logo': 'l1', 'code': 'BANK1', 'swift_code': 'B1SWIFT',
        }]
        with patch.object(requests, 'get', return_value=_FakeResponse(first)):
            self.env['vn.bank'].bank_information_sync()
        bank = self.env['vn.bank'].search([('code', '=', 'BANK1')])
        self.assertEqual(len(bank), 1)
        self.assertEqual(bank.bic, 'B1SWIFT')
        self.assertEqual(bank.short_name, 'B1')

        updated = [{
            'name': 'Bank One Renamed', 'bin': '111', 'shortName': 'B1',
            'logo': 'l1', 'code': 'BANK1', 'swift_code': 'B1SWIFT',
        }]
        with patch.object(requests, 'get', return_value=_FakeResponse(updated)):
            self.env['vn.bank'].bank_information_sync()
        bank = self.env['vn.bank'].search([('code', '=', 'BANK1')])
        self.assertEqual(len(bank), 1, "Sync must upsert, not duplicate")
        self.assertEqual(bank.name, 'Bank One Renamed')

    @mute_logger('odoo.addons.tangerine_bank_sync.models.vn_bank')
    def test_sync_network_error_raises_usererror(self):
        with patch.object(requests, 'get', side_effect=requests.RequestException('boom')):
            with self.assertRaises(UserError):
                self.env['vn.bank'].bank_information_sync()
