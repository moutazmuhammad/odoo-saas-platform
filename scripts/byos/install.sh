#!/usr/bin/env bash
# Veltnex "Bring Your Own Server" onboarding.
#
# Turns a FRESH, EMPTY server that the customer dedicates to Veltnex into a
# managed BYOS host: firewall, Docker, Traefik, Netdata, fail2ban and a
# per-server platform SSH key (owner decision, PLAN-implementation P1.32).
#
#   curl -fsSL https://<portal>/byos/install.sh | sudo VELTNEX_TOKEN=<token> bash
#   ... | sudo VELTNEX_TOKEN=<token> bash -s -- --yes      (non-interactive)
#   sudo bash install.sh --check                            (preflight only)
#   sudo bash install.sh --detach                           (stop Veltnex management)
#
# Written from scratch for Veltnex; it does not reuse third-party code.
# The control plane serves this file per token and substitutes the
# __PLACEHOLDERS__ below (API URL, version, pinned image digests).

set -Eeuo pipefail
umask 077

# ---------------------------------------------------------------- settings --
VELTNEX_API="${VELTNEX_API:-__VELTNEX_API__}"
SCRIPT_VERSION="__SCRIPT_VERSION__"
# Pinned by digest at serve time; never a moving tag.
TRAEFIK_IMAGE="${TRAEFIK_IMAGE:-__TRAEFIK_IMAGE__}"
NETDATA_IMAGE="${NETDATA_IMAGE:-__NETDATA_IMAGE__}"
# Docker's release signing key (download.docker.com/linux/*/gpg).
DOCKER_GPG_FINGERPRINT="9DC858229FC7DD38854AE2D88D81803C0EBFCD88"

STATE_DIR=/etc/veltnex
STATE_FILE="$STATE_DIR/server.env"
DATA_DIR=/opt/veltnex
LOG_FILE=/var/log/veltnex-install.log
KEY_MARK="veltnex-managed"
NETWORK=veltnex

MIN_CPU=2
MIN_RAM_MB=3600          # a "4 GB" VM reports a little under 4096
MIN_DISK_GB=40

SUPPORTED_OS="Ubuntu 22.04 / 24.04 / 26.04 LTS, Debian 12 / 13"

MODE=install
ASSUME_YES=false
STEP="start"
SERVER_ID=""
PROGRESS_TOKEN=""
REPAIR_CA=false

# ------------------------------------------------------------------ output --
if [[ -t 1 ]]; then
    C_RED=$'\e[31m'; C_YEL=$'\e[33m'; C_GRN=$'\e[32m'; C_DIM=$'\e[2m'; C_OFF=$'\e[0m'
else
    C_RED=""; C_YEL=""; C_GRN=""; C_DIM=""; C_OFF=""
