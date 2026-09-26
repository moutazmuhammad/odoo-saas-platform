import datetime
import logging
import shlex
import time
import urllib.parse

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

DEFAULT_MAX_BACKUPS = 7
# Presigned download links are bearer authority over a full tenant backup, so
# they live for minutes, not days (SEC-008). The link is re-minted lazily by
# _refresh_download_url whenever the customer lists/opens backups, so a short
# TTL costs nothing but slams the data-exfiltration window shut.
PRESIGNED_URL_EXPIRY = 15 * 60
# On-demand backups are transient: the customer clicks "Download", we
# build + upload to the bucket, hand back a presigned URL the browser
# downloads from directly, and then the object is reaped — it is NOT
# retained on the bucket. The object lives just long enough for the
# download to complete (1 hour), and the presigned URL is signed for
# the same window so the link dies with the object. The cleanup cron
# then removes both the bucket object and the local record. Combined
# with the single-slot policy (a new on-demand backup wipes the prior
# one) the bucket never accumulates customer backups.
ONDEMAND_URL_EXPIRY = 1 * 3600
ONDEMAND_PREFIX = 'ondemand'

# Per-read socket timeout while streaming a backup. Must exceed the
# longest gap between pg_dump output chunks (NOT the total backup
# duration) — generous so a slow disk can't trip it, finite so a wedged
# host can't hang the worker thread forever.
BACKUP_STREAM_READ_TIMEOUT = 3600


# In-pod builder for the on-demand ZIP. Runs INSIDE the target pod
# itself (via KubernetesDriver.exec_stream_out — see
# _stream_odoo_zip_to_bucket), streaming an Odoo-format zip
# (manifest.json + dump.sql + filestore/) straight to its own stdout,
# which IS the exec channel Odoo reads from — nothing is staged on
# disk, so DB size is not a constraint. ``dump.sql`` is PLAIN SQL (not
# -Fc) because that's what Odoo's zip-restore feeds to psql.
#
# Unlike the ssh_docker-era ``_HOST_ZIP_BUILDER`` (which ran on the
# DOCKER HOST and shelled out to ``docker exec``/``docker exec ... tar``
# to reach the container from outside), this script already runs
# INSIDE the same container as the target database's client tools and
# filestore mount — so pg_dump is a plain local subprocess and the
# filestore is walked directly with ``os.walk`` instead of round-
# tripping through a ``tar`` subprocess. DB connection info comes from
# ``/etc/odoo/odoo.conf`` (see ``saas_instance.py``'s
# ``_PSQL_CONN_PRELUDE`` note), not from separate SSH env vars — there
# is no host-side SSH env to populate any more.
_ZIP_BUILDER_SCRIPT = r'''
import configparser, os, shutil, subprocess, sys, zipfile

DB = %(db)r
MANIFEST = %(manifest)r
CHUNK = 4 * 1024 * 1024

cfg = configparser.ConfigParser()
cfg.read('/etc/odoo/odoo.conf')
o = cfg['options']
env = dict(os.environ)
env['PGPASSWORD'] = o.get('db_password', '')

out = sys.stdout.buffer
zf = zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, allowZip64=True)
zf.writestr('manifest.json', MANIFEST)

p = subprocess.Popen(
    ['pg_dump', '-h', o.get('db_host', 'localhost'), '-p', o.get('db_port', '5432'),
     '-U', o.get('db_user', 'odoo'), '-d', DB, '--no-owner', '--no-privileges'],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
with zf.open('dump.sql', 'w') as e:
    shutil.copyfileobj(p.stdout, e, CHUNK)
rc = p.wait()
if rc != 0:
    sys.stderr.write(p.stderr.read().decode('utf-8', 'replace'))
    sys.exit(10)

for candidate in ('/var/lib/odoo/filestore/' + DB,
                   '/var/lib/odoo/.local/share/Odoo/filestore/' + DB):
    if os.path.isdir(candidate):
        for root, _dirs, files in os.walk(candidate):
            for fname in files:
                fpath = os.path.join(root, fname)
                arcname = 'filestore/' + os.path.relpath(fpath, candidate)
                zf.write(fpath, arcname)
        break

zf.close()
out.flush()
'''


class _CountingReader:
    """Wrap a readable file-like and tally bytes read.

    Lets the streaming upload report the final object size without
    buffering anything — we never know the size of a streamed
    ``pg_dump`` up front.
    """

    def __init__(self, fileobj):
        self._f = fileobj
        self.bytes_read = 0

    def read(self, size=-1):
        chunk = self._f.read(size)
        self.bytes_read += len(chunk)
        return chunk


