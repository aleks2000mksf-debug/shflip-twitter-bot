#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/twitter-telegram-bot}"
SERVICE_NAME="twitter-bot"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/install.sh"
  exit 1
fi

apt-get update -qq
apt-get install -y -qq python3 python3-venv git

cd "${APP_DIR}"
python3 -m venv venv
./venv/bin/pip install -U pip
./venv/bin/pip install -r requirements.txt

if [[ ! -f "${APP_DIR}/config.json" ]]; then
  cp "${APP_DIR}/config.example.json" "${APP_DIR}/config.json"
  echo "Edit ${APP_DIR}/config.json before starting the service."
fi

install -m 644 "${APP_DIR}/deploy/${SERVICE_NAME}.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

if grep -q "your_bot_token_here" "${APP_DIR}/config.json" 2>/dev/null; then
  echo "Warning: bot_token is not configured in config.json"
else
  systemctl restart "${SERVICE_NAME}"
  systemctl --no-pager status "${SERVICE_NAME}"
fi

echo "Logs: journalctl -u ${SERVICE_NAME} -f"
