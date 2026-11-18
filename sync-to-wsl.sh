#!/usr/bin/env bash
# sync-to-wsl.sh — copy the Windows-side project files into WSL2 home.
#
# Run from INSIDE WSL 2 (Ubuntu):
#   bash /mnt/d/MOHIT/job-plans/acko-mohit-devops/sync-to-wsl.sh
#
# What it does:
#   1. Copies everything from the Windows folder (/mnt/d/...) to ~/acko-mohit-devops
#   2. Skips junk (zips already extracted, temp files) — safe to run repeatedly (it only overwrites)
#   3. Verifies the starter kit is intact and Docker/WSL is ready

set -u

SRC="/mnt/d/MOHIT/job-plans/acko-mohit-devops"
DST="$HOME/acko-mohit-devops"

echo "== Acko Buildathon: Windows -> WSL2 sync =="
echo "From: $SRC"
echo "To:   $DST"
echo ""

# 1. Sanity checks -----------------------------------------------------------
if [ ! -d "$SRC" ]; then
    echo "[FAIL] Source not found: $SRC"
    echo "       Is the D: drive mounted? Try: ls /mnt/d/MOHIT/job-plans/"
    exit 1
fi

if grep -qi microsoft /proc/version 2>/dev/null; then
    echo "[PASS] Running inside WSL 2"
else
    echo "[WARN] Not WSL 2 (native Linux?) — continuing anyway"
fi

# 2. Make destination exist --------------------------------------------------
mkdir -p "$DST"

# 3. Copy --------------------------------------------------------------------
# Use rsync if available (faster, resumable), otherwise fall back to cp.
echo ""
echo "== Copying files =="
if command -v rsync >/dev/null 2>&1; then
    rsync -av --update \
        --exclude='*.pyc' --exclude='__pycache__' \
        --exclude='node_modules' --exclude='.venv' \
        --exclude='setup-report.txt' \
        "$SRC/" "$DST/"
else
    echo "(rsync not installed, using cp)"
    cp -ru "$SRC/." "$DST/"
fi

# 4. Verify the important stuff landed ---------------------------------------
echo ""
echo "== Verifying =="
ok=0; bad=0
check() {
    if [ -e "$DST/$1" ]; then echo "[PASS] $1"; ok=$((ok+1)); else echo "[FAIL] $1 missing"; bad=$((bad+1)); fi
}
check "openai.py"
check "DevOps Intern Product Build – Problem Statements.pdf"
check "ACKO_6_hour_Buildathon_Approach_Note Share.pdf"
check "Acko Buildathon – setup check script.pdf"
check "devops-startup-kit/setup-check.sh"
check "devops-startup-kit/pulse-starter-kit.zip"
check "devops-startup-kit/launchpad-starter-kit.zip"
check "devops-startup-kit/launchpad-starter-kit/launchpad-starter-kit/docker-compose.yml"

# 5. Make scripts executable -------------------------------------------------
find "$DST/devops-startup-kit" -name "*.sh" -exec chmod +x {} \; 2>/dev/null
chmod +x "$DST/sync-to-wsl.sh" 2>/dev/null

# 6. Report ------------------------------------------------------------------
echo ""
if [ $bad -eq 0 ]; then
    echo "RESULT: SYNCED — $ok/$((ok+bad)) items present"
    echo ""
    echo "Next steps (all inside WSL 2):"
    echo "  cd ~/acko-mohit-devops/devops-startup-kit"
    echo "  bash setup-check.sh          # must print RESULT: READY"
    echo ""
    echo "From now on, build/check/run everything in this WSL folder:"
    echo "  cd ~/acko-mohit-devops"
else
    echo "RESULT: INCOMPLETE — $bad item(s) missing. Check the source folder."
fi
