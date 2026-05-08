#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# install.sh — Studio 8 Transcription System
# Full installation script for Ubuntu Server 24.04 + NVIDIA GPU
#
# Usage:
#   chmod +x install.sh
#   sudo ./install.sh
#
# The script saves its state to /tmp/.transcription_install_state
# so it can resume automatically after a reboot.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Constants ─────────────────────────────────────────────────────────────────
INSTALL_DIR="/opt/transcription"
STATE_FILE="/tmp/.transcription_install_state"
RESUME_SERVICE="/etc/systemd/system/transcription-install-resume.service"
LOG_FILE="/var/log/transcription_install.log"

# Colours
RED='\033[0;31m'; GREEN='\033[0;32m'; AMBER='\033[0;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'

# ── Logging ───────────────────────────────────────────────────────────────────
log()  { echo -e "${BLUE}▶${RESET} $*" | tee -a "$LOG_FILE"; }
ok()   { echo -e "${GREEN}✓${RESET} $*" | tee -a "$LOG_FILE"; }
warn() { echo -e "${AMBER}⚠${RESET} $*" | tee -a "$LOG_FILE"; }
fail() { echo -e "${RED}✗ ERROR:${RESET} $*" | tee -a "$LOG_FILE"; exit 1; }

# ── State management ──────────────────────────────────────────────────────────
save_state() { echo "$1" > "$STATE_FILE"; }
load_state() { [[ -f "$STATE_FILE" ]] && cat "$STATE_FILE" || echo "start"; }
clear_state() { rm -f "$STATE_FILE"; }

# ── Preflight ─────────────────────────────────────────────────────────────────
preflight() {
    log "Running preflight checks…"
    [[ $EUID -eq 0 ]] || fail "This script must be run as root (use sudo)"
    . /etc/os-release
    [[ "$ID" == "ubuntu" && "$VERSION_ID" == "24.04" ]] \
        || warn "Tested on Ubuntu 24.04. Current: $PRETTY_NAME"
    ping -c1 -W3 8.8.8.8 &>/dev/null || fail "No internet connection"
    ok "Preflight checks passed"
}

# ── Banner ────────────────────────────────────────────────────────────────────
banner() {
    echo ""
    echo -e "${BOLD}╔══════════════════════════════════════════════════════════╗${RESET}"
    echo -e "${BOLD}║   Studio 8 Transcription System — Installer             ║${RESET}"
    echo -e "${BOLD}║   Ubuntu Server 24.04 · NVIDIA GPU · v0.9.1-beta        ║${RESET}"
    echo -e "${BOLD}╚══════════════════════════════════════════════════════════╝${RESET}"
    echo ""
}

# ── Interactive prompts ───────────────────────────────────────────────────────
prompt_config() {
    echo -e "${BOLD}Please answer the following questions to configure the system:${RESET}"
    echo ""

    local default_user="${SUDO_USER:-$(logname 2>/dev/null || echo '')}"
    read -rp "$(echo -e "  Service user [${default_user}]: ")" INPUT_USER
    APP_USER="${INPUT_USER:-$default_user}"
    id "$APP_USER" &>/dev/null || fail "User '$APP_USER' does not exist"

    local detected_ip
    detected_ip=$(hostname -I | awk '{print $1}')
    read -rp "$(echo -e "  Server IP address [${detected_ip}]: ")" INPUT_IP
    APP_IP="${INPUT_IP:-$detected_ip}"

    echo ""
    echo -e "  ${AMBER}Hugging Face token — required for speaker diarization${RESET}"
    echo -e "  Get yours at: https://huggingface.co/settings/tokens"
    read -rp "  HF_TOKEN: " HF_TOKEN
    [[ -n "$HF_TOKEN" ]] || fail "HF_TOKEN cannot be empty"

    echo ""
    local default_secret
    default_secret=$(openssl rand -hex 16)
    read -rp "$(echo -e "  Webhook secret [auto-generated: ${default_secret}]: ")" INPUT_SECRET
    WEBHOOK_SECRET="${INPUT_SECRET:-$default_secret}"

    echo ""
    read -rp "  Organization name [Your Organization]: " INPUT_ORG
    APP_ORG="${INPUT_ORG:-Your Organization}"

    echo ""
    echo -e "${BOLD}Configuration summary:${RESET}"
    echo -e "  User:             ${APP_USER}"
    echo -e "  Server IP:        ${APP_IP}"
    echo -e "  HF_TOKEN:         ${HF_TOKEN:0:8}…"
    echo -e "  Webhook secret:   ${WEBHOOK_SECRET:0:8}…"
    echo -e "  Organization:     ${APP_ORG}"
    echo -e "  Install path:     ${INSTALL_DIR}"
    echo ""
    read -rp "$(echo -e "${BOLD}Proceed with installation? [y/N]: ${RESET}")" CONFIRM
    [[ "${CONFIRM,,}" == "y" ]] || { echo "Aborted."; exit 0; }

    cat > /tmp/.transcription_config <<EOF
APP_USER="${APP_USER}"
APP_IP="${APP_IP}"
HF_TOKEN="${HF_TOKEN}"
WEBHOOK_SECRET="${WEBHOOK_SECRET}"
APP_ORG="${APP_ORG}"
REPO_URL="${REPO_URL}"
EOF
    chmod 600 /tmp/.transcription_config
}

