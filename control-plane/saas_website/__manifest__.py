{
    'name': 'SaaS Website',
    'version': '18.0.5.0.11',
    'category': 'SaaS',
    'summary': 'Customer-facing website for SaaS plan selection, ordering, and instance management',
    'author': 'SaaS Platform',
    'license': 'LGPL-3',
    'depends': ['saas_core', 'website', 'portal', 'payment', 'account_payment'],
    'data': [
        'security/saas_website_security.xml',
        'security/ir.model.access.csv',
        'data/mail_templates.xml',
        'views/saas_frontend_layout.xml',
        'views/saas_login_templates.xml',
        'views/saas_services_templates.xml',
        'views/saas_hosting_templates.xml',
        'views/saas_registration_templates.xml',
        'views/saas_invoice_templates.xml',
        'views/saas_portal_templates.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            # Theme bootstrap — first so it sets `data-theme` before
            # any other JS runs. Has `@odoo-module ignore` so it's
            # delivered as a plain script (not wrapped as a module).
            'saas_website/static/src/js/veltnex_theme.js',
            'saas_website/static/src/css/cloudodoo.css',
            'saas_website/static/src/js/cloudodoo_app.js',
            # portal_actions.js / portal_logs.js / subdomain_check.js
            # removed: the portal pages they drove (instance start/stop,
            # live logs) are now the VELTNEX SPA (calls /saas/api/v1 + the
            # log SSE). subdomain_check.js was a dead duplicate — its widget
            # selector (#subdomain) is in no template; the live subdomain
            # checker is initSubdomainCheck() in cloudodoo_app.js (targets
            # #subdomain-input, present on the configure pages).
        ],
    },
    'installable': True,
    'auto_install': False,
    'post_init_hook': 'post_init_hook',
}
