#!/usr/bin/env bash
# AdaOS Sandbox Launcher
# Threat model: prevent shell escape and embedding/memory data exfiltration.
# Audio/camera access is fine — the concern is shell + data.
#
# What this blocks:
#   - No shell binaries (bash, sh, zsh) inside the sandbox
#   - No common exfil tools (curl, wget, nc, ssh, scp, rsync)
#   - No access to home directory (SSH keys, tokens, git config, browser data)
#   - No access to Docker socket (prevents container escape)
#   - Isolated PID namespace (can't see/signal host processes)
#
# What this allows:
#   - Python runtime + venv (Ada itself)
#   - Audio devices (PipeWire/ALSA) — voice mode
#   - GPU passthrough (NVIDIA) — local inference
#   - Read-write to project dir (sentinel reports, etc.)
#   - Localhost network (Ollama:11434, Postgres:5432)
#
# Usage:
#   ./sandbox.sh              → voice mode
#   ./sandbox.sh --text       → text mode
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="/home/neosynaptics/venvs/voice"
XDG_RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
MODE="${1:---voice}"

# ── Load .env so we can pass secrets into the sandbox ──
ENV_ARGS=()
if [ -f "$SCRIPT_DIR/.env" ]; then
    while IFS='=' read -r key value; do
        [[ -z "$key" || "$key" == \#* ]] && continue
        ENV_ARGS+=(--setenv "$key" "$value")
    done < "$SCRIPT_DIR/.env"
fi

# ── Build sandbox command ──
SANDBOX=(
    bwrap
    --die-with-parent

    # Isolated PID + IPC namespaces (can't see/kill host processes)
    --unshare-pid
    --unshare-ipc

    # Read-only system (Python stdlib, shared libs)
    --ro-bind /usr/lib /usr/lib
    --ro-bind /usr/share/zoneinfo /usr/share/zoneinfo
    --ro-bind /lib /lib
)

# /usr/lib64 and /lib64 may not exist on all systems
[ -d /usr/lib64 ] && SANDBOX+=(--ro-bind /usr/lib64 /usr/lib64)
[ -d /lib64 ] && SANDBOX+=(--ro-bind /lib64 /lib64)

SANDBOX+=(
    # Minimal /usr/bin — tmpfs wipes all binaries, we whitelist below
    --tmpfs /usr/bin
    --tmpfs /usr/sbin
    --tmpfs /usr/local/bin

    # System config (minimal, read-only)
    --ro-bind /etc/resolv.conf /etc/resolv.conf
    --ro-bind /etc/ssl /etc/ssl
    --ro-bind /etc/ca-certificates /etc/ca-certificates
    --ro-bind /etc/ld.so.cache /etc/ld.so.cache
    --ro-bind /etc/passwd /etc/passwd
    --ro-bind /etc/nsswitch.conf /etc/nsswitch.conf

    # Minimal /dev + proc
    --dev /dev
    --proc /proc

    # Writable /tmp (isolated from host /tmp)
    --tmpfs /tmp

    # ── Block entire home directory first ──
    --tmpfs /home/neosynaptics

    # ── Then mount project dir read-write inside the cleared home ──
    --bind "$SCRIPT_DIR" "$SCRIPT_DIR"

    # ── Venv: read-only ──
    --ro-bind "$VENV" "$VENV"

    # ── Environment ──
    --setenv HOME "$SCRIPT_DIR"
    --setenv PATH "$VENV/bin"
    --setenv PYTHONPATH "$SCRIPT_DIR"
    --setenv XDG_RUNTIME_DIR "$XDG_RUNTIME"
    --setenv PYTHONDONTWRITEBYTECODE 1
)

# ── Whitelist only safe /usr/bin tools ──
# Everything else (bash, sh, curl, wget, ssh, git, docker, etc.) is gone.
SAFE_BINS=(
    env cat head tail tee wc sort uniq tr cut
    ls stat mkdir rm cp mv ln
    id whoami
    date sleep true false
    arecord aplay
)
for bin in "${SAFE_BINS[@]}"; do
    [ -f "/usr/bin/$bin" ] && SANDBOX+=(--ro-bind "/usr/bin/$bin" "/usr/bin/$bin")
done

# ── Audio: always allowed (user's threat model is shell, not audio) ──
[ -e "$XDG_RUNTIME/pipewire-0" ] && SANDBOX+=(--ro-bind "$XDG_RUNTIME/pipewire-0" "$XDG_RUNTIME/pipewire-0")
[ -d "$XDG_RUNTIME/pulse" ] && SANDBOX+=(--ro-bind "$XDG_RUNTIME/pulse" "$XDG_RUNTIME/pulse")
[ -d /dev/snd ] && SANDBOX+=(--dev-bind /dev/snd /dev/snd)

# ── GPU passthrough (local inference only) ──
[ -d /dev/dri ] && SANDBOX+=(--dev-bind /dev/dri /dev/dri)
if [ -e /dev/nvidia0 ]; then
    SANDBOX+=(
        --dev-bind /dev/nvidia0 /dev/nvidia0
        --dev-bind /dev/nvidiactl /dev/nvidiactl
    )
    [ -e /dev/nvidia-uvm ] && SANDBOX+=(--dev-bind /dev/nvidia-uvm /dev/nvidia-uvm)
    [ -e /dev/nvidia-uvm-tools ] && SANDBOX+=(--dev-bind /dev/nvidia-uvm-tools /dev/nvidia-uvm-tools)
    for nv_path in /usr/lib/x86_64-linux-gnu/libnvidia* /usr/lib/x86_64-linux-gnu/libcuda*; do
        [ -e "$nv_path" ] && SANDBOX+=(--ro-bind "$nv_path" "$nv_path")
    done
fi

# ── Pass .env variables into sandbox ──
SANDBOX+=("${ENV_ARGS[@]}")

# ── Preflight (runs OUTSIDE sandbox, on host) ──
echo "=== AdaOS Sandboxed Launch ==="

if docker ps --format '{{.Names}}' 2>/dev/null | grep -q postgres; then
    echo "[OK] PostgreSQL running"
else
    echo "[>>] Starting PostgreSQL..."
    cd "$SCRIPT_DIR" && docker compose up -d postgres
    sleep 3
fi

if curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "[OK] Ollama running"
else
    echo "[!!] Ollama not running — start with: ollama serve"
    exit 1
fi

echo ""
echo "[sandbox] Shell binaries:  BLOCKED (no bash/sh/zsh inside)"
echo "[sandbox] Exfil tools:     BLOCKED (no curl/wget/nc/ssh/git)"
echo "[sandbox] Docker socket:   BLOCKED"
echo "[sandbox] Home directory:  BLOCKED (only project dir accessible)"
echo "[sandbox] Audio:           allowed"
echo "[sandbox] GPU:             allowed"
echo "==========================="
echo ""

# ── Launch inside sandbox ──
cd "$SCRIPT_DIR"
if [ "$MODE" = "--text" ]; then
    echo "Starting Ada (text mode, sandboxed)..."
    exec "${SANDBOX[@]}" "$VENV/bin/python" -m ada.main --text
else
    echo "Starting Ada (voice mode, sandboxed)..."
    exec "${SANDBOX[@]}" "$VENV/bin/python" -m ada.main
fi
