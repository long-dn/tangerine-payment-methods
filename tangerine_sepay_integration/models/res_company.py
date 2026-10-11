import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import config

_logger = logging.getLogger(__name__)

SEPAY_API_ENDPOINTS = {
    'production': 'https://userapi.sepay.vn/v2/transactions',
    'sandbox': 'https://userapi-sandbox.sepay.vn/v2/transactions',
}
VIETNAM_TIMEZONE = ZoneInfo('Asia/Ho_Chi_Minh')
MAX_REPORTED_SYNC_FAILURES = 20


class ResCompany(models.Model):
    _inherit = 'res.company'

    sepay_enabled = fields.Boolean(
        string='Enable SePay',
        help='Use SePay QR codes and accept authenticated SePay transactions for this company.',
    )
    sepay_environment = fields.Selection(
        selection=[
            ('production', 'Production'),
            ('sandbox', 'Sandbox'),
        ],
        string='SePay Environment',
        required=True,
        default='production',
    )
    sepay_sync_lookback_days = fields.Integer(
        string='Initial Lookback Days',
        required=True,
        default=2,
        help='Number of days fetched on the first API synchronization.',
    )
    sepay_activity_user_id = fields.Many2one(
        comodel_name='res.users',
        string='SePay Review Responsible',
        domain=[('share', '=', False)],
        help='User who gets an activity whenever a SePay transaction needs '
        'accounting review or fails to process. Leave empty to rely on the '
        'SePay Transactions screen alone.',
    )
    sepay_match_content = fields.Boolean(
        string='Match the Transfer Content',
        default=True,
        help='When SePay sends no payment code, look the document reference up '
        'inside the free-text transfer content. A document is only accepted '
        'when its whole reference appears there and it is the only match.',
    )
    sepay_cache_qr_images = fields.Boolean(
        string='Store SePay QR Images',
        help='Download each SePay QR code once and keep it as an attachment, '
        'so printing an invoice or opening the portal no longer depends on '
        'reaching SePay. Falls back to the SePay image URL when the download '
        'fails.',
    )
    sepay_last_api_id = fields.Char(
        string='Last SePay API Transaction',
        readonly=True,
        copy=False,
    )
    sepay_last_sync_at = fields.Datetime(
        string='Last SePay Synchronization',
        readonly=True,
        copy=False,
    )
    sepay_last_sync_error = fields.Text(
        string='Last SePay Synchronization Error',
        readonly=True,
        copy=False,
    )

    _sql_constraints = [
        (
            'sepay_lookback_positive',
            'CHECK(sepay_sync_lookback_days BETWEEN 1 AND 31)',
            'The initial SePay lookback must be between 1 and 31 days.',
        ),
    ]

    def _sepay_check_manager(self):
        if not self.env.user.has_group('account.group_account_manager'):
            raise AccessError(
                _('Only accounting managers can operate SePay synchronization.'),
            )

    @api.private
    def _sepay_api_token(self):
        self.ensure_one()
        return config.get(f'sepay_api_token_{self.id}') or config.get(
            'sepay_api_token',
        )

    @api.private
    def _sepay_webhook_secret(self):
        """Return the HMAC secret SePay signs this company's webhooks with."""
        self.ensure_one()
        return config.get(f'sepay_webhook_secret_{self.id}') or config.get(
            'sepay_webhook_secret',
        )

    @api.private
    def _sepay_request_page(self, params):
        self.ensure_one()
        token = self._sepay_api_token()
        if not token:
            raise UserError(
                _(
                    'No SePay API token is configured. Set sepay_api_token or '
                    'sepay_api_token_%(company_id)s in odoo.conf.',
                    company_id=self.id,
                ),
            )
        response = requests.get(
            SEPAY_API_ENDPOINTS[self.sepay_environment],
            headers={
                'Accept': 'application/json',
                'Authorization': f'Bearer {token}',
            },
            params=params,
            timeout=(5, 30),
        )
        response.raise_for_status()
        try:
            result = response.json()
        except requests.exceptions.JSONDecodeError as error:
            raise ValidationError(
                _('SePay returned an invalid JSON response.'),
            ) from error
        if result.get('status') != 'success' or not isinstance(
            result.get('data'),
            list,
        ):
            raise ValidationError(_('SePay returned an unexpected API response.'))
        return result

    @api.private
    def _sepay_sync_transactions(self):
        self.ensure_one()
        if not self.sepay_enabled:
            return self.env['sepay.transaction']

        params = {
            'page': 1,
            'per_page': 100,
            'timestamp_format': 'iso8601',
            'transaction_date_sort': 'asc',
        }
        if self.sepay_last_api_id:
            params['since_id'] = self.sepay_last_api_id
        else:
            start = datetime.now(VIETNAM_TIMEZONE) - timedelta(
                days=self.sepay_sync_lookback_days,
            )
            params['transaction_date_from'] = start.strftime('%Y-%m-%d %H:%M:%S')

        transactions = self.env['sepay.transaction']
        failed_ids = []
        last_api_id = False
        page_count = 0
        while True:
            page_count += 1
            if page_count > 1000:
                raise ValidationError(
                    _('SePay API pagination exceeded the safety limit.'),
                )
            result = self._sepay_request_page(params)
            for payload in result['data']:
                payload_id = payload.get('id')
                try:
                    with self.env.cr.savepoint():
                        transactions |= transactions._ingest_api(self, payload)
                except Exception:  # noqa: BLE001 - one bad payload must not stall the cursor
                    _logger.exception(
                        'Skipping SePay API transaction %s of company %s.',
                        payload_id,
                        self.id,
                    )
                    failed_ids.append(str(payload_id))
                # The cursor advances even for a payload we could not import,
                # otherwise every later run would stop on the same record.
                last_api_id = payload_id or last_api_id
            pagination = result.get('meta', {}).get('pagination', {})
            if not pagination.get('has_more'):
                break
            if not result['data']:
                raise ValidationError(
                    _('SePay reported another page but returned no transactions.'),
                )
            params['page'] += 1

        values = {
            'sepay_last_sync_at': fields.Datetime.now(),
            'sepay_last_sync_error': self._sepay_sync_report(failed_ids),
        }
        if last_api_id:
            values['sepay_last_api_id'] = last_api_id
        self.write(values)
        return transactions

    @api.private
    def _sepay_sync_report(self, failed_ids):
        if not failed_ids:
            return False
        return _(
            '%(count)s SePay transaction(s) could not be imported: %(references)s.',
            count=len(failed_ids),
            references=', '.join(failed_ids[:MAX_REPORTED_SYNC_FAILURES]),
        )

    def action_sepay_sync_now(self):
        self.ensure_one()
        self._sepay_check_manager()
        transactions = self.sudo()._sepay_sync_transactions()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('SePay synchronization completed'),
                'message': _(
                    '%(count)s transaction(s) were received.',
                    count=len(transactions),
                ),
                'type': 'success',
            },
        }

    @api.model
    def _cron_sepay_sync_transactions(self):
        # A webhook-only installation has no API token on purpose. Polling it
        # would raise on every run and park a misleading error on the settings
        # screen, so those companies are skipped instead of reported.
        companies = self.sudo().search([('sepay_enabled', '=', True)]).filtered(
            lambda company: company._sepay_api_token(),
        )
        for company in companies:
            try:
                with self.env.cr.savepoint():
                    company._sepay_sync_transactions()
            except Exception as error:  # noqa: BLE001 - isolate one company from the cron batch
                _logger.exception(
                    'SePay API synchronization failed for company %s.',
                    company.id,
                )
                company.sepay_last_sync_error = str(error)[:2000]
