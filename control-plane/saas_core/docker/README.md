# Lightweight Odoo Docker Images — Setup Guide

## Overview

This setup replaces the standard Odoo Docker images with lightweight versions that share a single copy of Odoo source code across all containers on the same server.

**Standard approach:** Each container has its own Odoo source (~500MB per instance)
**This approach:** One shared read-only copy per Odoo version, mounted into all containers

**Result:** 100 instances on Odoo 18.0 use ~500MB for source instead of ~50GB.

---

## Architecture

```
Server
├── /opt/odoo-source/
│   ├── 14.0/              ← Odoo 14.0 source (git clone, shared)
│   ├── 15.0/              ← Odoo 15.0 source
│   ├── 16.0/              ← Odoo 16.0 source
│   ├── 17.0/              ← Odoo 17.0 source
│   └── 18.0/              ← Odoo 18.0 source
│
├── /home/saas/instances/
│   ├── customer-a/
│   │   ├── docker-compose.yml    ← mounts /opt/odoo-source/18.0:/opt/odoo:ro
│   │   ├── addons/               ← customer's custom addons (unique)
│   │   ├── config/odoo.conf      ← instance config (unique)
│   │   └── data/odoo/            ← database + filestore (unique)
│   │
│   ├── customer-b/
│   │   ├── docker-compose.yml    ← mounts /opt/odoo-source/17.0:/opt/odoo:ro
│   │   └── ...
│   └── ...
│
└── Docker images
    ├── odoo-light:14.0       ← Python 3.8 + deps (no Odoo source)
    ├── odoo-light:15.0       ← Python 3.8 + deps
    ├── odoo-light:16.0       ← Python 3.10 + deps
    ├── odoo-light:17.0       ← Python 3.10 + deps
    └── odoo-light:18.0       ← Python 3.12 + deps
```

---

## Step 1: Build the Lightweight Images

Run this on your **build machine** (or CI/CD server).

### 1.1 Navigate to the docker directory

```bash
cd saas_core/docker/
```

### 1.2 Build all versions at once

```bash
chmod +x build-all.sh
./build-all.sh
```

This builds `odoo-light:11.0` through `odoo-light:18.0`.

### 1.3 Or build a specific version

```bash
docker build \
  --build-arg ODOO_VERSION=18.0 \
  -t odoo-light:18.0 \
  -f Dockerfile.odoo-light .
```

### 1.4 Verify the images

```bash
docker images | grep odoo-light
```

Expected output — images are roughly half the size of official images:
```
odoo-light   18.0    abc123   600MB
odoo-light   17.0    def456   580MB
odoo-light   16.0    ghi789   560MB
...
```

---

## Step 2: Push Images to Your Registry

If your Docker host servers are remote, push the images to a container registry.

### Option A: Docker Hub (private)

```bash
# Tag for your registry
docker tag odoo-light:18.0 yourname/odoo-light:18.0
docker tag odoo-light:17.0 yourname/odoo-light:17.0
# ... repeat for all versions

# Push
docker push yourname/odoo-light:18.0
docker push yourname/odoo-light:17.0
```

### Option B: Private registry

```bash
docker tag odoo-light:18.0 registry.yourcompany.com/odoo-light:18.0
docker push registry.yourcompany.com/odoo-light:18.0
```

### Option C: Same machine (no push needed)

If building directly on the Docker host, skip this step.

---

## Step 3: Setup Odoo Source on Each Docker Host

Run this **once per server** where instances will run.

### 3.1 Copy and run the setup script

```bash
scp setup-source.sh user@your-server:/tmp/
ssh user@your-server "chmod +x /tmp/setup-source.sh && sudo /tmp/setup-source.sh"
```

Or manually clone specific versions:

```bash
# On the Docker host server
sudo mkdir -p /opt/odoo-source/18.0
cd /opt/odoo-source/18.0
sudo git clone --depth 1 --branch 18.0 https://github.com/odoo/odoo.git .
```

