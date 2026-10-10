# Running the SaaS control plane on a server

The control plane is Odoo 18 plus four addons (how they fit together: [01-ARCHITECTURE.md](01-ARCHITECTURE.md)):

| Addon | What it covers |
|---|---|
| `saas_core` | instances, clusters, job queue |
| `saas_billing` | plans, invoices, wallet, payments |
| `saas_website` | portal and JSON API; also serves the prebuilt React SPA |
| `saas_iam` | project team members, roles and environment scopes ([docs/iam.md](../docs/iam.md)) |

It runs as two processes on one server:

- **`odoo`**: the web server (prefork workers plus cron).
- **`saas-jobs`**: the durable-job worker. It runs deploys, restores, backups and database operations outside Odoo's time limits.

It manages tenants on Kubernetes clusters, set up after this guide with [03a-MICROK8S-CLUSTER-SETUP.md](03a-MICROK8S-CLUSTER-SETUP.md) (your own servers: HA or single node) or [03b-DOKS-CLUSTER-SETUP.md](03b-DOKS-CLUSTER-SETUP.md) (DigitalOcean Kubernetes). It never needs SSH to them, only each cluster's kubeconfig.

This guide installs natively on Ubuntu 24.04 with systemd. There is no container image for the control plane.

## 0. What you need

| Item | Notes |
|---|---|
| Server | Ubuntu 24.04, 4 vCPU, 8 GB RAM, 50 GB disk. Separate from the tenant cluster. |
| Domain | e.g. `saas.example.com` → this server (the portal + backend). |
| Ports | `80`, `443` public; `22` for you. Outbound `16443` to each cluster, `443` to the object storage and payment provider. |
| Object storage | S3-compatible or GCS bucket for tenant backups. |
| Accounts | A payment provider (Stripe or another Odoo-supported one) and SMTP for mail. |

## 1. System packages

```bash
sudo apt-get update
sudo apt-get install -y git python3.12 python3.12-venv python3.12-dev build-essential \
  libpq-dev libldap2-dev libsasl2-dev libxml2-dev libxslt1-dev libjpeg-dev zlib1g-dev \
  postgresql nginx certbot python3-certbot-nginx
# PDF reports (invoices): the patched-Qt wkhtmltopdf build
wget https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-3/wkhtmltox_0.12.6.1-3.jammy_amd64.deb
sudo apt-get install -y ./wkhtmltox_0.12.6.1-3.jammy_amd64.deb
```

## 2. User, code, Python environment

```bash
sudo useradd -m -d /opt/saas -s /bin/bash odoo
sudo -iu odoo bash <<'EOF'
git clone --depth 1 --branch 18.0 https://github.com/odoo/odoo.git ~/odoo18
git clone https://github.com/moutazmuhammad/odoo-saas-platform.git ~/platform
python3.12 -m venv ~/venv
~/venv/bin/pip install --upgrade pip wheel
~/venv/bin/pip install -r ~/odoo18/requirements.txt
~/venv/bin/pip install -r ~/platform/control-plane/requirements.txt
mkdir -p ~/data ~/log
EOF
```

The SPA is committed prebuilt (`control-plane/saas_website/static/spa/`), so no Node is needed on the server.

## 3. PostgreSQL

```bash
sudo -u postgres createuser --createdb odoo
sudo -u postgres psql -c "ALTER USER odoo PASSWORD '<db-password>'"
```

## 4. Configuration: `/etc/odoo/saas.conf`

Generate the secret-encryption key **once** and back it up with the database backups. Without it, the encrypted fields can't be read: kubeconfigs, registry and storage credentials.