load_config() {
    # shellcheck source=/dev/null
    [[ -f /tmp/.transcription_config ]] && source /tmp/.transcription_config
}

# ── Step 1: NVIDIA Driver ─────────────────────────────────────────────────────
step_nvidia() {
    log "Step 1/10 — NVIDIA Driver"

    if nvidia-smi &>/dev/null; then
        ok "NVIDIA driver already installed: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader)"
        return
    fi

    log "Installing NVIDIA driver via ubuntu-drivers…"
    apt-get update -qq
    apt-get install -y ubuntu-drivers-common
    ubuntu-drivers autoinstall

    local script_path
    script_path="$(realpath "$0")"

    cat > "$RESUME_SERVICE" <<EOF
[Unit]
Description=Transcription Installer Resume
After=network-online.target
Wants=network-online.target
ConditionPathExists=${STATE_FILE}

[Service]
Type=oneshot
ExecStart=/bin/bash ${script_path} --resume
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable transcription-install-resume

    save_state "step_packages"
    warn "NVIDIA driver installed. The system must reboot to continue."
    warn "The installation will resume automatically after reboot."
    echo ""
    read -rp "$(echo -e "${BOLD}Reboot now? [y/N]: ${RESET}")" REBOOT_NOW
    if [[ "${REBOOT_NOW,,}" == "y" ]]; then
        log "Rebooting…"
        reboot
    else
        warn "Please reboot manually and run: sudo ./install.sh --resume"
        exit 0
    fi
}

# ── Step 2: System packages ───────────────────────────────────────────────────
step_packages() {
    log "Step 2/10 — System packages"
    apt-get update -qq
    apt-get install -y ffmpeg nginx cifs-utils git curl
    ffmpeg -version &>/dev/null || fail "ffmpeg installation failed"
    ok "System packages installed"
}

# ── Step 3: Python 3.11 ───────────────────────────────────────────────────────
step_python() {
    log "Step 3/10 — Python 3.11"

    if python3.11 --version &>/dev/null; then
        ok "Python 3.11 already installed: $(python3.11 --version)"
        return
    fi

    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -qq
    apt-get install -y python3.11 python3.11-venv python3.11-dev
    python3.11 --version &>/dev/null || fail "Python 3.11 installation failed"
    ok "Python 3.11 installed"
}

# ── Step 4: Project directory + repo clone ────────────────────────────────────
step_clone() {
    log "Step 4/10 — Project directory and repository"

    mkdir -p "$INSTALL_DIR"
    chown "${APP_USER}:${APP_USER}" "$INSTALL_DIR"

    if [[ -f "${INSTALL_DIR}/app.py" ]]; then
        ok "Repository already cloned — pulling latest"
        cd "$INSTALL_DIR"
        sudo -u "$APP_USER" git pull
    else
        log "Cloning from ${REPO_URL}…"
        sudo -u "$APP_USER" git clone "$REPO_URL" "$INSTALL_DIR"
    fi

    sudo -u "$APP_USER" mkdir -p "${INSTALL_DIR}/watchfolder" "${INSTALL_DIR}/output"
    ok "Repository ready at ${INSTALL_DIR}"
}

# ── Step 5: Python venv + PyTorch + dependencies ──────────────────────────────
step_venv() {
    log "Step 5/10 — Python virtual environment and dependencies"

    local venv="${INSTALL_DIR}/venv"

    if [[ ! -d "$venv" ]]; then
        sudo -u "$APP_USER" python3.11 -m venv "$venv"
        ok "Virtual environment created"
    else
        ok "Virtual environment already exists"
    fi

    local pip="${venv}/bin/pip"

    local cuda_ver cuda_tag
    cuda_ver=$(nvidia-smi | grep -oP 'CUDA Version: \K[\d.]+' | head -1)
    log "Detected CUDA version: ${cuda_ver}"

    if [[ "$cuda_ver" == 12.* ]]; then
        local cuda_minor
        cuda_minor=$(echo "$cuda_ver" | cut -d. -f2)
        if   [[ $cuda_minor -ge 8 ]]; then cuda_tag="cu128"
        elif [[ $cuda_minor -ge 6 ]]; then cuda_tag="cu126"
        elif [[ $cuda_minor -ge 4 ]]; then cuda_tag="cu124"
        else                               cuda_tag="cu121"; fi
    elif [[ "$cuda_ver" == 11.* ]]; then
        cuda_tag="cu118"
    else
        warn "Unrecognised CUDA version ${cuda_ver} — defaulting to cu128"
        cuda_tag="cu128"
    fi

    log "Installing PyTorch with ${cuda_tag}…"
    sudo -u "$APP_USER" "$pip" install --quiet \
        torch torchvision torchaudio \
        --index-url "https://download.pytorch.org/whl/${cuda_tag}"

    sudo -u "$APP_USER" "${venv}/bin/python" -c \
        "import torch; assert torch.cuda.is_available(), 'CUDA not available'" \
        || fail "PyTorch cannot access the GPU — check NVIDIA driver"
    ok "PyTorch with CUDA installed and verified"

    log "Installing project dependencies…"
    sudo -u "$APP_USER" "$pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"
    sudo -u "$APP_USER" "$pip" install --quiet pyyaml
    ok "Dependencies installed"
}

