#!/usr/bin/env bash
#
# One-shot Hostinger / Ubuntu 24.04 VPS setup for the Amazon Sourcing Tool.
#
# What this script does (idempotent — safe to re-run):
#   1. Installs system packages (python venv, curl, nginx, apache2-utils, git, ufw).
#   2. Creates a locked-down `sourcing` service user.
#   3. Clones (or updates) the repo into /opt/amazon-sourcing/app.
#   4. Builds the Python venv and installs requirements.
#   5. Writes the .env from the KEEPA_API_KEY and BASIC_AUTH_USER/PASS env vars.
#   6. Installs the systemd service + nginx site config.
#   7. Configures ufw firewall (SSH + HTTP) and starts everything.
#
# Usage (run as root on the VPS):
#   KEEPA_API_KEY="your_key_here" \
#   APP_USERNAME="zeshan" \
#   APP_PASSWORD="a_strong_password" \
#   REPO_URL="https://github.com/softonic-tech/amazon-automation.git" \
#   bash setup_vps.sh
#

set -euo pipefail

# ---- configurable via env ---------------------------------------------------
: "${KEEPA_API_KEY:?Set KEEPA_API_KEY before running this script}"
# APP_USERNAME/APP_PASSWORD used to be BASIC_AUTH_USER/BASIC_AUTH_PASS. Accept
# either name so old command-lines keep working.
APP_USERNAME="${APP_USERNAME:-${BASIC_AUTH_USER:-}}"
APP_PASSWORD="${APP_PASSWORD:-${BASIC_AUTH_PASS:-}}"
: "${APP_USERNAME:?Set APP_USERNAME (login username) before running this script}"
: "${APP_PASSWORD:?Set APP_PASSWORD (login password) before running this script}"
REPO_URL="${REPO_URL:-https://github.com/softonic-tech/amazon-automation.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"

# Public port nginx listens on. Set LISTEN_PORT=8080 if port 80 is already
# taken by another service (e.g. Docker control panel on some VPS templates).
LISTEN_PORT="${LISTEN_PORT:-80}"

APP_USER="sourcing"
APP_ROOT="/opt/amazon-sourcing"
APP_DIR="$APP_ROOT/app"
VENV_DIR="$APP_DIR/venv"

if [[ $EUID -ne 0 ]]; then
    echo "Run this script as root (sudo -i, then bash setup_vps.sh)." >&2
    exit 1
fi

echo "==> [1/7] Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y \
    python3 python3-venv python3-pip \
    curl git tar \
    nginx \
    ufw ca-certificates

# curl-impersonate: statically-linked curl that mimics Chrome/Firefox TLS
# fingerprint. Needed to beat Cloudflare Bot Fight Mode from datacenter IPs
# (Hostinger, DigitalOcean, etc.). Without it, sites like iHerb 403 every
# request from the VPS even though the same code works from a residential IP.
CURL_IMP_VERSION="${CURL_IMP_VERSION:-v1.5.6}"
if ! command -v curl-impersonate &>/dev/null; then
    echo "    installing curl-impersonate $CURL_IMP_VERSION"
    ARCH=$(uname -m)
    case "$ARCH" in
        x86_64)  TARBALL="curl-impersonate-$CURL_IMP_VERSION.x86_64-linux-gnu.tar.gz" ;;
        aarch64) TARBALL="curl-impersonate-$CURL_IMP_VERSION.aarch64-linux-gnu.tar.gz" ;;
        *) echo "    unknown arch $ARCH, skipping curl-impersonate"; TARBALL="" ;;
    esac
    if [[ -n "$TARBALL" ]]; then
        TMPDIR=$(mktemp -d)
        curl -fsSL -o "$TMPDIR/curl-imp.tar.gz" \
            "https://github.com/lexiforest/curl-impersonate/releases/download/$CURL_IMP_VERSION/$TARBALL"
        tar -xzf "$TMPDIR/curl-imp.tar.gz" -C "$TMPDIR"
        # v1.x layout: single statically-linked `curl-impersonate` binary
        # plus per-browser wrapper scripts. Install everything to /usr/local/bin.
        find "$TMPDIR" -maxdepth 2 -type f \( -name 'curl-impersonate' -o -name 'curl_chrome*' -o -name 'curl_ff*' -o -name 'curl_safari*' -o -name 'curl_edge*' \) \
            -exec install -m 0755 {} /usr/local/bin/ \;
        rm -rf "$TMPDIR"
        if command -v curl-impersonate &>/dev/null; then
            echo "    curl-impersonate installed: $(curl-impersonate --version | head -1)"
        else
            echo "    WARNING: curl-impersonate install didn't drop a binary — check tarball layout"
        fi
    fi
else
    echo "    curl-impersonate already present: $(curl-impersonate --version | head -1)"
