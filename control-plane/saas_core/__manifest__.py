{
    'name': 'SaaS Instance Manager',
    'version': '18.0.49.0.0',
    'category': 'SaaS',
    'summary': 'Provision and manage multi-tenant Odoo instances with Docker containers',
    'description': """
Manage your SaaS platform directly from Odoo.

Key capabilities:

- **Instance lifecycle** -- provision, start, stop, restart, suspend, and delete
  Odoo instances running in Docker containers on remote servers.
- **Automatic provisioning** -- generates docker-compose and odoo.conf files,
  creates PostgreSQL users and databases, assigns ports, and initialises the
  Odoo database, all over SSH.
- **Snapshot-based provisioning** -- deploy instances from pre-built database
  snapshots stored in cloud buckets (S3, GCS, DigitalOcean Spaces), with
  automatic repo cloning for the correct codebase.
- **Product catalog integration** -- services and plans integrate with standard
  Odoo products for quoting, selling, and invoicing.
- **Infrastructure registry** -- keep track of Docker host servers, PostgreSQL
  servers, SSH keys, and base domains used by the platform.
""",
    'author': 'SaaS Platform',
    'license': 'LGPL-3',
    'depends': [
        'base', 'mail', 'sale', 'account', 'portal', 'phone_validation',
        # 'payment' + 'account_payment' for saved-card auto-renewal:
        # saas.instance.payment_token_id -> payment.token, and renewal
        # crons create payment.transaction records to charge that token.
        'payment', 'account_payment',
    ],
    'external_dependencies': {
        'python': ['paramiko', 'jinja2', 'boto3', 'google-cloud-storage'],
    },
    'data': [
        'security/saas_security.xml',
        'security/ir.model.access.csv',
        'data/ir_config_parameter.xml',
        'data/mail_templates.xml',
        'data/saas_backup_cron.xml',
        'data/saas_storage_check_cron.xml',
        'data/saas_health_check_cron.xml',
        'data/saas_server_health_cron.xml',
        'data/saas_usage_refresh_cron.xml',
        'data/saas_trial_expiry_cron.xml',
        'data/saas_recurring_billing_cron.xml',
        'data/saas_margin_alert_cron.xml',
        'data/saas_metrics_cron.xml',
        'data/saas_pending_provision_cron.xml',
        'data/saas_job_cron.xml',
        'data/saas_addon_data.xml',
        'data/saas_region_data.xml',
        'data/saas_support_plan_data.xml',
        'wizards/saas_config_viewer_views.xml',
        'wizards/saas_restore_retained_views.xml',
        'views/saas_product_views.xml',
        'views/saas_plan_views.xml',
        'views/saas_addon_views.xml',
        'views/saas_instance_views.xml',
        'views/saas_ssh_key_pair_views.xml',
        'views/saas_docker_container_views.xml',
        'views/saas_region_views.xml',
        'views/saas_wallet_views.xml',
        'views/saas_support_plan_views.xml',
        'views/saas_server_views.xml',
        'views/saas_margin_views.xml',
        'views/saas_domain_views.xml',
        'views/saas_odoo_version_views.xml',
        'views/res_partner_views.xml',
        'views/res_config_settings_views.xml',
        'views/saas_menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'saas_core/static/src/components/container_logs/container_logs.js',
            'saas_core/static/src/components/container_logs/container_logs.xml',
            'saas_core/static/src/components/ssh_terminal/ssh_terminal.js',
            'saas_core/static/src/components/ssh_terminal/ssh_terminal.xml',
        ],
    },
    'installable': True,
    'application': True,
    'auto_install': False,
}
