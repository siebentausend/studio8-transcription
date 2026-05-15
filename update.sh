#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# update.sh — Studio 8 Transcription System
# Updates the system to the latest GitHub release with automatic backup.
#
# Usage:
#   sudo ./update.sh              # Update to latest release
#   sudo ./update.sh --rollback   # Restore previous backup
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

INSTALL_DIR="/opt/transcription"
BACKUP_DIR="/opt/transcription_backup"
LOG_FILE="/var/log/transcription_update.log"
REPO="siebentausend/studio8-transcription"

RED='\033[0;31m'; GREEN='\033[0;32m'; AMBER='\033[0;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'
CYAN='\033[0;36m'

log()     { echo -e "${BLUE}▶${RESET} $*" | tee -a "$LOG_FILE"; }
ok()      { echo -e "${GREEN}✓${RESET} $*" | tee -a "$LOG_FILE"; }
warn()    { echo -e "${AMBER}⚠${RESET} $*" | tee -a "$LOG_FILE"; }
fail()    { echo -e "${RED}✗ ERROR:${RESET} $*" | tee -a "$LOG_FILE"; exit 1; }
drylog()  { echo -e "${CYAN}  [dry-run]${RESET} $*"; }

# Parse arguments
DRYRUN=false
ROLLBACK=false
for arg in "$@"; do
    case "$arg" in
        --dryrun)   DRYRUN=true ;;
        --rollback) ROLLBACK=true ;;
    esac
done