fi
info() { printf '%s\n' "$*"; }
ok()   { printf '  %s✓%s %s\n' "$C_GRN" "$C_OFF" "$*"; }
warn() { printf '  %s!%s %s\n' "$C_YEL" "$C_OFF" "$*" >&2; }
fail() { printf '  %s✗%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; }
die()  { fail "$*"; exit 1; }

# JSON without hand-built strings: every value goes through json.dumps.
# Usage: json key1 value1 key2 value2 ...  (values are strings; *_int keys are numbers)
json() {
    python3 - "$@" <<'PY'
import json, sys
a = sys.argv[1:]
out = {}
for k, v in zip(a[0::2], a[1::2]):
    if k.endswith("_int"):
        out[k[:-4]] = int(v or 0)
    elif k.endswith("_bool"):
        out[k[:-5]] = v == "true"
    else:
        out[k] = v
print(json.dumps(out))
PY
}

# Read one field from a JSON document on stdin; empty if missing.
json_get() {
    python3 -c 'import json,sys
d=json.load(sys.stdin)
v=d
for p in sys.argv[1].split("."):
    v=v.get(p) if isinstance(v,dict) else None
print("" if v is None else (",".join(v) if isinstance(v,list) else v))' "$1"
}

# ------------------------------------------------------------- API client --
# HTTPS only, TLS 1.2+, bounded time, a few retries. Never traced.
api() {  # api METHOD PATH [JSON]  -> body on stdout, non-zero on HTTP error
    local method="$1" path="$2" body="${3:-}" auth=()
    if [[ -n "$PROGRESS_TOKEN" ]]; then auth=(-H "Authorization: Bearer $PROGRESS_TOKEN"); fi
    curl --proto '=https' --tlsv1.2 -fsS --retry 3 --retry-delay 2 \
         --connect-timeout 10 --max-time 60 -X "$method" \
         -H "Content-Type: application/json" -H "X-Veltnex-Script: $SCRIPT_VERSION" \
         "${auth[@]}" ${body:+--data "$body"} "$VELTNEX_API$path"
}

report() {  # report STATUS MESSAGE — best effort, never fails the install
    [[ -n "$SERVER_ID" && -n "$PROGRESS_TOKEN" ]] || return 0
    api POST "/byos/v1/servers/$SERVER_ID/progress" \
        "$(json step "$STEP" status "$1" message "$2")" >/dev/null 2>&1 || true
}

step() {  # step NAME "Human title"
    STEP="$1"
    info ""
    info "${C_DIM}──${C_OFF} $2"
    report running "$2"
}

on_error() {
    local code=$? line=$1
    fail "Setup stopped during '$STEP' (line $line, exit $code)."
    report failed "Stopped during $STEP (line $line, exit $code)"
    info "  Log: $LOG_FILE"
    info "  Fix the issue and run the same command again; it resumes safely."
    exit "$code"
}

# ----------------------------------------------------------------- helpers --
have() { command -v "$1" >/dev/null 2>&1; }

in_container() {
    if have systemd-detect-virt && systemd-detect-virt --container -q 2>/dev/null; then return 0; fi
    [[ -f /.dockerenv || -f /run/systemd/container ]]
}

wait_apt() {  # cloud-init / unattended-upgrades hold the dpkg lock on first boot
    local waited=0
    while fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/lib/apt/lists/lock \
                /var/cache/apt/archives/lock >/dev/null 2>&1; do
        (( waited == 0 )) && info "  waiting for apt (first-boot updates)…"
        (( waited >= 600 )) && die "apt is still locked after 10 minutes."
        sleep 5; waited=$((waited + 5))
    done
}

apt_install() {
    wait_apt
    DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 \
        install -y -q --no-install-recommends "$@" >>"$LOG_FILE" 2>&1
}

ssh_ports() {  # the ports sshd really listens on, not a guess from the config
    local ports=""
    if have sshd; then ports=$(sshd -T 2>/dev/null | awk '$1=="port"{print $2}' | sort -u | tr '\n' ' '); fi
    printf '%s' "${ports:-22}"
}

port_busy() { ss -Hltn "sport = :$1" 2>/dev/null | grep -q .; }

ram_mb()  { awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo; }
disk_gb() { df -BG --output=avail / | awk 'NR==2{gsub("G","");print int($1)}'; }

load_state() {
    [[ -f "$STATE_FILE" ]] || return 1
    SERVER_ID=$(awk -F= '$1=="SERVER_ID"{print $2}' "$STATE_FILE")
    PROGRESS_TOKEN=$(awk -F= '$1=="PROGRESS_TOKEN"{print $2}' "$STATE_FILE")
    [[ -n "$SERVER_ID" ]]
}

save_state() {
    install -d -m 0700 "$STATE_DIR"
    printf 'SERVER_ID=%s\nPROGRESS_TOKEN=%s\nINSTALLED_BY=%s\n' \
        "$SERVER_ID" "$PROGRESS_TOKEN" "$SCRIPT_VERSION" >"$STATE_FILE"
    chmod 0600 "$STATE_FILE"
}

# --------------------------------------------------------------- preflight --
OS_ID="" OS_VERSION="" OS_CODENAME=""

preflight() {
    STEP=preflight
    local errors=0
    info "Checking this server before changing anything…"

    if [[ $EUID -ne 0 ]]; then fail "Run as root (use sudo)."; exit 1; fi
    ok "running as root"

    # shellcheck source=/dev/null
    . /etc/os-release
    OS_ID="${ID:-}" OS_VERSION="${VERSION_ID:-}" OS_CODENAME="${VERSION_CODENAME:-}"
    case "$OS_ID:$OS_VERSION" in
        ubuntu:22.04|ubuntu:24.04|ubuntu:26.04|debian:12|debian:13) ok "OS ${PRETTY_NAME:-$OS_ID $OS_VERSION}" ;;
        *) fail "Unsupported OS '${PRETTY_NAME:-unknown}'. Supported: $SUPPORTED_OS."; errors=$((errors+1)) ;;
    esac

    case "$(uname -m)" in
        x86_64|aarch64) ok "architecture $(uname -m)" ;;
        *) fail "Unsupported CPU architecture $(uname -m) (x86_64 or aarch64 needed)."; errors=$((errors+1)) ;;
    esac

    if [[ "$(ps -p 1 -o comm= 2>/dev/null)" != "systemd" ]]; then
        fail "systemd is required."; errors=$((errors+1))
    fi
    if in_container; then
        fail "This looks like a container (LXC/Docker). Use a VM or a dedicated server."; errors=$((errors+1))
    fi

    local cpu ram disk
    cpu=$(nproc); ram=$(ram_mb); disk=$(disk_gb)
    if (( cpu < MIN_CPU )); then fail "$cpu CPU core(s); at least $MIN_CPU needed."; errors=$((errors+1)); else ok "$cpu CPU cores"; fi
    if (( ram < MIN_RAM_MB )); then fail "${ram} MB RAM; at least 4 GB needed."; errors=$((errors+1)); else ok "${ram} MB RAM"; fi
    if (( disk < MIN_DISK_GB )); then fail "${disk} GB free on /; at least $MIN_DISK_GB GB needed."; errors=$((errors+1)); else ok "${disk} GB free disk"; fi

    # Fresh-server rule: Veltnex resets the firewall and takes ports 80/443,
    # so the server must not already be running anything (owner decision).
    local rerun=false
    if load_state; then rerun=true; ok "previous Veltnex setup found (server $SERVER_ID); resuming"; fi
    if ! $rerun; then
        local busy=()
        for p in 80 443; do port_busy "$p" && busy+=("$p"); done
        if (( ${#busy[@]} )); then fail "Port(s) ${busy[*]} already in use; the server is not empty."; errors=$((errors+1)); fi
        local svc
        for svc in nginx apache2 httpd caddy lsws mysql mariadb postgresql; do
            if systemctl is-active --quiet "$svc" 2>/dev/null; then
                fail "Service '$svc' is running; the server is not empty."; errors=$((errors+1))
            fi
        done
        if have docker && [[ -n "$(docker ps -aq 2>/dev/null)" ]]; then
            fail "Docker already has containers; the server is not empty."; errors=$((errors+1))
        fi
        if [[ -d /opt/oecsh || -f /etc/cloudpanel ]]; then
            fail "Another hosting panel is installed here."; errors=$((errors+1))
        fi
        (( errors == 0 )) && ok "server is empty"
    fi

    if [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" != "yes" ]]; then
        warn "Clock is not NTP-synchronised; TLS certificates need a correct clock."
    fi

    if ! curl --proto '=https' -fsS --max-time 15 -o /dev/null "https://download.docker.com/" 2>/dev/null; then
        if [[ ! -s /etc/ssl/certs/ca-certificates.crt ]]; then
            warn "The CA certificate bundle is missing; it will be repaired."
            REPAIR_CA=true
        else
            fail "Cannot reach download.docker.com over HTTPS (outbound 443 is required)."; errors=$((errors+1))
        fi
    fi
    if [[ "$VELTNEX_API" == __*__ ]]; then
        fail "This copy of the script has no API address; download it from your Veltnex dashboard."; errors=$((errors+1))
    fi
    have python3 || { fail "python3 is required (present on all supported cloud images)."; errors=$((errors+1)); }

    if (( errors )); then
        info ""
        die "$errors check(s) failed. Nothing was changed."
    fi
    ok "all checks passed"
}

confirm_changes() {
    info ""
    info "Veltnex will now configure this server. Because it must be a FRESH server"
    info "dedicated to Veltnex, it will:"
    info "  • reset the firewall (UFW) to allow only SSH ($(ssh_ports | xargs)), 80 and 443"
    info "  • disable CSF if present"
    info "  • add this server's own Veltnex SSH key to root, usable only from Veltnex's IPs"
    info "  • install Docker, Traefik (80/443), Netdata (local only) and fail2ban"
    info "  • add swap if missing and raise inotify limits for Odoo"
    $ASSUME_YES && return 0
    [[ -r /dev/tty ]] || die "Re-run with --yes to confirm (no terminal to ask on)."
    local answer
    printf '\nType YES to continue: ' >/dev/tty
    read -r answer </dev/tty
    [[ "$answer" == "YES" ]] || die "Cancelled. Nothing was changed."
}

# ------------------------------------------------------------ registration --
SSH_PUBLIC_KEY="" PLATFORM_IPS="" ACME_EMAIL=""

register() {
    step register "Registering this server with Veltnex"
    local facts resp
    facts=$(json \
        hostname "$(hostname -f 2>/dev/null || hostname)" \
        os "$OS_ID" os_version "$OS_VERSION" arch "$(uname -m)" kernel "$(uname -r)" \
        cpu_int "$(nproc)" ram_mb_int "$(ram_mb)" disk_free_gb_int "$(disk_gb)" \
        ssh_ports "$(ssh_ports)" script_version "$SCRIPT_VERSION")

    if load_state; then
        resp=$(api POST "/byos/v1/servers/$SERVER_ID/resume" "$facts") \
            || die "Veltnex did not accept the resume (the setup may have been cancelled in the dashboard)."
    else
        [[ -n "${VELTNEX_TOKEN:-}" ]] || die "VELTNEX_TOKEN is missing. Copy the command from your Veltnex dashboard."
        resp=$(PROGRESS_TOKEN="$VELTNEX_TOKEN" api POST "/byos/v1/register" "$facts") \
            || die "Registration was refused. The token may be used or expired (30 minutes); create a new one."
        SERVER_ID=$(json_get server_id <<<"$resp")
        PROGRESS_TOKEN=$(json_get progress_token <<<"$resp")
        [[ -n "$SERVER_ID" && -n "$PROGRESS_TOKEN" ]] || die "Unexpected answer from Veltnex; nothing was changed."
        save_state
    fi
    unset VELTNEX_TOKEN

    SSH_PUBLIC_KEY=$(json_get ssh_public_key <<<"$resp")
    PLATFORM_IPS=$(json_get platform_ips <<<"$resp")
    ACME_EMAIL=$(json_get acme_email <<<"$resp")
    [[ "$SSH_PUBLIC_KEY" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+$ ]] || die "Veltnex sent an invalid key; nothing was installed."
    [[ "$PLATFORM_IPS" =~ ^[0-9a-fA-F:./,]+$ ]] || die "Veltnex sent an invalid IP list."
    [[ "$ACME_EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+$ ]] || die "Veltnex sent an invalid ACME e-mail."
    ok "registered as server $SERVER_ID"
}

# ------------------------------------------------------------------ system --
repair_ca() {
    $REPAIR_CA || return 0
    wait_apt
    DEBIAN_FRONTEND=noninteractive apt-get install --reinstall -y -q ca-certificates >>"$LOG_FILE" 2>&1
    update-ca-certificates --fresh >>"$LOG_FILE" 2>&1 || true
    curl --proto '=https' -fsS --max-time 15 -o /dev/null https://download.docker.com/ \
        || die "HTTPS still fails after repairing the CA bundle."
    ok "CA bundle repaired"
}

prepare_system() {
    step system "Preparing the system"
    repair_ca
    wait_apt
    DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 update -q >>"$LOG_FILE" 2>&1
    apt_install ca-certificates curl gnupg ufw fail2ban unattended-upgrades jq
    ok "base packages"

    # Security updates keep applying on their own.
    printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' \
        >/etc/apt/apt.conf.d/20auto-upgrades
    ok "automatic security updates"

    # Swap: same size as RAM up to 4 GB, half above, at most 8 GB.
    if ! swapon --noheadings --show=NAME | grep -q .; then
        local ram_gb size
        ram_gb=$(( ( $(ram_mb) + 512 ) / 1024 ))
        if (( ram_gb <= 4 )); then size=$ram_gb; else size=$(( ram_gb / 2 )); (( size > 8 )) && size=8; fi
        if (( $(disk_gb) > size + 20 )); then
            fallocate -l "${size}G" /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=$((size*1024)) status=none
            chmod 0600 /swapfile
            mkswap /swapfile >/dev/null
            swapon /swapfile
            grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
            ok "swap ${size} GB"
        else
            warn "not enough disk for swap; skipped"
        fi
    else
        ok "swap already present"
    fi

    # Odoo 19+ watches its module tree; the kernel default (8192 watches) is too low.
    cat >/etc/sysctl.d/90-veltnex.conf <<'EOF'
# Managed by Veltnex (BYOS)
fs.inotify.max_user_watches = 524288
fs.inotify.max_user_instances = 512
vm.swappiness = 10
EOF
    sysctl --system >/dev/null
    ok "kernel limits"
}

# ---------------------------------------------------------------- security --
configure_firewall() {
    step firewall "Configuring the firewall"
    local ports p
    ports=$(ssh_ports)

    if have csf; then
        csf -x >>"$LOG_FILE" 2>&1 || true
        systemctl disable --now csf lfd >>"$LOG_FILE" 2>&1 || true
        ok "CSF disabled"
    fi

    # Fresh dedicated server (confirmed above): start from a clean rule set.
    ufw --force reset >>"$LOG_FILE" 2>&1
    ufw default deny incoming >/dev/null
    ufw default allow outgoing >/dev/null
    for p in $ports; do ufw allow "$p/tcp" comment 'ssh' >/dev/null; done
    ufw allow 80/tcp comment 'http (Traefik)' >/dev/null
    ufw allow 443/tcp comment 'https (Traefik)' >/dev/null
    ufw --force enable >/dev/null
    ok "UFW: SSH ($ports), 80, 443"

    # Ban SSH brute force, never Veltnex's own addresses. The systemd backend
    # works on Debian 12+ where /var/log/auth.log no longer exists.
    install -d /etc/fail2ban/jail.d
    cat >/etc/fail2ban/jail.d/veltnex.local <<EOF
# Managed by Veltnex (BYOS)
[DEFAULT]
ignoreip = 127.0.0.1/8 ::1 ${PLATFORM_IPS//,/ }
bantime  = 1h
findtime = 10m
maxretry = 5

[sshd]
enabled = true
backend = systemd
port    = ${ports// /,}
EOF
    systemctl enable --now fail2ban >>"$LOG_FILE" 2>&1
    systemctl restart fail2ban
    ok "fail2ban"
}

install_platform_key() {
    step ssh_key "Installing this server's Veltnex SSH key"
    local file=/root/.ssh/authorized_keys line tmp
    install -d -m 0700 /root/.ssh
    touch "$file"; chmod 0600 "$file"

    # One key per server, only from Veltnex's addresses, no forwarding.
    line="from=\"${PLATFORM_IPS}\",no-agent-forwarding,no-port-forwarding,no-X11-forwarding ${SSH_PUBLIC_KEY} ${KEY_MARK}:${SERVER_ID}"
    tmp=$(mktemp)
    grep -v " ${KEY_MARK}:" "$file" >"$tmp" || true   # replace, never duplicate
    printf '%s\n' "$line" >>"$tmp"
    cat "$tmp" >"$file"; rm -f "$tmp"
    ok "key installed (from: ${PLATFORM_IPS})"

    # Key login for root must be allowed; passwords are left as they were.
    if [[ "$(sshd -T 2>/dev/null | awk '$1=="permitrootlogin"{print $2}')" == "no" ]]; then
        install -d /etc/ssh/sshd_config.d
        printf '# Managed by Veltnex (BYOS)\nPermitRootLogin prohibit-password\n' >/etc/ssh/sshd_config.d/90-veltnex.conf
        sshd -t || die "The SSH configuration did not validate; it was not reloaded."
        if systemctl list-unit-files ssh.service >/dev/null 2>&1; then systemctl reload ssh; else systemctl reload sshd; fi
        ok "root key login enabled (prohibit-password)"
    fi
}

# ------------------------------------------------------------------ docker --
install_docker() {
    step docker "Installing Docker"
    if have docker && docker info >/dev/null 2>&1; then
        ok "Docker already running ($(docker --version | awk '{print $3}' | tr -d ,))"
    else
        local keyring=/etc/apt/keyrings/docker.asc fp
        install -d -m 0755 /etc/apt/keyrings
        curl --proto '=https' --tlsv1.2 -fsSL "https://download.docker.com/linux/${OS_ID}/gpg" -o "$keyring"
        chmod 0644 "$keyring"
        fp=$(gpg --show-keys --with-colons "$keyring" 2>/dev/null | awk -F: '$1=="fpr"{print $10; exit}')
        [[ "$fp" == "$DOCKER_GPG_FINGERPRINT" ]] || die "Docker's signing key did not match the expected fingerprint."
        printf 'deb [arch=%s signed-by=%s] https://download.docker.com/linux/%s %s stable\n' \
            "$(dpkg --print-architecture)" "$keyring" "$OS_ID" "$OS_CODENAME" \
            >/etc/apt/sources.list.d/docker.list
        wait_apt
        DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 update -q >>"$LOG_FILE" 2>&1
        apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        ok "Docker installed"
    fi

    if [[ ! -f /etc/docker/daemon.json ]] || grep -q '"veltnex"' /etc/docker/daemon.json; then
        install -d /etc/docker
        cat >/etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "20m", "max-file": "5" },
  "live-restore": true,
  "labels": ["managed-by=veltnex"]
}
EOF
        chmod 0644 /etc/docker/daemon.json
    fi
    systemctl enable --now docker >>"$LOG_FILE" 2>&1
    systemctl restart docker
    for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
    docker info >/dev/null 2>&1 || die "Docker did not start."
    docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK" >/dev/null
    ok "Docker running, network '$NETWORK'"
}

# ---------------------------------------------------------- platform stack --
pinned() {  # refuse moving tags: images must be pinned by digest
    [[ "$1" == *@sha256:* ]] || die "Image '$1' is not pinned by digest; download the script again from the dashboard."
}

start_traefik() {
    step traefik "Starting Traefik"
    pinned "$TRAEFIK_IMAGE"
    install -d -m 0755 "$DATA_DIR/traefik" "$DATA_DIR/traefik/dynamic"
    install -d -m 0700 "$DATA_DIR/traefik/acme"
    touch "$DATA_DIR/traefik/acme/acme.json"; chmod 0600 "$DATA_DIR/traefik/acme/acme.json"
    cat >"$DATA_DIR/traefik/traefik.yml" <<EOF
# Managed by Veltnex (BYOS)
api:
  dashboard: false
global:
  sendAnonymousUsage: false
entryPoints:
  web:
    address: ":80"
    http:
      redirections:
        entryPoint: { to: websecure, scheme: https }
  websecure:
    address: ":443"
providers:
  docker:
    exposedByDefault: false
    network: ${NETWORK}
  file:
    directory: /etc/traefik/dynamic
    watch: true
certificatesResolvers:
  letsencrypt:
    acme:
      email: ${ACME_EMAIL}
      storage: /acme/acme.json
      httpChallenge: { entryPoint: web }
log:
  level: WARN
EOF
    chmod 0644 "$DATA_DIR/traefik/traefik.yml"

    docker rm -f veltnex-traefik >/dev/null 2>&1 || true
    docker run -d --name veltnex-traefik --restart unless-stopped \
        --network "$NETWORK" -p 80:80 -p 443:443 \
        --label managed-by=veltnex \
        --read-only --security-opt no-new-privileges:true \
        -v /var/run/docker.sock:/var/run/docker.sock:ro \
        -v "$DATA_DIR/traefik/traefik.yml:/etc/traefik/traefik.yml:ro" \
        -v "$DATA_DIR/traefik/dynamic:/etc/traefik/dynamic:ro" \
        -v "$DATA_DIR/traefik/acme:/acme" \
        "$TRAEFIK_IMAGE" >>"$LOG_FILE"
    sleep 2
    [[ "$(docker inspect -f '{{.State.Running}}' veltnex-traefik 2>/dev/null)" == "true" ]] || die "Traefik did not start."
    ok "Traefik on 80/443"
}

start_netdata() {
    step monitoring "Starting Netdata"
    pinned "$NETDATA_IMAGE"
    # Sized to the server so monitoring never competes with Odoo.
    local ram mem
    ram=$(ram_mb)
    if   (( ram < 6000 ));  then mem=256m
    elif (( ram < 12000 )); then mem=512m
    else mem=768m; fi

    install -d -m 0755 "$DATA_DIR/netdata"
    cat >"$DATA_DIR/netdata/netdata.conf" <<'EOF'
# Managed by Veltnex (BYOS). Local only: Veltnex reads it over SSH.
[global]
    run as user = netdata
[db]
    mode = dbengine
    update every = 2
[web]
    bind to = 127.0.0.1
[ml]
    enabled = no
EOF
    # Odoo's PostgreSQL containers have no 'netdata' role; probing them only fills logs.
    printf 'modules:\n  postgres: no\n' >"$DATA_DIR/netdata/go.d.conf"
    chmod 0644 "$DATA_DIR/netdata/"*.conf

    docker rm -f veltnex-netdata >/dev/null 2>&1 || true
    # Privileged host visibility is accepted on dedicated BYOS servers (owner decision).
    docker run -d --name veltnex-netdata --restart unless-stopped \
        --label managed-by=veltnex \
        --hostname "$(hostname)" --pid host --network host --memory "$mem" \
        --cap-add SYS_PTRACE --cap-add SYS_ADMIN --security-opt apparmor=unconfined \
        -v veltnex-netdata-lib:/var/lib/netdata -v veltnex-netdata-cache:/var/cache/netdata \
        -v "$DATA_DIR/netdata/netdata.conf:/etc/netdata/netdata.conf:ro" \
        -v "$DATA_DIR/netdata/go.d.conf:/etc/netdata/go.d.conf:ro" \
        -v /etc/passwd:/host/etc/passwd:ro -v /etc/group:/host/etc/group:ro \
        -v /etc/os-release:/host/etc/os-release:ro \
        -v /proc:/host/proc:ro -v /sys:/host/sys:ro \
        -v /var/run/docker.sock:/var/run/docker.sock:ro \
        -e DO_NOT_TRACK=1 \
        "$NETDATA_IMAGE" >>"$LOG_FILE"
    for _ in $(seq 1 30); do curl -fsS -o /dev/null --max-time 2 http://127.0.0.1:19999/api/v1/info && break; sleep 1; done
    curl -fsS -o /dev/null --max-time 2 http://127.0.0.1:19999/api/v1/info || die "Netdata did not start."
    ok "Netdata (memory limit $mem, listening on 127.0.0.1 only)"
}

# ----------------------------------------------------------------- finish --
verify_and_complete() {
    step verify "Verifying"
    local fails=0
    check() {  # check LABEL COMMAND...
        local label="$1"; shift
        if "$@" >/dev/null 2>&1; then ok "$label"; else fail "$label"; fails=$((fails+1)); fi
    }
    traefik_running() { [[ "$(docker inspect -f '{{.State.Running}}' veltnex-traefik 2>/dev/null)" == "true" ]]; }
    ufw_active()      { ufw status | grep -q '^Status: active'; }
    check "Docker"       docker info
    check "Traefik"      traefik_running
    check "Netdata"      curl -fsS -o /dev/null --max-time 3 http://127.0.0.1:19999/api/v1/info
    check "UFW"          ufw_active
    check "fail2ban"     systemctl is-active --quiet fail2ban
    check "platform key" grep -q " ${KEY_MARK}:${SERVER_ID}\$" /root/.ssh/authorized_keys
    (( fails == 0 )) || die "$fails component(s) are not healthy."

    api POST "/byos/v1/servers/$SERVER_ID/complete" "$(json \
        docker "$(docker --version | awk '{print $3}' | tr -d ,)" \
        compose "$(docker compose version --short 2>/dev/null || true)" \
        traefik_image "$TRAEFIK_IMAGE" netdata_image "$NETDATA_IMAGE" \
        ssh_ports "$(ssh_ports)" script_version "$SCRIPT_VERSION")" >/dev/null \
        || warn "Could not notify Veltnex; the dashboard will pick the server up on its next check."
    STEP="done"
    info ""
    info "${C_GRN}This server is connected to Veltnex.${C_OFF}"
    info "  Server:  $SERVER_ID"
    info "  Log:     $LOG_FILE"
    info "Return to your Veltnex dashboard to create a project on it."
}

detach() {
    [[ $EUID -eq 0 ]] || die "Run as root (use sudo)."
    load_state || die "This server is not connected to Veltnex."
    info "Detaching server $SERVER_ID from Veltnex. Your Odoo environments and data stay."
    report running "Detaching"
    local tmp
    tmp=$(mktemp)
    grep -v " ${KEY_MARK}:" /root/.ssh/authorized_keys >"$tmp" || true
    cat "$tmp" >/root/.ssh/authorized_keys; rm -f "$tmp"
    ok "Veltnex SSH key removed"
    if docker rm -f veltnex-netdata >/dev/null 2>&1; then ok "Netdata removed"; fi
    rm -f /etc/fail2ban/jail.d/veltnex.local
    systemctl restart fail2ban 2>/dev/null || true
    api POST "/byos/v1/servers/$SERVER_ID/detached" "$(json reason customer)" >/dev/null 2>&1 || true
    rm -f "$STATE_FILE"
    info "Traefik and your Odoo containers keep running; manage them yourself from now on."
}

usage() {
    cat <<EOF
Veltnex BYOS onboarding ($SCRIPT_VERSION)

  sudo VELTNEX_TOKEN=<token> bash install.sh [--yes]   connect this server
  sudo bash install.sh --check                          checks only, change nothing
  sudo bash install.sh --detach                         stop Veltnex management

Requirements: a fresh, empty server dedicated to Veltnex; $SUPPORTED_OS;
at least $MIN_CPU CPU, 4 GB RAM, $MIN_DISK_GB GB free disk; ports 80/443 reachable.
EOF
}

main() {
    while (( $# )); do
        case "$1" in
            --yes|-y) ASSUME_YES=true ;;
            --check)  MODE=check ;;
            --detach) MODE=detach ;;
            -h|--help) usage; exit 0 ;;
            *) usage; die "Unknown option: $1" ;;
        esac
        shift
    done

    case "$MODE" in
        detach) detach; exit 0 ;;
    esac

    preflight
    [[ "$MODE" == check ]] && { info "Check only: nothing was changed."; exit 0; }
    confirm_changes

    touch "$LOG_FILE"; chmod 0600 "$LOG_FILE"
    exec > >(tee -a "$LOG_FILE") 2>&1
    info "=== Veltnex BYOS setup $SCRIPT_VERSION — $(date -u '+%F %T UTC') ==="
    trap 'on_error $LINENO' ERR

    register
    prepare_system
    configure_firewall
    install_platform_key
    install_docker
    start_traefik
    start_netdata
    verify_and_complete
}

# Allow tests to source the functions without running the installer.
if [[ "${BASH_SOURCE[0]}" == "$0" || -z "${BASH_SOURCE[0]:-}" ]]; then
    main "$@"
fi