# ── Step 6: .env and config files ─────────────────────────────────────────────
step_env() {
    log "Step 6/10 — Environment file (.env)"

    local env_file="${INSTALL_DIR}/.env"

    if [[ -f "$env_file" ]]; then
        warn ".env already exists — skipping (delete it manually to regenerate)"
    else
        cat > "$env_file" <<EOF
HF_TOKEN=${HF_TOKEN}
OUTPUT_DIR=${INSTALL_DIR}/output
WEBHOOK_SECRET=${WEBHOOK_SECRET}
SETTLE_TIME=5
BATCH_POLL_INTERVAL=10
WORKER_POLL=3
EOF
        chmod 600 "$env_file"
        chown "${APP_USER}:${APP_USER}" "$env_file"
        ok ".env written"
    fi

    local cfg="${INSTALL_DIR}/config.yaml"
    if [[ -f "${cfg}.example" && ! -f "$cfg" ]]; then
        cp "${cfg}.example" "$cfg"
        sed -i "s|organization:.*|organization: \"${APP_ORG}\"|" "$cfg"
        chown "${APP_USER}:${APP_USER}" "$cfg"
        ok "config.yaml created from example"
    fi

    local wf="${INSTALL_DIR}/watchfolders.yaml"
    if [[ -f "${wf}.example" && ! -f "$wf" ]]; then
        cp "${wf}.example" "$wf"
        chown "${APP_USER}:${APP_USER}" "$wf"
        ok "watchfolders.yaml created from example"
    fi
}

# ── Step 7: Systemd services ──────────────────────────────────────────────────
step_services() {
    log "Step 7/10 — Systemd services"

    local venv="${INSTALL_DIR}/venv"

    for svc in worker watchfolder webgui; do
        local svc_file="/etc/systemd/system/transcription-${svc}.service"
        local description exec_start

        case "$svc" in
            worker)
                description="Transcription Worker (GPU)"
                exec_start="${venv}/bin/python worker.py"
                ;;
            watchfolder)
                description="Transcription Watchfolder"
                exec_start="${venv}/bin/python watchfolder.py"
                ;;
            webgui)
                description="Transcription Web GUI"
                exec_start="${venv}/bin/uvicorn app:app --host 0.0.0.0 --port 8000"
                ;;
        esac

        cat > "$svc_file" <<EOF
[Unit]
Description=${description}
After=network.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/.env
ExecStart=${INSTALL_DIR}/${exec_start}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
        ok "Service file written: transcription-${svc}"
    done

    systemctl daemon-reload

    for svc in worker watchfolder webgui; do
        systemctl enable "transcription-${svc}"
        systemctl start  "transcription-${svc}"
        sleep 2
        systemctl is-active --quiet "transcription-${svc}" \
            && ok "transcription-${svc} running" \
            || warn "transcription-${svc} failed to start — check: journalctl -u transcription-${svc}"
    done
}

# ── Step 8: nginx + TLS ───────────────────────────────────────────────────────
step_nginx() {
    log "Step 8/10 — nginx HTTPS configuration"

    mkdir -p /etc/nginx/certs
    if [[ ! -f /etc/nginx/certs/transcription.crt ]]; then
        openssl req -x509 -nodes -newkey rsa:4096 \
            -keyout /etc/nginx/certs/transcription.key \
            -out    /etc/nginx/certs/transcription.crt \
            -days 3650 \
            -subj "/CN=${APP_IP}/O=${APP_ORG}" \
            -addext "subjectAltName=IP:${APP_IP}" \
            2>/dev/null
        ok "Self-signed certificate generated for ${APP_IP}"
    else
        ok "Certificate already exists — skipping"
    fi

    cat > /etc/nginx/sites-available/transcription <<EOF
server {
    listen 80;
    return 301 https://\$host\$request_uri;
}

server {
    listen 443 ssl;
    server_name ${APP_IP};

    ssl_certificate     /etc/nginx/certs/transcription.crt;
    ssl_certificate_key /etc/nginx/certs/transcription.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    client_max_body_size 4096M;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_read_timeout 3600;
        proxy_send_timeout 3600;
    }
}
EOF

    ln -sf /etc/nginx/sites-available/transcription \
           /etc/nginx/sites-enabled/transcription
    rm -f /etc/nginx/sites-enabled/default

    nginx -t || fail "nginx configuration test failed"
    systemctl enable nginx
    systemctl restart nginx
    ok "nginx configured and running"
}

