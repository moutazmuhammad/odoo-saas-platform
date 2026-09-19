{
    'name': 'SaaS Billing',
    'version': '18.0.1.0.0',
    'category': 'SaaS',
    'summary': 'Pricing, wallet, payments and commercial add-ons for the SaaS platform',
    'description': """
Billing/pricing layer for the SaaS platform, split out of `saas_core`
(see /ROADMAP.md §4.6 and the billing-architecture proposal that led to
this addon). Owns the *commercial* relationship — pricing, invoicing,
payments, wallet, subscriptions — while `saas_core` keeps tenant
identity, provisioning, and lifecycle.

This is Step A of the migration: the wholesale-relocated models (payment-
provider routing/gateway, wallet, support plans, the sellable add-on
catalog, and the pricing engine) together with `saas.instance`'s and
`saas.plan`'s billing-facing fields/methods, moved in one coordinated
cutover (see the plan doc for why this couldn't be split into smaller
independently-shippable steps: `saas.instance` holds direct FK columns
into these models).
""",
    'author': 'SaaS Platform',
    'license': 'LGPL-3',
    'depends': [
        'saas_core', 'sale', 'account', 'payment', 'account_payment',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/saas_addon_data.xml',
        'data/saas_support_plan_data.xml',
        'data/saas_billing_cron.xml',
        'data/mail_templates.xml',
        'views/saas_wallet_views.xml',
        'views/saas_support_plan_views.xml',
        'views/saas_addon_views.xml',
        'views/saas_instance_views.xml',
        'views/saas_plan_views.xml',
        'views/saas_compute_tier_views.xml',
        'views/res_config_settings_views.xml',
        'views/saas_menus.xml',
    ],
    'installable': True,
    'auto_install': False,
    'pre_init_hook': 'pre_init_hook',
    'post_init_hook': 'post_init_hook',
}
