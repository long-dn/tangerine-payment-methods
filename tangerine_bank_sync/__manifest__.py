{
    'name': 'Bank Information Vietnam',
    'summary': 'Synchronize Vietnamese banking information (BIN, BIC, logo) via the VietQR API and feed the BIN to VietQR payment codes.',
    'author': 'Long Duong Nhat',
    'website': 'https://github.com/long-dn',
    'support': 'odoo.tangerine@gmail.com',
    'category': 'Accounting',
    'version': '20.0.2.0.0',
    'license': 'LGPL-3',
    'depends': ['account', 'l10n_vn'],
    'data': [
        'security/ir.access.csv',
        'data/ir_cron.xml',
        'data/ir_actions_server.xml',
        'views/vn_bank_views.xml',
        'views/res_partner_bank_views.xml',
        'views/bank_menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'tangerine_bank_sync/static/src/res_bank_kanban.scss',
        ],
    },
    'images': ['static/description/thumbnail.png'],
    'installable': True,
    'auto_install': False,
    'application': False,
    'post_init_hook': '_bank_post_init_hook',
}
