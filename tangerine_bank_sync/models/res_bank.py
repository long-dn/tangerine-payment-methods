import logging

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

VIETQR_BANKS_URL = 'https://api.vietqr.io/v2/banks'


class ResBank(models.Model):
    _inherit = 'res.bank'
    _rec_names_search = ['name', 'bic', 'code']

    short_name = fields.Char(string='Short Name', help="Abbreviated bank name from the VietQR directory.")
    bin = fields.Char(string='BIN', help="Bank Identification Number used for VietQR / NAPAS routing.")
    code = fields.Char(string='Code', help="Short bank code from the VietQR directory (e.g. VCB, TCB).")
    logo_url = fields.Char(string='Logo URL', help="URL of the bank logo from the VietQR directory.")

    @api.depends('code', 'name')
    def _compute_display_name(self):
        # Surface the bank code in every res.bank selector (journals, partner
        # bank accounts, ...) while keeping a clean label when a bank has no code.
        for bank in self:
            name = bank.name or ''
            bank.display_name = f'[{bank.code}] {name}' if bank.code else name

    @api.model
    def _search_display_name(self, operator, value):
        # Core res.bank overrides _search_display_name to match only bic (as a
        # prefix) and name for `ilike`, ignoring _rec_names_search. Because m2o
        # autocomplete uses `ilike`, a bank could not be found by its VietQR
        # `code` (and bic was only prefix-matched). Extend the ilike path to
        # name/bic/code/short_name as substrings; defer other operators to super.
        if operator in ('ilike', 'not ilike') and value:
            domain = [
                '|', '|', '|',
                ('name', 'ilike', value),
                ('bic', 'ilike', value),
                ('code', 'ilike', value),
                ('short_name', 'ilike', value),
            ]
            if operator == 'not ilike':
                domain = ['!', *domain]
            return domain
        return super()._search_display_name(operator, value)

    @staticmethod
    def _payload_bank(record):
        return {
            'name': record.get('name'),
            'bin': record.get('bin'),
            'short_name': record.get('shortName'),
            'logo_url': record.get('logo'),
            'code': record.get('code'),
            'bic': record.get('swift_code'),
        }

    @api.model
    def bank_information_sync(self):
        """Synchronize Vietnamese banks from the VietQR public API."""
        try:
            response = requests.get(VIETQR_BANKS_URL, timeout=30)
            response.raise_for_status()
        except requests.RequestException as error:
            _logger.warning("VietQR bank synchronization failed: %s", error)
            raise UserError(_("Could not synchronize bank information: %s", error)) from error
        to_create = []
        for record in response.json().get('data', []):
            bank = self.search([('code', '=', record.get('code'))], limit=1)
            if bank:
                bank.write(self._payload_bank(record))
            else:
                to_create.append(self._payload_bank(record))
        if to_create:
            self.create(to_create)