```bash
sudo -u odoo /opt/saas/venv/bin/python -c \
  "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

```ini
[options]
admin_passwd = <long random master password>
db_host = localhost
db_port = 5432
db_user = odoo
db_password = <db-password>
db_name = saas
dbfilter = ^saas$
list_db = False
addons_path = /opt/saas/odoo18/addons,/opt/saas/platform/control-plane
data_dir = /opt/saas/data
logfile = /opt/saas/log/odoo.log
proxy_mode = True
http_interface = 127.0.0.1
http_port = 8069
gevent_port = 8072
workers = 9                 ; (2 x CPU) + 1
max_cron_threads = 2
limit_time_cpu = 120
limit_time_real = 330       ; live-log/terminal streams stay open up to 300s
limit_time_real_cron = 300
limit_memory_soft = 2147483648
limit_memory_hard = 2684354560
saas_secret_key = <the Fernet key from above>
```

```bash
sudo mkdir -p /etc/odoo && sudo chown root:odoo /etc/odoo && sudo chmod 750 /etc/odoo
# write the file above to /etc/odoo/saas.conf, then:
sudo chown root:odoo /etc/odoo/saas.conf && sudo chmod 640 /etc/odoo/saas.conf
```

- `saas_secret_key` must be set before the first cluster is registered. Without it, secrets are stored unencrypted.
- Each open live-log or terminal view holds one worker for up to 300s. Size `workers` for the number of admins and customers watching at once.

## 5. Create the database

```bash
sudo -iu odoo /opt/saas/venv/bin/python /opt/saas/odoo18/odoo-bin -c /etc/odoo/saas.conf \
  -d saas -i saas_core,saas_billing,saas_website,saas_iam --without-demo=all --stop-after-init
```

Do **not** run `control-plane/scripts/seed_dev.py` on a real server: it creates fake customers, regions and instances. Do not install `payment_demo` either.

## 6. Services

`/etc/systemd/system/saas-odoo.service`:

```ini
[Unit]
Description=SaaS control plane (Odoo)
After=network.target postgresql.service
Requires=postgresql.service

[Service]
User=odoo
Group=odoo
ExecStart=/opt/saas/venv/bin/python /opt/saas/odoo18/odoo-bin -c /etc/odoo/saas.conf
Restart=on-failure
RestartSec=5
KillMode=mixed

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/saas-jobs.service`:

```ini
[Unit]
Description=SaaS control plane - durable job worker
After=network.target postgresql.service
Requires=postgresql.service

[Service]
User=odoo
Group=odoo
Environment=SAAS_JOB_THREADS=4
ExecStart=/opt/saas/venv/bin/python /opt/saas/odoo18/odoo-bin \
  --addons-path=/opt/saas/odoo18/addons,/opt/saas/platform/control-plane \
  saas-jobs -c /etc/odoo/saas.conf -d saas
Restart=on-failure
RestartSec=5
# Let a running deploy/restore finish on stop/restart.
TimeoutStopSec=900

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now saas-odoo saas-jobs
grep "saas-jobs:" /opt/saas/log/odoo.log | tail -1   # "4 thread(s) serving database saas"
```

- `--addons-path=` has to come before `saas-jobs`, so Odoo can find the command.
- Without `saas-jobs`, jobs still run, but inside the web workers, under their time limits.

## 7. Nginx + TLS

`/etc/nginx/sites-available/saas`:

```nginx
upstream odoo      { server 127.0.0.1:8069; }
upstream odoochat  { server 127.0.0.1:8072; }
map $http_upgrade $connection_upgrade { default upgrade; '' close; }

