import logging

from . import models

_logger = logging.getLogger(__name__)


def _bank_post_init_hook(env):
    """Populate Vietnamese bank information at install time (best effort)."""
    try:
        env['res.bank'].bank_information_sync()
    except Exception:  # noqa: BLE001 - install must not fail on network errors
        _logger.warning(
            "Could not populate Vietnamese bank information at install time.",
            exc_info=True,
        )
