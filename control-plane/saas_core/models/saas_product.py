import datetime as dt
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..fields import EncryptedChar

_logger = logging.getLogger(__name__)


class SaasProduct(models.Model):
    _name = 'saas.product'
    _description = 'SaaS Service Product'
    _order = 'sequence, name'

    name = fields.Char(
        string='Service Name',
        required=True,
        help='Display name shown on the website (e.g. "Pharmacy Management").',
    )
    subtitle = fields.Char(
        string='Subtitle',
        help='Short tagline shown below the service name on the catalog page.',
    )
    description = fields.Html(
        string='Description',
        help='Detailed description of the service, shown on the service detail page.',
    )
    icon = fields.Char(
        string='Icon Class',
        default='fa fa-cogs',
        help='Font Awesome icon class (e.g. "fa fa-medkit" for pharmacy).',
    )
    image = fields.Image(
        string='Image',
        max_width=512,
        max_height=512,
        help='Optional image displayed on the service card.',
    )
    sequence = fields.Integer(default=10)
    is_published = fields.Boolean(
        string='Published',
        default=True,
        help='Only published services are visible on the website.',
    )
    is_hosting = fields.Boolean(
        string='Self-Managed Hosting',
        default=False,
        help='If checked, this product represents a self-managed hosting service. '
             'Customers provide their own Git repository and Odoo version.',
    )
    odoo_version_id = fields.Many2one(
        'saas.odoo.version',
        string='Odoo Version',
        help='Odoo version used by instances of this service.',
    )
    plan_ids = fields.Many2many(
        'saas.plan',
        'saas_plan_product_rel',
        'product_id',
        'plan_id',
        string='Plans',
    )
    plan_count = fields.Integer(
        string='Plans',
        compute='_compute_plan_count',
    )
    instance_count = fields.Integer(
        string='Instances',
        compute='_compute_instance_count',
    )

    # ========== Snapshot ==========
    backup_bucket_path = fields.Char(
        string='Snapshot Path',
        help='Full object key of the snapshot zip inside the bucket '
             '(e.g. "pharmacy/2026-03-20/snapshot.zip"). '
             'The bucket itself is configured in Settings > Backup Storage.',
    )

    # ========== Repositories ==========
    repo_ids = fields.One2many(
        'saas.product.repo',
        'product_id',
        string='Repositories',
        help='GitHub repositories containing the Odoo codebase and custom '
             'addons for this service. Cloned during instance provisioning.',
    )

    # Feature highlights shown on the service card
    feature_line_ids = fields.One2many(
        'saas.product.feature',
        'product_id',
        string='Key Features',
        help='Bullet-point features displayed on the service card.',
    )

    @api.depends('plan_ids')
    def _compute_plan_count(self):
        for rec in self:
            rec.plan_count = len(rec.plan_ids)

    def action_view_plans(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Plans',
            'res_model': 'saas.plan',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.plan_ids.ids)],
        }

    def action_view_instances(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Instances',
            'res_model': 'saas.instance',
            'view_mode': 'list,form',
            'domain': [('saas_product_id', '=', self.id)],
            'context': {'default_saas_product_id': self.id},
        }

    def _compute_instance_count(self):
        data = self.env['saas.instance']._read_group(
            [('saas_product_id', 'in', self.ids)],
            ['saas_product_id'],
            ['__count'],
        )
        counts = {prod.id: count for prod, count in data}
        for rec in self:
            rec.instance_count = counts.get(rec.id, 0)

    # ========== Snapshot Helpers ==========

    def _get_storage_config(self):
        """Return the cloud storage configuration for product snapshots.

        Snapshots and backups always live in the same bucket — there's a
        single Storage section in SaaS settings and the snapshot path is
        just a different prefix inside it. We read the backup params
        directly (no shadow ``saas_snapshot.*`` keys to keep in sync).
        """
        ICP = self.env['ir.config_parameter'].sudo()
        provider = ICP.get_param('saas_backup.provider', '')
        bucket = ICP.get_param('saas_backup.bucket_name', '')
        if not provider or not bucket:
            raise UserError(_(
                "Cloud storage is not configured. Go to SaaS Manager > "
                "Configuration > Settings and set the storage provider "
                "and bucket."
            ))
        return {
            'provider': provider,
            'bucket': bucket,
            'access_key': ICP.get_param('saas_backup.access_key', ''),
            'secret_key': ICP.get_param('saas_backup.secret_key', ''),
            'region': ICP.get_param('saas_backup.region', ''),
            'endpoint_url': ICP.get_param('saas_backup.endpoint', ''),
            'gcs_credentials': ICP.get_param('saas_backup.service_account_key', ''),
        }

    def _generate_snapshot_download_url(self):
        """Generate a presigned download URL for this product's snapshot."""
        self.ensure_one()
        if not self.backup_bucket_path:
            raise UserError(_(
                "No snapshot path configured on service '%s'."
            ) % self.name)

        bucket_path = self.backup_bucket_path.strip('/')
        cfg = self._get_storage_config()

        if cfg['provider'] == 'gcs':
            import json
            from google.cloud import storage as gcs_storage
            from google.oauth2 import service_account
            if not cfg['gcs_credentials']:
                raise UserError(_(
                    "GCS service account key is not configured in Settings."
                ))
            key_info = json.loads(cfg['gcs_credentials'])
            credentials = service_account.Credentials.from_service_account_info(key_info)
            client = gcs_storage.Client(
                credentials=credentials, project=key_info.get('project_id'),
            )
            bucket = client.bucket(cfg['bucket'])
            blob = bucket.blob(bucket_path)
            return blob.generate_signed_url(
                expiration=dt.timedelta(hours=1), method='GET',
            )
        else:
            import boto3
            from botocore.config import Config as BotoConfig
            region = cfg['region'] or 'us-east-1'
            kwargs = {
                'aws_access_key_id': cfg['access_key'],
                'aws_secret_access_key': cfg['secret_key'],
                'region_name': region,
            }
            if cfg['provider'] == 'digitalocean':
                kwargs['endpoint_url'] = 'https://%s.digitaloceanspaces.com' % region
            elif cfg['provider'] == 'hetzner':
                region = cfg['region'] or 'fsn1'
                kwargs['region_name'] = region
                kwargs['endpoint_url'] = 'https://%s.your-objectstorage.com' % region
                kwargs['config'] = BotoConfig(s3={'addressing_style': 'virtual'})
            elif cfg['endpoint_url']:
                kwargs['endpoint_url'] = cfg['endpoint_url']
                kwargs['config'] = BotoConfig(s3={'addressing_style': 'path'})
            client = boto3.client('s3', **kwargs)
            return client.generate_presigned_url(
                'get_object',
                Params={'Bucket': cfg['bucket'], 'Key': bucket_path},
                ExpiresIn=3600,
            )


