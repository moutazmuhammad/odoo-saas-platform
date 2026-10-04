{
    'name': 'SaaS Project IAM',
    'version': '18.0.1.1.0',
    'category': 'SaaS',
    'summary': 'Fixed project roles, team invitations and environment scopes',
    'author': 'SaaS Platform',
    'license': 'LGPL-3',
    'depends': ['saas_website', 'sms'],
    'data': ['security/ir.model.access.csv', 'security/iam_security.xml'],
    'installable': True,
}
