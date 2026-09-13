# Server Setup Guide — Docker, Nginx, PostgreSQL

Complete guide to setting up a production server for the SaaS platform.

---

## Requirements

- **OS:** Ubuntu 22.04 LTS or 24.04 LTS (recommended)
- **RAM:** Minimum 4GB, recommended 16GB+
- **Disk:** SSD, minimum 50GB, recommended 200GB+
- **CPU:** Minimum 2 cores, recommended 4+
- **Network:** Static IP, ports 80/443 open

---

## Step 1: Initial Server Setup

### 1.1 Update the system

```bash
sudo apt update && sudo apt upgrade -y
```

### 1.2 Set timezone

```bash
sudo timedatectl set-timezone UTC
```

### 1.3 Create the SaaS user

The platform connects via SSH using this user. It needs `sudo` privileges for Docker, Nginx, and PostgreSQL commands.

```bash
sudo adduser --system --group --home /home/saas --shell /bin/bash saas
sudo usermod -aG sudo saas

# Allow passwordless sudo (required — the platform runs commands via SSH)
echo "saas ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/saas
```

### 1.4 Setup SSH keys

On your **management server** (where the Odoo SaaS Manager module runs):

```bash
# Generate a key pair if you don't have one
ssh-keygen -t ed25519 -C "saas-platform" -f ~/.ssh/saas_deploy

# Copy public key to the Docker host
ssh-copy-id -i ~/.ssh/saas_deploy.pub saas@your-server-ip
```

Test the connection:
```bash
ssh -i ~/.ssh/saas_deploy saas@your-server-ip "echo OK"
```

---

## Step 1.5: Install restic (required for daily backups)

The daily-backup feature uses [restic](https://restic.net/) to push
incremental, deduplicated, encrypted snapshots to the same object
store you've configured for backups (S3 / DO Spaces / GCS). Without
restic on this host, `daily_backup_enabled=True` instances will fail
the nightly cron with a clear "restic not installed" error.

```bash
# Debian 12+ / Ubuntu 22.04+: restic is in the repos
sudo apt install -y restic

# Verify
restic version
# expected: restic 0.16.x (or newer)
```

If your distro's restic is older than 0.16, pull a static binary
from <https://github.com/restic/restic/releases> and drop it at
`/usr/local/bin/restic` (chmod +x). The Odoo backup code only needs
the `restic` binary on `$PATH`.

**You do not need to set any restic env vars in the shell.** The
SaaS Manager passes the repository URL, encryption password, and
cloud credentials via environment on each invocation — they never
sit on disk and never appear in `ps`.

## Step 2: Install Docker

### 2.1 Install Docker Engine

```bash
# Remove old versions
sudo apt remove -y docker docker-engine docker.io containerd runc 2>/dev/null

# Install prerequisites
sudo apt install -y ca-certificates curl gnupg

# Add Docker's official GPG key
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

# Add Docker repository
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

### 2.2 Configure Docker

```bash
# Add saas user to docker group
sudo usermod -aG docker saas

# Enable Docker to start on boot
sudo systemctl enable docker
sudo systemctl start docker
```

### 2.3 Configure Docker daemon

```bash
sudo tee /etc/docker/daemon.json << 'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  },
  "storage-driver": "overlay2",
  "live-restore": true,
  "default-ulimits": {
    "nofile": {
      "Name": "nofile",
      "Hard": 65536,
      "Soft": 65536
    }
  }
}
EOF

sudo systemctl restart docker
```

### 2.4 Verify

```bash
docker --version
docker compose version
docker run --rm hello-world
```

---

## Step 3: Install PostgreSQL

The platform creates databases and roles via SSH using `sudo -u postgres psql`. No special admin role is needed — just a working PostgreSQL installation accessible from Docker containers.

### 3.1 Install PostgreSQL 16

```bash
sudo sh -c 'echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list'
wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo apt-key add -
sudo apt update
sudo apt install -y postgresql-16 postgresql-client-16
```

### 3.2 Allow connections from Docker containers and remote servers

Docker containers and remote Docker host servers need to connect to PostgreSQL.

```bash
# Allow listening on all interfaces (required for remote connections)
sudo sed -i "s/#listen_addresses = 'localhost'/listen_addresses = '*'/" /etc/postgresql/16/main/postgresql.conf

