#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Frigate Email Alert — First-Time Setup
# Run once from the HA terminal add-on or SSH:
#   bash /config/scripts/setup_frigate_email.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="/config"
PKG_DST="$CONFIG_DIR/packages/frigate_email.yaml"
SCRIPT_DST="$CONFIG_DIR/scripts/frigate_email.py"
SETUP_DST="$CONFIG_DIR/scripts/setup_frigate_email.sh"
LOG_FILE="$CONFIG_DIR/frigate_email.log"

echo "═══════════════════════════════════════════════════════════"
echo "  Frigate Email Alert — Setup"
echo "═══════════════════════════════════════════════════════════"

# ── Verify we're inside HA --------------------------------------------------
if [[ ! -f "$CONFIG_DIR/configuration.yaml" ]]; then
    echo "ERROR: $CONFIG_DIR/configuration.yaml not found."
    echo "       Run this script from within the HA container (SSH add-on or terminal)."
    exit 1
fi

# ── Create directories -------------------------------------------------------
echo ""
echo "▶ Creating directories ..."
mkdir -p "$CONFIG_DIR/packages"
mkdir -p "$CONFIG_DIR/scripts"
mkdir -p "$CONFIG_DIR/www"

# ── Detect source files ------------------------------------------------------
# The script expects files already at /config/scripts/ (copied via SCP or
# the Advanced SSH add-on from the repo). If they are not there yet, print
# instructions and exit.
REPO_PKG="$SCRIPT_DIR/../packages/frigate_email.yaml"
REPO_PY="$SCRIPT_DIR/frigate_email.py"

if [[ -f "$REPO_PKG" && -f "$REPO_PY" ]]; then
    echo "▶ Copying files from repo location ..."
    cp "$REPO_PKG" "$PKG_DST"
    cp "$REPO_PY" "$SCRIPT_DST"
    cp "${BASH_SOURCE[0]}" "$SETUP_DST"
    chmod +x "$SCRIPT_DST"
    echo "   ✓ $PKG_DST"
    echo "   ✓ $SCRIPT_DST"
elif [[ -f "$PKG_DST" && -f "$SCRIPT_DST" ]]; then
    echo "▶ Files already in place — skipping copy."
else
    echo ""
    echo "ERROR: Source files not found."
    echo "       Copy the following files to /config before running setup:"
    echo "         • frigate_email.yaml → /config/packages/frigate_email.yaml"
    echo "         • frigate_email.py   → /config/scripts/frigate_email.py"
    echo ""
    echo "       Example (from your workstation):"
    echo "         scp frigate_email.yaml hassio@bnu-homeassistant:/config/packages/"
    echo "         scp frigate_email.py   hassio@bnu-homeassistant:/config/scripts/"
    exit 1
fi

# ── Check packages: in configuration.yaml -----------------------------------
echo ""
echo "▶ Checking configuration.yaml for packages ..."
if grep -q "packages:" "$CONFIG_DIR/configuration.yaml"; then
    echo "   ✓ packages: already configured"
else
    echo ""
    echo "   ⚠ 'packages:' not found in configuration.yaml."
    echo "   Add the following block to enable package loading:"
    echo ""
    echo "     homeassistant:"
    echo "       packages: !include_dir_named packages"
    echo ""
    echo "   Then re-run this script or reload HA manually."
fi

# ── Touch log file -----------------------------------------------------------
touch "$LOG_FILE"
echo "▶ Log file: $LOG_FILE"

# ── Print required secrets ---------------------------------------------------
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Add the following to /config/secrets.yaml"
echo "═══════════════════════════════════════════════════════════"
cat <<'SECRETS'

# ── Frigate Email Alerts ──────────────────────────────────────────────────────
frigate_host: "10.1.1.160"                    # Frigate IP (hostname may not resolve inside HA containers)
frigate_port: "5000"
frigate_email_recipients: "you@example.com"   # comma-separated list of recipients
# SMTP: reuse boiler_smtp_user / boiler_smtp_pass already defined above

SECRETS

# ── Python dependencies check ------------------------------------------------
echo "▶ Checking Python dependencies ..."
python3 -c "import requests, yaml" 2>/dev/null \
    && echo "   ✓ requests and pyyaml are available" \
    || echo "   ⚠ Missing dependencies — run: pip3 install requests pyyaml"

# ── Done --------------------------------------------------------------------
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Next steps"
echo "═══════════════════════════════════════════════════════════"
echo "  1. Edit /config/secrets.yaml and add the block above"
echo "  2. Reload HA: Developer Tools → YAML → Reload All"
echo "  3. Test manually with a real review_id:"
echo "       python3 /config/scripts/frigate_email.py '<review_id>'"
echo "     Get a review_id from: http://bnu-frigate:5000/api/reviews?limit=5&has_clip=1"
echo "  4. Watch the log: tail -f /config/frigate_email.log"
echo ""
