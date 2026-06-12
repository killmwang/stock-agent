#!/bin/bash
# Stock Agent Tencent Cloud CVM update script

set -e

APP_DIR="${APP_DIR:-/home/ubuntu/stock-agent}"
BACKEND_SERVICE="${BACKEND_SERVICE:-stock-agent-backend.service}"

echo "=========================================="
echo "  Stock Agent deploy/update"
echo "=========================================="

cd "$APP_DIR"

echo "[1/4] Pull latest source"
git pull origin main

echo "[2/4] Install/update backend dependencies"
if [ -d ".venv" ]; then
  source .venv/bin/activate
else
  python3 -m venv .venv
  source .venv/bin/activate
fi
pip install -r requirements.txt
pip install -r web-app/backend/requirements.txt

echo "[3/4] Build frontend"
cd "$APP_DIR/web-app/frontend"
npm install
npm run build

echo "[4/4] Restart backend"
sudo systemctl restart "$BACKEND_SERVICE"
sudo systemctl status "$BACKEND_SERVICE" --no-pager

echo "Done."
