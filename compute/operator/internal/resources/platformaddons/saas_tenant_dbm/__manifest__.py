{
    'name': 'SaaS Tenant Database Manager',
    'version': '1.0',
    'category': 'Hidden',
    'summary': "Customer access to /web/database/manager without the master password",
    'description': """
Loaded as a server-wide module (--load) by the SaaS operator. The customer
opens the database manager from the SaaS portal through a short-lived
signed link; the master password is never shown or asked for, and only
databases with the instance's prefix can be created, restored, copied,
backed up or dropped.
""",
    'depends': ['web'],
    'license': 'LGPL-3',
    'installable': True,
    'auto_install': False,
}
