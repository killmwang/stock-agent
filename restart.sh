#!/bin/bash
# Local helper for restarting Stock Agent services.
# Usage: ./restart.sh [backend|frontend|all|stop|status|clean]

set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$PROJECT_DIR/web-app/backend"
FRONTEND_DIR="$PROJECT_DIR/web-app/frontend"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_DIR/.venv/bin/python}"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

clean_cache() {
  info "Cleaning Python cache..."
  find "$PROJECT_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
  find "$PROJECT_DIR" -name "*.pyc" -delete 2>/dev/null || true
}

stop_backend() {
  info "Stopping backend..."
  pkill -f "uvicorn.*app.main" 2>/dev/null || true
  sleep 1
  if command -v lsof >/dev/null 2>&1 && lsof -i :8000 >/dev/null 2>&1; then
    warn "Port 8000 is still busy; releasing it."
    kill -9 $(lsof -t -i:8000) 2>/dev/null || true
  fi
}

start_backend() {
  info "Starting backend..."
  cd "$BACKEND_DIR"
  mkdir -p "$PROJECT_DIR/logs"
  PYTHONPATH="$PROJECT_DIR" nohup "$PYTHON_BIN" -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --reload \
    --reload-dir "$PROJECT_DIR/stock_agent" \
    --reload-dir "$BACKEND_DIR/app" \
    > "$PROJECT_DIR/logs/backend.log" 2>&1 &
  info "Backend started: $!"
}

stop_frontend() {
  info "Stopping frontend..."
  pkill -f "vite" 2>/dev/null || true
  pkill -f "npm.*dev" 2>/dev/null || true
  sleep 1
  if command -v lsof >/dev/null 2>&1 && lsof -i :5173 >/dev/null 2>&1; then
    kill -9 $(lsof -t -i:5173) 2>/dev/null || true
  fi
}

start_frontend() {
  info "Starting frontend..."
  cd "$FRONTEND_DIR"
  mkdir -p "$PROJECT_DIR/logs"
  nohup npm run dev > "$PROJECT_DIR/logs/frontend.log" 2>&1 &
  info "Frontend started: $!"
}

show_status() {
  echo "Backend:  $(pgrep -f "uvicorn.*app.main" >/dev/null && echo running || echo stopped)"
  echo "Frontend: $(pgrep -f "vite" >/dev/null && echo running || echo stopped)"
}

case "${1:-all}" in
  backend|b)
    stop_backend
    clean_cache
    start_backend
    ;;
  frontend|f)
    stop_frontend
    start_frontend
    ;;
  all|a)
    stop_backend
    stop_frontend
    clean_cache
    start_backend
    start_frontend
    ;;
  stop)
    stop_backend
    stop_frontend
    ;;
  status|s)
    show_status
    exit 0
    ;;
  clean|c)
    clean_cache
    exit 0
    ;;
  *)
    error "Usage: $0 [backend|frontend|all|stop|status|clean]"
    exit 1
    ;;
esac

sleep 1
show_status