# Verify the change took effect (should NOT show 127.0.0.1 after restart):
#   ss -tlnp | grep 5432
# Expected: 0.0.0.0:5432 (listening on all interfaces)

# Allow all hosts to connect with password authentication.
# For PostgreSQL 16+, use scram-sha-256 (md5 may be rejected).
# In production, restrict 0.0.0.0/0 to your private subnet (e.g. 10.135.0.0/16).
sudo tee -a /etc/postgresql/16/main/pg_hba.conf << 'EOF'

# Docker container networks (172.16.0.0/12 covers all Docker bridge subnets)
host    all    all    172.16.0.0/12    scram-sha-256
# Remote Docker host servers (private network)
host    all    all    0.0.0.0/0        scram-sha-256
EOF

sudo systemctl restart postgresql
```

### 3.3 Performance tuning

Create a tuning file (adjust values based on your server RAM):

```bash
sudo tee /etc/postgresql/16/main/conf.d/saas.conf << 'EOF'
# For 16GB RAM server — adjust proportionally
shared_buffers = 4GB
effective_cache_size = 12GB
work_mem = 16MB
maintenance_work_mem = 1GB
max_connections = 500
wal_buffers = 64MB
checkpoint_completion_target = 0.9
random_page_cost = 1.1
effective_io_concurrency = 200
default_statistics_target = 100
log_min_duration_statement = 1000
log_checkpoints = on
EOF
```

### 3.4 Restart PostgreSQL

```bash
sudo systemctl restart postgresql
sudo systemctl enable postgresql
```

### 3.5 Verify

```bash
# Check PostgreSQL is running
sudo systemctl status postgresql

# Check it's listening
sudo -u postgres psql -c "SELECT version();"
```

---

## Step 4: Install Nginx

The platform writes Nginx configs to `/etc/nginx/sites-enabled/` and reloads Nginx via SSH for each instance.

### 4.1 Install Nginx

```bash
sudo apt install -y nginx
```

### 4.2 Remove default site

```bash
sudo rm -f /etc/nginx/sites-enabled/default
```

### 4.3 Global Nginx optimization

```bash
sudo tee /etc/nginx/conf.d/optimization.conf << 'EOF'
proxy_connect_timeout 720s;
proxy_read_timeout 720s;
proxy_send_timeout 720s;
proxy_buffers 16 64k;
proxy_buffer_size 128k;

gzip on;
gzip_types text/plain text/css application/json application/javascript text/xml application/xml text/javascript image/svg+xml;
gzip_min_length 1000;
gzip_comp_level 6;

add_header X-Frame-Options "SAMEORIGIN" always;
add_header X-Content-Type-Options "nosniff" always;
add_header X-XSS-Protection "1; mode=block" always;

client_max_body_size 200M;

