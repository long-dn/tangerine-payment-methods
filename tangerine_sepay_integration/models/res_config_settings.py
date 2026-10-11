from odoo import api, fields, models
from odoo.tools import config

PROCESSING_CRON_XMLID = 'tangerine_sepay_integration.ir_cron_process_transactions'
SYNC_CRON_XMLID = 'tangerine_sepay_integration.ir_cron_sync_transactions'


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    sepay_enabled = fields.Boolean(
        related='company_id.sepay_enabled',
        readonly=False,
    )
    sepay_environment = fields.Selection(
        related='company_id.sepay_environment',
        readonly=False,
    )
    sepay_sync_lookback_days = fields.Integer(
        related='company_id.sepay_sync_lookback_days',
        readonly=False,
    )
    sepay_activity_user_id = fields.Many2one(
        related='company_id.sepay_activity_user_id',
        readonly=False,
    )
    sepay_match_content = fields.Boolean(
        related='company_id.sepay_match_content',
        readonly=False,
    )
    sepay_cache_qr_images = fields.Boolean(
        related='company_id.sepay_cache_qr_images',
        readonly=False,
    )
    sepay_last_sync_at = fields.Datetime(
        related='company_id.sepay_last_sync_at',
    )
    sepay_last_sync_error = fields.Text(
        related='company_id.sepay_last_sync_error',
    )
    sepay_auto_process = fields.Boolean(
        string='Process Transactions Automatically',
        compute='_compute_sepay_cron_state',
        readonly=False,
        store=True,
        help='Run the scheduled action that turns received SePay transactions '
        'into payments and bank transactions. A webhook only stores the raw '
        'transaction, so nothing reaches the books while this is off.',
    )
    sepay_auto_sync = fields.Boolean(
        string='Poll the SePay API',
        compute='_compute_sepay_cron_state',
        readonly=False,
        store=True,
        help='Fetch transactions from the SePay API on a schedule. This picks '
        'up transfers that SePay never delivered by webhook and needs an API '
        'token.',
    )
    sepay_webhook_secret_configured = fields.Boolean(
        string='Webhook Secret Configured',
        compute='_compute_sepay_secret_status',
    )
    sepay_api_token_configured = fields.Boolean(
        string='API Token Configured',
        compute='_compute_sepay_secret_status',
    )

    @api.depends('company_id')
    def _compute_sepay_secret_status(self):
        for settings in self:
            company = settings.company_id
            settings.sepay_webhook_secret_configured = bool(
                config.get(f'sepay_webhook_secret_{company.id}')
                or config.get('sepay_webhook_secret'),
            )
            settings.sepay_api_token_configured = bool(
                config.get(f'sepay_api_token_{company.id}')
                or config.get('sepay_api_token'),
            )

    @api.depends('company_id')
    def _compute_sepay_cron_state(self):
        """Mirror the two scheduled actions into the settings screen.

        The crons are the only thing standing between an accepted webhook and
        the books, so an accountant has to be able to see and flip them where
        the rest of SePay is configured, not only under Technical settings.
        """
        crons = self.env['ir.cron'].sudo()
        processing = self.env.ref(PROCESSING_CRON_XMLID, raise_if_not_found=False) or crons
        sync = self.env.ref(SYNC_CRON_XMLID, raise_if_not_found=False) or crons
        for settings in self:
            settings.sepay_auto_process = bool(processing.sudo().active)
            settings.sepay_auto_sync = bool(sync.sudo().active)

    def set_values(self):
        super().set_values()
        # Writing on a cron takes a write lock on the table, so only a real
        # change is pushed through.
        for xmlid, enabled in (
            (PROCESSING_CRON_XMLID, self.sepay_auto_process),
            (SYNC_CRON_XMLID, self.sepay_auto_sync),
        ):
            cron = self.env.ref(xmlid, raise_if_not_found=False)
            if cron and cron.sudo().active != enabled:
                cron.sudo().active = enabled

    def action_sepay_sync_now(self):
        self.ensure_one()
        return self.company_id.action_sepay_sync_now()

    def action_sepay_process_pending(self):
        self.ensure_one()
        self.company_id._sepay_check_manager()
        transactions = self.env['sepay.transaction'].search(
            [
                ('company_id', '=', self.company_id.id),
                ('state', '=', 'received'),
            ],
        )
        transactions.action_process()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': self.env._('SePay processing completed'),
                'message': self.env._(
                    '%(count)s transaction(s) were processed.',
                    count=len(transactions),
                ),
                'type': 'success',
            },
        }
