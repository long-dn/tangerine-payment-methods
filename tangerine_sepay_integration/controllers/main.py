import hashlib
import hmac
import json
import logging
import time

from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request
from odoo.tools import config

_logger = logging.getLogger(__name__)

MAX_WEBHOOK_BODY_BYTES = 64 * 1024
MAX_TIMESTAMP_DRIFT_SECONDS = 5 * 60
WEBHOOK_SECRET_OPTION = 'sepay_webhook_secret'
# SePay signs the form-encoded string instead of a JSON document when the
# webhook is configured with either of these content types.
FORM_CONTENT_TYPES = ('application/x-www-form-urlencoded', 'multipart/form-data')


class SePayWebhookController(http.Controller):
    @staticmethod
    def _configured_secrets():
        """Return every SePay webhook secret this deployment knows about."""
        return [
            value
            for key, value in config.options.items()
            if value
            and (
                key == WEBHOOK_SECRET_OPTION
                or key.startswith(f'{WEBHOOK_SECRET_OPTION}_')
            )
        ]

    @staticmethod
    def _verify_signature(raw_body, signature_header, timestamp_header, secret):
        if not secret or not signature_header or not timestamp_header:
            return False
        try:
            timestamp = int(timestamp_header)
        except (TypeError, ValueError):
            return False
        if abs(int(time.time()) - timestamp) > MAX_TIMESTAMP_DRIFT_SECONDS:
            return False
        prefix = 'sha256='
        if not signature_header.startswith(prefix):
            return False
        supplied_signature = signature_header[len(prefix) :]
        expected_signature = hmac.new(
            secret.encode(),
            timestamp_header.encode() + b'.' + raw_body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(supplied_signature, expected_signature)

    @classmethod
    def _matches_any_secret(cls, raw_body, signature_header, timestamp_header, secrets):
        return any(
            cls._verify_signature(raw_body, signature_header, timestamp_header, secret)
            for secret in secrets
        )

    # The second URL is the one version 1.x published. It is kept so that a
    # SePay webhook configured for it keeps working once the HMAC secret is
    # set, without having to edit the webhook URL at the same time.
    @http.route(
        ['/webhook/sepay/v1/transactions', '/webhook/v1/payment/sepay'],
        type='http',
        auth='public',
        methods=['POST'],
        csrf=False,
        save_session=False,
    )
    def sepay_transaction_webhook(self):
        announced_length = request.httprequest.content_length
        if announced_length and announced_length > MAX_WEBHOOK_BODY_BYTES:
            return request.make_json_response(
                {'success': False, 'error': 'Payload too large'},
                status=413,
            )
        raw_body = request.httprequest.get_data(cache=True, as_text=False)
        if len(raw_body) > MAX_WEBHOOK_BODY_BYTES:
            return request.make_json_response(
                {'success': False, 'error': 'Payload too large'},
                status=413,
            )

        signature = request.httprequest.headers.get('X-SePay-Signature')
        timestamp = request.httprequest.headers.get('X-SePay-Timestamp')
        # First gate: the request carries a signature made with a secret this
        # deployment holds. It says the caller is SePay, not yet which company
        # the transfer belongs to.
        if not self._matches_any_secret(
            raw_body,
            signature,
            timestamp,
            self._configured_secrets(),
        ):
            _logger.warning('Rejected a SePay webhook with invalid authentication.')
            return request.make_json_response(
                {'success': False, 'error': 'Invalid signature'},
                status=401,
            )

        if (request.httprequest.mimetype or '').lower() in FORM_CONTENT_TYPES:
            return request.make_json_response(
                {
                    'success': False,
                    'error': 'Configure the SePay webhook to send JSON',
                },
                status=415,
            )

        try:
            payload = json.loads(raw_body)
        except (TypeError, ValueError, UnicodeDecodeError):
            return request.make_json_response(
                {'success': False, 'error': 'Invalid JSON payload'},
                status=400,
            )
        if not isinstance(payload, dict):
            return request.make_json_response(
                {'success': False, 'error': 'Invalid payload'},
                status=400,
            )

        transactions = request.env['sepay.transaction'].sudo()
        try:
            company = transactions._company_for_account(payload.get('accountNumber'))
        except ValidationError as error:
            _logger.warning('Rejected a SePay webhook: %s', error)
            return request.make_json_response(
                {'success': False, 'error': str(error)},
                status=422,
            )

        # Second gate: the signature must be the one of the company that owns
        # the receiving account, so a secret issued for one company cannot be
        # used to post transfers into another.
        if not self._verify_signature(
            raw_body,
            signature,
            timestamp,
            company._sepay_webhook_secret(),
        ):
            _logger.warning(
                'Rejected a SePay webhook signed with another company secret.',
            )
            return request.make_json_response(
                {'success': False, 'error': 'Invalid signature'},
                status=401,
            )

        try:
            transaction = transactions._ingest_webhook(payload, raw_body, company)
        except ValidationError as error:
            _logger.warning('Rejected a SePay webhook: %s', error)
            return request.make_json_response(
                {'success': False, 'error': str(error)},
                status=422,
            )

        # The route answers as the public user, who has no read access at all
        # on ir.cron, so the recordset is elevated before its state is read.
        cron = request.env.ref(
            'tangerine_sepay_integration.ir_cron_process_transactions',
            raise_if_not_found=False,
        )
        if cron:
            cron = cron.sudo()
            if cron.active:
                cron._trigger()
        _logger.info('Accepted SePay webhook transaction %s.', transaction.id)
        return request.make_json_response({'success': True})
