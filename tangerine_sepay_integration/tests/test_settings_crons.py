from unittest.mock import patch

from odoo.tests import tagged

from .common import SePayCommon
from odoo.addons.tangerine_sepay_integration.models.res_company import ResCompany

PROCESSING_CRON = 'tangerine_sepay_integration.ir_cron_process_transactions'
SYNC_CRON = 'tangerine_sepay_integration.ir_cron_sync_transactions'


@tagged('post_install', '-at_install')
class TestSePaySettingsCrons(SePayCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Saving the settings screen is a system-level operation.
        cls.env.user.groups_id |= cls.env.ref('base.group_system')

    def test_processing_cron_ships_enabled(self):
        """The webhook route only stores the transaction; the cron books it.

        Shipping this cron disabled turned every accepted webhook into a
        record that nothing ever processed, with no error anywhere.
        """
        self.assertTrue(self.env.ref(PROCESSING_CRON).active)

    def test_api_sync_cron_ships_disabled(self):
        """Polling a third-party API is opt-in; it needs a token to be useful."""
        self.assertFalse(self.env.ref(SYNC_CRON).active)

    def test_settings_mirror_the_cron_state(self):
        self.env.ref(PROCESSING_CRON).active = False
        self.env.ref(SYNC_CRON).active = True
        settings = self.env['res.config.settings'].create({})
        self.assertFalse(settings.sepay_auto_process)
        self.assertTrue(settings.sepay_auto_sync)

    def test_settings_write_back_to_the_crons(self):
        settings = self.env['res.config.settings'].create({})
        settings.sepay_auto_process = False
        settings.sepay_auto_sync = True
        settings.execute()
        self.assertFalse(self.env.ref(PROCESSING_CRON).active)
        self.assertTrue(self.env.ref(SYNC_CRON).active)

    def test_api_sync_skips_companies_without_a_token(self):
        """A webhook-only company must not be reported as a sync failure."""
        with (
            patch.object(ResCompany, '_sepay_api_token', return_value=False),
            patch.object(ResCompany, '_sepay_sync_transactions') as sync,
        ):
            self.env['res.company']._cron_sepay_sync_transactions()
        sync.assert_not_called()

    def test_api_sync_runs_for_companies_with_a_token(self):
        # Proves the skip above is a real filter and not an empty search.
        with (
            patch.object(ResCompany, '_sepay_api_token', return_value='token'),
            patch.object(ResCompany, '_sepay_sync_transactions') as sync,
        ):
            self.env['res.company']._cron_sepay_sync_transactions()
        self.assertTrue(sync.called)
