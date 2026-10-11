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
        payload = self.env['res.bank']._payload_bank({
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
        bank = self.env['res.bank'].create({'name': 'My Bank', 'code': 'MBX'})
        self.assertEqual(bank.display_name, '[MBX] My Bank')

    def test_display_name_without_code(self):
        bank = self.env['res.bank'].create({'name': 'BNP Paribas'})
        self.assertEqual(bank.display_name, 'BNP Paribas')

    def test_search_display_name_by_code(self):
        # Regression: m2o autocomplete must find a bank by its VietQR code even
        # when neither the name nor the BIC contains the term. Core res.bank's
        # _search_display_name matches only bic (prefix) + name and ignores code.
        bank = self.env['res.bank'].create({
            'name': 'Foreign Trade Bank',
            'code': 'VCB',
            'bic': 'BFTVVNVX',
        })
        ids = [r[0] for r in self.env['res.bank'].name_search('vcb')]
        self.assertIn(bank.id, ids)

    def test_search_display_name_by_bic_substring(self):
        # BIC is matched as a substring (core only matches a BIC prefix).
        bank = self.env['res.bank'].create({'name': 'Some Bank', 'bic': 'ZZBFTVVN'})
        ids = [r[0] for r in self.env['res.bank'].name_search('bftv')]
        self.assertIn(bank.id, ids)

    def test_sync_creates_then_updates(self):
        first = [{
            'name': 'Bank One', 'bin': '111', 'shortName': 'B1',
            'logo': 'l1', 'code': 'BANK1', 'swift_code': 'B1SWIFT',
        }]
        with patch.object(requests, 'get', return_value=_FakeResponse(first)):
            self.env['res.bank'].bank_information_sync()
        bank = self.env['res.bank'].search([('code', '=', 'BANK1')])
        self.assertEqual(len(bank), 1)
        self.assertEqual(bank.bic, 'B1SWIFT')
        self.assertEqual(bank.short_name, 'B1')

        updated = [{
            'name': 'Bank One Renamed', 'bin': '111', 'shortName': 'B1',
            'logo': 'l1', 'code': 'BANK1', 'swift_code': 'B1SWIFT',
        }]
        with patch.object(requests, 'get', return_value=_FakeResponse(updated)):
            self.env['res.bank'].bank_information_sync()
        bank = self.env['res.bank'].search([('code', '=', 'BANK1')])
        self.assertEqual(len(bank), 1, "Sync must upsert, not duplicate")
        self.assertEqual(bank.name, 'Bank One Renamed')

    @mute_logger('odoo.addons.tangerine_bank_sync.models.res_bank')
    def test_sync_network_error_raises_usererror(self):
        with patch.object(requests, 'get', side_effect=requests.RequestException('boom')):
            with self.assertRaises(UserError):
                self.env['res.bank'].bank_information_sync()
