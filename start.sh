#!/usr/bin/env bash
# AdaWindOS startup script (Linux/macOS/WSL)
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load environment
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

echo "=== AdaWindOS Preflight ==="

# 1. Python
if ! command -v python3 &>/dev/null; then
    echo "[!!] Python 3 not found"
    exit 1
fi
echo "[OK] Python found"

# 2. DeepSeek API key
if [ -z "$DEEPSEEK_API_KEY" ]; then
    echo "[!!] DEEPSEEK_API_KEY not set"
    echo "     Create a .env file with: DEEPSEEK_API_KEY=your_key_here"
    exit 1
fi
echo "[OK] DeepSeek API key configured"

# 3. Docker + PostgreSQL
if ! docker ps &>/dev/null; then
    echo "[!!] Docker not running — start Docker first"
    exit 1
fi

if docker ps --format '{{.Names}}' | grep -q postgres; then
    echo "[OK] PostgreSQL running"
else
    echo "[>>] Starting PostgreSQL..."
    docker compose up -d
    sleep 3
    echo "[OK] PostgreSQL started"
fi

echo "==========================="
echo ""

# Run Ada
MODE="${1:---text}"
if [ "$MODE" = "--ui" ]; then
    echo "Starting Ada with UI on http://localhost:8765 ..."
    exec python3 -m ada.main --ui
else
    echo "Starting Ada (text mode)..."
    exec python3 -m ada.main
fi
