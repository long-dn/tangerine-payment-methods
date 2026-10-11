{
    'name': 'SePay on Website',
    'summary': 'Display SePay VietQR codes on eCommerce order confirmations.',
    'description': """
From Odoo 20 the eCommerce order confirmation renders the wire-transfer
instructions of payment_custom, whose QR code tangerine_sepay_integration
already replaces with the SePay VietQR. This module only binds both apps so
the feature is installed with the shop, and keeps upgrades from 19.0 clean.
""",
    'author': 'Long Duong Nhat',
    'website': 'https://github.com/long-dn',
    'support': 'odoo.tangerine@gmail.com',
    'category': 'Website/eCommerce',
    'version': '20.0.2.0.0',
    'license': 'LGPL-3',
    'depends': ['tangerine_sepay_integration', 'website_sale'],
    'images': ['static/description/thumbnail.png'],
    'installable': True,
    'auto_install': True,
    'application': False,
}