map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}
EOF
```

### 4.4 Restart Nginx

```bash
sudo nginx -t
sudo systemctl restart nginx
sudo systemctl enable nginx
```

---

## Step 5: Install Certbot (SSL)

The platform runs `certbot certonly --nginx` via SSH for each new instance.

```bash
sudo apt install -y certbot python3-certbot-nginx
```

Verify auto-renewal is enabled:
```bash
sudo systemctl status certbot.timer
```

---

## Step 6: Setup Odoo Source (Shared)

One copy of Odoo source per version, shared by all containers. See [README.md](README.md) for details.

```bash
chmod +x setup-source.sh
sudo ./setup-source.sh
```

Or manually:
```bash
sudo mkdir -p /opt/odoo-source/18.0
cd /opt/odoo-source/18.0
sudo git clone --depth 1 --branch 18.0 https://github.com/odoo/odoo.git .
sudo chmod -R a+rX /opt/odoo-source/
```

---

## Step 7: Load Docker Images

Build locally or pull from your registry. See [README.md](README.md) for details.

```bash
chmod +x build-all.sh
./build-all.sh
```

---

## Step 8: Create Instance Directory

```bash
sudo mkdir -p /home/saas/instances
sudo chown saas:saas /home/saas/instances
```

---

## Step 9: Configure DNS

Create a **wildcard DNS record** for each base domain:

```
Type: A
Name: *.saas.yourcompany.com
Value: YOUR_SERVER_IP
TTL: 300
```

Verify:
```bash
dig +short anything.saas.yourcompany.com
# Should return your server IP
```

---

## Step 10: Configure the SaaS Platform

In your Odoo backend (**SaaS Manager** module):

### 10.1 Add the server

**SaaS Manager > Configuration > Servers:**

| Field | Value |
|-------|-------|
| Name | Production Server 1 |
| Hostname / IP | your server IP |
| SSH Port | 22 |
| SSH User | `saas` |
| Is Docker Host | Yes |
| Is DB Server | Yes |
| Instance Base Path | `/home/saas/instances` |

Configure the SSH private key on the server record.

### 10.2 Add base domains

**SaaS Manager > Configuration > Base Domains:**

| Field | Value |
|-------|-------|
| Name | `saas.yourcompany.com` |

### 10.3 Add Odoo versions

**SaaS Manager > Configuration > Odoo Versions:**

| Version | Docker Image | Image Tag | Nginx Template | Hosting Version |
|---------|-------------|-----------|----------------|-----------------|
| 14.0 | odoo-light | 14.0 | Old (≤15) | Yes |
| 15.0 | odoo-light | 15.0 | Old (≤15) | Yes |
| 16.0 | odoo-light | 16.0 | New (16+) | Yes |
| 17.0 | odoo-light | 17.0 | New (16+) | Yes |
| 18.0 | odoo-light | 18.0 | New (16+) | Yes |
| 19.0 | odoo-light | 19.0 | New (16+) | Yes |

Check **Hosting Version** for versions available to hosting customers.

### 10.4 Configure pricing

**Settings > SaaS > Custom Plan Builder** (services) and **Hosting Plan Builder** (hosting).

### 10.5 Test SSH connection

On the server record, click **Test Connection** to verify the platform can reach the server.

---

## Step 11: Test

1. Go to your website `/services` or `/hosting`
2. Create a test instance
3. Wait for provisioning
4. Verify the instance URL works with HTTPS

On the server:
```bash
docker ps | grep odoo_
docker logs odoo_testsubdomain --tail 20
```

---

## Firewall

```bash
sudo ufw allow 22/tcp      # SSH
sudo ufw allow 80/tcp      # HTTP
sudo ufw allow 443/tcp     # HTTPS
sudo ufw deny 5432/tcp     # Block PostgreSQL from internet
sudo ufw enable
```

---

## Security Checklist

- [ ] SSH key authentication only (disable password login)
- [ ] Firewall enabled (ports 22, 80, 443 only)
- [ ] PostgreSQL not exposed to internet
- [ ] Odoo source mounted read-only
- [ ] `saas` user has passwordless sudo
- [ ] Unattended security updates enabled
- [ ] Fail2ban installed

```bash
# Enable unattended upgrades
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades

