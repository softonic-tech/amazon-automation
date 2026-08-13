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
#   BASIC_AUTH_USER="client" \
#   BASIC_AUTH_PASS="a_strong_password" \
#   REPO_URL="https://github.com/softonic-tech/amazon-automation.git" \
#   bash setup_vps.sh
#

set -euo pipefail

# ---- configurable via env ---------------------------------------------------
: "${KEEPA_API_KEY:?Set KEEPA_API_KEY before running this script}"
: "${BASIC_AUTH_USER:?Set BASIC_AUTH_USER before running this script}"
: "${BASIC_AUTH_PASS:?Set BASIC_AUTH_PASS before running this script}"
REPO_URL="${REPO_URL:-https://github.com/softonic-tech/amazon-automation.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"

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
    curl git \
    nginx apache2-utils \
    ufw ca-certificates

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
install -o "$APP_USER" -g "$APP_USER" -m 0600 /dev/null "$APP_DIR/.env"
cat > "$APP_DIR/.env" <<ENV_EOF
KEEPA_API_KEY=$KEEPA_API_KEY
PORT=5000
ENV_EOF
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

echo "==> [6/7] Installing systemd service + nginx site"
install -m 0644 "$APP_DIR/deploy/amazon-sourcing.service" /etc/systemd/system/amazon-sourcing.service
install -m 0644 "$APP_DIR/deploy/nginx-amazon-sourcing.conf" /etc/nginx/sites-available/amazon-sourcing

# Enable the nginx site + drop the default one so port 80 is ours.
ln -sf /etc/nginx/sites-available/amazon-sourcing /etc/nginx/sites-enabled/amazon-sourcing
rm -f /etc/nginx/sites-enabled/default

# Basic-auth credentials.
htpasswd -bc /etc/nginx/.htpasswd "$BASIC_AUTH_USER" "$BASIC_AUTH_PASS"
chmod 640 /etc/nginx/.htpasswd
chown root:www-data /etc/nginx/.htpasswd

nginx -t

systemctl daemon-reload
systemctl enable --now amazon-sourcing
systemctl restart amazon-sourcing

# `enable --now` starts nginx if it isn't already running, and `reload` picks
# up the new site config if it is. This handles both fresh installs and re-runs.
systemctl enable --now nginx
systemctl reload nginx

echo "==> [7/7] Configuring UFW firewall (SSH + HTTP)"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 'Nginx HTTP'
ufw --force enable

IP=$(curl -fsS -4 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')

cat <<DONE

============================================================
  Amazon Sourcing Tool is deployed.
  URL:       http://$IP/
  Username:  $BASIC_AUTH_USER
  Password:  (the one you passed via BASIC_AUTH_PASS)

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