### 3.2 Verify

```bash
ls /opt/odoo-source/
# Should show: 14.0  15.0  16.0  17.0  18.0

ls /opt/odoo-source/18.0/odoo-bin
# Should exist

du -sh /opt/odoo-source/*/
# Shows size per version (~300-500MB each)
```

### 3.3 Set permissions

```bash
# Make source read-only to prevent accidental modifications
sudo chmod -R a+rX /opt/odoo-source/
```

---

## Step 4: Configure the SaaS Platform

### 4.1 Update Odoo Version Records

In the Odoo backend: **SaaS Manager > Configuration > Odoo Versions**

For each version, update:

| Field | Old Value | New Value |
|-------|-----------|-----------|
| Docker Image | `odoo` | `odoo-light` |
| Image Tag | `18.0` | `18.0` (unchanged) |

Example:
- **Version:** 18.0
- **Docker Image:** `odoo-light`
- **Image Tag:** `18.0`
- **Nginx Template:** New (16+)

Repeat for all versions you support.

### 4.2 For private registries

If you pushed to a private registry, use the full image path:

- **Docker Image:** `registry.yourcompany.com/odoo-light`

### 4.3 Pull images on remote servers

If using remote Docker hosts, pull the images:

```bash
ssh user@docker-host "docker pull odoo-light:18.0"
# or for private registry:
ssh user@docker-host "docker pull registry.yourcompany.com/odoo-light:18.0"
```

---

## Step 5: Test with a New Instance

### 5.1 Create a test instance

From the website:
1. Go to `/services` or `/hosting`
2. Configure a plan
3. Complete the order

### 5.2 Verify the container

SSH to the Docker host and check:

```bash
# Check the container is running
docker ps | grep odoo_testsubdomain

# Check the source is mounted correctly
docker exec odoo_testsubdomain ls /opt/odoo/odoo-bin
# Should show: /opt/odoo/odoo-bin

# Check Odoo can import its modules
docker exec odoo_testsubdomain python3 -c "import odoo; print(odoo.__version__)"
# Should print the version number

# Check the mount is read-only
docker exec odoo_testsubdomain touch /opt/odoo/test 2>&1
# Should fail with: Read-only file system
```

### 5.3 Check the docker-compose.yml

```bash
cat /home/saas/instances/testsubdomain/docker-compose.yml
```

Should contain:
```yaml
volumes:
  - /opt/odoo-source/18.0:/opt/odoo:ro    # ← shared source
  - ./data/odoo:/var/lib/odoo
  - ./config:/etc/odoo
  - ./addons:/mnt/extra-addons
```

---

## Step 6: Migrate Existing Instances

For instances already running with the old (full) images:

### 6.1 Update docker-compose.yml

The SaaS platform regenerates `docker-compose.yml` on restart/redeploy. So:

1. In Odoo backend, go to the instance
2. Click **Redeploy** (or **Restart**)
3. The new `docker-compose.yml` will use `odoo-light` image + shared source mount

### 6.2 Or manually update a specific instance

```bash
cd /home/saas/instances/customer-subdomain/

# Stop the container
docker compose down

# The SaaS platform will regenerate configs on next restart
# Or manually edit docker-compose.yml:
# Change image: odoo:18.0 → odoo-light:18.0
# Add volume: /opt/odoo-source/18.0:/opt/odoo:ro

# Start with new config
docker compose up -d
```

### 6.3 Bulk migration

To update all instances on a server at once:

```bash
# List all instance directories
for dir in /home/saas/instances/*/; do
    cd "$dir"
    echo "Migrating: $dir"
    docker compose down
    docker compose up -d
    cd -
done
```

Note: This causes brief downtime per instance. For zero-downtime migration, restart instances one by one during maintenance windows.

---

## Ongoing Maintenance

### Update Odoo Source (Security Patches, Bug Fixes)

When Odoo releases patches:

