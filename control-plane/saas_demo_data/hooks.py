# Named tiers are auto-priced from the resources and the pricing rates
# (Settings > SaaS Manager), so no prices are set on them here.

VERSIONS = ('17.0', '18.0', '19.0')

HOSTING_FEATURES = [
    'Your own Odoo on a dedicated container',
    'Deploy custom modules from your Git repository',
    'Free HTTPS on your subdomain',
    'Daily backups with one-click restore',
    'Staging and development copies',
]

# name -> values; sequence follows the order here
HOSTING_PLANS = [
    ('Hosting Free Trial', {'is_trial_plan': True, 'price': 0.0, 'yearly_price': 0.0,
                            'workers': 1, 'storage_limit': 5, 'cpu_limit': 1.0, 'ram_limit': '1g'}),
    ('Starter', {'is_public_tier': True,
                 'workers': 1, 'storage_limit': 10, 'cpu_limit': 1.0, 'ram_limit': '1g'}),
    ('Professional', {'is_public_tier': True, 'is_recommended': True, 'badge': 'Popular',
                      'workers': 2, 'storage_limit': 20, 'cpu_limit': 2.0, 'ram_limit': '2g'}),
    ('Business', {'is_public_tier': True,
                  'workers': 4, 'storage_limit': 40, 'cpu_limit': 4.0, 'ram_limit': '4g'}),
    ('Custom (Enterprise)', {'is_custom': True,
                             'workers': 8, 'storage_limit': 100, 'cpu_limit': 8.0, 'ram_limit': '8g'}),
]

SUPPORT_PRICES = {
    'saas_billing.saas_support_plan_standard': 29.0,
    'saas_billing.saas_support_plan_pro': 99.0,
    'saas_billing.saas_support_plan_enterprise': 299.0,
}


def _upsert(env, model, domain, vals):
    rec = env[model].sudo().with_context(active_test=False).search(domain, limit=1)
    if rec:
        rec.write(vals)
        return rec
    return env[model].sudo().create(vals)


def post_init_hook(env):
    versions = {
        v: _upsert(env, 'saas.odoo.version', [('name', '=', v)], {
            'name': v, 'docker_image': 'odoo', 'docker_image_tag': v,
            'nginx_template': 'new', 'is_hosting_version': True})
        for v in VERSIONS
    }

    product = _upsert(env, 'saas.product', [('is_hosting', '=', True)], {
        'name': 'Odoo Hosting', 'is_hosting': True, 'is_published': True,
        'subtitle': 'Bring your own code — we run it.',
        'odoo_version_id': versions['18.0'].id, 'sequence': 1})
    if not product.feature_line_ids:
        product.feature_line_ids = [
            (0, 0, {'name': f, 'sequence': i}) for i, f in enumerate(HOSTING_FEATURES)]

    currency = env.company.currency_id
    for seq, (name, vals) in enumerate(HOSTING_PLANS, start=1):
        _upsert(env, 'saas.plan', [('name', '=', name)], dict(
            vals, name=name, sequence=seq * 10, currency_id=currency.id,
            saas_product_ids=[(4, product.id)]))

    for xmlid, price in SUPPORT_PRICES.items():
        plan = env.ref(xmlid, raise_if_not_found=False)
        if plan and not plan.monthly_price:
            plan.sudo().monthly_price = price
