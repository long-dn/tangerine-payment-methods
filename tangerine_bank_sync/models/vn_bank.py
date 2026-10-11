import logging

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

VIETQR_BANKS_URL = 'https://api.vietqr.io/v2/banks'


class VnBank(models.Model):
    """Directory of the Vietnamese banks published by VietQR.

    Odoo 20 dropped `res.bank`: a bank account now carries its own bank name
    and BIC. VietQR still needs the NAPAS BIN of the receiving bank, and
    SePay its short name, so the directory lives in this model and a bank
    account points to it through `res.partner.bank.vn_bank_id`.
    """
    _name = 'vn.bank'
    _description = 'Vietnamese Bank'
    _order = 'name, id'
    _rec_names_search = ('name', 'short_name', 'code', 'bic', 'bin')

    name = fields.Char(string='Name', required=True)
    short_name = fields.Char(string='Short Name', help="Abbreviated bank name from the VietQR directory.")
    bin = fields.Char(string='BIN', index='btree_not_null', help="Bank Identification Number used for VietQR / NAPAS routing.")
    code = fields.Char(string='Code', index='btree_not_null', help="Short bank code from the VietQR directory (e.g. VCB, TCB).")
    bic = fields.Char(string='BIC/SWIFT', help="SWIFT code of the bank, used for international wires.")
    logo_url = fields.Char(string='Logo URL', help="URL of the bank logo from the VietQR directory.")
    active = fields.Boolean(default=True)

    _code_unique = models.Constraint(
        'UNIQUE(code)',
        'A bank with this code already exists.',
    )

    @api.depends('code', 'name')
    def _compute_display_name(self):
        for bank in self:
            name = bank.name or ''
            bank.display_name = f'[{bank.code}] {name}' if bank.code else name

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
        banks = self.with_context(active_test=False)
        for record in response.json().get('data', []):
            bank = banks.search([('code', '=', record.get('code'))], limit=1)
            if bank:
                bank.write(self._payload_bank(record))
            else:
                to_create.append(self._payload_bank(record))
        if to_create:
            self.create(to_create)