```bash
# On each Docker host
cd /opt/odoo-source/18.0
sudo git pull --ff-only

# Restart affected containers to pick up changes
# Option A: Restart all 18.0 instances
for dir in /home/saas/instances/*/; do
    if grep -q "odoo-light:18.0" "$dir/docker-compose.yml" 2>/dev/null; then
        echo "Restarting: $dir"
        cd "$dir" && docker compose restart && cd -
    fi
done

# Option B: Use the SaaS platform bulk restart (from Odoo backend)
```

### Update the Lightweight Image (System Dependencies)

When you need to update Python packages or system libraries:

```bash
# Rebuild the image
docker build --build-arg ODOO_VERSION=18.0 --no-cache -t odoo-light:18.0 -f Dockerfile.odoo-light .

# Push to registry (if using remote servers)
docker push yourregistry/odoo-light:18.0

# Pull on servers and recreate containers
ssh user@server "docker pull odoo-light:18.0"
# Containers using this image need to be recreated:
# docker compose up -d --force-recreate
```

### Add a New Odoo Version

When Odoo releases a new version (e.g., 19.0):

```bash
# 1. Build the image
docker build --build-arg ODOO_VERSION=19.0 -t odoo-light:19.0 -f Dockerfile.odoo-light .

# 2. Clone source on servers
ssh user@server "
  mkdir -p /opt/odoo-source/19.0
  cd /opt/odoo-source/19.0
  git clone --depth 1 --branch 19.0 https://github.com/odoo/odoo.git .
"

# 3. Create version record in Odoo backend:
#    Name: 19.0
#    Docker Image: odoo-light
#    Image Tag: 19.0
#    Nginx Template: New (16+)

# 4. For hosting: check "Hosting Version" on the version record
```

---

## Troubleshooting

### Container fails to start: "Odoo source not found at /opt/odoo"

The source directory is not mounted. Check:
```bash
# Is the source cloned?
ls /opt/odoo-source/18.0/odoo-bin

# Is docker-compose.yml correct?
grep "odoo-source" docker-compose.yml
# Should show: /opt/odoo-source/18.0:/opt/odoo:ro
```

### "ModuleNotFoundError: No module named 'odoo'"

The symlink inside the image is broken. Rebuild:
```bash
docker build --no-cache --build-arg ODOO_VERSION=18.0 -t odoo-light:18.0 -f Dockerfile.odoo-light .
```

### Container starts but Odoo crashes with import errors

Python version mismatch. Ensure the source version matches the image version:
- `odoo-light:18.0` must mount `/opt/odoo-source/18.0` (not 17.0)
- Check: `docker exec container_name python3 --version`

### Permission denied on /opt/odoo

The mount must be readable by the odoo user (UID 101 in official images):
```bash
sudo chmod -R a+rX /opt/odoo-source/
```

### Slow first start after source update

Git pull changes file timestamps, causing Python to recompile `.pyc` files. This is normal and only happens once. Subsequent starts are fast.

To pre-compile:
```bash
sudo python3 -m compileall /opt/odoo-source/18.0/odoo/ -q
```

---

## File Reference

```
saas_core/docker/
├── Dockerfile.odoo-light    # Dockerfile for lightweight images
├── entrypoint-light.sh      # Container entrypoint (finds and runs odoo-bin)
├── build-all.sh             # Script to build all version images
├── setup-source.sh          # Script to clone Odoo source on servers
└── README.md                # This file

saas_core/templates/
├── docker-compose.yml.jinja # Template with shared source mount
├── pip_install.sh           # Startup script for pip packages
└── odoo.conf.jinja          # Odoo configuration template
```

---

## Security Notes

- Odoo source is mounted **read-only** (`:ro`) — containers cannot modify shared code
- Each instance has its own **data directory**, **config**, and **addons** — fully isolated
- The shared source is the same as what Odoo ships in their official Docker images — no security risk
- Customer custom addons are stored per-instance in `./addons/`, not in the shared source
