#!/usr/bin/env bash
# start.sh — Launchpad all-in-one startup script (Must-Have #10)
# Starts local Docker registry (:5000), webhook receiver (:9900), and Launchpad control plane (:7000).

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT_DIR="$DIR/devops-startup-kit/launchpad-starter-kit/launchpad-starter-kit"
LAUNCHPAD_DIR="$DIR/launchpad"

echo "=========================================================="
echo "  🚀 Starting Launchpad Control Plane Environment"
echo "=========================================================="

# 1. Check Docker
if ! docker info >/dev/null 2>&1; then
    echo "❌ Error: Docker daemon is not running or accessible."
    echo "   Please start Docker Desktop / daemon and try again."
    exit 1
fi
echo "✅ Docker daemon is running."

# 2. Boot Registry (:5000) and Webhook Receiver (:9900)
if [ -d "$KIT_DIR" ]; then
    echo "📦 Booting local registry (:5000) & webhook receiver (:9900)..."
    (cd "$KIT_DIR" && docker compose up -d)
else
    echo "⚠️  Warning: Starter kit directory not found at $KIT_DIR"
fi

# 3. Ensure python3 & flask are available
if ! python3 -c "import flask" >/dev/null 2>&1; then
    echo "🔧 Installing Flask..."
    pip3 install --break-system-packages flask >/dev/null 2>&1 || pip install flask
fi

# 4. Start Launchpad Control Plane
echo "=========================================================="
echo "  🛰️  Launchpad Control Plane is ready!"
echo "=========================================================="
echo "  Cockpit UI:        http://localhost:7000"
echo "  Docker Registry:   http://localhost:5000"
echo "  Webhook Receiver:  http://localhost:9900"
echo ""
echo "  Demo Credentials:"
echo "    Manager:  admin  / 1234 -> /admin"
echo "    Team 1:   team1  / 1234 -> /dashboard"
echo "    Team 2:   team2  / 1234 -> /dashboard"
echo "=========================================================="

cd "$LAUNCHPAD_DIR"
exec python3 app.py
