{
    'name': 'SaaS Demo Catalog',
    'version': '18.0.1.0.0',
    'category': 'SaaS',
    'summary': 'Test catalog: Odoo versions, the hosting product, plans and support prices',
    'description': """
Fills an empty control plane with a sellable test catalog so the portal
can be tried end to end on a real cluster:

* Odoo versions 17.0 / 18.0 / 19.0 (official ``odoo`` image)
* the "Odoo Hosting" product with its feature bullets
* hosting plans: Free Trial, Starter, Professional, Business, Custom
* prices for the Standard / Pro / Enterprise support plans

Catalog only: no customers, instances, regions or payment providers.
Existing records with the same names are updated, not duplicated.
Uninstalling keeps the records (instances may reference them).
""",
    'author': 'SaaS Platform',
    'license': 'LGPL-3',
    'depends': ['saas_core', 'saas_billing'],
    'data': [],
    'installable': True,
    'auto_install': False,
    'post_init_hook': 'post_init_hook',
}