server {
    listen 80;
    server_name saas.example.com;
    client_max_body_size 512m;          # backup uploads
    proxy_read_timeout 360s;            # > the 300s log/terminal streams
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Real-IP $remote_addr;

    location /websocket {
        proxy_pass http://odoochat;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
    }
    # Server-sent events (live logs, terminal output): no buffering.
    location ~ ^/saas/(instance/\d+/logs/stream|terminal/) {
        proxy_pass http://odoo;
        proxy_buffering off;
        proxy_cache off;
    }
    location / { proxy_pass http://odoo; }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/saas /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d saas.example.com      # adds 443 + renewal
sudo ufw allow OpenSSH && sudo ufw allow 'Nginx Full' && sudo ufw enable
```

- Give tenants (not this server) their own wildcard domain; see the cluster guide (03a step 7.3, 03b step 3).

## 8. First configuration (backend: `https://saas.example.com/web`)

1. **Log in:** as `admin` / `admin`, and change the password immediately.
2. **Groups:** on the Users form, *SaaS* sets User or Manager, and *SaaS Terminal* sets Pod Shell (terminals into tenant pods) or Cluster Shell (kubectl on whole clusters; needs the cluster's toolbox ServiceAccount: [03a](03a-MICROK8S-CLUSTER-SETUP.md) step 12.4 or [03b](03b-DOKS-CLUSTER-SETUP.md) step 7.2). The `admin` user gets Manager and Cluster Shell on install.
3. **Mail:** Settings → Technical → Outgoing Mail Servers (SMTP). Set the company email.
4. **SaaS Manager settings:** Settings → SaaS Manager:
   - support email;
   - trial length;
   - backup storage (provider, keys, bucket, region/endpoint);
   - pricing rates.
5. **Payments:** Invoicing/Website → Payment Providers. Enable the real provider with its keys and test a small real charge.
6. **Catalog** (*SaaS Manager → Catalog*; for a test catalog, install the **SaaS Demo Catalog** app instead):
   - *Odoo versions*: e.g. `18.0`, image `odoo`, tag `18.0`, hosting version on.
   - *Products*: e.g. "Odoo Hosting" with *Is hosting* on.
   - *Plans*: CPU/RAM/workers/storage limits and prices, linked to products.
   - *Scaling*: adjust the plan CPU/RAM limits; each tenant runs one Odoo pod and one PostgreSQL instance.
7. **Register each cluster** ([03a](03a-MICROK8S-CLUSTER-SETUP.md) step 13, or [03b](03b-DOKS-CLUSTER-SETUP.md) step 9). That step has a copy-paste script. To do it by hand in the backend instead (*SaaS Manager → Configuration*):
   1. *Regions*: the location customers pick (name, code).
   2. *Kubernetes Clusters*: the region, upload the kubeconfig, the TLS ClusterIssuer, the node IP. Fill the *Image Builds* tab if Git repos are used. A region can have several clusters.
   3. *Base Domains*: the tenant wildcard domain (e.g. `apps.example.com`) and the cluster its DNS points at.
8. **Smoke test:**
   1. Sign up as a customer on `https://saas.example.com/`.
   2. Order a plan, pay; the instance deploys in about 2-4 minutes.
   3. Check `https://<sub>.apps.example.com`.
   4. Cancel it again.

## 9. Operating it

For automated builds and production updates, follow [06-CICD.md](06-CICD.md). It lists the required GitHub secrets and server preparation, builds the SPA on CI, and deploys both services with pre-upgrade backups. Once enabled, use that release process instead of `git pull` on the managed release symlink.

**Updating the code:**

```bash
sudo -iu odoo git -C ~/platform pull
sudo systemctl stop saas-jobs saas-odoo
sudo -iu odoo /opt/saas/venv/bin/python /opt/saas/odoo18/odoo-bin -c /etc/odoo/saas.conf \
  -d saas -u saas_core,saas_billing,saas_website,saas_iam --stop-after-init
sudo systemctl start saas-odoo saas-jobs
```

Always restart **both** services: the worker runs the same Python code.

**Backups of the control-plane DB and filestore:** nightly, plus before every upgrade. Keep copies off-server, together with `/etc/odoo/saas.conf`, which holds the encryption key.

```bash
sudo -u postgres pg_dump -Fc saas > /backup/saas-$(date +%F).dump
sudo tar czf /backup/saas-filestore-$(date +%F).tgz -C /opt/saas/data filestore/saas
```

To restore:
1. Recreate the database with `pg_restore -d saas`.
2. Put the filestore back.
3. Keep the same `saas_secret_key`.

**Logs:** `/opt/saas/log/odoo.log` holds both the web server and the worker; they have different PIDs.

**Queued work:**

```bash
sudo -u postgres psql saas -c \
  "select id, model, res_id, method, state, attempts from saas_job order by id desc limit 20"
```

**Health:**
- `systemctl status saas-odoo saas-jobs`
- `curl -sI https://saas.example.com/web/login` should return `200`
- Tenants: `kubectl get odooinstances -A` on each cluster.