# Install Fail2ban
sudo apt install -y fail2ban
sudo systemctl enable fail2ban
```

---

## Step 12: Configure the SaaS Manager Server

These settings apply to the **Odoo server running the SaaS Manager module** (not the Docker/DB/Nginx servers).

### 12.1 Memory limits

The SaaS Manager runs SSH operations and background deploy threads that consume significant memory. The default Odoo worker memory limits are too low.

In your Odoo config (`/etc/odoo/odoo.conf` or CLI flags):

```ini
# Required for SaaS Manager — default (640MB) is too low
limit_memory_soft = 2684354560
limit_memory_hard = 3355443200
```

Without this, workers get killed mid-deployment, leaving instances stuck in "provisioning".

### 12.2 Worker mode

Any worker count works. The SSH terminal forwards input/output across workers via PostgreSQL `LISTEN`/`NOTIFY`, so `--workers > 0` is supported.

### 12.3 Set web.base.url

Required for **webhook auto-deploy** (automatic git pull on push). Go to:

**Settings > Technical > System Parameters > web.base.url**

Set it to your public HTTPS domain:
```
https://your-saas-manager.example.com
```

This URL is registered on GitHub/GitLab/Bitbucket as the webhook callback. Verify the endpoint is reachable:

```bash
curl https://your-saas-manager.example.com/saas/webhook-test
# Should return: {"status": "ok", "message": "Webhook endpoint is reachable"}
```

If using Nginx in front of the SaaS Manager, ensure `/saas/webhook/` is proxied:

```nginx
location /saas/webhook/ {
    proxy_pass http://127.0.0.1:8069;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

### 12.4 Configure backup storage

Required for **backup-before-delete** (final backup on instance cancellation) and **customer backups**.

**Settings > Technical > System Parameters:**

| Parameter | Example Value | Description |
|-----------|---------------|-------------|
| `saas_backup.provider` | `s3` or `gcs` | Cloud storage provider |
| `saas_backup.bucket_name` | `my-saas-backups` | Bucket name |
| `saas_backup.access_key` | `AKIA...` | S3 access key (or leave empty for GCS) |
| `saas_backup.secret_key` | `wJalr...` | S3 secret key (or leave empty for GCS) |
| `saas_backup.region` | `us-east-1` | S3 region |
| `saas_backup.endpoint` | `https://nyc3.digitaloceanspaces.com` | S3-compatible endpoint (DigitalOcean Spaces, MinIO, etc.) |
| `saas_backup.service_account_key` | `{...}` | GCS service account JSON key |

**S3-compatible providers** (AWS S3, DigitalOcean Spaces, MinIO, Wasabi):
```
saas_backup.provider = s3
saas_backup.bucket_name = my-saas-backups
saas_backup.access_key = YOUR_ACCESS_KEY
saas_backup.secret_key = YOUR_SECRET_KEY
saas_backup.region = nyc3
saas_backup.endpoint = https://nyc3.digitaloceanspaces.com
```

**Google Cloud Storage:**
```
saas_backup.provider = gcs
saas_backup.bucket_name = my-saas-backups
saas_backup.service_account_key = {"type":"service_account","project_id":"..."}
```

Without backup storage configured, instance cancellation will **skip the final backup** silently.

---

## Multi-Server Topology

For production, separate Docker hosts, database servers, and proxy servers:

| Server | Role | Key Config |
|--------|------|------------|
| Docker Host | Runs containers | Docker, Odoo source at `/opt/odoo-source/` |
| DB Server | Runs PostgreSQL | `listen_addresses = '*'`, `pg_hba.conf` allows Docker host IPs |
| Proxy Server | Runs Nginx + SSL | Certbot, wildcard DNS |
| SaaS Manager | Runs Odoo with SaaS module | `web.base.url`, memory limits, backup storage |

**DB Server firewall:** Allow port 5432 only from Docker host private IPs:
```bash
sudo ufw allow from 10.135.0.0/16 to any port 5432
```

**Proxy Server:** Must be able to reach Docker host container ports (8069/8072 range).

---

## Troubleshooting

### Instance stuck in "provisioning"

1. Check the **provisioning log** on the instance record in Odoo
2. Check Odoo server logs: `grep -i "saas_deploy\|background.*failed" /var/log/odoo/odoo-server.log`
3. Common causes:
   - Worker killed by memory limit (increase `limit_memory_soft`)
   - SSH connection to Docker/DB server failed
   - PostgreSQL not accepting connections (check `pg_hba.conf` and `listen_addresses`)

### Webhooks not triggering

1. Verify `web.base.url` is a public HTTPS URL
2. Test endpoint: `curl https://your-domain.com/saas/webhook-test`
3. Check webhook is registered: open repo record in Odoo > click "Check Webhook"
4. Check Odoo logs: `grep -i "webhook" /var/log/odoo/odoo-server.log`
5. Ensure the repo has a **git token** — required for webhook registration via API

### Terminal always disconnects

1. If behind Nginx, add to the SaaS Manager proxy config:
   ```nginx
   location /saas/terminal/ {
       proxy_buffering off;
       proxy_read_timeout 600s;
       proxy_send_timeout 600s;
       proxy_pass http://127.0.0.1:8069;
   }
   ```

### Database init fails with "connection refused"

1. On DB server: `ss -tlnp | grep 5432` — must show `0.0.0.0:5432`
2. If showing `127.0.0.1:5432`, fix `listen_addresses` in `postgresql.conf`
3. Verify `pg_hba.conf` allows connections from Docker host IP
4. Restart PostgreSQL: `sudo systemctl restart postgresql`

---

## Quick Reference

| Component | Config Location | Restart Command |
|-----------|----------------|-----------------|
| Docker | `/etc/docker/daemon.json` | `sudo systemctl restart docker` |
| PostgreSQL | `/etc/postgresql/16/main/` | `sudo systemctl restart postgresql` |
| Nginx | `/etc/nginx/sites-enabled/` | `sudo systemctl reload nginx` |
| Instance configs | `/home/saas/instances/<name>/` | `cd <dir> && docker compose restart` |
| Odoo source | `/opt/odoo-source/<version>/` | `git pull` + restart containers |
