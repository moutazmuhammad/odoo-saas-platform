"""On-demand snapshots (DigitalOcean-style): own prefix, no retention,
restorable without the daily add-on, deletable, and usable to seed a new
project."""
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

CFG = {'provider': 's3', 'bucket': 'b', 'access_key': 'a', 'secret_key': 's',
       'endpoint': '', 'region': ''}


@tagged('post_install', '-at_install')
class TestSnapshots(TransactionCase):

    def setUp(self):
        super().setUp()
        env = self.env
        product = env['saas.product'].sudo().create({'name': 'SN Hosting', 'is_hosting': True})
        plan = env['saas.plan'].sudo().create({
            'name': 'SN Plan', 'is_custom': True, 'workers': 1, 'storage_limit': 10,
            'cpu_limit': 1.0, 'ram_limit': '1g', 'price': 10.0, 'yearly_price': 96.0,
            'currency_id': env.company.currency_id.id, 'saas_product_ids': [(6, 0, [product.id])]})
        self.server = env['saas.server'].sudo().create({'name': 'sn-srv', 'compute_driver': 'kubernetes'})
        self.inst = env['saas.instance'].sudo().create({
            'subdomain': 'sntest',
            'domain_id': (env['saas.based.domain'].sudo().search([], limit=1)
                          or env['saas.based.domain'].sudo().create({'name': 'sn.example.com'})).id,
            'partner_id': env['res.partner'].sudo().create({'name': 'SN Cust'}).id,
            'saas_product_id': product.id, 'plan_id': plan.id,
            'docker_server_id': self.server.id, 'billing_period': 'monthly',
            'environment': 'production', 'region_id': False, 'state': 'running'})
        self.Backup = env['saas.instance.backup'].sudo()
        self.driver = MagicMock()
        for target, value in (
                (type(self.Backup), '_get_backup_config'),
                (type(self.inst), '_compute_driver')):
            pass
        p = patch.object(type(self.Backup), '_get_backup_config', lambda rec: dict(CFG))
        p.start(); self.addCleanup(p.stop)
        p = patch.object(type(self.Backup), '_storage_endpoint_url', lambda rec, cfg: 'https://s3')
        p.start(); self.addCleanup(p.stop)
        p = patch.object(type(self.inst), '_compute_driver', lambda rec, connection=None: self.driver)
        p.start(); self.addCleanup(p.stop)

    def _snapshot(self, name='snap', state='done', **kw):
        vals = {'instance_id': self.inst.id, 'name': name, 'state': state,
                'is_full_instance': True, 'format': 'operator', 'source': 'snapshot',
                'bucket_path': 'snapshots/sntest/%s' % name}
        vals.update(kw)
        return self.Backup.create(vals)

    def test_take_snapshot_queues_a_job_and_shows_in_progress(self):
        snap = self.inst.action_take_snapshot('before-upgrade')
        self.assertEqual((snap.source, snap.state, snap.is_full_instance), ('snapshot', 'running', True))
        job = self.env['saas.job'].sudo().search([('model', '=', 'saas.instance.backup'), ('res_id', '=', snap.id)])
        self.assertEqual(job.method, '_do_take_snapshot')
        with self.assertRaisesRegex(UserError, 'already being taken'):
            self.inst.action_take_snapshot('another')

    def test_snapshot_limit_and_names(self):
        self.env['ir.config_parameter'].sudo().set_param('saas_master.max_snapshots', '1')
        self._snapshot('one')
        with self.assertRaisesRegex(UserError, 'limit'):
            self.inst.action_take_snapshot('two')
        self.env['ir.config_parameter'].sudo().set_param('saas_master.max_snapshots', '5')
        with self.assertRaisesRegex(UserError, 'already exists'):
            self.inst.action_take_snapshot('one')
        with self.assertRaises(UserError):
            self.inst.action_take_snapshot('bad/name')

    def test_snapshot_job_writes_under_its_own_prefix_without_retention(self):
        placeholder = self._snapshot('pre-migration', state='running', bucket_path=False)
        stamps = [set(), {'20260101T000000Z'}]
        resynced = []
        with patch.object(type(self.Backup), '_list_backup_stamps', lambda rec, cfg, prefix: stamps.pop(0) if stamps else {'20260101T000000Z'}), \
                patch.object(type(self.Backup), '_bucket_object_size', lambda rec, key: 1024 * 1024), \
                patch.object(type(self.inst), '_sync_scheduled_backup', lambda rec: resynced.append(rec.id)), \
                patch('odoo.addons.saas_core.models.saas_instance_backup.time.sleep', lambda s: None):
            placeholder._do_take_snapshot()
        self.driver.set_scheduled_backup.assert_called_once()
        self.assertEqual(self.driver.set_scheduled_backup.call_args.kwargs['prefix'], 'backups/sntest')
        env = self.driver.trigger_backup_now.call_args.kwargs['env']
        self.assertEqual(env, {'DESTINATION_PREFIX': 'snapshots/sntest', 'RETENTION': '0'})
        self.assertEqual(placeholder.state, 'done')
        self.assertEqual(placeholder.name, 'pre-migration')
        self.assertEqual(placeholder.bucket_path, 'snapshots/sntest/20260101T000000Z')
        self.assertEqual(placeholder.source, 'snapshot')
        self.assertEqual(resynced, [self.inst.id], "the paid schedule is put back afterwards")

    def test_snapshot_restores_without_the_daily_addon(self):
        snap = self._snapshot('restore-me')
        scheduled = self.Backup.create({
            'instance_id': self.inst.id, 'name': 'nightly', 'state': 'done',
            'is_full_instance': True, 'format': 'operator', 'source': 'scheduled',
            'bucket_path': 'backups/sntest/nightly'})
        queued = []
        self.inst.daily_backup_enabled = False
        with patch.object(type(self.inst), 'queue_full_instance_restore', lambda rec, b: queued.append(b.id) or True):
            self.inst.action_restore_full_instance(snap.id)
            with self.assertRaisesRegex(UserError, 'Daily Backups'):
                self.inst.action_restore_full_instance(scheduled.id)
        self.assertEqual(queued, [snap.id])

    def test_delete_snapshot_removes_its_data(self):
        snap = self._snapshot('old')
        deleted = []
        with patch.object(type(self.Backup), '_delete_bucket_prefix', lambda rec, prefix: deleted.append(prefix)):
            self.inst.action_delete_snapshot(snap.id)
        self.assertEqual(deleted, ['snapshots/sntest/old'])
        self.assertFalse(snap.exists())

    def test_nightly_reaper_never_touches_snapshots(self):
        self.inst.daily_backup_enabled = True
        snap = self._snapshot('keep')
        stale = self.Backup.create({
            'instance_id': self.inst.id, 'name': 'gone', 'state': 'done',
            'is_full_instance': True, 'format': 'operator', 'source': 'scheduled',
            'bucket_path': 'backups/sntest/gone'})
        with patch.object(type(self.Backup), '_list_backup_stamps', lambda rec, cfg, prefix: set()), \
                patch.object(type(self.inst), '_sync_scheduled_backup', lambda rec: None), \
                patch.object(type(self.Backup), '_delete_from_bucket', lambda rec: None):
            self.Backup._cron_sync_scheduled_backups()
        self.assertTrue(snap.exists())
        self.assertFalse(stale.exists())

    def test_new_project_seeded_from_snapshot_on_deploy(self):
        snap = self._snapshot('seed')
        new = self.inst.copy({'subdomain': 'sntest2', 'state': 'draft', 'seed_backup_id': snap.id})
        queued = []
        created = []
        driver = MagicMock(); driver.create.return_value = MagicMock()
        with patch.object(type(new), '_compute_driver', return_value=driver), \
                patch.object(type(new), '_data_service') as mock_ds, \
                patch.object(type(new), 'queue_full_instance_restore', lambda rec, b: queued.append((rec.id, b.id)) or True), \
                patch.object(type(new), 'hosting_db_create', lambda rec, *a, **k: created.append(1)), \
                patch.object(type(new), 'hosting_db_list', lambda rec: []):
            mock_ds.return_value._wait_until_healthy.return_value = None
            new._do_deploy_locked_kubernetes()
        self.assertEqual(queued, [(new.id, snap.id)])
        self.assertEqual(created, [], "no empty database when seeding from a snapshot")
        self.assertFalse(new.seed_backup_id, "seeded once, never on redeploy")
