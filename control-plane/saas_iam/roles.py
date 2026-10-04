"""The fixed catalog. Customers cannot define or modify roles."""
ENVIRONMENTS = ('production', 'staging', 'development')
ROLE_DEFINITIONS = {
    'viewer': ('Project Viewer', {'project.view', 'build.view'}),
    'logs': ('Logs Viewer', {'logs.view', 'build.view'}),
    'deploy': ('Deployment Operator', {'deploy', 'build.view', 'db.upgrade', 'db.view'}),
    'operator': ('Instance Operator', {'instance.operate'}),
    'environment_creator': ('Environment Creator', {'environment.create'}),
    'environment_deleter': ('Environment Deleter', {'environment.delete'}),
    'database_creator': ('Database Creator', {'db.view', 'db.create'}),
    'backup_operator': ('Database Backup Operator', {'db.view', 'backup.view', 'backup.create'}),
    'backup_downloader': ('Database Backup Downloader', {'db.view', 'backup.view', 'backup.download'}),
    'database_restore': ('Database Restore Operator', {'db.view', 'backup.view', 'db.restore'}),
    'database_deleter': ('Database Deleter', {'db.view', 'db.delete'}),
    'database_access': ('Database Access Administrator', {'db.view', 'db.password'}),
    'sql': ('SQL Operator', {'db.view', 'sql.execute'}),
    'terminal': ('Terminal Operator', {'terminal.open'}),
    'access_admin': ('Project Access Administrator', {'iam.manage'}),
}
for _name, (_label, _permissions) in list(ROLE_DEFINITIONS.items()):
    ROLE_DEFINITIONS[_name] = (_label, frozenset(_permissions | {'project.view'}))
PROJECT_PERMISSIONS = frozenset().union(*(p for _, p in ROLE_DEFINITIONS.values())) | {'project.configure', 'project.delete'}
ROLE_DEFINITIONS['project_admin'] = ('Project Administrator', PROJECT_PERMISSIONS)
ROLE_SELECTION = [(code, label) for code, (label, _) in ROLE_DEFINITIONS.items()]


def permissions_for(role):
    return ROLE_DEFINITIONS.get(role, ('', frozenset()))[1]
