import hashlib
import logging
import unicodedata
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from psycopg2 import IntegrityError

from odoo import Command, _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)

VIETNAM_TIMEZONE = ZoneInfo('Asia/Ho_Chi_Minh')
UTC = timezone.utc

# A reference shorter than this is too generic to be matched safely against the
# free-text transfer content, so only exact lookups are attempted for it.
MIN_MATCHABLE_REFERENCE_LENGTH = 6
# Bound the candidate set of the normalised and content-based lookups. Those
# run in Python, so they must never scan the whole ledger; the exact indexed
# lookups above them stay unbounded.
DOCUMENT_LOOKUP_WINDOW_DAYS = 180
DOCUMENT_LOOKUP_LIMIT = 2000
# How far a manually encoded payment may sit from the transfer date and still be
# recognised as recording the very same transfer.
EXISTING_PAYMENT_WINDOW_DAYS = 7
# Automatic retries of a failed transaction before an accountant has to step in.
MAX_AUTOMATIC_RETRIES = 5
AUTOMATIC_RETRY_DELAY_MINUTES = 5

PROTECTED_FIELDS = {
    'company_id',
    'source',
    'webhook_id',
    'api_id',
    'dedup_key',
    'transaction_date',
    'transaction_date_source',
    'gateway',
    'account_number',
    'sub_account',
    'code',
    'content',
    'transfer_type',
    'amount',
    'accumulated',
    'reference_code',
    'description',
    'raw_webhook_payload',
    'webhook_payload',
    'api_payload',
}