fi

echo "==> [2/7] Creating service user '$APP_USER'"
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --create-home --shell /usr/sbin/nologin --home-dir "$APP_ROOT" "$APP_USER"
else
    echo "    (user already exists)"
fi
mkdir -p "$APP_ROOT"
chown "$APP_USER:$APP_USER" "$APP_ROOT"

echo "==> [3/7] Cloning / updating the repo at $APP_DIR"
if [[ -d "$APP_DIR/.git" ]]; then
    sudo -u "$APP_USER" git -C "$APP_DIR" fetch --all --prune
    sudo -u "$APP_USER" git -C "$APP_DIR" reset --hard "origin/$REPO_BRANCH"
else
    sudo -u "$APP_USER" git clone --branch "$REPO_BRANCH" "$REPO_URL" "$APP_DIR"
fi

echo "==> [4/7] Building Python venv + installing requirements"
if [[ ! -d "$VENV_DIR" ]]; then
    sudo -u "$APP_USER" python3 -m venv "$VENV_DIR"
fi
sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install --upgrade pip
sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> [5/7] Writing $APP_DIR/.env"
# Preserve SECRET_KEY across re-runs so existing sessions survive redeploys.
EXISTING_SECRET=""
if [[ -f "$APP_DIR/.env" ]]; then
    EXISTING_SECRET=$(grep -E '^SECRET_KEY=' "$APP_DIR/.env" | cut -d= -f2- || true)
fi
SECRET_KEY="${SECRET_KEY:-${EXISTING_SECRET:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}}"

install -o "$APP_USER" -g "$APP_USER" -m 0600 /dev/null "$APP_DIR/.env"
cat > "$APP_DIR/.env" <<ENV_EOF
KEEPA_API_KEY=$KEEPA_API_KEY
APP_USERNAME=$APP_USERNAME
APP_PASSWORD=$APP_PASSWORD
SECRET_KEY=$SECRET_KEY
PORT=5000
ENV_EOF
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

echo "==> [6/7] Installing systemd service + nginx site (listen on port $LISTEN_PORT)"
install -m 0644 "$APP_DIR/deploy/amazon-sourcing.service" /etc/systemd/system/amazon-sourcing.service
install -m 0644 "$APP_DIR/deploy/nginx-amazon-sourcing.conf" /etc/nginx/sites-available/amazon-sourcing

# Rewrite the listen port if the caller asked for something other than 80.
if [[ "$LISTEN_PORT" != "80" ]]; then
    sed -i "s/listen 80 default_server;/listen $LISTEN_PORT default_server;/" \
        /etc/nginx/sites-available/amazon-sourcing
    sed -i "s/listen \[::\]:80 default_server;/listen [::]:$LISTEN_PORT default_server;/" \
        /etc/nginx/sites-available/amazon-sourcing
fi

# Enable the nginx site + drop the default one so port 80 is ours.
ln -sf /etc/nginx/sites-available/amazon-sourcing /etc/nginx/sites-enabled/amazon-sourcing
rm -f /etc/nginx/sites-enabled/default

# Remove any old htpasswd file from previous versions of this script — auth
# now lives in the Flask app, not in nginx.
rm -f /etc/nginx/.htpasswd

nginx -t

systemctl daemon-reload
systemctl enable --now amazon-sourcing
systemctl restart amazon-sourcing

# `enable --now` starts nginx if it isn't already running, and `reload` picks
# up the new site config if it is. This handles both fresh installs and re-runs.
systemctl enable --now nginx
systemctl reload nginx

echo "==> [7/7] Configuring UFW firewall (SSH + port $LISTEN_PORT)"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
if [[ "$LISTEN_PORT" == "80" ]]; then
    ufw allow 'Nginx HTTP'
else
    ufw allow "$LISTEN_PORT/tcp" comment 'Amazon Sourcing UI'
fi
ufw --force enable

IP=$(curl -fsS -4 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
if [[ "$LISTEN_PORT" == "80" ]]; then
    URL="http://$IP/"
else
    URL="http://$IP:$LISTEN_PORT/"
fi

cat <<DONE

============================================================
  Amazon Sourcing Tool is deployed.
  URL:       $URL
  Username:  $APP_USERNAME
  Password:  (the one you passed via APP_PASSWORD)

  Handy commands:
    systemctl status amazon-sourcing
    journalctl -u amazon-sourcing -f       # live app logs
    nginx -t && systemctl reload nginx     # after editing nginx
    systemctl restart amazon-sourcing      # after code changes

  Update the app later:
    sudo -u sourcing git -C $APP_DIR pull
    sudo -u sourcing $VENV_DIR/bin/pip install -r $APP_DIR/requirements.txt
    sudo systemctl restart amazon-sourcing
============================================================
DONE
