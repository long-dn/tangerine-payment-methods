"""Keep SePay QR codes on companies that used the 1.x generation of the module.

Version 1.x printed a SePay QR on the invoices of every Vietnamese company,
with no per-company switch. From 2.0 SePay is enabled company by company in
the Accounting settings, so without this step an upgrade would silently drop
the QR code from their invoices and payment pages.

Only an upgrade from 1.x is concerned: a database already on 2.x keeps the
choices made in its settings.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version or version.split('.')[2] != '1':
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    companies = env['res.company'].search([
        ('country_id.code', '=', 'VN'),
        ('sepay_enabled', '=', False),
    ])
    if companies:
        companies.sepay_enabled = True
        _logger.info('Enabled SePay on companies %s after upgrading from %s.', companies.ids, version)
