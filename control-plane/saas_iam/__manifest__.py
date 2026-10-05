{
    'name': 'SaaS Project IAM',
    'version': '18.0.1.3.6',
    'category': 'SaaS',
    'summary': 'Fixed project roles, team invitations and environment scopes',
    'author': 'SaaS Platform',
    'license': 'LGPL-3',
    'depends': ['saas_website', 'phone_validation'],
    'external_dependencies': {'python': ['phonenumbers']},
    'data': ['security/ir.model.access.csv', 'security/iam_security.xml', 'views/whatsapp_settings.xml'],
    'installable': True,
}
