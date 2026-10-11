import hashlib
import logging
from urllib.parse import urlencode

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.image import image_data_uri

_logger = logging.getLogger(__name__)

SEPAY_QR_METHOD = 'sepay_qr'
# SePay's documented QR endpoint. `https://vietqr.app/img` is the same service
# under SePay's other brand and answers identically.
SEPAY_QR_ENDPOINT = 'https://qr.sepay.vn/img'
SEPAY_QR_DOWNLOAD_TIMEOUT = (5, 10)


class ResPartnerBank(models.Model):
    _inherit = 'res.partner.bank'

    sepay_va_number = fields.Char(
        string='SePay Virtual Account',
        help='Virtual account (VA) number SePay shows for this bank account. '
        'Money sent to it lands on the main account.',
    )
    sepay_use_va = fields.Boolean(
        string='Use Virtual Account in QR',
        help='Encode the virtual account instead of the main account number in '
        'SePay QR codes. Needed for banks that only report transfers made to a '
        'virtual account, such as BIDV, MSB, KienlongBank, OCB or a Vietcombank '
        'business account connected through the bank API.',
    )

    @api.constrains('sepay_use_va', 'sepay_va_number')
    def _check_sepay_va_number(self):
        for account in self:
            if account.sepay_use_va and not account._sepay_va_number():
                raise ValidationError(
                    _(
                        'Enter the SePay virtual account number of %(account)s '
                        'before using it in QR codes.',
                        account=account.display_name,
                    ),
                )

    @api.private
    def _sepay_va_number(self):
        self.ensure_one()
        return ''.join(
            char for char in (self.sepay_va_number or '') if char.isalnum()
        )

    @api.private
    def _sepay_qr_account_number(self):
        """Return the account number customers must transfer to."""
        self.ensure_one()
        if self.sepay_use_va:
            return self._sepay_va_number()
        return ''.join(char for char in self.acc_number if char.isalnum())

    @api.model
    def _get_available_qr_methods(self):
        return [
            *super()._get_available_qr_methods(),
            (SEPAY_QR_METHOD, _('SePay - VietQR'), 1),
        ]

    def _get_error_messages_for_qr(self, qr_method, debtor_partner, currency):
        if qr_method != SEPAY_QR_METHOD:
            return super()._get_error_messages_for_qr(
                qr_method,
                debtor_partner,
                currency,
            )
        if currency.name != 'VND':
            return _('SePay QR codes only support VND.')
        company = self.company_id or self.journal_id[:1].company_id
        if not company and self.partner_id:
            companies = self.env['res.company'].search(
                [
                    ('partner_id', '=', self.partner_id.id),
                    ('sepay_enabled', '=', True),
                ],
            )
            company = companies if len(companies) == 1 else self.env['res.company']
        if not company or not company.sepay_enabled:
            return _('SePay is not enabled for the recipient bank account company.')
        if not self.acc_number:
            return _('The recipient bank account number is missing.')
        if not self.bank_id:
            return _('The recipient bank is missing.')
        if not (self.bank_id.short_name or self.bank_id.code or self.bank_id.bin):
            return _(
                'The recipient bank has no SePay-compatible short name, code, or BIN.',
            )
        return None

    @api.private
    def _sepay_qr_company(self):
        """Return the company the SePay settings must be read from."""
        self.ensure_one()
        company = self.company_id or self.journal_id[:1].company_id
        if company:
            return company
        companies = self.env['res.company'].search(
            [
                ('partner_id', '=', self.partner_id.id),
                ('sepay_enabled', '=', True),
            ],
        )
        return companies if len(companies) == 1 else self.env['res.company']

    @api.private
    def _sepay_qr_url(
        self,
        amount,
        communication,
        currency,
        template='compact',
        silent_errors=True,
    ):
        if not self:
            return None
        self.ensure_one()
        error = self._get_error_messages_for_qr(SEPAY_QR_METHOD, False, currency)
        if amount <= 0:
            error = _('The SePay QR amount must be greater than zero.')
        if error:
            if silent_errors:
                return None
            raise UserError(error)
        bank_code = self.bank_id.short_name or self.bank_id.code or self.bank_id.bin
        params = {
            'acc': self._sepay_qr_account_number(),
            'bank': bank_code,
            'amount': int(currency.round(amount)),
            'des': communication or '',
            'template': template,
        }
        if self.acc_holder_name:
            params['holder'] = self.acc_holder_name
        remote_url = f'{SEPAY_QR_ENDPOINT}?{urlencode(params)}'
        if not self._sepay_qr_company().sepay_cache_qr_images:
            return remote_url
        return self._sepay_cached_qr_image(remote_url) or remote_url

    @api.private
    def _sepay_cached_qr_image(self, remote_url):
        """Return `remote_url` as a stored data URI, or None if unavailable.

        Printing an invoice or opening the portal otherwise has to reach SePay
        for every render, and a slow or unreachable service silently drops the
        QR code from the document. Each distinct QR is downloaded once and kept
        as an attachment of this bank account.
        """
        self.ensure_one()
        digest = hashlib.sha256(remote_url.encode()).hexdigest()
        name = f'sepay_qr_{digest}.png'
        attachments = self.env['ir.attachment'].sudo()
        attachment = attachments.search(
            [
                ('res_model', '=', self._name),
                ('res_id', '=', self.id),
                ('name', '=', name),
            ],
            limit=1,
        )
        if attachment:
            return image_data_uri(attachment.datas)
        try:
            response = requests.get(remote_url, timeout=SEPAY_QR_DOWNLOAD_TIMEOUT)
            response.raise_for_status()
        except requests.exceptions.RequestException:
            _logger.warning(
                'Could not download the SePay QR image for bank account %s.',
                self.id,
                exc_info=True,
            )
            return None
        if not response.content or not response.headers.get(
            'Content-Type',
            '',
        ).startswith('image/'):
            _logger.warning('SePay returned a non-image response for a QR code.')
            return None
        attachment = attachments.create(
            {
                'name': name,
                'res_model': self._name,
                'res_id': self.id,
                'mimetype': 'image/png',
                'raw': response.content,
            },
        )
        return image_data_uri(attachment.datas)

    def _get_qr_code_url(
        self,
        qr_method,
        amount,
        currency,
        debtor_partner,
        free_communication,
        structured_communication,
    ):
        if qr_method == SEPAY_QR_METHOD:
            return self._sepay_qr_url(
                amount,
                structured_communication or free_communication,
                currency,
            )
        return super()._get_qr_code_url(
            qr_method,
            amount,
            currency,
            debtor_partner,
            free_communication,
            structured_communication,
        )

    def _get_qr_code_base64(
        self,
        qr_method,
        amount,
        currency,
        debtor_partner,
        free_communication,
        structured_communication,
    ):
        if qr_method == SEPAY_QR_METHOD:
            return self._sepay_qr_url(
                amount,
                structured_communication or free_communication,
                currency,
            )
        return super()._get_qr_code_base64(
            qr_method,
            amount,
            currency,
            debtor_partner,
            free_communication,
            structured_communication,
        )
