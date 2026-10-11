"""Move the Vietnamese bank directory from `res.bank` to `vn.bank`.

Odoo 20 removed `res.bank`; bank accounts now carry their own bank name and
BIC. Up to 19.0 this module stored the VietQR directory (code, BIN, short
name, logo) on `res.bank` and bank accounts pointed to it through `bank_id`.

When the old table survived the upgrade, its rows and the links of the bank
accounts are copied over. Otherwise the directory is downloaded again and
each bank account is linked to the single directory bank whose SWIFT code or
BIN matches the BIC that the upgrade copied onto the account.
"""
import logging

from odoo import SUPERUSER_ID, api
from odoo.tools.sql import column_exists, table_exists

_logger = logging.getLogger(__name__)


def _copy_res_bank(cr):
    cr.execute("""
        INSERT INTO vn_bank (name, short_name, code, bin, bic, logo_url, active,
                             create_uid, create_date, write_uid, write_date)
             SELECT name, short_name, code, bin, bic, logo_url, COALESCE(active, TRUE),
                    %(uid)s, NOW() AT TIME ZONE 'UTC', %(uid)s, NOW() AT TIME ZONE 'UTC'
               FROM res_bank
              WHERE code IS NOT NULL
        ON CONFLICT (code) DO NOTHING
    """, {'uid': SUPERUSER_ID})
    _logger.info('Copied %s banks from res_bank to vn_bank.', cr.rowcount)
    if column_exists(cr, 'res_partner_bank', 'bank_id'):
        cr.execute("""
            UPDATE res_partner_bank account
               SET vn_bank_id = directory.id
              FROM res_bank bank
              JOIN vn_bank directory ON directory.code = bank.code
             WHERE account.bank_id = bank.id
               AND account.vn_bank_id IS NULL
        """)
        _logger.info('Linked %s bank accounts through their former bank.', cr.rowcount)


def _link_by_bic(cr):
    cr.execute("""
        UPDATE res_partner_bank account
           SET vn_bank_id = match.vn_bank_id
          FROM (
                SELECT account.id, MIN(directory.id) AS vn_bank_id
                  FROM res_partner_bank account
                  JOIN vn_bank directory
                    ON UPPER(directory.bic) = UPPER(account.bank_bic)
                    OR directory.bin = account.bank_bic
                 WHERE account.vn_bank_id IS NULL
                   AND COALESCE(account.bank_bic, '') != ''
              GROUP BY account.id
                HAVING COUNT(DISTINCT directory.id) = 1
          ) match
         WHERE account.id = match.id
    """)
    _logger.info('Linked %s bank accounts through their BIC.', cr.rowcount)


def migrate(cr, version):
    if not version or int(version.split('.')[0]) >= 20:
        return
    if table_exists(cr, 'res_bank') and column_exists(cr, 'res_bank', 'code'):
        _copy_res_bank(cr)
    else:
        env = api.Environment(cr, SUPERUSER_ID, {})
        try:
            with cr.savepoint():
                env['vn.bank'].bank_information_sync()
        except Exception:  # noqa: BLE001 - the upgrade must not fail on network errors
            _logger.warning('Could not download the Vietnamese bank directory.', exc_info=True)
    _link_by_bic(cr)