class SaasProductRepo(models.Model):
    _name = 'saas.product.repo'
    _description = 'SaaS Product Repository'
    _order = 'sequence, id'
    _sql_constraints = [
        ('unique_repo_per_product',
         'UNIQUE(product_id, repo_url)',
         'This repository is already added to this service.'),
    ]

    product_id = fields.Many2one(
        'saas.product',
        string='Service',
        required=True,
        ondelete='cascade',
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(
        string='Name',
        compute='_compute_name',
        store=True,
        help='Repository name derived from the URL.',
    )
    repo_url = fields.Char(
        string='Repository URL',
        required=True,
        help='Git clone URL (HTTPS). e.g. https://github.com/user/repo.git',
    )
    branch = fields.Char(
        string='Branch',
        default='main',
        required=True,
    )
    github_token = EncryptedChar(
        string='GitHub Token',
        help='Personal access token for private repositories. '
             'Leave empty for public repos.',
        copy=False,
        groups='base.group_system',
    )
    addons_subdir = fields.Char(
        string='Addons Subdirectory',
        help='Subdirectory inside the repo that contains addons. '
             'Leave empty if addons are at the root of the repo.',
    )

    @api.depends('repo_url')
    def _compute_name(self):
        for rec in self:
            if rec.repo_url:
                url = rec.repo_url.rstrip('/')
                if url.endswith('.git'):
                    url = url[:-4]
                rec.name = url.split('/')[-1] if '/' in url else url
            else:
                rec.name = ''

    def _get_repo_dir_name(self):
        """Return a safe directory name for this repo."""
        self.ensure_one()
        return self.name or 'repo_%d' % self.id

    def _get_clone_url(self):
        """Return the clone URL, injecting token if needed for private repos."""
        self.ensure_one()
        url = self.repo_url
        token = self.sudo().github_token
        if token and url.startswith('https://'):
            url = 'https://x-access-token:%s@%s' % (
                token, url[len('https://'):]
            )
        return url

    def _get_container_addons_path(self):
        """Return the addons path inside the container for this repo."""
        self.ensure_one()
        base = '/mnt/extra-addons/%s' % self._get_repo_dir_name()
        if self.addons_subdir:
            return '%s/%s' % (base, self.addons_subdir.strip('/'))
        return base


class SaasProductFeature(models.Model):
    _name = 'saas.product.feature'
    _description = 'SaaS Product Feature Line'
    _order = 'sequence, id'

    product_id = fields.Many2one(
        'saas.product',
        string='Service',
        required=True,
        ondelete='cascade',
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(string='Feature', required=True)
    icon = fields.Char(
        string='Icon',
        default='fa fa-check',
        help='Font Awesome icon class for this feature bullet.',
    )
