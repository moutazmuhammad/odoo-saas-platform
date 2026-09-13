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


# Host-side builder for the on-demand ZIP. Streams an Odoo-format zip
# (manifest.json + dump.sql + filestore/) to STDOUT so Odoo can pipe it
# straight to object storage — nothing is staged on disk, so DB size is
# not a constraint. ``dump.sql`` is PLAIN SQL (not -Fc) because that's
# what Odoo's zip-restore feeds to psql. All inputs arrive via env.
_HOST_ZIP_BUILDER = r'''
import os, sys, json, shutil, subprocess, zipfile, tarfile

C = os.environ['SAAS_C']
DB = os.environ['SAAS_DB']
H = os.environ['SAAS_H']
P = os.environ['SAAS_P']
U = os.environ['SAAS_U']
PW = os.environ['SAAS_PGPASSWORD']
MANIFEST = os.environ.get('SAAS_MANIFEST', '{}')
CHUNK = 4 * 1024 * 1024

out = sys.stdout.buffer
zf = zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, allowZip64=True)
zf.writestr('manifest.json', MANIFEST)

# dump.sql — plain SQL streamed from pg_dump inside the container.
p = subprocess.Popen(
    ['docker', 'exec', '-e', 'PGPASSWORD=' + PW, C, 'pg_dump',
     '-h', H, '-p', P, '-U', U, '-d', DB,
     '--no-owner', '--no-privileges'],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
with zf.open('dump.sql', 'w') as e:
    shutil.copyfileobj(p.stdout, e, CHUNK)
rc = p.wait()
if rc != 0:
    sys.stderr.write(p.stderr.read().decode('utf-8', 'replace'))
    sys.exit(10)

# filestore/ — streamed as a tar from the container, repacked into the zip.
probe = subprocess.run(
    ['docker', 'exec', C, 'sh', '-c',
     'if [ -d /var/lib/odoo/filestore/%s ]; then echo A; '
     'elif [ -d /var/lib/odoo/.local/share/Odoo/filestore/%s ]; then echo B; '
     'else echo N; fi' % (DB, DB)],
    stdout=subprocess.PIPE)
loc = probe.stdout.decode().strip()
fspath = None
if loc == 'A':
    fspath = '/var/lib/odoo/filestore/%s' % DB
elif loc == 'B':
    fspath = '/var/lib/odoo/.local/share/Odoo/filestore/%s' % DB
if fspath:
    tp = subprocess.Popen(
        ['docker', 'exec', C, 'tar', '-C', fspath, '-cf', '-', '.'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tar = tarfile.open(fileobj=tp.stdout, mode='r|')
    for m in tar:
        if not m.isfile():
            continue
        name = m.name[2:] if m.name.startswith('./') else m.name
        src = tar.extractfile(m)
        if src is None:
            continue
        with zf.open('filestore/' + name, 'w') as e:
            shutil.copyfileobj(src, e, CHUNK)
    tar.close()
    tp.wait()

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
        help='Hosting daily backups: a complete instance snapshot — '
             'every database dump, the filestore, custom addons, '
             'configuration files, docker-compose, and pip requirements. '
             'Restorable as a single unit.',
    )
    format = fields.Selection(
        [('zip', 'Zip (dump.sql + filestore)'),
         ('dump', 'SQL dump (pg_dump custom)'),
         ('restic', 'Restic (deduplicated)')],
        string='Backup Format', default='zip', index=True,
        help='Storage format. Daily full-instance backups use restic '
             '(deduplicated, encrypted). On-demand backups are either '
             '``zip`` (Odoo zip: dump.sql + filestore, restorable via '
             "Odoo's database manager) or ``dump`` (pg_dump custom "
             'format, DB only, restorable via pg_restore) — both '
             'streamed straight to storage so any DB size works.',
    )
    restic_run_tag = fields.Char(
        string='Restic Run Tag', index=True,
        help='ISO-8601 timestamp used as the restic ``run`` tag '
             'binding together all snapshots from one backup run '
             '(one per database + one for the filesystem). Set only '
             'on ``format=restic`` rows.',
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
        - ``is_full_instance`` (restic) → ``action_restore_full_instance``
          which walks the per-DB ``restic dump`` + filesystem ``restic
          restore`` path.
        - Otherwise (legacy zip) → ``action_restore_backup`` which
          downloads the single zip object and replays the SQL dump
          + filestore inside it.

        Without this dispatch, clicking Restore on the backend form of
        a restic snapshot ran the zip path, which then crashed in
        ``_generate_presigned_url`` because restic backups have no
        ``backup_path`` (a restic repo isn't a single S3 object).
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
        ``pg_dump`` stdout is piped over SSH and handed to the SDK's
        multipart uploader, so database size is not a constraint.
        """
        instance._ensure_can_ssh()
        container = instance._get_container_name()
        db_host = instance._get_db_host()
        db_port = instance.db_server_id.psql_port or 5432
        cmd = (
            'docker exec -e PGPASSWORD=%s %s pg_dump -Fc -Z3 '
            '-h %s -p %s -U %s -d %s --no-owner'
        ) % (
            shlex.quote(instance.sudo().db_password or ''),
            shlex.quote(container),
            shlex.quote(db_host),
            shlex.quote(str(db_port)),
            shlex.quote(instance.sudo().db_user or ''),
            shlex.quote(db_name),
        )
        with instance.docker_server_id._get_ssh_connection() as ssh:
            stdout, stderr = ssh.exec_command_streaming(
                cmd, timeout=BACKUP_STREAM_READ_TIMEOUT,
            )
            reader = _CountingReader(stdout)
            upload_error = None
            try:
                self._upload_stream_to_bucket(object_key, reader)
            except Exception as e:
                upload_error = e
            # stdout is at EOF (upload drained it, or it errored) — now
            # the exit code is available without deadlocking.
            exit_code = stdout.channel.recv_exit_status()
            err_tail = ''
            try:
                err_tail = stderr.read().decode('utf-8', 'replace')[-2000:]
            except Exception:
                pass

            if upload_error is not None or exit_code != 0:
                # Don't leave a truncated/corrupt object behind.
                try:
                    self._delete_bucket_path(object_key)
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
        filestore) from the instance's container straight to object
        storage. Returns the uploaded size in bytes.

        Diskless and bounded-memory: a small Python builder runs on the
        docker host, writes the zip to stdout (pg_dump piped into a zip
        entry, filestore repacked from a container tar stream), and Odoo
        pipes that into the SDK's multipart uploader — so DB/filestore
        size is not a constraint. The result restores via Odoo's
        database manager (it's the same layout as Odoo's own zip backup).
        """
        instance._ensure_can_ssh()
        container = instance._get_container_name()
        db_host = instance._get_db_host()
        db_port = str(instance.db_server_id.psql_port or 5432)

        import json
        manifest = json.dumps({
            'odoo_version': instance.odoo_version_id.name or '',
            'database': db_name,
            'partner': instance.partner_id.name or '',
            'timestamp': fields.Datetime.now().isoformat(),
            'instance': instance.name or '',
        }, indent=2)

        ts = fields.Datetime.now().strftime('%Y%m%d%H%M%S')
        script_path = '/tmp/saas_zipbuild_%s_%s.py' % (db_name, ts)
        env = {
            'SAAS_C': container,
            'SAAS_DB': db_name,
            'SAAS_H': db_host,
            'SAAS_P': db_port,
            'SAAS_U': instance.sudo().db_user or '',
            'SAAS_PGPASSWORD': instance.sudo().db_password or '',
            'SAAS_MANIFEST': manifest,
        }
        env_prefix = ' '.join(
            '%s=%s' % (k, shlex.quote(str(v))) for k, v in env.items()
        )
        with instance.docker_server_id._get_ssh_connection() as ssh:
            ssh.write_file(script_path, _HOST_ZIP_BUILDER)
            stdout, stderr = ssh.exec_command_streaming(
                '%s python3 %s' % (env_prefix, shlex.quote(script_path)),
                timeout=BACKUP_STREAM_READ_TIMEOUT,
            )
            reader = _CountingReader(stdout)
            upload_error = None
            try:
                self._upload_stream_to_bucket(
                    object_key, reader, content_type='application/zip',
                )
            except Exception as e:
                upload_error = e
            exit_code = stdout.channel.recv_exit_status()
            err_tail = ''
            try:
                err_tail = stderr.read().decode('utf-8', 'replace')[-2000:]
            except Exception:
                pass
            try:
                ssh.execute('rm -f %s' % shlex.quote(script_path))
            except Exception:
                pass

            if upload_error is not None or exit_code != 0:
                try:
                    self._delete_bucket_path(object_key)
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
            self._delete_bucket_path(self.bucket_path)

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
    def _delete_bucket_path(self, bucket_path):
        """Delete an arbitrary object key from the configured backup bucket.

        Use this when you have a bucket path but no `saas.instance.backup`
        record (e.g. retained backup paths after the source instance has
        been wiped). Replaces the previous `Backup.new(...)._delete_from_bucket()`
        anti-pattern.
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
    # Backup creation — zip on server, upload directly to bucket
    # ------------------------------------------------------------------
    def _create_and_upload_backup(self, instance, object_key, db_name=None):
        """SSH to the docker server, dump DB + copy filestore + manifest, zip,
        then upload directly from the server to the bucket via presigned URL.

        ``db_name`` selects which database to dump. Defaults to the
        record's own ``db_name`` (or ``instance.subdomain`` if blank
        for service instances).

        Returns the zip size in bytes.
        """
        instance._ensure_can_ssh()
        docker_server = instance.docker_server_id
        container_name = instance._get_container_name()
        db_name = db_name or self.db_name or instance.subdomain
        db_server = instance.db_server_id
        db_host = instance._get_db_host()
        db_port = db_server.psql_port or 5432
        ts = fields.Datetime.now().strftime('%Y%m%d%H%M%S')
        tmp_dir = '/tmp/saas_backup_%s_%s' % (db_name, ts)
        zip_path = '%s.zip' % tmp_dir
        script_path = '/tmp/saas_backup_script_%s_%s.sh' % (db_name, ts)

        import json
        manifest = json.dumps({
            'odoo_version': instance.odoo_version_id.name or '',
            'database': db_name,
            'partner': instance.partner_id.name or '',
            'timestamp': fields.Datetime.now().isoformat(),
            'instance': instance.name or '',
        }, indent=2)

        # Generate presigned PUT URL for direct upload from server to bucket
        try:
            presigned_put_url = self._generate_presigned_put_url(object_key)
        except Exception:
            presigned_put_url = None

        # Use environment variables instead of embedding credentials in shell script
        env_vars = {
            'SAAS_TMP_DIR': tmp_dir,
            'SAAS_ZIP_PATH': zip_path,
            'SAAS_CONTAINER': container_name,
            'SAAS_DB_NAME': db_name,
            'SAAS_DB_HOST': db_host,
            'SAAS_DB_PORT': str(db_port),
            'SAAS_DB_USER': instance.db_user,
            'SAAS_DB_PASS': instance.db_password,
        }
        if presigned_put_url:
            env_vars['SAAS_UPLOAD_URL'] = presigned_put_url

        env_prefix = ' '.join(
            '%s=%s' % (k, shlex.quote(v)) for k, v in env_vars.items()
        )

        # Build the upload step based on whether presigned URL is available
        if presigned_put_url:
            upload_step = (
                '# 5) Upload directly to cloud storage via presigned URL\n'
                'if curl -f -X PUT -H "Content-Type: application/zip" '
                '--data-binary "@$SAAS_ZIP_PATH" "$SAAS_UPLOAD_URL"; then\n'
                '    UPLOAD_OK=0\n'
                'else\n'
                '    UPLOAD_OK=1\n'
                'fi\n'
            )
        else:
            upload_step = 'UPLOAD_OK=1  # No presigned URL, will download via SFTP\n'

        script = r"""#!/bin/bash
set -e

mkdir -p "$SAAS_TMP_DIR/filestore"

# 1) pg_dump via docker exec (pass PGPASSWORD into the container env)
# Try inside container first (uses container's pg_dump + network access)
docker exec -e PGPASSWORD="$SAAS_DB_PASS" "$SAAS_CONTAINER" pg_dump \
    -h "$SAAS_DB_HOST" -p "$SAAS_DB_PORT" -U "$SAAS_DB_USER" \
    -d "$SAAS_DB_NAME" --no-owner > "$SAAS_TMP_DIR/dump.sql" 2>/tmp/saas_pgdump_err_$$ || true

# If container pg_dump failed or produced empty dump, try from host
if [ ! -s "$SAAS_TMP_DIR/dump.sql" ]; then
    echo "Container pg_dump failed or empty, trying from host..." >&2
    # Try pg_dump directly from the host (if installed)
    if command -v pg_dump >/dev/null 2>&1; then
        PGPASSWORD="$SAAS_DB_PASS" pg_dump \
            -h "$SAAS_DB_HOST" -p "$SAAS_DB_PORT" -U "$SAAS_DB_USER" \
            -d "$SAAS_DB_NAME" --no-owner > "$SAAS_TMP_DIR/dump.sql" 2>&1
    else
        # Try via the DB server's psql if host has no pg_dump
        echo "No pg_dump on host either. Backup will have empty DB dump." >&2
    fi
fi

# Verify dump is not empty
if [ ! -s "$SAAS_TMP_DIR/dump.sql" ]; then
    echo "ERROR: pg_dump produced empty output." >&2
    cat /tmp/saas_pgdump_err_$$ 2>/dev/null >&2 || true
    rm -f /tmp/saas_pgdump_err_$$
    exit 1
fi
rm -f /tmp/saas_pgdump_err_$$

# 2) Copy filestore from inside the container using docker cp
if docker exec "$SAAS_CONTAINER" test -d "/var/lib/odoo/filestore/$SAAS_DB_NAME" 2>/dev/null; then
    docker cp "$SAAS_CONTAINER:/var/lib/odoo/filestore/$SAAS_DB_NAME/." "$SAAS_TMP_DIR/filestore/" 2>/dev/null || true
elif docker exec "$SAAS_CONTAINER" test -d "/var/lib/odoo/.local/share/Odoo/filestore/$SAAS_DB_NAME" 2>/dev/null; then
    docker cp "$SAAS_CONTAINER:/var/lib/odoo/.local/share/Odoo/filestore/$SAAS_DB_NAME/." "$SAAS_TMP_DIR/filestore/" 2>/dev/null || true
fi

# 3) Write manifest.json
cat > "$SAAS_TMP_DIR/manifest.json" << 'MANIFEST_EOF'
%s
MANIFEST_EOF

# 4) Zip via Python's stdlib so we don't depend on `zip` being
# installed on the docker host. Walks $SAAS_TMP_DIR recursively and
# packs everything under root-relative paths inside the archive.
cd "$SAAS_TMP_DIR"
python3 - "$SAAS_TMP_DIR" "$SAAS_ZIP_PATH" <<'PYZIP'
import os, sys, zipfile
src, dst = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
    for root, dirs, files in os.walk(src):
        for f in files:
            full = os.path.join(root, f)
            zf.write(full, os.path.relpath(full, src))
PYZIP

# Cleanup temp dir (keep zip)
rm -rf "$SAAS_TMP_DIR"

%s

# Output zip size for tracking
stat -c %%s "$SAAS_ZIP_PATH" 2>/dev/null || stat -f %%z "$SAAS_ZIP_PATH" 2>/dev/null || echo 0

# Remove zip from server if upload succeeded (so Python knows not to SFTP it)
if [ "${UPLOAD_OK:-1}" = "0" ]; then
    rm -f "$SAAS_ZIP_PATH"
fi
""" % (manifest, upload_step)

        with docker_server._get_ssh_connection() as ssh:
            # Upload script file to avoid shell quoting issues
            ssh.write_file(script_path, script)
            ssh.execute('chmod +x %s' % shlex.quote(script_path))

            exit_code, stdout, stderr = ssh.execute(
                '%s bash %s' % (env_prefix, shlex.quote(script_path)),
                timeout=600,
            )

            # Remove script
            ssh.execute('rm -f %s' % shlex.quote(script_path))

            if exit_code != 0:
                ssh.execute('rm -f %s' % shlex.quote(zip_path))
                raise UserError(
                    _("Backup failed on server %s:\n%s") % (docker_server.name, stderr or stdout)
                )

            # Parse size from stdout (last line)
            size_bytes = 0
            for line in stdout.strip().splitlines():
                line = line.strip()
                if line.isdigit():
                    size_bytes = int(line)

            # Check if the zip still exists on the server (means upload
            # didn't happen or failed — need SFTP fallback)
            # Use ``__`` (not ``_``) for the throwaways: ``_`` is the
            # translation function imported at module top, and rebinding
            # it in the local scope shadows it for the entire function —
            # so the earlier ``_("Backup failed…")`` blows up with
            # UnboundLocalError.
            check_code, __out, __err = ssh.execute(
                'test -f %s' % shlex.quote(zip_path)
            )
            if check_code == 0:
                # Zip still on server — download via SFTP and upload from Odoo
                try:
                    zip_data = ssh.read_file_bytes(zip_path)
                    size_bytes = len(zip_data)
                finally:
                    ssh.execute('rm -f %s' % shlex.quote(zip_path))
                if not zip_data:
                    raise UserError(_(
                        "The backup came back empty. Please try again, "
                        "or contact support if this keeps happening."
                    ))
                self._upload_to_bucket(object_key, zip_data)

        return size_bytes

    # ------------------------------------------------------------------
    # Restic plumbing — daily snapshots are stored in a per-instance,
    # password-encrypted, deduplicated repository in the same object
    # store we already use for zip backups. We never embed credentials
    # in shell args (they leak via `ps`); everything goes via env.
    # ------------------------------------------------------------------
    @api.model
    def _restic_repository_url(self, instance):
        """Build the ``RESTIC_REPOSITORY`` URL for an instance.

        Layout:  restic/<partner>/<subdomain>  inside the configured
        backup bucket. Provider-specific URL prefix.
        """
        cfg = self._get_backup_config()
        partner = instance.partner_id
        partner_folder = '%s_%s' % (
            partner.id, self._sanitize_name(partner.name),
        ) if partner else 'no_partner'
        path = 'restic/%s/%s' % (partner_folder, instance.subdomain)

        if cfg['provider'] == 'gcs':
            return 'gs:%s:%s' % (cfg['bucket'], path)

        # S3 / S3-compatible (AWS / DO Spaces / MinIO / etc.). restic
        # accepts s3:<host>/<bucket>/<path>. For AWS-region setups
        # we synthesize the regional endpoint; otherwise rely on the
        # admin-provided endpoint.
        endpoint = cfg.get('endpoint')
        if not endpoint and cfg['provider'] == 'aws':
            region = cfg.get('region') or 'us-east-1'
            endpoint = 'https://s3.%s.amazonaws.com' % region
        elif not endpoint and cfg['provider'] == 'digitalocean':
            region = cfg.get('region') or 'nyc3'
            endpoint = 'https://%s.digitaloceanspaces.com' % region
        elif not endpoint and cfg['provider'] == 'hetzner':
            region = cfg.get('region') or 'fsn1'
            endpoint = 'https://%s.your-objectstorage.com' % region
        endpoint = (endpoint or '').rstrip('/')
        # strip scheme — restic expects s3:host/bucket/path
        host = endpoint.split('://', 1)[-1] if endpoint else 's3.amazonaws.com'
        return 's3:%s/%s/%s' % (host, cfg['bucket'], path)

    @api.model
    def _ensure_restic_password(self, instance):
        """Lazy-generate the per-instance restic password.

        Called once on the first backup. Stored on
        ``saas.instance.restic_password`` with manager-only ACL.
        """
        if instance.sudo().restic_password:
            return instance.sudo().restic_password
        import secrets as _secrets
        pwd = _secrets.token_urlsafe(48)
        instance.sudo().restic_password = pwd
        return pwd

    @api.model
    def _restic_env_vars(self, instance, gcs_credentials_path=None):
        """Env vars to expose restic to its repository.

        On GCS, the caller must first stage the service-account JSON
        to a path on the docker host (``gcs_credentials_path``) and
        pass it in — restic reads it via ``GOOGLE_APPLICATION_CREDENTIALS``.
        """
        cfg = self._get_backup_config()
        env = {
            'RESTIC_REPOSITORY': self._restic_repository_url(instance),
            'RESTIC_PASSWORD': self._ensure_restic_password(instance),
        }
        if cfg['provider'] == 'gcs':
            # Cloud Storage project id env is optional; restic
            # picks up the project from the SA JSON. Keep it out.
            if gcs_credentials_path:
                env['GOOGLE_APPLICATION_CREDENTIALS'] = gcs_credentials_path
        else:
            env['AWS_ACCESS_KEY_ID'] = cfg.get('access_key') or ''
            env['AWS_SECRET_ACCESS_KEY'] = cfg.get('secret_key') or ''
            if cfg.get('region'):
                env['AWS_DEFAULT_REGION'] = cfg['region']
        return env

    @api.model
    def _stage_gcs_credentials(self, ssh, instance):
        """Write the GCS service-account JSON to a temp file on the
        docker host so restic can pick it up via env. Returns the path,
        or ``None`` if not applicable. Caller MUST clean up via
        ``_unstage_gcs_credentials``.
        """
        cfg = self._get_backup_config()
        if cfg['provider'] != 'gcs':
            return None
        ICP = self.env['ir.config_parameter'].sudo()
        sa_key = ICP.get_param('saas_backup.service_account_key', '')
        if not sa_key:
            raise UserError(_("GCS provider selected but no service-account JSON configured."))
        path = '/tmp/saas_restic_gcs_%s_%d.json' % (
            instance.subdomain, int(time.time()),
        )
        ssh.write_file(path, sa_key)
        ssh.execute('chmod 600 %s' % shlex.quote(path))
        return path

    @api.model
    def _unstage_gcs_credentials(self, ssh, path):
        if path:
            try:
                ssh.execute('rm -f %s' % shlex.quote(path))
            except Exception:
                _logger.warning("Failed to clean up GCS creds at %s", path)

    @api.model
    def _ensure_restic_installed(self, ssh, docker_server_name=''):
        """Verify restic is present on the docker host. Raises UserError
        with an install hint if absent."""
        exit_code, stdout, stderr = ssh.execute(
            'command -v restic >/dev/null 2>&1 && restic version 2>&1 || echo MISSING'
        )
        if 'MISSING' in (stdout or '') or exit_code != 0:
            raise UserError(_(
                "restic is not installed on docker host %s. "
                "Install it with `sudo apt-get install -y restic` "
                "(Debian 12+/Ubuntu 22.04+) or grab the static binary "
                "from https://github.com/restic/restic/releases and "
                "place it on $PATH. See saas_core/docker/SERVER-SETUP.md."
            ) % (docker_server_name or 'this server'))

    @api.model
    def _restic_cmd(self, env_vars, args, stdin_pipeline=None):
        """Build a shell command that runs ``restic <args>`` with
        env_vars exported (NOT inline-quoted on the command line, so
        passwords don't show up in `ps`).
        """
        # `env -` clears the environment then sets ours; we then run
        # restic. Using env vars from a heredoc-style assignment is
        # safer than embedding the password in the command line.
        exports = ' '.join(
            '%s=%s' % (k, shlex.quote(v or ''))
            for k, v in env_vars.items()
        )
        cmd = '%s restic %s' % (exports, ' '.join(args))
        if stdin_pipeline:
            cmd = '%s | %s' % (stdin_pipeline, cmd)
        return cmd

    # ------------------------------------------------------------------
    # Full-instance backup (hosting daily) — bundle DBs + filestore +
    # addons + config + docker-compose + pip into a single restore zip.
    # ------------------------------------------------------------------
    def _create_full_instance_backup(self, instance, object_key):
        """Zip the entire instance directory + every DB dump, upload it.

        Layout inside the zip:
            manifest.json
            dumps/<db>.sql           — one per database owned by the role
            data/                    — the instance's data dir (filestore +
                                       Odoo data); sessions/ pruned.
            addons/                  — customer addons + cloned repos
            config/                  — odoo.conf and friends
            docker-compose.yml
            requirements.txt
            pip_install.sh           — if present

        Returns the zip size in bytes. Raises UserError on failure.
        """
        instance._ensure_can_ssh()
        docker_server = instance.docker_server_id
        container_name = instance._get_container_name()
        instance_path = instance._get_instance_path()
        db_server = instance.db_server_id
        db_host = instance._get_db_host()
        db_port = db_server.psql_port or 5432
        ts = fields.Datetime.now().strftime('%Y%m%d%H%M%S')
        tmp_dir = '/tmp/saas_full_%s_%s' % (instance.subdomain, ts)
        zip_path = '%s.zip' % tmp_dir
        script_path = '/tmp/saas_full_script_%s_%s.sh' % (
            instance.subdomain, ts,
        )

        # Enumerate the databases owned by this instance's role. We do
        # this from the saas master (not the bash script) so a failure
        # surfaces as a clean UserError before the SSH session even
        # starts the dump.
        try:
            db_names = [r['name'] for r in instance.hosting_db_list()]
        except Exception as e:
            raise UserError(
                _("Could not list databases on instance %s: %s")
                % (instance.name, e)
            )

        import json
        manifest = json.dumps({
            'backup_type': 'full_instance',
            'odoo_version': instance.odoo_version_id.name or '',
            'docker_image': instance.odoo_version_id._get_docker_image()
                if hasattr(instance.odoo_version_id, '_get_docker_image')
                else '',
            'subdomain': instance.subdomain,
            'domain': instance.domain_id.name or '',
            'partner': instance.partner_id.name or '',
            'partner_id': instance.partner_id.id,
            'timestamp': fields.Datetime.now().isoformat(),
            'databases': db_names,
            'instance': instance.name or '',
            'pip_packages': instance.pip_packages or '',
            'plan': instance.plan_id.name or '',
            'workers': (instance.plan_id.workers or 0)
                if instance.plan_id else 0,
            'storage_limit': (instance.plan_id.storage_limit or 0)
                if instance.plan_id else 0,
        }, indent=2)

        try:
            presigned_put_url = self._generate_presigned_put_url(object_key)
        except Exception:
            presigned_put_url = None

        env_vars = {
            'SAAS_TMP_DIR': tmp_dir,
            'SAAS_ZIP_PATH': zip_path,
            'SAAS_INSTANCE_PATH': instance_path,
            'SAAS_CONTAINER': container_name,
            'SAAS_DB_HOST': db_host,
            'SAAS_DB_PORT': str(db_port),
            'SAAS_DB_USER': instance.db_user,
            'SAAS_DB_PASS': instance.db_password,
            # newline-separated, no shell injection because we read it
            # back through `mapfile -t` (no word splitting / no eval).
            'SAAS_DB_NAMES': '\n'.join(db_names),
        }
        if presigned_put_url:
            env_vars['SAAS_UPLOAD_URL'] = presigned_put_url

        env_prefix = ' '.join(
            '%s=%s' % (k, shlex.quote(v)) for k, v in env_vars.items()
        )

        if presigned_put_url:
            upload_step = (
                'if curl -f -X PUT -H "Content-Type: application/zip" '
                '--data-binary "@$SAAS_ZIP_PATH" "$SAAS_UPLOAD_URL"; then\n'
                '    UPLOAD_OK=0\n'
                'else\n'
                '    UPLOAD_OK=1\n'
                'fi\n'
            )
        else:
            upload_step = 'UPLOAD_OK=1\n'

        script = r"""#!/bin/bash
set -e

mkdir -p "$SAAS_TMP_DIR"
mkdir -p "$SAAS_TMP_DIR/dumps"

# 1) pg_dump every database owned by the instance role
mapfile -t DBS <<< "$SAAS_DB_NAMES"
for db in "${DBS[@]}"; do
    [ -z "$db" ] && continue
    echo "Dumping $db..." >&2
    docker exec -e PGPASSWORD="$SAAS_DB_PASS" "$SAAS_CONTAINER" pg_dump \
        -h "$SAAS_DB_HOST" -p "$SAAS_DB_PORT" -U "$SAAS_DB_USER" \
        -d "$db" --no-owner > "$SAAS_TMP_DIR/dumps/$db.sql" 2>>/tmp/saas_pgdump_err_$$
    if [ ! -s "$SAAS_TMP_DIR/dumps/$db.sql" ]; then
        # container pg_dump failed; try from the host (if present)
        if command -v pg_dump >/dev/null 2>&1; then
            PGPASSWORD="$SAAS_DB_PASS" pg_dump \
                -h "$SAAS_DB_HOST" -p "$SAAS_DB_PORT" -U "$SAAS_DB_USER" \
                -d "$db" --no-owner > "$SAAS_TMP_DIR/dumps/$db.sql" 2>&1
        fi
    fi
    if [ ! -s "$SAAS_TMP_DIR/dumps/$db.sql" ]; then
        echo "ERROR: pg_dump produced empty file for $db" >&2
        cat /tmp/saas_pgdump_err_$$ 2>/dev/null >&2 || true
        rm -f /tmp/saas_pgdump_err_$$
        exit 1
    fi
done
rm -f /tmp/saas_pgdump_err_$$

# 2) Copy instance directory contents — addons, config, docker-compose,
#    requirements, pip script. Use a manifest exclude file so we skip
#    transient noise (logs, lock files) but keep everything else.
echo "Copying instance files..." >&2
if [ -d "$SAAS_INSTANCE_PATH/addons" ]; then
    cp -a "$SAAS_INSTANCE_PATH/addons" "$SAAS_TMP_DIR/addons"
fi
if [ -d "$SAAS_INSTANCE_PATH/config" ]; then
    cp -a "$SAAS_INSTANCE_PATH/config" "$SAAS_TMP_DIR/config"
fi
for f in docker-compose.yml requirements.txt pip_install.sh; do
    if [ -f "$SAAS_INSTANCE_PATH/$f" ]; then
        cp -a "$SAAS_INSTANCE_PATH/$f" "$SAAS_TMP_DIR/$f"
    fi
done

# 3) Filestore + data dir. Exclude session files — they're regenerated
#    on next login and can be huge. Also exclude __pycache__/.
if [ -d "$SAAS_INSTANCE_PATH/data" ]; then
    echo "Copying data/filestore (skip sessions)..." >&2
    mkdir -p "$SAAS_TMP_DIR/data"
    # rsync if available, otherwise a tar pipe.
    if command -v rsync >/dev/null 2>&1; then
        rsync -a \
            --exclude='sessions' \
            --exclude='__pycache__' \
            "$SAAS_INSTANCE_PATH/data/" "$SAAS_TMP_DIR/data/"
    else
        ( cd "$SAAS_INSTANCE_PATH" \
          && tar --exclude='sessions' --exclude='__pycache__' -cf - data ) \
        | ( cd "$SAAS_TMP_DIR" && tar -xf - )
    fi
fi

# 4) Manifest
cat > "$SAAS_TMP_DIR/manifest.json" << 'MANIFEST_EOF'
%s
MANIFEST_EOF

# 5) Zip via Python's stdlib so we don't depend on `zip` being
# installed on the docker host.
echo "Zipping..." >&2
python3 - "$SAAS_TMP_DIR" "$SAAS_ZIP_PATH" <<'PYZIP'
import os, sys, zipfile
src, dst = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
    for root, dirs, files in os.walk(src):
        for f in files:
            full = os.path.join(root, f)
            zf.write(full, os.path.relpath(full, src))
PYZIP

# Cleanup workdir (keep zip)
rm -rf "$SAAS_TMP_DIR"

%s

# Output zip size on stdout so the caller can record it.
stat -c %%s "$SAAS_ZIP_PATH" 2>/dev/null \
    || stat -f %%z "$SAAS_ZIP_PATH" 2>/dev/null \
    || echo 0

if [ "${UPLOAD_OK:-1}" = "0" ]; then
    rm -f "$SAAS_ZIP_PATH"
fi
""" % (manifest, upload_step)

        with docker_server._get_ssh_connection() as ssh:
            ssh.write_file(script_path, script)
            ssh.execute('chmod +x %s' % shlex.quote(script_path))

            # Generous timeout — full-instance dumps can take many
            # minutes on a 10 GB filestore.
            exit_code, stdout, stderr = ssh.execute(
                '%s bash %s' % (env_prefix, shlex.quote(script_path)),
                timeout=3600,
            )

            ssh.execute('rm -f %s' % shlex.quote(script_path))

            if exit_code != 0:
                ssh.execute('rm -f %s' % shlex.quote(zip_path))
                raise UserError(
                    _("Full-instance backup failed on %s:\n%s")
                    % (docker_server.name, stderr or stdout)
                )

            size_bytes = 0
            for line in stdout.strip().splitlines():
                line = line.strip()
                if line.isdigit():
                    size_bytes = int(line)

            # SFTP fallback if presigned PUT didn't fly
            # Use ``__`` (not ``_``) for the throwaways: ``_`` is the
            # translation function imported at module top, and rebinding
            # it in the local scope shadows it for the entire function —
            # so the earlier ``_("Backup failed…")`` blows up with
            # UnboundLocalError.
            check_code, __out, __err = ssh.execute(
                'test -f %s' % shlex.quote(zip_path)
            )
            if check_code == 0:
                try:
                    zip_data = ssh.read_file_bytes(zip_path)
                    size_bytes = len(zip_data)
                finally:
                    ssh.execute('rm -f %s' % shlex.quote(zip_path))
                if not zip_data:
                    raise UserError(_(
                        "The snapshot came back empty. Please try again, "
                        "or contact support if this keeps happening."
                    ))
                self._upload_to_bucket(object_key, zip_data)

        return size_bytes

    def _perform_full_instance_backup(self, instance, keep_target_run_tag=None):
        """Create a restic-based full-instance snapshot.

        Each backup run produces N+1 restic snapshots (N = number of
        databases on the instance, +1 for the filesystem) all tagged
        ``run=<iso-ts>``. The local ``saas.instance.backup`` row holds
        the run tag, the list of DB snapshot names, and total size
        info from ``restic stats`` for display.

        Retention is handled by ``restic forget --keep-last 7 --prune``
        after a successful run — at most 7 snapshots per instance,
        oldest dropped. The tracking-row side
        (``_trim_hosting_snapshots``) enforces the same cap on
        ``saas.instance.backup`` records.

        ``keep_target_run_tag`` pins a specific run tag so neither the
        restic ``forget`` nor the tracking-row trim drops it. The
        restore flow passes the target backup's run tag so a pre-
        restore safety snapshot taken at the cap can't accidentally
        delete the snapshot we're about to restore from.
        """
        instance._ensure_can_ssh()
        docker_server = instance.docker_server_id
        container_name = instance._get_container_name()
        instance_path = instance._get_instance_path()
        db_server = instance.db_server_id
        db_host = instance._get_db_host()
        db_port = db_server.psql_port or 5432

        now = fields.Datetime.now()
        run_tag = now.strftime('%Y%m%dT%H%M%SZ')
        backup_name = 'full_%s' % run_tag

        # Enumerate databases up-front so a clean failure surfaces
        # before we touch restic.
        try:
            db_names = [r['name'] for r in instance.hosting_db_list()]
        except Exception:
            raise UserError(
                _("We couldn't get the list of databases for %s right "
                  "now. Please try again in a moment.") % instance.name
            )

        backup = self.create({
            'instance_id': instance.id,
            'name': backup_name,
            'is_full_instance': True,
            'format': 'restic',
            'restic_run_tag': run_tag,
            'restic_db_names': ','.join(db_names),
            'state': 'running',
        })

        gcs_path = None
        try:
            with docker_server._get_ssh_connection() as ssh:
                self._ensure_restic_installed(ssh, docker_server.name)

                gcs_path = self._stage_gcs_credentials(ssh, instance)
                env_vars = self._restic_env_vars(instance, gcs_path)

                # 1) Init repo (idempotent — restic init fails if it
                # already exists; we swallow that specific case).
                init_cmd = self._restic_cmd(
                    env_vars, ['init', '--quiet'],
                )
                exit_code, stdout, stderr = ssh.execute(
                    init_cmd, timeout=180,
                )
                already_exists = (
                    'already initialized' in (stdout + stderr).lower()
                    or 'config file already exists' in (stdout + stderr).lower()
                )
                if exit_code != 0 and not already_exists:
                    raise UserError(_(
                        "restic init failed:\n%s\n%s"
                    ) % (stdout, stderr))

                # 2) Per-DB pg_dump → restic backup --stdin
                for db in db_names:
                    pg_dump = (
                        'docker exec -e PGPASSWORD=%s %s pg_dump '
                        '-h %s -p %d -U %s -d %s --no-owner'
                    ) % (
                        shlex.quote(instance.db_password),
                        shlex.quote(container_name),
                        shlex.quote(db_host),
                        db_port,
                        shlex.quote(instance.db_user),
                        shlex.quote(db),
                    )
                    backup_cmd = self._restic_cmd(
                        env_vars,
                        [
                            'backup', '--stdin',
                            '--stdin-filename', shlex.quote('%s.sql' % db),
                            '--tag', 'db', '--tag', 'run=' + run_tag,
                            '--tag', 'db=' + db,
                            '--host', shlex.quote(instance.subdomain),
                            '--quiet',
                        ],
                        stdin_pipeline='set -o pipefail; ' + pg_dump,
                    )
                    exit_code, stdout, stderr = ssh.execute(
                        backup_cmd, timeout=3600,
                    )
                    if exit_code != 0:
                        raise UserError(_(
                            "restic backup of database '%s' failed:\n%s\n%s"
                        ) % (db, stdout[-500:], stderr[-500:]))

                # 3) Filesystem snapshot — data/addons/config/compose/
                # requirements/pip script in a single restic run.
                paths = []
                for p in ('data', 'addons', 'config',
                          'docker-compose.yml', 'requirements.txt',
                          'pip_install.sh'):
                    full = '%s/%s' % (instance_path, p)
                    paths.append(shlex.quote(full))
                fs_cmd = self._restic_cmd(
                    env_vars,
                    [
                        'backup', *paths,
                        '--tag', 'fs', '--tag', 'run=' + run_tag,
                        '--host', shlex.quote(instance.subdomain),
                        '--exclude', shlex.quote('**/sessions'),
                        '--exclude', shlex.quote('**/__pycache__'),
                        '--quiet',
                    ],
                )
                # `restic backup` returns 3 for partial errors (e.g.
                # transient permission denied on a file). 0 = clean.
                exit_code, stdout, stderr = ssh.execute(
                    fs_cmd, timeout=7200,
                )
                if exit_code not in (0, 3):
                    raise UserError(_(
                        "restic backup of filesystem failed:\n%s\n%s"
                    ) % (stdout[-500:], stderr[-500:]))
                if exit_code == 3:
                    _logger.warning(
                        "restic backup of fs for %s had partial errors; "
                        "snapshot still created", instance.name,
                    )

                # 4) Retention: keep the 7 most recent runs.
                # ``--keep-last`` (count-based) rather than
                # ``--keep-daily`` (date-based) so pre-restore safety
                # snapshots taken in quick succession can't push older
                # daily ones out of the day-bucket and the customer's
                # visible total stays at the documented maximum of 7.
                # Group by host so we don't accidentally trim across
                # instances if a repo gets reused. ``--keep-tag`` pins
                # an explicit run tag (the restore target) so it's
                # protected from this round of pruning regardless of
                # its age.
                #
                # ``--prune`` runs with every nightly forget so space
                # freed by retention is reclaimed from the bucket
                # immediately.
                forget_args = [
                    'forget', '--prune',
                    '--keep-last', '7', '--group-by', 'host,tags', '--quiet',
                ]
                if keep_target_run_tag:
                    forget_args.extend(
                        ['--keep-tag', 'run=' + keep_target_run_tag],
                    )
                forget_cmd = self._restic_cmd(env_vars, forget_args)
                # Forget is best-effort. Failing here shouldn't fail
                # the backup as a whole — surface but continue.
                ec, fout, ferr = ssh.execute(forget_cmd, timeout=1800)
                if ec != 0:
                    _logger.warning(
                        "restic forget for %s exit=%s out=%s err=%s",
                        instance.name, ec, fout[-200:], ferr[-200:],
                    )

                # 5) Stats — read the repo size for display. Optional;
                # if it fails we just skip size_mb.
                size_mb = False
                stats_cmd = self._restic_cmd(
                    env_vars,
                    ['stats', '--mode', 'raw-data', '--json',
                     '--tag', 'run=' + run_tag],
                )
                ec, sout, _serr = ssh.execute(stats_cmd, timeout=120)
                if ec == 0:
                    try:
                        import json as _json
                        stats = _json.loads(sout.strip().splitlines()[-1])
                        size_mb = round(
                            stats.get('total_size', 0) / (1024 * 1024), 2,
                        )
                    except Exception:
                        pass

            backup.write({
                'state': 'done',
                'size_mb': size_mb or 0.0,
            })
            # Enforce the per-instance cap inline. The daily cleanup
            # cron also enforces this, but pre-restore safety snapshots
            # call this method directly (outside that cron), so without
            # an inline trim a customer who restores a few times in a
            # day can accumulate past ``HOSTING_MAX_SNAPSHOTS``. The
            # restic-side ``forget --keep-last`` ran a moment ago, so
            # restic and the tracking rows stay in sync.
            self._trim_hosting_snapshots(
                instance, keep_target_run_tag=keep_target_run_tag,
            )
        except Exception as e:
            backup.write({
                'state': 'failed',
                'error_message': str(e),
            })
            raise
        finally:
            if gcs_path:
                try:
                    with docker_server._get_ssh_connection() as ssh2:
                        self._unstage_gcs_credentials(ssh2, gcs_path)
                except Exception:
                    pass

    def _trim_hosting_snapshots(self, instance, keep_target_run_tag=None):
        """Drop the oldest ``done`` full-instance tracking rows on
        ``instance`` so at most ``HOSTING_MAX_SNAPSHOTS`` remain.

        ``keep_target_run_tag`` pins a specific run tag (the restore
        target) so it's never deleted, even if it's the oldest row.
        Without this, restoring from the oldest snapshot at the cap
        would race the inline trim and lose the target.

        Best-effort: a row that fails to unlink (e.g. transient bucket
        error) is logged and skipped — the next call will retry.
        """
        if not instance.is_hosting:
            return
        backups = self.search([
            ('instance_id', '=', instance.id),
            ('state', '=', 'done'),
            ('ephemeral', '=', False),
            ('is_full_instance', '=', True),
        ], order='create_date desc')
        if len(backups) <= self.HOSTING_MAX_SNAPSHOTS:
            return
        excess = backups[self.HOSTING_MAX_SNAPSHOTS:]
        if keep_target_run_tag:
            excess = excess.filtered(
                lambda b: b.restic_run_tag != keep_target_run_tag
            )
            if not excess:
                return
        _logger.info(
            "Trimming %d excess full-instance snapshot row(s) for %s "
            "(keeping %d most recent%s).",
            len(excess), instance.subdomain, self.HOSTING_MAX_SNAPSHOTS,
            ' + restore target' if keep_target_run_tag else '',
        )
        for backup in excess:
            try:
                if backup.bucket_path:
                    backup._delete_from_bucket()
                backup.unlink()
            except Exception as e:
                _logger.warning(
                    "Failed to trim snapshot row %s on %s: %s",
                    backup.name, instance.subdomain, e,
                )

    # ------------------------------------------------------------------
    # Cron entry point
    # ------------------------------------------------------------------
    @api.model
    def _cron_backup_all_instances(self):
        """Create backups for all running instances and clean up old ones.

        Both hosting and services snapshots are a paid, opt-in add-on:
        skipped unless ``daily_backup_enabled`` and not
        ``daily_backup_suspended``. Hosting takes a full-instance snapshot
        (capped at ``HOSTING_MAX_SNAPSHOTS``); services take a per-DB zip
        backup (capped at ``DEFAULT_MAX_BACKUPS``) — both via
        ``_cleanup_old_backups``. Trial-plan instances are skipped.
        """
        instances = self.env['saas.instance'].search([
            ('state', '=', 'running'),
            '|',
            ('plan_id', '=', False),
            ('plan_id.is_trial_plan', '=', False),
        ])
        Job = self.env['saas.job']
        enqueued = 0
        for instance in instances:
            # Both hosting + services snapshots are a paid, opt-in add-on:
            # skip unless subscribed and not paused for an overdue invoice.
            if not instance.daily_backup_enabled or instance.daily_backup_suspended:
                continue
            # PERF-001: enqueue one backup job per instance instead of running
            # them serially in this cron thread. The queue's per-channel worker
            # pool runs up to the concurrency cap in parallel, so the nightly
            # window scales; lock_key='instance:<id>' serialises with any portal
            # backup of the same instance (shared restic repo).
            method = ('_run_daily_full_backup' if instance.is_hosting
                      else '_run_daily_db_backup')
            Job._enqueue(
                instance, method, channel='backup',
                lock_key='instance:%s' % instance.id, max_attempts=1,
                idempotency_key='daily_backup:%s:%s' % (
                    instance.id, fields.Date.today()))
            enqueued += 1
        _logger.info("Daily backup cron enqueued %d instance backup job(s).",
                     enqueued)
        self._cleanup_old_backups()

    def _perform_full_instance_backup_in_new_cursor(self, instance_id):
        """Run a full-instance backup in a separate cursor."""
        new_cr = self.pool.cursor()
        try:
            new_env = api.Environment(new_cr, self.env.uid, self.env.context)
            new_env['saas.instance.backup']._perform_full_instance_backup(
                new_env['saas.instance'].browse(instance_id),
            )
            new_cr.commit()
        except Exception:
            new_cr.rollback()
            raise
        finally:
            new_cr.close()

    def _perform_backup_in_new_cursor(self, instance_id, db_name=None):
        """Run a single backup in a separate cursor to isolate transactions."""
        new_cr = self.pool.cursor()
        try:
            new_env = api.Environment(new_cr, self.env.uid, self.env.context)
            new_env['saas.instance.backup']._perform_backup(
                new_env['saas.instance'].browse(instance_id),
                db_name=db_name,
            )
            new_cr.commit()
        except Exception:
            new_cr.rollback()
            raise
        finally:
            new_cr.close()

    def _perform_backup(self, instance, db_name=None):
        """Perform a single backup for an instance.

        ``db_name`` is the database to snapshot. Defaults to
        ``instance.subdomain`` (service instances). For hosting
        instances the cron passes each enumerated DB name in turn.
        """
        db_name = db_name or instance.subdomain
        now_str = fields.Datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        backup_name = 'backup_%s' % now_str
        partner = instance.partner_id
        partner_folder = '%s_%s' % (
            partner.id, self._sanitize_name(partner.name),
        ) if partner else 'no_partner'
        # For hosting, segregate per DB so multiple DBs per instance
        # don't collide in the bucket. Service instances continue with
        # the legacy layout (partner/db/backup.zip) since their db == sub.
        if instance.is_hosting:
            object_key = '%s/%s/%s/%s.zip' % (
                partner_folder, instance.subdomain, db_name, backup_name,
            )
        else:
            object_key = '%s/%s/%s.zip' % (
                partner_folder, db_name, backup_name,
            )

        backup = self.create({
            'instance_id': instance.id,
            'name': backup_name,
            'db_name': db_name,
            'bucket_path': object_key,
            'state': 'running',
        })

        try:
            size_bytes = backup._create_and_upload_backup(
                instance, object_key, db_name=db_name,
            )
            url = backup._generate_presigned_url()
            now = fields.Datetime.now()
            backup.write({
                'state': 'done',
                'size_mb': round(size_bytes / (1024 * 1024), 2),
                'download_url': url,
                'download_url_expiry': now + datetime.timedelta(seconds=PRESIGNED_URL_EXPIRY),
            })
        except Exception as e:
            backup.write({
                'state': 'failed',
                'error_message': str(e),
            })
            raise

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
        db_name = self.db_name or instance.subdomain

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
            if self.ephemeral:
                # On-demand: stream straight to the bucket — diskless,
                # multipart, any size — in the customer's chosen format.
                if self.format == 'dump':
                    size_bytes = self._stream_pg_dump_to_bucket(
                        instance, object_key, db_name,
                    )
                else:
                    size_bytes = self._stream_odoo_zip_to_bucket(
                        instance, object_key, db_name,
                    )
            else:
                size_bytes = self._create_and_upload_backup(
                    instance, object_key, db_name=db_name,
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

    # Fixed retention for hosting full-instance snapshots, per product
    # spec: keep the N most recent snapshots, drop older ones. Switched
    # away from a day-based cutoff because pre-restore safety snapshots
    # (taken on every restore) could push the customer's daily snapshot
    # out of its bucket and we'd retain only the most recent restore-
    # adjacent ones. Count-based retention is what the portal copy
    # documents.
    HOSTING_MAX_SNAPSHOTS = 7
    # Legacy alias — some older callers still reference this name. The
    # value is meaningless now (we don't cull by age anymore) but kept
    # to avoid attribute-error surprises in field code.
    HOSTING_RETENTION_DAYS = 7

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
        - Hosting instances: keep at most ``HOSTING_MAX_SNAPSHOTS``
          full-instance snapshots per instance, drop the oldest.
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

        # --- Hosting: keep the ``HOSTING_MAX_SNAPSHOTS`` most recent
        # full-instance snapshots per instance, drop the rest. Backup
        # creation calls ``_trim_hosting_snapshots`` inline so the cap
        # is also enforced between cron runs; this sweep catches any
        # rows the inline path missed (e.g. instances that haven't
        # taken a new snapshot since the cap changed).
        instances = self.env['saas.instance'].search([
            ('is_hosting', '=', True),
        ])
        for instance in instances:
            self._trim_hosting_snapshots(instance)

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

    def _cleanup_excess_for_instance(self, instance):
        """Remove excess backups for a single instance against the fixed
        retention limit (DEFAULT_MAX_BACKUPS), without waiting for the
        daily cron.
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
