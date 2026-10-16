"""C3.7 crash-safety harness: a ``saas-jobs`` worker for a THROWAWAY database
whose external effects (cluster calls, snapshots) are faked.

Each faked effect appends ``start <effect> <subdomain>`` to the effect log
and, while ``<block dir>/block-<effect>`` exists, holds there (after logging
``blocked <effect> <subdomain>``) so the orchestrator can SIGKILL the worker
mid-operation. Everything else — the queue, payment hooks, locks, state
changes — is the real code.

    python worker.py -c /etc/odoo/saas.conf --addons-path=... -d saas_tmp_c37 \
        --block-dir /tmp/saas-crash --effect-log /tmp/saas-crash/effects.log

Never point it at a real database: it replaces the compute driver.
"""
import argparse
import os
import sys
import time

import odoo
from odoo.modules.module import initialize_sys_path
from odoo.modules.registry import Registry
from odoo.tools import config

MAX_BLOCK_SEC = 900


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--block-dir', required=True)
    parser.add_argument('--effect-log', required=True)
    parser.add_argument('--threads', type=int, default=6)
    ours, odoo_args = parser.parse_known_args()
    config.parse_config(odoo_args, setup_logging=True)
    dbname = (config['db_name'] or '').split(',')[0].strip()
    if not dbname.startswith('saas_tmp_'):
        sys.exit("crash-safety worker: refusing database %r (need saas_tmp_*)" % dbname)
    initialize_sys_path()
    install_fakes(Registry(dbname), ours.block_dir, ours.effect_log)
    from odoo.addons.saas_core.cli.saas_jobs import run_worker
    run_worker(dbname, ours.threads)


def install_fakes(registry, block_dir, effect_log):
    def effect(name, subdomain):
        with open(effect_log, 'a') as f:
            f.write('%s start %s %s\n' % (os.getpid(), name, subdomain))
        block = os.path.join(block_dir, 'block-%s' % name)
        logged = False
        deadline = time.time() + MAX_BLOCK_SEC
        while os.path.exists(block) and time.time() < deadline:
            if not logged:
                with open(effect_log, 'a') as f:
                    f.write('%s blocked %s %s\n' % (os.getpid(), name, subdomain))
                logged = True
            time.sleep(1)

    class FakeDriver:
        def __init__(self, subdomain):
            self.subdomain = subdomain

        def stop(self, handle):
            effect('stop', self.subdomain)

        def restart(self, handle):
            effect('restart', self.subdomain)

        def destroy(self, handle, **kw):
            effect('destroy', self.subdomain)

        def exists(self, handle):
            return False

        def package_in_effect(self, handle, res):
            return {'odoo': False, 'database': False, 'volume_gi': 0}

    Instance = registry['saas.instance']  # the model class itself
    Backup = registry['saas.instance.backup']
    Alert = registry['saas.alert']
    real_notify = Alert._notify

    def fake_notify(self, event, message, **kw):
        # C6.5: the T1 escalation's alert is sent after its activity is
        # created, so a kill here must leave no activity behind.
        if event == 'upgrade_delayed':
            effect('escalate', message.split(':')[0])
        return real_notify(self, event, message, **kw)

    def fake_deploy_locked(self):
        effect('deploy', self.subdomain)
        self.write({'state': 'running', 'pending_operation': False,
                    'pre_provisioning_state': False})
        self._append_log("Deployment completed successfully (crash-safety fake).")
        return True

    def fake_snapshot(self, instance, *args, **kw):
        effect('snapshot', instance.subdomain)
        stamp = 'final-%d' % int(time.time() * 1000)
        return self.create({
            'instance_id': instance.id, 'name': stamp, 'state': 'done',
            'is_full_instance': True, 'format': 'operator', 'source': 'final',
            'bucket_path': 'backups/%s/%s' % (instance.subdomain, stamp)})

    patches = {
        (Instance, '_compute_driver'): lambda self, connection=None: FakeDriver(self.subdomain),
        (Instance, '_compute_handle'): lambda self: self.subdomain,
        (Instance, '_do_deploy_locked'): fake_deploy_locked,
        # A resize that the cluster runs at once: C6.3 verified it, so a
        # paid-for change it delivers moves on to its billing switch (C6.10).
        (Instance, '_update_container_resources'):
            lambda self: (effect('resize', self.subdomain), self._on_package_verified()),
        (Alert, '_notify'): fake_notify,
        (Backup, '_create_full_instance_backup_sync'): fake_snapshot,
        (Backup, '_delete_bucket_prefix'): lambda self, prefix: None,
        (Backup, '_delete_from_bucket'): lambda self: None,
    }
    for (cls, attr), fn in patches.items():
        setattr(cls, attr, fn)


if __name__ == '__main__':
    main()