class SaasInstanceBackup(models.Model):
    _name = 'saas.instance.backup'
    _description = 'SaaS Instance Backup'
    _order = 'create_date desc'

    instance_id = fields.Many2one(
        'saas.instance', string='Instance',
        required=True, ondelete='cascade', index=True,
    )
    name = fields.Char(string='Backup Name', required=True)
    db_name = fields.Char(
        string='Database', index=True,
        help='PostgreSQL database name this backup is a snapshot of. '
             'For service instances this equals instance.subdomain. '
             'Hosting instances can have multiple databases; the cron '
             'creates one backup record per database per day.',
    )
    bucket_path = fields.Char(
        string='Bucket Path', readonly=True,
        help='Full object key inside the cloud bucket.',
    )
    size_mb = fields.Float(string='Size (MB)', readonly=True)
    state = fields.Selection([
        ('running', 'In Progress'),
        ('done', 'Done'),
        ('failed', 'Failed'),
    ], string='Status', default='running', required=True)
    error_message = fields.Text(string='Error', readonly=True)
    download_url = fields.Char(
        string='Download URL', readonly=True,
        help='Presigned download link valid for 7 days.',
    )
    download_url_expiry = fields.Datetime(
        string='Link Expires', readonly=True,
    )
    ephemeral = fields.Boolean(
        string='On-Demand', default=False, index=True,
        help='True for on-demand backups requested by the customer. '
             'The bucket object is deleted automatically 8 hours after '
             'creation via the ephemeral-cleanup cron.',
    )
    expires_at = fields.Datetime(
        string='Auto-Delete At', index=True,
        help='When this on-demand backup is reaped from the bucket. '
             'Only set on ephemeral backups.',
    )
    is_full_instance = fields.Boolean(
        string='Full Instance', default=False, index=True,
        help='A complete instance snapshot produced by the Kubernetes '
             "operator's own backup mechanism (spec.backup CronJob): a "
             'pg_dump of the instance\'s primary database plus a tar of '
             'the whole filestore volume. Restorable as a single unit.',
    )
    format = fields.Selection(
        [('zip', 'Zip (dump.sql + filestore)'),
         ('dump', 'SQL dump (pg_dump custom)'),
         ('operator', 'Operator (pg_dump + filestore tar)'),
         ('restic', 'Restic (deduplicated) — legacy, pre-Kubernetes')],
        string='Backup Format', default='zip', index=True,
        help='Storage format. Full-instance snapshots use ``operator`` '
             "(the Kubernetes operator's own CronJob-produced db.dump + "
             'filestore.tar.gz pair; see set_scheduled_backup). '
             '``restic`` rows are historical, from before the ssh_docker '
             'backend was removed, and are read-only going forward. '
             'On-demand per-database backups are either ``zip`` (Odoo '
             "zip: dump.sql + filestore, restorable via Odoo's database "
             "manager) or ``dump`` (pg_dump custom format, DB only, "
             'restorable via pg_restore) — both streamed straight to '
             'storage so any DB size works.',
    )
    restic_run_tag = fields.Char(
        string='Restic Run Tag', index=True,
        help='ISO-8601 timestamp used as the restic ``run`` tag '
             'binding together all snapshots from one backup run '
             '(one per database + one for the filesystem). Set only '
             'on legacy ``format=restic`` rows.',
    )
    restic_db_names = fields.Char(
        string='Restic DB Snapshots',
        help='Comma-separated list of databases included in this '
             'restic run, used at restore time to enumerate which '
             'per-DB snapshots to fetch by tag. Set only on '
             '``format=restic`` rows.',
    )

    def _refresh_download_url(self):
        """Regenerate presigned URL if expired or missing."""
        now = fields.Datetime.now()
        for rec in self:
            if rec.state != 'done' or not rec.bucket_path:
                continue
            if rec.download_url and rec.download_url_expiry and rec.download_url_expiry > now:
                continue
            try:
                url = rec._generate_presigned_url()
                rec.write({
                    'download_url': url,
                    'download_url_expiry': now + datetime.timedelta(seconds=PRESIGNED_URL_EXPIRY),
                })
            except Exception as e:
                _logger.warning("Failed to refresh download URL for backup %s: %s", rec.id, e)

    def action_download(self):
        self.ensure_one()
        self._refresh_download_url()
        if not self.download_url:
            raise UserError(_(
                "We couldn't generate the download link right now. "
                "Please try again in a moment."
            ))
        return {
            'type': 'ir.actions.act_url',
            'url': self.download_url,
            'target': 'new',
        }

    def action_restore(self):
        """Restore this backup to its instance.

        Dispatches by backup shape:
        - ``is_full_instance`` → ``action_restore_full_instance``, which
          downloads the operator-produced ``db.dump`` + ``filestore.tar.gz``
          pair and replays them with the exact same ``pg_restore``/``tar``
          invocation the operator's own restore tool uses (or, for legacy
          ``format='restic'`` rows predating the Kubernetes-only backend,
          this dispatch target no longer supports them — see that
          method's docstring).
        - Otherwise (per-database ``zip``/``dump``) → ``action_restore_backup``,
          which downloads the single object and replays the SQL dump +
          filestore inside it.

        Without this dispatch, clicking Restore on the backend form of a
        full-instance snapshot ran the per-DB path, which then crashed in
        ``_generate_presigned_url`` because a full-instance run has TWO
        artifacts under a stamp directory, not one single object key.
        """
        self.ensure_one()
        if self.is_full_instance:
            self.instance_id.action_restore_full_instance(self.id)
        else:
            self.instance_id.action_restore_backup(self.id)
        return True

    def action_delete_backup(self):
        self.ensure_one()
        # The unlink() override below also deletes the cloud object;
        # action_delete_backup is kept as a thin wrapper for the UI button.
        self.unlink()
        return True

    def unlink(self):
        """Delete cloud objects when the backup record is removed.

        Without this override, cancelling/deleting an instance triggers
        ondelete='cascade' on instance_id and silently leaks every
        backup object in cloud storage. We best-effort delete here; failures
        are logged but do not block the unlink.
        """
        for rec in self:
            if rec.bucket_path and rec.state == 'done':
                try:
                    rec._delete_from_bucket()
                except Exception:
                    _logger.exception(
                        "Failed to delete cloud object for backup %s "
                        "(path: %s) on unlink — orphan possible.",
                        rec.id, rec.bucket_path,
                    )
        return super().unlink()

    # ------------------------------------------------------------------
    # Cloud storage helpers
    # ------------------------------------------------------------------
    def _get_backup_config(self):
        """Return backup configuration from system parameters."""
        ICP = self.env['ir.config_parameter'].sudo()
        provider = ICP.get_param('saas_backup.provider', '')
        bucket = ICP.get_param('saas_backup.bucket_name', '')
        if not provider or not bucket:
            raise UserError(_(
                "Backups aren't available right now. Please contact "
                "support so we can get them turned on for your account."
            ))
        return {
            'provider': provider,
            'bucket': bucket,
            'access_key': ICP.get_param('saas_backup.access_key', ''),
            'secret_key': ICP.get_param('saas_backup.secret_key', ''),
            'region': ICP.get_param('saas_backup.region', ''),
            'endpoint': ICP.get_param('saas_backup.endpoint', ''),
            'service_account_key': ICP.get_param('saas_backup.service_account_key', ''),
        }

    def _get_s3_client(self, access_key=None, secret_key=None):
        """Return a boto3 S3-compatible client configured from settings.

        ``access_key`` / ``secret_key`` override the stored backup
        credentials — used by the CORS button, which runs under a
        separate, broader-privilege key than the least-privilege key
        used for object read/write.
        """
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError:
            raise UserError(_("The 'boto3' Python package is required. Install it with: pip install boto3"))

        cfg = self._get_backup_config()
        ak = access_key or cfg['access_key']
        sk = secret_key or cfg['secret_key']
        if not ak or not sk:
            raise UserError(_(
                "Access Key and Secret Key are required for %s. "
                "Go to SaaS Manager > Configuration > Settings."
            ) % cfg['provider'].upper())

        region = cfg['region'] or 'us-east-1'
        kwargs = {
            'aws_access_key_id': ak,
            'aws_secret_access_key': sk,
            'region_name': region,
        }

        if cfg['provider'] == 'digitalocean':
            # DigitalOcean Spaces requires virtual-hosted style addressing
            # for presigned URLs to work correctly.
            # Endpoint: https://{region}.digitaloceanspaces.com
            kwargs['endpoint_url'] = 'https://%s.digitaloceanspaces.com' % region
            kwargs['config'] = BotoConfig(s3={'addressing_style': 'virtual'})
        elif cfg['provider'] == 'hetzner':
            # Hetzner Object Storage — virtual-hosted style, endpoint
            # derived from the region (fsn1 / nbg1 / hel1).
            # Endpoint: https://{region}.your-objectstorage.com
            region = cfg['region'] or 'fsn1'
            kwargs['region_name'] = region
            kwargs['endpoint_url'] = 'https://%s.your-objectstorage.com' % region
            kwargs['config'] = BotoConfig(s3={'addressing_style': 'virtual'})
        elif cfg['endpoint']:
            kwargs['endpoint_url'] = cfg['endpoint']
            kwargs['config'] = BotoConfig(s3={'addressing_style': 'path'})

        return boto3.client('s3', **kwargs), cfg['bucket']

    def _get_gcs_client(self):
        """Return a google-cloud-storage client configured from settings."""
        try:
            from google.cloud import storage as gcs_storage
            from google.oauth2 import service_account
        except ImportError:
            raise UserError(_(
                "The 'google-cloud-storage' Python package is required. "
                "Install it with: pip install google-cloud-storage"
            ))

        import json as _json

        cfg = self._get_backup_config()
        sa_key = cfg['service_account_key']
        if not sa_key:
            raise UserError(_(
                "Service Account JSON Key is required for Google Cloud Storage. "
                "Go to SaaS Manager > Configuration > Settings."
            ))

        try:
            key_info = _json.loads(sa_key)
        except (ValueError, TypeError):
            raise UserError(_("Invalid Service Account JSON Key. Please check the format."))

        credentials = service_account.Credentials.from_service_account_info(key_info)
        client = gcs_storage.Client(credentials=credentials, project=key_info.get('project_id'))
        return client, cfg['bucket']

    def _upload_to_bucket(self, object_key, data_bytes):
        cfg = self._get_backup_config()
        _logger.info(
            "Uploading backup to %s bucket=%s key=%s size=%d bytes",
            cfg['provider'], cfg['bucket'], object_key, len(data_bytes),
        )
        if cfg['provider'] == 'gcs':
            client, bucket_name = self._get_gcs_client()
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(object_key)
            blob.upload_from_string(data_bytes, content_type='application/zip')
        else:
            client, bucket = self._get_s3_client()
            _logger.info(
                "S3 client endpoint=%s region=%s bucket=%s",
                client.meta.endpoint_url, client.meta.region_name, bucket,
            )
            try:
                client.put_object(
                    Bucket=bucket,
                    Key=object_key,
                    Body=data_bytes,
                    ContentType='application/zip',
                )
            except Exception as e:
                _logger.error(
                    "put_object failed: %s — trying upload_fileobj fallback", e,
                )
                # Fallback: use upload_fileobj which handles chunked upload
                import io
                client.upload_fileobj(
                    io.BytesIO(data_bytes),
                    bucket,
                    object_key,
                    ExtraArgs={'ContentType': 'application/zip'},
                )

    def _upload_stream_to_bucket(self, object_key, fileobj,
                                 content_type='application/octet-stream'):
        """Stream ``fileobj`` to the configured bucket via the SDK's
        native multipart/resumable upload.

        Bounded memory (one chunk at a time), no object-size limit, no
        temp file — this is what lets an on-demand backup of a 50 GB+
        database succeed where the old single-PUT path (5 GB cap, whole
        file in RAM) could not.
        """
        cfg = self._get_backup_config()
        if cfg['provider'] == 'gcs':
            client, bucket_name = self._get_gcs_client()
            blob = client.bucket(bucket_name).blob(object_key)
            # Resumable upload requires an explicit chunk size for a
            # non-seekable stream.
            blob.chunk_size = 16 * 1024 * 1024
            blob.upload_from_file(fileobj, content_type=content_type)
        else:
            from boto3.s3.transfer import TransferConfig
            client, bucket = self._get_s3_client()
            # ``use_threads=False`` so parts are read sequentially from
            # the (non-seekable) pipe; 64 MB parts keep the part count
            # well under S3's 10k limit even for very large dumps.
            transfer = TransferConfig(
                multipart_threshold=16 * 1024 * 1024,
                multipart_chunksize=64 * 1024 * 1024,
                use_threads=False,
            )
            client.upload_fileobj(
                fileobj, bucket, object_key,
                ExtraArgs={'ContentType': content_type},
                Config=transfer,
            )

    def _bucket_object_size(self, object_key):
        """Return the byte size of a bucket object (0 if unknown)."""
        cfg = self._get_backup_config()
        try:
            if cfg['provider'] == 'gcs':
                client, bucket_name = self._get_gcs_client()
                blob = client.bucket(bucket_name).get_blob(object_key)
                return blob.size if blob and blob.size else 0
            client, bucket = self._get_s3_client()
            head = client.head_object(Bucket=bucket, Key=object_key)
            return head.get('ContentLength', 0) or 0
        except Exception:
            return 0

    def _stream_pg_dump_to_bucket(self, instance, object_key, db_name):
        """Stream ``pg_dump -Fc`` from the instance's container straight
        to object storage. Returns the uploaded size in bytes.

        Produces Odoo's native "dump" custom format (the same thing the
        database-manager "pg_dump custom format" backup gives), so a
        developer can restore it locally with ``pg_restore`` or via
        Odoo's Restore. It's diskless and bounded-memory end to end:
        ``pg_dump`` runs INSIDE the target pod (via
        ``KubernetesDriver.exec_stream_out``) and its stdout is handed
        straight to the SDK's multipart uploader, so database size is
        not a constraint. Connection info comes from ``/etc/odoo/
        odoo.conf`` inside the pod, same as ``saas.instance._docker_exec_sql``
        — there is no more separately-configured ``psql_port``.
        """
        script = (
            "import configparser, os, subprocess, sys\n"
            "cfg = configparser.ConfigParser()\n"
            "cfg.read('/etc/odoo/odoo.conf')\n"
            "o = cfg['options']\n"
            "env = dict(os.environ)\n"
            "env['PGPASSWORD'] = o.get('db_password', '')\n"
            "p = subprocess.Popen(['pg_dump', '-Fc', '-Z3',\n"
            "    '-h', o.get('db_host', 'localhost'), '-p', o.get('db_port', '5432'),\n"
            "    '-U', o.get('db_user', 'odoo'), '-d', %s, '--no-owner'],\n"
            "    stdout=sys.stdout.buffer, stderr=subprocess.PIPE, env=env)\n"
            "rc = p.wait()\n"
            "if rc != 0:\n"
            "    sys.stderr.write(p.stderr.read().decode('utf-8', 'replace'))\n"
            "    sys.exit(rc)\n"
        ) % repr(db_name)
        command = "python3 - <<'SAAS_DUMP_EOF'\n%s\nSAAS_DUMP_EOF" % script
        raw = instance._compute_driver().exec_stream_out(
            instance._compute_handle(), command, timeout=BACKUP_STREAM_READ_TIMEOUT)
        reader = _CountingReader(raw)
        upload_error = None
        try:
            self._upload_stream_to_bucket(object_key, reader)
        except Exception as e:
            upload_error = e
        exit_code = raw.returncode
        err_tail = (raw.stderr or '')[-2000:]

        if upload_error is not None or exit_code != 0:
            # Don't leave a truncated/corrupt object behind.
            try:
                self.delete_bucket_path(object_key)
            except Exception:
                pass
            if exit_code != 0:
                raise UserError(_(
                    "The database backup (pg_dump) failed:\n%s"
                ) % (err_tail or 'exit code %s' % exit_code))
            raise UserError(_(
                "Uploading the backup failed:\n%s"
            ) % upload_error)

        return reader.bytes_read

    def _stream_odoo_zip_to_bucket(self, instance, object_key, db_name):
        """Stream an Odoo-format zip (manifest + plain dump.sql +
        filestore) from the instance's pod straight to object storage.
        Returns the uploaded size in bytes.

        Diskless and bounded-memory: ``_ZIP_BUILDER_SCRIPT`` runs INSIDE
        the pod, writes the zip to its own stdout (pg_dump piped into a
        zip entry, filestore walked directly off the pod's own
        filestore mount), and Odoo pipes that into the SDK's multipart
        uploader — so DB/filestore size is not a constraint. The result
        restores via Odoo's database manager (same layout as Odoo's own
        zip backup).
        """
        import json
        manifest = json.dumps({
            'odoo_version': instance.odoo_version_id.name or '',
            'database': db_name,
            'partner': instance.partner_id.name or '',
            'timestamp': fields.Datetime.now().isoformat(),
            'instance': instance.name or '',
        }, indent=2)

        script = _ZIP_BUILDER_SCRIPT % {'db': db_name, 'manifest': manifest}
        command = "python3 - <<'SAAS_ZIP_EOF'\n%s\nSAAS_ZIP_EOF" % script
        raw = instance._compute_driver().exec_stream_out(
            instance._compute_handle(), command, timeout=BACKUP_STREAM_READ_TIMEOUT)
        reader = _CountingReader(raw)
        upload_error = None
        try:
            self._upload_stream_to_bucket(
                object_key, reader, content_type='application/zip')
        except Exception as e:
            upload_error = e
        exit_code = raw.returncode
        err_tail = (raw.stderr or '')[-2000:]

        if upload_error is not None or exit_code != 0:
            try:
                self.delete_bucket_path(object_key)
            except Exception:
                pass
            if exit_code != 0:
                raise UserError(_(
                    "The database backup (zip) failed:\n%s"
                ) % (err_tail or 'exit code %s' % exit_code))
            raise UserError(_(
                "Uploading the backup failed:\n%s"
            ) % upload_error)

        return reader.bytes_read

    def _generate_presigned_url(self, expiry=None):
        """Return a presigned GET URL for this backup's bucket object.

        ``expiry`` overrides the default 7-day TTL — used by on-demand
        backups (1 hour) so the URL dies with the bucket object.
        """
        ttl = expiry or PRESIGNED_URL_EXPIRY
        cfg = self._get_backup_config()
        if cfg['provider'] == 'gcs':
            client, bucket_name = self._get_gcs_client()
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(self.bucket_path)
            return blob.generate_signed_url(
                expiration=datetime.timedelta(seconds=ttl),
                method='GET',
            )
        else:
            client, bucket = self._get_s3_client()
            return client.generate_presigned_url(
                'get_object',
                Params={'Bucket': bucket, 'Key': self.bucket_path},
                ExpiresIn=ttl,
            )

    def _generate_presigned_put_url(self, object_key):
        """Generate a presigned PUT URL for direct server-to-bucket upload."""
        cfg = self._get_backup_config()
        if cfg['provider'] == 'gcs':
            client, bucket_name = self._get_gcs_client()
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(object_key)
            return blob.generate_signed_url(
                expiration=datetime.timedelta(hours=1),
                method='PUT',
                content_type='application/zip',
            )
        else:
            client, bucket = self._get_s3_client()
            return client.generate_presigned_url(
                'put_object',
                Params={
                    'Bucket': bucket,
                    'Key': object_key,
                    'ContentType': 'application/zip',
                },
                ExpiresIn=3600,
            )

    def _delete_from_bucket(self):
        self.ensure_one()
        if self.bucket_path:
            self.delete_bucket_path(self.bucket_path)

    @api.model
    def apply_bucket_cors(self, origins=None):
        """Configure the storage bucket's CORS policy.

        Needed so a customer's browser can upload a restore file
        straight to the bucket (presigned PUT) — browsers block a
        cross-origin PUT unless the bucket explicitly allows the app's
        origin. This does that from Odoo itself (using the configured
        credentials) so the admin never has to touch the cloud console.

        One-time, idempotent (safe to re-run). ``origins`` defaults to
        the app's own origin derived from ``web.base.url``. Returns the
        list of origins applied.
        """
        self._get_backup_config()  # validates provider/bucket are set
        if not origins:
            base = self.env['ir.config_parameter'].sudo().get_param(
                'web.base.url', '',
            )
            parsed = urllib.parse.urlparse(base or '')
            if not parsed.netloc:
                raise UserError(_(
                    "Set the System Parameter 'web.base.url' to your portal "
                    "address first (e.g. https://saas.odex.sa)."
                ))
            # The browser sends the origin of the page it's on. A portal
            # served over HTTPS sends ``https://host`` even if
            # ``web.base.url`` is recorded as http (common behind a
            # proxy). Allow BOTH schemes so the rule matches the real
            # origin regardless of that mismatch.
            origins = [
                'https://%s' % parsed.netloc,
                'http://%s' % parsed.netloc,
            ]

        # The CORS button runs under a dedicated, broader-privilege key
        # (it needs PutBucketCORS) kept separate from the least-privilege
        # key used for object read/write. Fall back to the backup key
        # only if no dedicated key is configured.
        ICP = self.env['ir.config_parameter'].sudo()
        cors_ak = ICP.get_param('saas_backup.cors_access_key', '') or None
        cors_sk = ICP.get_param('saas_backup.cors_secret_key', '') or None
        client, bucket = self._get_s3_client(
            access_key=cors_ak, secret_key=cors_sk,
        )
        try:
            client.put_bucket_cors(
                Bucket=bucket,
                CORSConfiguration={'CORSRules': [{
                    'AllowedOrigins': origins,
                    'AllowedMethods': ['PUT', 'GET'],
                    'AllowedHeaders': ['*'],
                    'ExposeHeaders': ['ETag'],
                    'MaxAgeSeconds': 3600,
                }]},
            )
        except Exception as e:
            msg = str(e)
            _logger.warning("put_bucket_cors failed: %s", msg)
            if 'AccessDenied' in msg or 'Forbidden' in msg or '403' in msg:
                # The backup key is least-privilege (object read/write); it
                # can't change bucket-level config. Tell the admin exactly
                # how to fix it — either widen the key, or set the rule
                # once by hand (the values they'd need are right here).
                raise UserError(_(
                    "Your storage key isn't allowed to change the bucket's "
                    "CORS policy (Access Denied on PutBucketCORS).\n\n"
                    "Pick one:\n\n"
                    "1) Grant the key the 's3:PutBucketCORS' permission "
                    "(AWS / Hetzner), or use a Spaces key with full access "
                    "(DigitalOcean), then click this button again.\n\n"
                    "2) Or add this CORS rule once in your provider's "
                    "console:\n"
                    "   • Allowed origins: %s\n"
                    "   • Allowed methods: PUT, GET\n"
                    "   • Allowed headers: *\n"
                    "   • Max age: 3600 seconds"
                ) % ', '.join(origins))
            raise UserError(_(
                "Couldn't set the bucket CORS policy: %s"
            ) % msg)
        _logger.info("Applied bucket CORS for origins %s", origins)
        return origins

    # Hard limit on backup size we'll download just to inspect manifest.
    # For larger backups the version check is skipped — safer to allow
    # the restore than to OOM the worker reading hundreds of MB.
    _MANIFEST_PEEK_MAX_MB = 100

    def _read_manifest_safe(self):
        """Read manifest.json from this backup's bucket object if available.

        Returns the parsed dict, or None if anything fails. Skips the
        check entirely for backups larger than _MANIFEST_PEEK_MAX_MB to
        avoid memory blowup. Used by restore to verify Odoo-version
        compatibility before nuking the target database.
        """
        self.ensure_one()
        if not self.bucket_path:
            return None
        if self.size_mb and self.size_mb > self._MANIFEST_PEEK_MAX_MB:
            _logger.info(
                "Skipping manifest check for %s (size %.1f MB > %d MB)",
                self.bucket_path, self.size_mb, self._MANIFEST_PEEK_MAX_MB,
            )
            return None
        try:
            cfg = self._get_backup_config()
            import io
            import json as _json
            import zipfile
            buf = io.BytesIO()
            if cfg['provider'] == 'gcs':
                client, bucket_name = self._get_gcs_client()
                bucket = client.bucket(bucket_name)
                blob = bucket.blob(self.bucket_path)
                blob.download_to_file(buf)
            else:
                client, bucket_name = self._get_s3_client()
                client.download_fileobj(bucket_name, self.bucket_path, buf)
            buf.seek(0)
            with zipfile.ZipFile(buf) as zf:
                if 'manifest.json' not in zf.namelist():
                    return None
                with zf.open('manifest.json') as mf:
                    return _json.load(mf)
        except Exception:
            _logger.warning(
                "Could not read manifest from backup %s",
                self.bucket_path, exc_info=True,
            )
            return None

    @api.model
    def delete_bucket_path(self, bucket_path):
        """Delete an arbitrary object key from the configured backup bucket.

        Use this when you have a bucket path but no `saas.instance.backup`
        record (e.g. retained backup paths after the source instance has
        been wiped). Replaces the previous `Backup.new(...)._delete_from_bucket()`
        anti-pattern.

        Public: also called from saas_billing's retained-restore wizard.
        """
        if not bucket_path:
            return
        try:
            cfg = self._get_backup_config()
            if cfg['provider'] == 'gcs':
                client, bucket_name = self._get_gcs_client()
                bucket = client.bucket(bucket_name)
                blob = bucket.blob(bucket_path)
                blob.delete()
            else:
                client, bucket = self._get_s3_client()
                client.delete_object(Bucket=bucket, Key=bucket_path)
        except Exception as e:
            _logger.warning("Failed to delete backup object %s: %s", bucket_path, e)

    @api.model
    def _delete_bucket_prefix(self, prefix):
        """Delete every object under a bucket "directory" (best-effort).

        Full-instance (``operator``-format) backups are TWO+ objects
        under one stamp directory (``db.dump``, ``filestore.tar.gz``,
        ``manifest.json``), unlike a per-DB backup's single object key
        — this is the bulk equivalent of :meth:`delete_bucket_path` for
        that shape, used e.g. when pruning old snapshots on
        cancellation.
        """
        if not prefix:
            return
        try:
            cfg = self._get_backup_config()
            if cfg['provider'] == 'gcs':
                client, bucket_name = self._get_gcs_client()
                bucket = client.bucket(bucket_name)
                for blob in client.list_blobs(bucket_name, prefix=prefix + '/'):
                    blob.delete()
            else:
                client, bucket = self._get_s3_client()
                paginator = client.get_paginator('list_objects_v2')
                for page in paginator.paginate(Bucket=bucket, Prefix=prefix + '/'):
                    keys = [{'Key': o['Key']} for o in page.get('Contents', [])]
                    if keys:
                        client.delete_objects(Bucket=bucket, Delete={'Objects': keys})
        except Exception as e:
            _logger.warning("Failed to delete backup prefix %s: %s", prefix, e)

    def _move_to_cancelled_folder(self):
        """Move this backup's cloud object into the cancelled_backups/ prefix.

        Returns the new object key, or False on failure.
        Used when an instance is deleted to keep one retained backup in
        a separate folder for easy identification and lifecycle management.
        """
        self.ensure_one()
        if not self.bucket_path:
            return False
        new_key = 'cancelled_backups/%s' % self.bucket_path
        try:
            cfg = self._get_backup_config()
            if cfg['provider'] == 'gcs':
                client, bucket_name = self._get_gcs_client()
                bucket = client.bucket(bucket_name)
                src_blob = bucket.blob(self.bucket_path)
                bucket.copy_blob(src_blob, bucket, new_key)
                src_blob.delete()
            else:
                client, bucket_name = self._get_s3_client()
                client.copy_object(
                    Bucket=bucket_name,
                    CopySource={'Bucket': bucket_name, 'Key': self.bucket_path},
                    Key=new_key,
                )
                client.delete_object(Bucket=bucket_name, Key=self.bucket_path)
            _logger.info(
                "Moved backup %s → %s in bucket %s",
                self.bucket_path, new_key, bucket_name,
            )
            return new_key
        except Exception as e:
            _logger.warning(
                "Failed to move backup %s to cancelled folder: %s",
                self.bucket_path, e,
            )
            # Fallback: keep the original path rather than losing track
            return self.bucket_path

    # ------------------------------------------------------------------
    # Full-instance backup/restore — Kubernetes-native redesign (Phase 5,
    # part B). The Go operator already runs its OWN cloud-agnostic
    # CronJob (pg_dump + filestore tar to object storage, see
    # compute/operator/internal/resources/backup.go and
    # compute/tools/backup-tool/run-backup.sh) once
    # ``saas.instance._sync_scheduled_backup`` has patched
    # ``spec.backup`` onto the instance's OdooInstance CR — Odoo no
    # longer drives the schedule or does the dump/tar work itself the
    # way the old restic-over-SSH cron did. This section's job is just
    # to mirror what the operator already wrote into the bucket into
    # ``saas.instance.backup`` records for the portal/admin UI, and to
    # restore from one back onto a live instance.
    #
    # Known limitation, inherited from the operator's own backup design
    # (not something to work around here): ``spec.backup`` dumps only
    # the instance's ONE primary database (``instance.subdomain``) plus
    # the ENTIRE filestore volume. For a multi-database hosting
    # instance, customer databases created via ``hosting_db_create``
    # beyond the primary one are NOT individually pg_dump'd by this
    # mechanism — only their filestore content (bundled in the same
    # tar) is covered. Per-database coverage for those still exists via
    # ``hosting_db_backup`` (on-demand, one DB at a time).
    # ------------------------------------------------------------------

    def _list_backup_stamps(self, cfg, prefix):
        """Return the set of run "stamps" (subdirectory names) that
        exist directly under ``<bucket>/<prefix>/`` — one per backup
        run, named ``run-backup.sh``'s own ``date -u +%Y%m%dT%H%M%SZ``
        format. Pure bucket listing, no Kubernetes exec."""
        stamps = set()
        try:
            if cfg['provider'] == 'gcs':
                client, bucket_name = self._get_gcs_client()
                it = client.list_blobs(
                    bucket_name, prefix=prefix + '/', delimiter='/')
                list(it)  # force iteration so .prefixes gets populated
                for p in (it.prefixes or []):
                    stamp = p[len(prefix) + 1:].rstrip('/')
                    if stamp:
                        stamps.add(stamp)
            else:
                client, bucket = self._get_s3_client()
                paginator = client.get_paginator('list_objects_v2')
                for page in paginator.paginate(
                        Bucket=bucket, Prefix=prefix + '/', Delimiter='/'):
                    for cp in page.get('CommonPrefixes', []):
                        stamp = cp['Prefix'][len(prefix) + 1:].rstrip('/')
                        if stamp:
                            stamps.add(stamp)
        except Exception:
            _logger.exception(
                "Failed to list backup stamps under prefix %s", prefix)
        return stamps

    def _record_full_instance_backup(self, instance, prefix, stamp):
        """Create/update the ``saas.instance.backup`` row mirroring the
        operator-written run at ``<prefix>/<stamp>/``. Returns the
        record, or an empty recordset if the artifacts aren't fully
        there yet (still uploading)."""
        bucket_path = '%s/%s' % (prefix, stamp)
        dump_size = self._bucket_object_size('%s/db.dump' % bucket_path)
        fs_size = self._bucket_object_size('%s/filestore.tar.gz' % bucket_path)
        if not dump_size and not fs_size:
            return self.browse()
        existing = self.search([
            ('instance_id', '=', instance.id),
            ('is_full_instance', '=', True),
            ('bucket_path', '=', bucket_path),
        ], limit=1)
        vals = {
            'instance_id': instance.id,
            'name': stamp,
            'db_name': False,
            'bucket_path': bucket_path,
            'size_mb': round((dump_size + fs_size) / (1024 * 1024), 2),
            'state': 'done',
            'is_full_instance': True,
            'ephemeral': False,
            'format': 'operator',
        }
        if existing:
            existing.write(vals)
            return existing
        return self.create(vals)

    @api.model
    def _cron_sync_scheduled_backups(self):
        """Mirror the operator's own backup CronJob output into
        ``saas.instance.backup`` records — pure bucket listing, no
        Kubernetes exec at all. Replaces ``_cron_backup_all_instances``
        (which used to DRIVE the backup itself via restic-over-SSH): the
        operator now owns the actual nightly schedule/execution once
        ``saas.instance._sync_scheduled_backup`` has enabled
        ``spec.backup`` on the CR.
        """
        instances = self.env['saas.instance'].search([
            ('daily_backup_enabled', '=', True),
            ('daily_backup_suspended', '=', False),
            ('state', 'in', ('running', 'stopped', 'suspended')),
        ])
        if not instances:
            return
        try:
            cfg = self._get_backup_config()
        except UserError:
            return
        for instance in instances:
            # Defensive resync, not just listing: `_sync_scheduled_backup`
            # is normally called once at the specific moments the flag
            # changes (checkout, payment webhook, suspend/resume, deploy
            # completion) — a direct field write bypassing those call
            # sites (e.g. an admin editing the backend form directly), or
            # a transient failure at one of those moments, would leave
            # the CR's spec.backup silently out of sync otherwise. Riding
            # along on this cron's own periodic sweep restores the
            # self-healing property the old restic-driven cron had for
            # free (it re-evaluated the flag from scratch every run). The
            # CR patch is idempotent, so re-asserting it here even when
            # nothing actually drifted is harmless.
            try:
                instance._sync_scheduled_backup()
            except Exception:
                _logger.exception(
                    "Backup sync: failed to resync backup config for %s",
                    instance.subdomain)
            prefix = instance._backup_bucket_prefix()
            try:
                remote_stamps = self._list_backup_stamps(cfg, prefix)
            except Exception:
                _logger.exception(
                    "Backup sync: failed to list bucket prefix %s", prefix)
                continue
            for stamp in remote_stamps:
                try:
                    self._record_full_instance_backup(instance, prefix, stamp)
                except Exception:
                    _logger.exception(
                        "Backup sync: failed to record stamp %s for %s",
                        stamp, instance.subdomain)
            # The operator's own CronJob already prunes by retention
            # (BackupSpec.Retention) — this just reaps OUR tracking rows
            # for runs it has already pruned remotely.
            local = self.search([
                ('instance_id', '=', instance.id),
                ('is_full_instance', '=', True),
                ('format', '=', 'operator'),
            ])
            for rec in local:
                stamp = (rec.bucket_path or '').rsplit('/', 1)[-1]
                if stamp and stamp not in remote_stamps:
                    try:
                        rec.unlink()
                    except Exception:
                        _logger.exception(
                            "Backup sync: failed to reap stale record %s",
                            rec.id)
        # Ride along on the same schedule the old daily cron used —
        # trims stale/excess on-demand backups (no longer full-instance
        # ones; see that method's own docstring).
        self._cleanup_old_backups()

    @api.model
    def _create_full_instance_backup_sync(self, instance, wait_timeout=180):
        """Best-effort, SYNCHRONOUS full-instance snapshot: trigger a
        one-off Job cloned from the (enabled-if-needed) backup CronJob,
        then poll the bucket until a new stamped run appears or
        ``wait_timeout`` elapses.

        Used by the pre-cancellation "final snapshot" courtesy capture,
        which must complete (or fail) before teardown proceeds — unlike
        the portal's own "Create Backup" button, which is fire-and-forget
        and lets :meth:`_cron_sync_scheduled_backups` pick the result up
        later. Raises on any failure; callers already treat this as
        best-effort (same contract the old restic path had) and catch
        accordingly.
        """
        driver = instance._compute_driver()
        handle = instance._compute_handle()
        cfg = self._get_backup_config()
        prefix = instance._backup_bucket_prefix()
        before = self._list_backup_stamps(cfg, prefix)
        driver.set_scheduled_backup(
            handle, enabled=True, bucket=cfg['bucket'], prefix=prefix,
            access_key=cfg['access_key'], secret_key=cfg['secret_key'],
            endpoint=cfg['endpoint'] or '')
        driver.trigger_backup_now(handle)
        deadline = time.time() + wait_timeout
        stamp = None
        while time.time() < deadline:
            time.sleep(5)
            after = self._list_backup_stamps(cfg, prefix)
            new = after - before
            if new:
                stamp = sorted(new)[-1]
                break
        if not stamp:
            raise UserError(_("Timed out waiting for the snapshot to finish."))
        backup = self._record_full_instance_backup(instance, prefix, stamp)
        if not backup:
            raise UserError(_(
                "The snapshot job finished but its artifacts weren't "
                "found in the bucket."
            ))
        return backup

    def _presigned_get_url(self, object_key, expiry=None):
        """Presigned GET URL for an arbitrary object key (not
        necessarily ``self.bucket_path`` — a full-instance run has TWO
        artifacts under its stamp directory)."""
        ttl = expiry or PRESIGNED_URL_EXPIRY
        cfg = self._get_backup_config()
        if cfg['provider'] == 'gcs':
            client, bucket_name = self._get_gcs_client()
            blob = client.bucket(bucket_name).blob(object_key)
            return blob.generate_signed_url(
                expiration=datetime.timedelta(seconds=ttl), method='GET')
        client, bucket = self._get_s3_client()
        return client.generate_presigned_url(
            'get_object', Params={'Bucket': bucket, 'Key': object_key},
            ExpiresIn=ttl)

    def _do_restore_full_instance(self, instance_id):
        """Background worker (job model = ``saas.instance.backup``,
        ``self`` = the backup record being restored): restore this
        full-instance backup's ``db.dump`` + ``filestore.tar.gz`` onto
        the already-running instance ``instance_id`` — normally the
        backup's own, or another instance of the same customer when
        restoring a retained snapshot (saas_billing). The target's served
        database (odoo.conf ``db_name``) is the one recreated.

        Downloads both artifacts into the pod via presigned GET + curl
        (same "curl on the target" pattern the per-DB restore uses —
        see ``saas.instance._do_restore_backup``), then runs the EXACT
        same ``pg_restore``/``tar`` invocation the operator's own
        create-time restore tool uses
        (compute/tools/backup-tool/run-restore.sh), via
        ``driver.exec()`` against the live pod instead of a fresh
        restore Job — ``spec.restore`` is create-time-only and this
        instance already exists.
        """
        self.ensure_one()
        instance = self.env['saas.instance'].browse(instance_id)
        if not instance.exists():
            raise UserError(_("Instance no longer exists."))
        driver = instance._compute_driver()
        handle = instance._compute_handle()
        db_name = instance._served_db_name()

        dump_url = self._presigned_get_url('%s/db.dump' % self.bucket_path)
        fs_url = self._presigned_get_url('%s/filestore.tar.gz' % self.bucket_path)

        instance._append_log("Full-instance restore: downloading artifacts...")
        workdir = '/tmp/saas_full_restore_%s' % db_name
        driver.exec(handle, 'rm -rf %s && mkdir -p %s' % (
            shlex.quote(workdir), shlex.quote(workdir)))
        for label, url, fname in (
                ('db dump', dump_url, 'db.dump'),
                ('filestore archive', fs_url, 'filestore.tar.gz')):
            dest = '%s/%s' % (workdir, fname)
            r = driver.exec(
                handle, 'curl -fsSL -o %s %s' % (
                    shlex.quote(dest), shlex.quote(url)),
                timeout=1800)
            if not r.ok:
                raise UserError(_(
                    "Failed to download %s:\n%s\n%s"
                ) % (label, r.stdout, r.stderr))

        instance._append_log("Releasing connections to '%s'..." % db_name)
        safe_db = db_name.replace("'", "''")
        try:
            instance._docker_exec_sql(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname='%s' AND pid <> pg_backend_pid()" % safe_db,
                timeout=30,
            )
        except Exception as e:
            instance._append_log(
                "Note: connection release failed (%s); continuing." % e)

        instance._append_log("Recreating database '%s'..." % db_name)
        rc, out, err = instance._docker_exec_sql(
            'DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % db_name, timeout=120)
        if rc != 0:
            raise UserError(_("dropdb failed for %s:\n%s") % (db_name, err or out))
        rc, out, err = instance._docker_exec_sql(
            'CREATE DATABASE "%s"' % db_name, timeout=120)
        if rc != 0:
            raise UserError(_("createdb failed for %s:\n%s") % (db_name, err or out))

        instance._append_log("Restoring database (pg_restore)...")
        conn_prelude = (
            "import configparser, os, subprocess, sys\n"
            "cfg = configparser.ConfigParser()\n"
            "cfg.read('/etc/odoo/odoo.conf')\n"
            "o = cfg['options']\n"
            "env = dict(os.environ)\n"
            "env['PGPASSWORD'] = o.get('db_password', '')\n"
            "conn = ['-h', o.get('db_host', 'localhost'), '-p', o.get('db_port', '5432'),\n"
            "        '-U', o.get('db_user', 'odoo')]\n"
        )
        # Same flags run-restore.sh uses: --no-owner (the dump's
        # original role has no reason to exist here), --single-
        # transaction + --exit-on-error (a partial restore is worse
        # than no restore — see that script's own comment).
        script = (
            conn_prelude
            + "p = subprocess.run(['pg_restore'] + conn + ['-d', %s,\n"
              "    '--no-owner', '--single-transaction', '--exit-on-error', %s],\n"
              "    env=env, capture_output=True, text=True)\n"
              "sys.stdout.write(p.stdout)\n"
              "sys.stderr.write(p.stderr)\n"
              "sys.exit(p.returncode)\n"
        ) % (repr(db_name), repr('%s/db.dump' % workdir))
        command = "python3 - <<'SAAS_PGRESTORE_EOF'\n%s\nSAAS_PGRESTORE_EOF" % script
        r = driver.exec(handle, command, timeout=1800)
        if not r.ok:
            raise UserError(_(
                "pg_restore failed:\n%s"
            ) % (r.stderr or r.stdout)[-4000:])

        instance._append_log("Extracting filestore archive...")
        r = driver.exec(
            handle,
            'tar -C /var/lib/odoo -xzf %s' % shlex.quote(
                '%s/filestore.tar.gz' % workdir),
            timeout=600,
        )
        if not r.ok:
            raise UserError(_(
                "Filestore extraction failed:\n%s"
            ) % (r.stderr or r.stdout))

        driver.exec(handle, 'rm -rf %s' % shlex.quote(workdir))

        instance.write({'state': 'running', 'pending_operation': False})
        instance._append_log(
            "Full-instance restore from '%s' completed successfully." % self.name)
        instance._safe_refresh_usage()

    def _on_restore_full_instance_error(self, exception, instance_id, prev_state):
        """``on_error`` shim: this job's record is a
        ``saas.instance.backup`` (not the instance), but a failure
        still needs to restore the INSTANCE's ``state``/
        ``pending_operation`` — ``saas.job``'s ``on_error`` always calls
        back on the job's own record model."""
        instance = self.env['saas.instance'].browse(instance_id)
        if instance.exists():
            instance._on_background_error(exception, prev_state)

    def _run_portal_backup(self):
        """Run backup for an already-created record (called from portal).

        Honours ``self.ephemeral`` (on-demand path) and ``self.db_name``
        (multi-DB hosting). Layout:

        - Daily / legacy portal:   ``<partner>/<sub>/<db>/<name>.zip``
          (or ``<partner>/<db>/<name>.zip`` for non-hosting)
        - On-demand:                ``ondemand/<partner>/<sub>/<db>/<name>.dump``

        The on-demand path streams ``pg_dump -Fc`` straight to object
        storage (diskless, multipart) so it works at ANY database size —
        it produces Odoo's native "dump" custom format, restorable with
        ``pg_restore`` or Odoo's Restore. On-demand backups also get a
        short-lived (1 hour) presigned URL and a matching ``expires_at``
        so the cleanup cron reaps them right after the download — they
        are not retained on the bucket.
        """
        self.ensure_one()
        instance = self.instance_id
        partner = instance.partner_id
        partner_folder = '%s_%s' % (
            partner.id, self._sanitize_name(partner.name),
        ) if partner else 'no_partner'
        # No db_name = the instance's own (served) database, which the
        # operator names — not the subdomain.
        db_name = self.db_name or instance._served_db_name()

        if self.ephemeral:
            # ``self.name`` already carries the .zip/.dump extension.
            object_key = '%s/%s/%s/%s/%s' % (
                ONDEMAND_PREFIX, partner_folder, instance.subdomain,
                db_name, self.name,
            )
        elif instance.is_hosting:
            object_key = '%s/%s/%s/%s.zip' % (
                partner_folder, instance.subdomain, db_name, self.name,
            )
        else:
            object_key = '%s/%s/%s.zip' % (partner_folder, db_name, self.name)

        self.bucket_path = object_key

        try:
            # Both the on-demand and legacy/portal paths now go through
            # the same in-pod streaming transports (see part A.8 of the
            # ssh_docker-removal plan) — ``_create_and_upload_backup``,
            # the old host-side "build zip, upload from server" mechanism,
            # was deleted as a redundant duplicate of
            # ``_stream_odoo_zip_to_bucket``, which already does the same
            # job diskless.
            if self.format == 'dump':
                size_bytes = self._stream_pg_dump_to_bucket(
                    instance, object_key, db_name,
                )
            else:
                size_bytes = self._stream_odoo_zip_to_bucket(
                    instance, object_key, db_name,
                )
            # Belt-and-braces: if the streamed byte count came back zero
            # for any reason, read the real object size from the bucket
            # so the customer never sees a bogus 0 MB.
            if not size_bytes:
                size_bytes = self._bucket_object_size(object_key)
            ttl = ONDEMAND_URL_EXPIRY if self.ephemeral else PRESIGNED_URL_EXPIRY
            url = self._generate_presigned_url(expiry=ttl)
            now = fields.Datetime.now()
            vals = {
                'state': 'done',
                'size_mb': round(size_bytes / (1024 * 1024), 2),
                'download_url': url,
                'download_url_expiry': now + datetime.timedelta(seconds=ttl),
            }
            if self.ephemeral:
                vals['expires_at'] = now + datetime.timedelta(
                    seconds=ONDEMAND_URL_EXPIRY,
                )
            self.write(vals)
        except Exception as e:
            # Persist the failure BEFORE re-raising. ``run_in_background``
            # rolls back the thread's cursor when the method bubbles an
            # exception, so without an explicit commit here the record
            # stays in ``state='running'`` forever — which then blocks
            # the singleton guard from ever releasing the on-demand slot.
            self.write({
                'state': 'failed',
                'error_message': str(e),
            })
            try:
                self.env.cr.commit()
            except Exception:
                pass
            raise

    @api.model
    def _cron_cleanup_ephemeral_backups(self):
        """Reap on-demand backups whose 1-hour window has elapsed.

        Deletes the bucket object and the local record. ``unlink()``
        already drops the bucket object via its ondelete handler, but
        we call ``_delete_from_bucket`` explicitly first so a deletion
        failure still removes the local record — we don't want a stuck
        object to keep us re-trying forever.
        """
        now = fields.Datetime.now()
        expired = self.search([
            ('ephemeral', '=', True),
            ('state', '=', 'done'),
            ('expires_at', '!=', False),
            ('expires_at', '<=', now),
        ])
        for backup in expired:
            try:
                if backup.bucket_path:
                    backup._delete_from_bucket()
            except Exception as e:
                _logger.warning(
                    "Ephemeral cleanup: bucket delete failed for %s: %s",
                    backup.bucket_path, e,
                )
            try:
                backup.with_context(_skip_bucket_delete=True).unlink()
            except Exception:
                _logger.exception(
                    "Ephemeral cleanup: unlink failed for backup %s",
                    backup.id,
                )

        # Also handle ephemeral backups stuck in 'running' for too long
        # (worker crash, network outage). 2 hours is generous.
        stuck_cutoff = now - datetime.timedelta(hours=2)
        stuck = self.search([
            ('ephemeral', '=', True),
            ('state', '=', 'running'),
            ('create_date', '<', stuck_cutoff),
        ])
        for backup in stuck:
            try:
                backup.unlink()
            except Exception:
                _logger.exception(
                    "Ephemeral cleanup: unlink stuck %s failed", backup.id,
                )

    @api.model
    def _cleanup_old_backups(self):
        """Trim old backups.

        - Service instances: keep at most ``DEFAULT_MAX_BACKUPS`` per
          instance (fixed platform-wide retention).
        - Full-instance (``is_full_instance``) snapshots are no longer
          trimmed here: the operator's own backup CronJob already
          prunes by ``spec.backup.retention``, and
          ``_cron_sync_scheduled_backups`` reaps our tracking rows to
          match whatever it pruned — see that method's docstring.
        - Stale ``running`` backups older than 1 day are dropped.
        """
        # Clean up stale 'running' backups older than 1 day (stuck records)
        stale_cutoff = fields.Datetime.now() - datetime.timedelta(days=1)
        stale_backups = self.search([
            ('create_date', '<', stale_cutoff),
            ('state', '=', 'running'),
        ])
        for backup in stale_backups:
            try:
                backup.unlink()
            except Exception as e:
                _logger.error("Failed to cleanup stale backup %s: %s", backup.name, e)

        # --- Service instances: keep at most DEFAULT_MAX_BACKUPS per instance.
        # Ephemeral excluded for the same reason as hosting above.
        data = self._read_group(
            [
                ('state', '=', 'done'),
                ('ephemeral', '=', False),
                ('instance_id.is_hosting', '=', False),
            ],
            ['instance_id'],
            ['__count'],
        )
        for instance, count in data:
            # Retention is fixed platform-wide; every Services instance keeps
            # the last DEFAULT_MAX_BACKUPS copies.
            max_backups = DEFAULT_MAX_BACKUPS
            if count <= max_backups:
                continue
            backups = self.search([
                ('instance_id', '=', instance.id),
                ('state', '=', 'done'),
            ], order='create_date desc')
            excess = backups[max_backups:]
            for backup in excess:
                try:
                    if backup.bucket_path:
                        backup._delete_from_bucket()
                    backup.unlink()
                except Exception as e:
                    _logger.error("Failed to cleanup backup %s: %s", backup.name, e)

    def cleanup_excess_for_instance(self, instance):
        """Remove excess backups for a single instance against the fixed
        retention limit (DEFAULT_MAX_BACKUPS), without waiting for the
        daily cron.

        Public: also called from saas_billing.
        """
        max_backups = DEFAULT_MAX_BACKUPS
        backups = self.search([
            ('instance_id', '=', instance.id),
            ('state', '=', 'done'),
        ], order='create_date desc')
        if len(backups) <= max_backups:
            return
        excess = backups[max_backups:]
        for backup in excess:
            try:
                if backup.bucket_path:
                    backup._delete_from_bucket()
                backup.unlink()
            except Exception as e:
                _logger.error("Failed to cleanup backup %s: %s", backup.name, e)

    @staticmethod
    def _sanitize_name(name):
        if not name:
            return 'unknown'
        return ''.join(
            c if c.isalnum() or c in ('-', '_') else '_' for c in name
        ).strip('_')