class SePayTransaction(models.Model):
    _name = 'sepay.transaction'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _description = 'SePay Transaction'
    _order = 'transaction_date desc, id desc'
    _check_company_auto = True

    name = fields.Char(
        string='Transaction',
        compute='_compute_name',
        store=True,
    )
    company_id = fields.Many2one(
        comodel_name='res.company',
        string='Company',
        required=True,
        index=True,
        ondelete='restrict',
    )
    state = fields.Selection(
        selection=[
            ('received', 'Received'),
            ('processed', 'Processed'),
            ('unmatched', 'Needs Review'),
            ('error', 'Error'),
            ('ignored', 'Ignored'),
        ],
        string='Status',
        required=True,
        default='received',
        index=True,
        copy=False,
        tracking=True,
    )
    source = fields.Selection(
        selection=[
            ('webhook', 'Webhook'),
            ('api', 'API'),
            ('both', 'Webhook and API'),
        ],
        string='Source',
        required=True,
        index=True,
    )
    webhook_id = fields.Char(string='Webhook ID', index=True, copy=False)
    api_id = fields.Char(string='API ID', index=True, copy=False)
    dedup_key = fields.Char(
        string='Deduplication Key',
        required=True,
        index=True,
        copy=False,
    )
    transaction_date = fields.Datetime(
        string='Transaction Date',
        required=True,
        index=True,
    )
    transaction_date_source = fields.Char(
        string='Source Transaction Date',
        required=True,
    )
    gateway = fields.Char(string='Bank Gateway')
    account_number = fields.Char(string='Account Number', required=True, index=True)
    sub_account = fields.Char(string='Virtual Account')
    code = fields.Char(string='Payment Code', index=True)
    content = fields.Char(string='Transaction Content')
    transfer_type = fields.Selection(
        selection=[
            ('in', 'Incoming'),
            ('out', 'Outgoing'),
        ],
        string='Transfer Type',
        required=True,
        index=True,
    )
    amount = fields.Monetary(
        string='Amount',
        required=True,
        currency_field='currency_id',
    )
    amount_signed = fields.Monetary(
        string='Signed Amount',
        compute='_compute_amount_signed',
        store=True,
        currency_field='currency_id',
    )
    currency_id = fields.Many2one(
        comodel_name='res.currency',
        string='Currency',
        related='company_id.currency_id',
        store=True,
    )
    accumulated = fields.Monetary(
        string='Running Balance',
        currency_field='currency_id',
    )
    reference_code = fields.Char(string='Bank Reference', index=True)
    description = fields.Char(string='Bank Description')
    raw_webhook_payload = fields.Text(
        string='Raw Webhook Payload',
        readonly=True,
        groups='account.group_account_manager',
    )
    webhook_payload = fields.Json(
        string='Webhook Payload',
        readonly=True,
        groups='account.group_account_manager',
    )
    api_payload = fields.Json(
        string='API Payload',
        readonly=True,
        groups='account.group_account_manager',
    )
    journal_id = fields.Many2one(
        comodel_name='account.journal',
        string='Bank Journal',
        check_company=True,
        copy=False,
        readonly=True,
    )
    bank_statement_line_id = fields.Many2one(
        comodel_name='account.bank.statement.line',
        string='Bank Transaction',
        check_company=True,
        copy=False,
        readonly=True,
        ondelete='restrict',
    )
    payment_id = fields.Many2one(
        comodel_name='account.payment',
        string='Payment',
        check_company=True,
        copy=False,
        readonly=True,
        ondelete='restrict',
    )
    payment_transaction_id = fields.Many2one(
        comodel_name='payment.transaction',
        string='Online Payment Transaction',
        check_company=True,
        copy=False,
        readonly=True,
        ondelete='restrict',
    )
    sale_order_id = fields.Many2one(
        comodel_name='sale.order',
        string='Sales Order',
        check_company=True,
        copy=False,
        readonly=True,
        ondelete='restrict',
    )
    invoice_ids = fields.Many2many(
        comodel_name='account.move',
        relation='sepay_transaction_account_move_rel',
        column1='sepay_transaction_id',
        column2='move_id',
        string='Invoices',
        copy=False,
        readonly=True,
        check_company=True,
    )
    error_message = fields.Text(string='Processing Note', readonly=True, copy=False)
    retry_count = fields.Integer(
        string='Automatic Retries',
        default=0,
        readonly=True,
        copy=False,
        help='Number of failed automatic processing attempts since the last '
        'successful one. The scheduled action stops retrying once the limit '
        'is reached, leaving the transaction to an accountant.',
    )
    processed_at = fields.Datetime(string='Processed At', readonly=True, copy=False)

    _company_dedup_unique = models.Constraint(
        'UNIQUE(company_id, dedup_key)',
        'This SePay transaction has already been imported for the company.',
    )
    _company_webhook_unique = models.Constraint(
        'UNIQUE(company_id, webhook_id)',
        'This SePay webhook has already been received for the company.',
    )
    _company_api_unique = models.Constraint(
        'UNIQUE(company_id, api_id)',
        'This SePay API transaction has already been imported for the company.',
    )
    _amount_positive = models.Constraint(
        'CHECK(amount > 0)',
        'The SePay transaction amount must be greater than zero.',
    )

    @api.depends('webhook_id', 'api_id', 'reference_code')
    def _compute_name(self):
        for transaction in self:
            transaction.name = (
                transaction.reference_code
                or transaction.api_id
                or transaction.webhook_id
                or _('SePay Transaction')
            )

    @api.depends('amount', 'transfer_type')
    def _compute_amount_signed(self):
        for transaction in self:
            transaction.amount_signed = (
                transaction.amount
                if transaction.transfer_type == 'in'
                else -transaction.amount
            )

    def write(self, values):
        if PROTECTED_FIELDS.intersection(values) and not self.env.su:
            raise AccessError(_('SePay source transaction data is immutable.'))
        return super().write(values)

    def unlink(self):
        # The access rules already forbid deletion; this guard also covers the
        # privileged code paths and any future relaxation of those rules.
        if self.filtered(
            lambda transaction: (
                transaction.bank_statement_line_id or transaction.payment_id
            ),
        ):
            raise UserError(
                _(
                    'A SePay transaction linked to accounting entries cannot be deleted.',
                ),
            )
        return super().unlink()

    @api.model
    def _sepay_check_manager(self):
        if not self.env.user.has_group('account.group_account_manager'):
            raise AccessError(
                _('Only accounting managers can process SePay transactions.'),
            )

    @api.model
    def _normalise_account_number(self, account_number):
        return ''.join(
            character
            for character in str(account_number or '').upper()
            if character.isalnum()
        )

    @api.model
    def _normalise_reference(self, reference):
        """Return the comparable form of a document reference.

        SePay and the banks fold the transfer content down to unaccented
        uppercase ASCII alphanumerics, so ``INV/2026/00001`` comes back as
        ``INV202600001`` and ``HĐ-2026-0459`` as ``HD20260459``. Both sides of
        every comparison go through the same folding, otherwise a Vietnamese
        document number can never match what the bank reports.

        ``Đ`` is mapped explicitly because, unlike accented vowels, it has no
        canonical decomposition for NFD to peel a combining mark off.
        """
        folded = unicodedata.normalize('NFD', str(reference or '').upper())
        return ''.join(
            character
            for character in folded.replace('Đ', 'D')
            if character.isascii() and character.isalnum()
        )

    @api.model
    def _parse_transaction_date(self, value):
        if not value or not isinstance(value, str):
            raise ValidationError(_('The SePay transaction date is missing.'))
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError as error:
            raise ValidationError(
                _('The SePay transaction date is invalid.'),
            ) from error
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=VIETNAM_TIMEZONE)
        local_date = parsed.astimezone(VIETNAM_TIMEZONE)
        utc_date = parsed.astimezone(UTC).replace(tzinfo=None)
        return utc_date, local_date.strftime('%Y-%m-%d %H:%M:%S')

    @api.model
    def _parse_amount(self, value):
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValidationError(
                _('The SePay transaction amount is invalid.'),
            ) from error
        if amount <= 0 or amount != amount.to_integral_value():
            raise ValidationError(
                _('SePay transaction amounts must be positive integer VND values.'),
            )
        return float(amount)

    @api.model
    def _parse_optional_amount(self, value):
        if value in (None, False, ''):
            return 0.0
        try:
            return float(Decimal(str(value)))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise ValidationError(_('The SePay running balance is invalid.')) from error

    @api.model
    def _deduplication_key(self, values):
        identity = [
            self._normalise_account_number(values['account_number']),
            values['transaction_date_source'],
            values['transfer_type'],
            str(int(values['amount'])),
            (values.get('reference_code') or '').strip().upper(),
        ]
        if not values.get('reference_code'):
            identity.extend(
                [
                    ' '.join((values.get('content') or '').split()).upper(),
                    str(int(values.get('accumulated') or 0)),
                ],
            )
        return hashlib.sha256('|'.join(identity).encode()).hexdigest()

    @api.model
    def _company_for_account(self, account_number):
        normalized = self._normalise_account_number(account_number)
        if not normalized:
            raise ValidationError(_('The SePay account number is missing.'))
        journals = (
            self.env['account.journal']
            .sudo()
            .search(
                [
                    ('type', '=', 'bank'),
                    ('bank_account_id', '!=', False),
                    ('company_id.sepay_enabled', '=', True),
                ],
            )
            .filtered(
                lambda journal: (
                    self._normalise_account_number(journal.bank_account_id.acc_number)
                    == normalized
                ),
            )
        )
        companies = journals.company_id
        if len(companies) != 1:
            raise ValidationError(
                _(
                    'The receiving account does not identify exactly one SePay-enabled company.',
                ),
            )
        return companies

    @api.private
    def _ingest_webhook(self, payload, raw_body, company=None):
        required_fields = {
            'id',
            'transactionDate',
            'accountNumber',
            'transferType',
            'transferAmount',
        }
        if missing_fields := sorted(required_fields.difference(payload)):
            raise ValidationError(
                _(
                    'The SePay webhook is missing required fields: %(fields)s.',
                    fields=', '.join(missing_fields),
                ),
            )
        if not company:
            company = self._company_for_account(payload['accountNumber'])
        values = self._webhook_values(company, payload, raw_body)
        return self._upsert_source(values)

    @api.private
    def _ingest_api(self, company, payload):
        company.ensure_one()
        values = self._api_values(company, payload)
        return self._upsert_source(values)

    @api.model
    def _webhook_values(self, company, payload, raw_body):
        transfer_type = payload.get('transferType')
        if transfer_type not in ('in', 'out'):
            raise ValidationError(_('The SePay transfer type must be in or out.'))
        transaction_date, transaction_date_source = self._parse_transaction_date(
            payload.get('transactionDate'),
        )
        values = {
            'company_id': company.id,
            'source': 'webhook',
            'webhook_id': str(payload['id']),
            'transaction_date': transaction_date,
            'transaction_date_source': transaction_date_source,
            'gateway': payload.get('gateway'),
            'account_number': str(payload.get('accountNumber') or ''),
            'sub_account': payload.get('subAccount'),
            'code': payload.get('code'),
            'content': payload.get('content'),
            'transfer_type': transfer_type,
            'amount': self._parse_amount(payload.get('transferAmount')),
            'accumulated': self._parse_optional_amount(payload.get('accumulated')),
            'reference_code': payload.get('referenceCode'),
            'description': payload.get('description'),
            'raw_webhook_payload': raw_body.decode('utf-8', errors='replace'),
            'webhook_payload': payload,
        }
        values['dedup_key'] = self._deduplication_key(values)
        return values

    @api.model
    def _api_values(self, company, payload):
        required_fields = {'id', 'transaction_date', 'account_number', 'transfer_type'}
        if missing_fields := sorted(required_fields.difference(payload)):
            raise ValidationError(
                _(
                    'The SePay API transaction is missing required fields: %(fields)s.',
                    fields=', '.join(missing_fields),
                ),
            )
        transfer_type = payload.get('transfer_type')
        if transfer_type not in ('in', 'out'):
            raise ValidationError(_('The SePay transfer type must be in or out.'))
        amount_key = 'amount_in' if transfer_type == 'in' else 'amount_out'
        transaction_date, transaction_date_source = self._parse_transaction_date(
            payload.get('transaction_date'),
        )
        values = {
            'company_id': company.id,
            'source': 'api',
            'api_id': str(payload['id']),
            'transaction_date': transaction_date,
            'transaction_date_source': transaction_date_source,
            'gateway': payload.get('bank_brand_name'),
            'account_number': str(payload.get('account_number') or ''),
            'sub_account': payload.get('va'),
            'code': payload.get('code'),
            'content': payload.get('transaction_content'),
            'transfer_type': transfer_type,
            'amount': self._parse_amount(payload.get(amount_key)),
            'accumulated': self._parse_optional_amount(payload.get('accumulated')),
            'reference_code': payload.get('reference_number'),
            'description': payload.get('transaction_content'),
            'api_payload': payload,
        }
        values['dedup_key'] = self._deduplication_key(values)
        return values

    @api.model
    def _upsert_source(self, values):
        company_id = values['company_id']
        domain = [('company_id', '=', company_id)]
        if values.get('webhook_id'):
            source_domain = [*domain, ('webhook_id', '=', values['webhook_id'])]
        else:
            source_domain = [*domain, ('api_id', '=', values['api_id'])]
        transaction = self.sudo().search(source_domain, limit=1)
        if not transaction:
            transaction = self.sudo().search(
                [*domain, ('dedup_key', '=', values['dedup_key'])],
                limit=1,
            )
        if transaction:
            return transaction._merge_source(values)
        try:
            with self.env.cr.savepoint():
                return self.sudo().create(values)
        except IntegrityError:
            transaction = self.sudo().search(source_domain, limit=1)
            if not transaction:
                transaction = self.sudo().search(
                    [*domain, ('dedup_key', '=', values['dedup_key'])],
                    limit=1,
                )
            if not transaction:
                raise
            return transaction._merge_source(values)

    @api.private
    def _merge_source(self, values):
        self.ensure_one()
        updates = {}
        if values['source'] == 'webhook' and not self.webhook_id:
            updates.update(
                {
                    'source': 'both' if self.api_id else 'webhook',
                    'webhook_id': values['webhook_id'],
                    'raw_webhook_payload': values['raw_webhook_payload'],
                    'webhook_payload': values['webhook_payload'],
                },
            )
        elif values['source'] == 'api' and not self.api_id:
            updates.update(
                {
                    'source': 'both' if self.webhook_id else 'api',
                    'api_id': values['api_id'],
                    'api_payload': values['api_payload'],
                },
            )
        if updates:
            self.sudo().write(updates)
        return self

    @api.private
    def _local_date(self):
        self.ensure_one()
        return (
            self.transaction_date.replace(tzinfo=UTC)
            .astimezone(VIETNAM_TIMEZONE)
            .date()
        )

    @api.private
    def _find_bank_journal(self):
        self.ensure_one()
        account_number = self._normalise_account_number(self.account_number)
        journals = (
            self.env['account.journal']
            .sudo()
            .search(
                [
                    ('company_id', '=', self.company_id.id),
                    ('type', '=', 'bank'),
                    ('bank_account_id', '!=', False),
                ],
            )
            .filtered(
                lambda journal: (
                    self._normalise_account_number(journal.bank_account_id.acc_number)
                    == account_number
                ),
            )
        )
        if len(journals) != 1:
            raise ValidationError(
                _('The SePay account number must match exactly one bank journal.'),
            )
        currency = journals.currency_id or journals.company_id.currency_id
        if currency.name != 'VND':
            raise ValidationError(
                _('SePay auto-processing only supports VND bank journals.'),
            )
        return journals

    @api.private
    def _lookup_cutoff(self):
        self.ensure_one()
        return self._local_date() - timedelta(days=DOCUMENT_LOOKUP_WINDOW_DAYS)

    @api.private
    def _warn_if_truncated(self, records, model_name):
        if len(records) >= DOCUMENT_LOOKUP_LIMIT:
            _logger.warning(
                'SePay transaction %s compared its reference against the %s '
                'most recent %s only.',
                self.id,
                DOCUMENT_LOOKUP_LIMIT,
                model_name,
            )
        return records

    @api.private
    def _recent_open_invoices(self):
        """Return the company's open customer invoices worth matching against."""
        self.ensure_one()
        invoices = (
            self.env['account.move']
            .sudo()
            .search(
                [
                    ('company_id', '=', self.company_id.id),
                    ('move_type', '=', 'out_invoice'),
                    ('state', '=', 'posted'),
                    ('amount_residual', '>', 0),
                    ('invoice_date', '>=', self._lookup_cutoff()),
                ],
                order='invoice_date desc, id desc',
                limit=DOCUMENT_LOOKUP_LIMIT,
            )
        )
        return self._warn_if_truncated(invoices, 'customer invoices')

    @api.private
    def _recent_open_orders(self):
        """Return the company's live sales orders worth matching against."""
        self.ensure_one()
        orders = (
            self.env['sale.order']
            .sudo()
            .search(
                [
                    ('company_id', '=', self.company_id.id),
                    ('state', '!=', 'cancel'),
                    ('date_order', '>=', self._lookup_cutoff()),
                ],
                order='date_order desc, id desc',
                limit=DOCUMENT_LOOKUP_LIMIT,
            )
        )
        return self._warn_if_truncated(orders, 'sales orders')

    @api.private
    def _find_invoice_for_reference(self, reference):
        """Return the single customer invoice this reference designates."""
        self.ensure_one()
        moves = self.env['account.move'].sudo()
        exact = moves.search(
            [
                ('company_id', '=', self.company_id.id),
                ('move_type', '=', 'out_invoice'),
                ('state', '=', 'posted'),
                '|',
                ('payment_reference', '=', reference),
                ('name', '=', reference),
            ],
        )
        if exact:
            return exact if len(exact) == 1 else moves
        normalised = self._normalise_reference(reference)
        if len(normalised) < MIN_MATCHABLE_REFERENCE_LENGTH:
            return moves
        matches = self._recent_open_invoices().filtered(
            lambda move: normalised
            in (
                self._normalise_reference(move.payment_reference),
                self._normalise_reference(move.name),
            ),
        )
        return matches if len(matches) == 1 else moves

    @api.private
    def _find_order_for_reference(self, reference):
        """Return the single sales order this reference designates."""
        self.ensure_one()
        orders = self.env['sale.order'].sudo()
        exact = orders.search(
            [
                ('company_id', '=', self.company_id.id),
                ('state', '!=', 'cancel'),
                '|',
                ('name', '=', reference),
                ('reference', '=', reference),
            ],
        )
        if exact:
            return exact if len(exact) == 1 else orders
        normalised = self._normalise_reference(reference)
        if len(normalised) < MIN_MATCHABLE_REFERENCE_LENGTH:
            return orders
        matches = self._recent_open_orders().filtered(
            lambda order: normalised
            in (
                self._normalise_reference(order.name),
                self._normalise_reference(order.reference),
            ),
        )
        return matches if len(matches) == 1 else orders

    @api.private
    def _open_invoice_of_order(self, order):
        """Return the order's single open customer invoice, if there is one."""
        self.ensure_one()
        open_invoices = order.invoice_ids.filtered(
            lambda invoice: (
                invoice.state == 'posted'
                and invoice.move_type == 'out_invoice'
                and invoice.amount_residual > 0
            ),
        )
        return (
            open_invoices
            if len(open_invoices) == 1
            else self.env['account.move'].sudo()
        )

    @api.private
    def _find_documents_in_content(self):
        """Match the transfer content against open documents by containment.

        SePay only fills ``code`` once a payment-code prefix is configured on
        its side; without it the document reference still travels inside the
        free-text content. A document is only accepted when its whole
        normalised reference appears in the normalised content and it is the
        only such document, so this stays an exact match rather than a fuzzy
        search.
        """
        self.ensure_one()
        moves = self.env['account.move'].sudo()
        orders = self.env['sale.order'].sudo()
        if not self.company_id.sepay_match_content:
            return moves, orders
        content = self._normalise_reference(self.content or self.description)
        if len(content) < MIN_MATCHABLE_REFERENCE_LENGTH:
            return moves, orders

        def is_named_in_content(*references):
            return any(
                len(normalised) >= MIN_MATCHABLE_REFERENCE_LENGTH
                and normalised in content
                for normalised in map(self._normalise_reference, references)
            )

        invoices = self._recent_open_invoices().filtered(
            lambda move: is_named_in_content(move.payment_reference, move.name),
        )
        if invoices:
            return (invoices, orders) if len(invoices) == 1 else (moves, orders)
        candidates = self._recent_open_orders().filtered(
            lambda order: is_named_in_content(order.name, order.reference),
        )
        if len(candidates) != 1:
            return moves, orders
        return self._open_invoice_of_order(candidates), candidates

    @api.private
    def _find_business_documents(self):
        """Return the ``(invoices, order)`` pair this transfer settles."""
        self.ensure_one()
        orders = self.env['sale.order'].sudo()
        reference = (self.code or '').strip()
        if reference:
            invoices = self._find_invoice_for_reference(reference)
            if invoices:
                return invoices, orders
            order = self._find_order_for_reference(reference)
            if order:
                return self._open_invoice_of_order(order), order
        return self._find_documents_in_content()

    @api.private
    def _find_pending_payment_transaction(self, journal, invoices, order):
        self.ensure_one()
        candidates = (
            self.env['payment.transaction']
            .sudo()
            .search(
                [
                    ('company_id', '=', self.company_id.id),
                    ('provider_code', '=', 'custom'),
                    ('state', '=', 'pending'),
                    ('payment_id', '=', False),
                    ('currency_id', '=', self.currency_id.id),
                ],
            )
            .filtered(
                lambda transaction: (
                    transaction.provider_id.journal_id == journal
                    and transaction.currency_id.compare_amounts(
                        transaction.amount,
                        self.amount,
                    )
                    == 0
                    and (
                        (invoices and bool(transaction.invoice_ids & invoices))
                        or (order and order in transaction.sale_order_ids)
                    )
                ),
            )
        )
        return candidates if len(candidates) == 1 else self.env['payment.transaction']

    @api.private
    def _find_existing_payment(self, journal, invoices):
        """Return the payment an accountant already booked for this transfer.

        Reuse is deliberately strict: same company, journal, currency and
        amount, booked close to the transfer date, still waiting for its bank
        statement, not already claimed by another SePay transaction, and tied
        to the matched document either through reconciliation or through its
        memo. Anything less certain creates nothing and goes to review.
        """
        self.ensure_one()
        transfer_date = self._local_date()
        window = timedelta(days=EXISTING_PAYMENT_WINDOW_DAYS)
        candidates = (
            self.env['account.payment']
            .sudo()
            .search(
                [
                    ('company_id', '=', self.company_id.id),
                    ('journal_id', '=', journal.id),
                    ('payment_type', '=', 'inbound'),
                    ('partner_type', '=', 'customer'),
                    ('currency_id', '=', self.currency_id.id),
                    ('state', 'not in', ('draft', 'canceled', 'rejected')),
                    ('is_matched', '=', False),
                    ('date', '>=', transfer_date - window),
                    ('date', '<=', transfer_date + window),
                ],
            )
        )
        if not candidates:
            return self.env['account.payment']
        claimed = self.sudo().search(
            [
                ('payment_id', 'in', candidates.ids),
                ('id', '!=', self.id),
            ],
        ).payment_id
        reference = self._normalise_reference(self.code)
        matches = (candidates - claimed).filtered(
            lambda payment: (
                self.currency_id.compare_amounts(payment.amount, self.amount) == 0
                and payment._seek_for_lines()[0].filtered(
                    lambda line: not line.reconciled,
                )
                and (
                    bool(payment.reconciled_invoice_ids & invoices)
                    or (
                        reference
                        and self._normalise_reference(payment.memo) == reference
                    )
                )
            ),
        )
        return matches if len(matches) == 1 else self.env['account.payment']

    @api.private
    def _create_invoice_payment(self, journal, invoice, payment_date):
        self.ensure_one()
        wizard = (
            self.env['account.payment.register']
            .sudo()
            .with_company(self.company_id)
            .with_context(
                active_model='account.move',
                active_ids=invoice.ids,
            )
            .create(
                {
                    'amount': self.amount,
                    'journal_id': journal.id,
                    'payment_date': payment_date,
                    'communication': self.code or self.reference_code or self.content,
                },
            )
        )
        return wizard._create_payments()

    @api.private
    def _create_order_payment(self, journal, order, payment_date):
        self.ensure_one()
        payment_method_line = journal.inbound_payment_method_line_ids.filtered(
            lambda line: line.code == 'manual',
        )[:1]
        if not payment_method_line:
            raise UserError(
                _('The SePay bank journal has no manual inbound payment method.'),
            )
        payment = (
            self.env['account.payment']
            .sudo()
            .with_company(self.company_id)
            .create(
                {
                    'payment_type': 'inbound',
                    'partner_type': 'customer',
                    'partner_id': order.partner_id.commercial_partner_id.id,
                    'amount': self.amount,
                    'currency_id': self.currency_id.id,
                    'journal_id': journal.id,
                    'company_id': self.company_id.id,
                    'payment_method_line_id': payment_method_line.id,
                    'date': payment_date,
                    'memo': self.code or self.reference_code or self.content,
                },
            )
        )
        payment.action_post()
        return payment

    @api.private
    def _create_statement_line(self, journal, partner, payment_date):
        self.ensure_one()
        return (
            self.env['account.bank.statement.line']
            .sudo()
            .create(
                {
                    'date': payment_date,
                    'journal_id': journal.id,
                    'partner_id': partner.id if partner else False,
                    'amount': self.amount_signed,
                    'payment_ref': self.content
                    or self.description
                    or self.reference_code
                    or self.name,
                    'transaction_type': f'SePay {self.transfer_type}',
                    'transaction_details': {
                        'source': 'sepay',
                        'sepay_transaction_id': self.id,
                        'sepay_webhook_id': self.webhook_id,
                        'sepay_api_id': self.api_id,
                        'reference_code': self.reference_code,
                        'payment_code': self.code,
                        'account_number': self.account_number,
                    },
                },
            )
        )

    @api.private
    def _link_payment_transaction(self, payment_transaction, payment):
        """Complete the online transaction with the standard core flow."""
        self.ensure_one()
        payment.payment_transaction_id = payment_transaction
        payment_transaction.payment_id = payment
        payment_transaction.provider_reference = (
            self.reference_code or self.webhook_id or self.api_id
        )
        payment_transaction._set_done()
        payment_transaction._post_process()

    @api.private
    def _reconcile_statement_with_payment(self, statement_line, payment):
        self.ensure_one()
        liquidity_lines = payment._seek_for_lines()[0].filtered(
            lambda line: not line.reconciled,
        )
        if not liquidity_lines:
            raise UserError(
                _('The payment has no open outstanding item for bank reconciliation.'),
            )
        statement_line._sepay_reconcile_with_lines(liquidity_lines)
        if not statement_line.is_reconciled:
            raise UserError(_('The SePay bank transaction could not be reconciled.'))

    @api.private
    def _schedule_review_activity(self, note):
        """Ask the configured accountant to look at a transaction we parked."""
        self.ensure_one()
        responsible = self.company_id.sepay_activity_user_id
        if not responsible or self.sudo().activity_ids:
            return
        self.sudo().activity_schedule(
            'mail.mail_activity_data_todo',
            user_id=responsible.id,
            summary=_('Review the SePay transaction'),
            note=note,
        )

    @api.private
    def _close_review_activity(self):
        self.ensure_one()
        if self.sudo().activity_ids:
            self.sudo().activity_unlink(['mail.mail_activity_data_todo'])

    @api.private
    def _mark_unmatched(self, values, note):
        self.ensure_one()
        self.sudo().write(
            {
                **values,
                'state': 'unmatched',
                'error_message': note,
                'retry_count': 0,
            },
        )
        self._schedule_review_activity(note)

    @api.private
    def _mark_processed(self, values=None):
        self.ensure_one()
        self.sudo().write(
            {
                **(values or {}),
                'state': 'processed',
                'error_message': False,
                'retry_count': 0,
                'processed_at': fields.Datetime.now(),
            },
        )
        self._close_review_activity()

    @api.private
    def _process_transaction(self):
        self.ensure_one()
        if self.state == 'processed':
            return
        if self.bank_statement_line_id.is_reconciled:
            self._mark_processed()
            return
        journal = self._find_bank_journal()
        local_date = self._local_date()
        invoices, order = self._find_business_documents()
        partner = (
            invoices[:1].commercial_partner_id
            or order[:1].partner_id.commercial_partner_id
        )
        statement_line = self.bank_statement_line_id or self._create_statement_line(
            journal,
            partner,
            local_date,
        )

        common_values = {
            'journal_id': journal.id,
            'bank_statement_line_id': statement_line.id,
            'sale_order_id': order.id,
            'invoice_ids': [Command.set(invoices.ids)],
        }
        if self.transfer_type != 'in':
            self._mark_unmatched(
                common_values,
                _('Outgoing SePay transactions require accounting review.'),
            )
            return
        if not invoices and not order:
            self._mark_unmatched(
                common_values,
                _('No document exactly matches the SePay payment code.'),
            )
            return

        payment_transaction = self._find_pending_payment_transaction(
            journal,
            invoices,
            order,
        )
        payment = self._find_existing_payment(journal, invoices)
        if not payment:
            if invoices:
                invoice = invoices.ensure_one()
                if invoice.currency_id.name != 'VND':
                    self._mark_unmatched(
                        common_values,
                        _('The matched invoice is not in VND.'),
                    )
                    return
                if invoice.currency_id.is_zero(invoice.amount_residual):
                    self._mark_unmatched(
                        common_values,
                        _(
                            'The matched invoice is already fully paid and no '
                            'existing payment records this transfer.',
                        ),
                    )
                    return
                if (
                    invoice.currency_id.compare_amounts(
                        self.amount,
                        invoice.amount_residual,
                    )
                    > 0
                ):
                    self._mark_unmatched(
                        common_values,
                        _('The SePay amount exceeds the matched invoice residual.'),
                    )
                    return
                payment = self._create_invoice_payment(journal, invoices, local_date)
            elif payment_transaction:
                payment = self._create_order_payment(journal, order, local_date)
            else:
                self._mark_unmatched(
                    common_values,
                    _(
                        'The sales order has no exact pending payment transaction '
                        'or posted invoice.',
                    ),
                )
                return
        if len(payment) != 1:
            raise UserError(_('SePay auto-processing expected exactly one payment.'))

        if payment_transaction:
            self._link_payment_transaction(payment_transaction, payment)
            if not invoices and payment_transaction.invoice_ids:
                invoices = payment_transaction.invoice_ids
                common_values['invoice_ids'] = [Command.set(invoices.ids)]

        self._reconcile_statement_with_payment(statement_line, payment)
        self._mark_processed(
            {
                **common_values,
                'payment_id': payment.id,
                'payment_transaction_id': payment_transaction.id,
            },
        )

    @api.private
    def _process_with_error_isolation(self):
        for transaction in self:
            try:
                with self.env.cr.savepoint():
                    transaction._process_transaction()
            except Exception as error:  # noqa: BLE001 - preserve inbox and isolate the batch
                _logger.exception(
                    'Failed to process SePay transaction %s.',
                    transaction.id,
                )
                note = str(error)[:2000]
                transaction.sudo().write(
                    {
                        'state': 'error',
                        'error_message': note,
                        'retry_count': transaction.retry_count + 1,
                    },
                )
                transaction._schedule_review_activity(note)

    def action_process(self):
        self._sepay_check_manager()
        self.sudo()._process_with_error_isolation()
        return True

    def action_ignore(self):
        """Close a transfer that legitimately needs no accounting action.

        Not every credit on the bank account is a customer payment: an owner
        top-up, a supplier refund or a test transfer has no document to match
        and would otherwise sit in Needs Review for good, because the access
        rules forbid deleting a SePay record and its bank transaction must
        survive for the normal bank reconciliation anyway.

        The state is tracked, so the chatter records who closed it and when;
        `action_retry` puts it back in the queue if that turns out wrong.
        """
        self._sepay_check_manager()
        if stuck := self.filtered(lambda transaction: transaction.state == 'processed'):
            raise UserError(
                _(
                    'A SePay transaction that is already processed cannot be ignored: %(names)s.',
                    names=', '.join(stuck.mapped('name')),
                ),
            )
        self.sudo().write({'state': 'ignored', 'retry_count': 0})
        for transaction in self:
            transaction._close_review_activity()
        return True

    def action_retry(self):
        self._sepay_check_manager()
        self.sudo().write(
            {
                'state': 'received',
                'error_message': False,
                'retry_count': 0,
            },
        )
        self.sudo()._process_with_error_isolation()
        return True

    @api.model
    def _cron_process_transactions(self, limit=100):
        enabled_company = ('company_id.sepay_enabled', '=', True)
        transactions = self.sudo().search(
            [('state', '=', 'received'), enabled_company],
            limit=limit,
        )
        # A failure is often transient (a lock, a temporarily missing setup), so
        # errored transactions are retried a bounded number of times before an
        # accountant has to take over. `write_date` moves on every attempt,
        # which spaces the retries out on its own.
        remaining = limit - len(transactions)
        if remaining > 0:
            cutoff = fields.Datetime.now() - timedelta(
                minutes=AUTOMATIC_RETRY_DELAY_MINUTES,
            )
            transactions |= self.sudo().search(
                [
                    ('state', '=', 'error'),
                    ('retry_count', '<', MAX_AUTOMATIC_RETRIES),
                    ('write_date', '<=', cutoff),
                    enabled_company,
                ],
                limit=remaining,
            )
        transactions._process_with_error_isolation()
