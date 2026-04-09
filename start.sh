#!/usr/bin/env bash
# AdaOS startup script
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="/home/neosynaptics/venvs/voice"
PYTHON="$VENV/bin/python"

cd "$SCRIPT_DIR"

# Load environment
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Check prerequisites
echo "=== AdaOS Preflight ==="

# 1. PostgreSQL
if sudo docker ps --format '{{.Names}}' | grep -q postgres; then
    echo "[OK] PostgreSQL running"
else
    echo "[>>] Starting PostgreSQL..."
    sudo docker compose up -d
    sleep 3
    echo "[OK] PostgreSQL started"
fi

# 2. Ollama
if curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "[OK] Ollama running"
else
    echo "[!!] Ollama not running — start with: ollama serve"
    exit 1
fi

# 3. Model check
MODEL=$(grep 'control_model' ada/config.py | head -1 | grep -oP '"[^"]*"' | tr -d '"')
if ollama list | grep -q "$MODEL"; then
    echo "[OK] Model $MODEL available"
else
    echo "[!!] Model $MODEL not found in Ollama"
    echo "     Pull it with: ollama pull $MODEL"
    exit 1
fi

# 4. Audio
if arecord -l 2>/dev/null | grep -q "card"; then
    echo "[OK] Audio capture device found"
else
    echo "[!!] No audio capture device detected"
fi

# 5. Goose (MCP tool engine)
GOOSED="$SCRIPT_DIR/../goose/target/release/goosed"
if [ -x "$GOOSED" ]; then
    if curl -sf -H "X-Secret-Key: ada_goose_bridge" http://localhost:3199/health > /dev/null 2>&1; then
        echo "[OK] Goose running"
    else
        echo "[>>] Starting Goose..."
        GOOSE_HOST=127.0.0.1 \
        GOOSE_PORT=3199 \
        GOOSE_TLS=false \
        GOOSE_SERVER__SECRET_KEY=ada_goose_bridge \
        OLLAMA_HOST=http://localhost:11434 \
        "$GOOSED" agent &
        sleep 2
        echo "[OK] Goose started"
    fi
else
    echo "[--] Goose not built (optional — MCP tools unavailable)"
fi

echo "======================="
echo ""

# Run Ada
MODE="${1:---voice}"
if [ "$MODE" = "--text" ]; then
    echo "Starting Ada (text mode)..."
    exec "$PYTHON" -m ada.main --text
elif [ "$MODE" = "--ui" ]; then
    echo "Starting Ada with UI server on http://localhost:8765 ..."
    exec "$PYTHON" -m ui.server --with-ada
elif [ "$MODE" = "--ui-only" ]; then
    echo "Starting UI server only (echo mode) on http://localhost:8765 ..."
    exec "$PYTHON" -m ui.server
else
    echo "Starting Ada (voice mode)..."
    exec "$PYTHON" -m ada.main
fi