# Read update_branch from config.yaml
UPDATE_BRANCH="main"
if command -v python3 &>/dev/null && [[ -f "$INSTALL_DIR/config.yaml" ]]; then
    UPDATE_BRANCH=$(python3 -c "
import yaml, sys
try:
    cfg = yaml.safe_load(open('$INSTALL_DIR/config.yaml'))
    print(cfg.get('runtime', {}).get('update_branch', 'main'))
except Exception:
    print('main')
" 2>/dev/null || echo "main")
fi

# ── Preflight ─────────────────────────────────────────────────────────────────
[[ -d "$INSTALL_DIR" ]] || fail "Install directory not found: $INSTALL_DIR"

if ! $DRYRUN; then
    [[ $EUID -eq 0 ]] || fail "This script must be run as root (use sudo) unless using --dryrun"
    touch "$LOG_FILE"
    echo "" >> "$LOG_FILE"
    echo "════════════════════════════════════════" >> "$LOG_FILE"
    echo "$(date '+%Y-%m-%d %H:%M:%S') — Update started" >> "$LOG_FILE"
fi

# ── Rollback ──────────────────────────────────────────────────────────────────
if $ROLLBACK; then
    echo ""
    echo -e "${BOLD}╔══════════════════════════════════════════╗${RESET}"
    echo -e "${BOLD}║   Transcription System — Rollback        ║${RESET}"
    echo -e "${BOLD}╚══════════════════════════════════════════╝${RESET}"
    echo ""

    [[ -d "$BACKUP_DIR" ]] || fail "No backup found at $BACKUP_DIR"

    # Show backup info
    if [[ -f "$BACKUP_DIR/VERSION" ]]; then
        BACKUP_VERSION=$(cat "$BACKUP_DIR/VERSION")
        log "Backup version: $BACKUP_VERSION"
    fi
    if [[ -f "$BACKUP_DIR/.backup_timestamp" ]]; then
        log "Backup created: $(cat "$BACKUP_DIR/.backup_timestamp")"
    fi

    echo ""
    read -rp "$(echo -e "${BOLD}Restore this backup? All current changes will be lost. [y/N]: ${RESET}")" CONFIRM
    [[ "${CONFIRM,,}" == "y" ]] || { echo "Aborted."; exit 0; }

    log "Stopping services…"
    systemctl stop transcription-worker transcription-watchfolder \
        transcription-webgui transcription-watchdog 2>/dev/null || true

    log "Restoring backup…"
    # Restore Python files and config (not venv, not .env, not output/)
    rsync -a --exclude='.env' --exclude='output/' --exclude='venv/' \
        "$BACKUP_DIR/" "$INSTALL_DIR/"

    log "Restarting services…"
    systemctl start transcription-worker transcription-watchfolder \
        transcription-webgui transcription-watchdog

    RESTORED_VERSION=$(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo "unknown")
    ok "Rollback complete — restored to $RESTORED_VERSION"
    exit 0
fi

# ── Check for update ──────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════╗${RESET}"
if $DRYRUN; then
echo -e "${BOLD}║   Transcription System — Dry Run         ║${RESET}"
else
echo -e "${BOLD}║   Transcription System — Update          ║${RESET}"
fi
echo -e "${BOLD}╚══════════════════════════════════════════╝${RESET}"
echo ""

if $DRYRUN; then
    echo -e "${CYAN}  Dry-run mode — no changes will be made${RESET}"
    echo ""
fi

CURRENT_VERSION=$(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo "unknown")
log "Current version: $CURRENT_VERSION"
log "Update branch:   $UPDATE_BRANCH"

# Fetch latest release from GitHub API
log "Checking GitHub for latest release…"
RELEASE_JSON=$(curl -sf "https://api.github.com/repos/$REPO/releases/latest" || true)

if [[ -z "$RELEASE_JSON" ]]; then
    fail "Could not reach GitHub API — check internet connection"
fi

LATEST_VERSION=$(echo "$RELEASE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])" 2>/dev/null || echo "")
RELEASE_NOTES=$(echo "$RELEASE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin).get('body','')[:500])" 2>/dev/null || echo "")
RELEASE_DATE=$(echo "$RELEASE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['published_at'][:10])" 2>/dev/null || echo "")

[[ -n "$LATEST_VERSION" ]] || fail "Could not parse latest release from GitHub"

log "Latest release:  $LATEST_VERSION ($RELEASE_DATE)"

if [[ "$CURRENT_VERSION" == "$LATEST_VERSION" ]]; then
    ok "Already on the latest version ($CURRENT_VERSION) — nothing to do."
    exit 0
fi

echo ""
echo -e "${BOLD}Update available:${RESET} $CURRENT_VERSION → $LATEST_VERSION"
if [[ -n "$RELEASE_NOTES" ]]; then
    echo ""
    echo -e "${BOLD}Release notes:${RESET}"
    echo "$RELEASE_NOTES" | head -20
fi
echo ""

if $DRYRUN; then
    echo -e "${CYAN}${BOLD}Dry-run summary — what would happen:${RESET}"
    drylog "Create backup: $INSTALL_DIR → $BACKUP_DIR"
    drylog "Stop services: transcription-worker, transcription-watchfolder, transcription-webgui, transcription-watchdog"
    drylog "Run: git fetch --tags && git checkout $LATEST_VERSION (branch: $UPDATE_BRANCH)"
    drylog "Run: pip install -r requirements.txt"
    drylog "Start services"
    echo ""
    echo -e "${CYAN}  No changes made. Run without --dryrun to apply the update.${RESET}"
    echo ""
    exit 0
fi

read -rp "$(echo -e "${BOLD}Proceed with update? [y/N]: ${RESET}")" CONFIRM
[[ "${CONFIRM,,}" == "y" ]] || { echo "Aborted."; exit 0; }

# ── Backup ────────────────────────────────────────────────────────────────────
log "Creating backup at $BACKUP_DIR…"
rm -rf "$BACKUP_DIR"
rsync -a --exclude='.env' --exclude='output/' --exclude='venv/' \
    "$INSTALL_DIR/" "$BACKUP_DIR/"
echo "$(date '+%Y-%m-%d %H:%M:%S')" > "$BACKUP_DIR/.backup_timestamp"
ok "Backup created (version $CURRENT_VERSION)"

# ── Stop services ─────────────────────────────────────────────────────────────
log "Stopping services…"
systemctl stop transcription-worker transcription-watchfolder \
    transcription-webgui transcription-watchdog 2>/dev/null || true
ok "Services stopped"

# ── Pull update ───────────────────────────────────────────────────────────────
log "Pulling $LATEST_VERSION from GitHub (branch: $UPDATE_BRANCH)…"
cd "$INSTALL_DIR"
git fetch --tags
git checkout "$LATEST_VERSION" 2>/dev/null || git pull origin "$UPDATE_BRANCH"

ok "Code updated to $LATEST_VERSION"

# ── Update dependencies ───────────────────────────────────────────────────────
log "Updating Python dependencies…"
"$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
ok "Dependencies updated"

# ── Restart services ──────────────────────────────────────────────────────────
log "Starting services…"
systemctl start transcription-worker transcription-watchfolder \
    transcription-webgui transcription-watchdog
sleep 3

FAILED=()
for svc in transcription-worker transcription-watchfolder transcription-webgui transcription-watchdog; do
    systemctl is-active --quiet "$svc" || FAILED+=("$svc")
done

if [[ ${#FAILED[@]} -gt 0 ]]; then
    warn "These services failed to start: ${FAILED[*]}"
    warn "Run 'sudo ./update.sh --rollback' to restore the previous version"
else
    ok "All services running"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
NEW_VERSION=$(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo "$LATEST_VERSION")
echo ""
echo -e "${GREEN}${BOLD}✓ Update complete: $CURRENT_VERSION → $NEW_VERSION${RESET}"
echo ""
echo -e "  Rollback available: ${BOLD}sudo ./update.sh --rollback${RESET}"
echo -e "  Update log:         ${BOLD}$LOG_FILE${RESET}"
echo ""