# ── Step 9: First run ─────────────────────────────────────────────────────────
step_first_run() {
    log "Step 9/10 — First run (Whisper model download ~3 GB)"
    warn "This may take several minutes depending on your connection."

    local venv="${INSTALL_DIR}/venv"
    local env_file="${INSTALL_DIR}/.env"

    local test_file
    test_file=$(find "${INSTALL_DIR}/watchfolder" \
        \( -name "*.mp3" -o -name "*.mp4" -o -name "*.wav" \) 2>/dev/null | head -1 || true)

    if [[ -z "$test_file" ]]; then
        warn "No test file found in ${INSTALL_DIR}/watchfolder — skipping first-run test"
        warn "Whisper model will download on the first real job"
        return
    fi

    log "Running test transcription on: $(basename "$test_file")"
    cd "$INSTALL_DIR"
    sudo -u "$APP_USER" bash -c "
        source ${venv}/bin/activate
        export \$(grep -v '^#' ${env_file} | xargs)
        python transcribe.py '${test_file}'
    " && ok "First-run test successful — model is cached" \
      || warn "First-run test failed — check logs. The system may still work for real jobs."
}

# ── Step 10: Cleanup ──────────────────────────────────────────────────────────
step_cleanup() {
    log "Step 10/10 — Cleanup"

    if [[ -f "$RESUME_SERVICE" ]]; then
        systemctl disable transcription-install-resume 2>/dev/null || true
        rm -f "$RESUME_SERVICE"
        systemctl daemon-reload
    fi

    rm -f /tmp/.transcription_config
    clear_state
    ok "Resume service removed"
}

# ── Summary ───────────────────────────────────────────────────────────────────
summary() {
    echo ""
    echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════════╗${RESET}"
    echo -e "${BOLD}${GREEN}║   Installation complete!                                 ║${RESET}"
    echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════════╝${RESET}"
    echo ""
    echo -e "  ${BOLD}Web interface:${RESET}  https://${APP_IP}"
    echo -e "  ${BOLD}Queue monitor:${RESET}  https://${APP_IP}/queue"
    echo -e "  ${BOLD}System status:${RESET}  https://${APP_IP}/system"
    echo ""
    echo -e "  ${BOLD}Next steps:${RESET}"
    echo -e "  1. Accept the browser certificate warning (self-signed)"
    echo -e "  2. Edit ${INSTALL_DIR}/watchfolders.yaml for your folder structure"
    echo -e "  3. Accept pyannote model terms at huggingface.co (if not done yet)"
    echo ""
    echo -e "  ${BOLD}Log file:${RESET} ${LOG_FILE}"
    echo ""
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
    local resume=false
    [[ "${1:-}" == "--resume" ]] && resume=true

    REPO_URL=$(git -C "$(dirname "$(realpath "$0")")" remote get-url origin 2>/dev/null || echo "")
    [[ -n "$REPO_URL" ]] || fail "Could not determine repo URL. Run this script from inside the cloned repository."

    touch "$LOG_FILE"
    chmod 644 "$LOG_FILE"

    if $resume; then
        log "Resuming installation after reboot…"
        load_config
    else
        banner
        preflight
        prompt_config
    fi

    local state
    state=$(load_state)

    run_from() {
        local steps=("step_nvidia" "step_packages" "step_python" "step_clone"
                     "step_venv" "step_env" "step_services" "step_nginx"
                     "step_first_run" "step_cleanup")
        local target="$1"
        local running=false

        for step in "${steps[@]}"; do
            [[ "$step" == "$target" || "$target" == "start" ]] && running=true
            $running && { save_state "$step"; $step; }
        done
    }

    case "$state" in
        start)           run_from "step_nvidia"    ;;
        step_packages)   run_from "step_packages"  ;;
        step_python)     run_from "step_python"    ;;
        step_clone)      run_from "step_clone"     ;;
        step_venv)       run_from "step_venv"      ;;
        step_env)        run_from "step_env"       ;;
        step_services)   run_from "step_services"  ;;
        step_nginx)      run_from "step_nginx"     ;;
        step_first_run)  run_from "step_first_run" ;;
        step_cleanup)    run_from "step_cleanup"   ;;
    esac

    summary
}

main "$@"
