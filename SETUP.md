# Studio 8, Washington — Transcription System
## Setup Guide

> **Target platform:** Ubuntu Server 24.04 LTS · NVIDIA GPU (tested on Quadro RTX 4000)

---

## Prerequisites

- Ubuntu Server 24.04 LTS (no desktop required)
- NVIDIA GPU with CUDA support
- Python 3.11 (see Step 3)
- A free [Hugging Face](https://huggingface.co) account

---

## Step 1 — NVIDIA Driver

```bash
sudo ubuntu-drivers autoinstall
sudo reboot
```

Verify:
```bash
nvidia-smi
# Should show your GPU and CUDA version
```

---

## Step 2 — System packages

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install ffmpeg nginx cifs-utils -y
```

Verify:
```bash
ffmpeg -version
ffprobe -version
```

---

## Step 3 — Python 3.11

Ubuntu 24.04 ships Python 3.12. WhisperX requires 3.11:

```bash
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt update
sudo apt install python3.11 python3.11-venv python3.11-dev -y
```

---

## Step 4 — Project directory

```bash
sudo mkdir -p /opt/transcription
sudo chown $USER:$USER /opt/transcription
cd /opt/transcription
mkdir watchfolder output
```

Copy all project files into `/opt/transcription/`:
```
app.py
transcribe.py
worker.py
watchfolder.py
service_watchdog.py
jobstore.py
settings.py
config.yaml
watchfolders.yaml
requirements.txt
VERSION
```

---

## Step 5 — Python environment

```bash
cd /opt/transcription
python3.11 -m venv venv
source venv/bin/activate
```

Install PyTorch with CUDA first (match your CUDA version — check with `nvidia-smi`):
```bash
# For CUDA 12.6/12.8 (driver 520+):
pip install torch torchvision torchaudio \
    --index-url https://download.pytorch.org/whl/cu126
```

Verify GPU access:
```bash
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
# Expected: True  Quadro RTX 4000
```

Install remaining packages:
```bash
pip install -r requirements.txt
pip install pyyaml
```

---

## Step 6 — Hugging Face token

The pyannote diarization model requires a free Hugging Face account.

1. Create account: https://huggingface.co
2. Generate a **Read** token: https://huggingface.co/settings/tokens
3. Accept model terms (required once):
   - https://huggingface.co/pyannote/speaker-diarization-3.1
   - https://huggingface.co/pyannote/segmentation-3.0

---

## Step 7 — Environment file (.env)

```bash
nano /opt/transcription/.env
```

Contents:
```
HF_TOKEN=hf_YourTokenHere
OUTPUT_DIR=/opt/transcription/output
WEBHOOK_SECRET=choose-a-strong-secret
SETTLE_TIME=5
BATCH_POLL_INTERVAL=10
WORKER_POLL=3
```

Secure it:
```bash
chmod 600 /opt/transcription/.env
```

> **Note:** `DEVICE`, `WHISPER_MODEL`, and `LANGUAGE` are now configured in
> `config.yaml`, not in `.env`.

---

## Step 8 — Configuration (config.yaml)

Edit to match your organization:

```bash
nano /opt/transcription/config.yaml
```

Key settings:
```yaml
branding:
  organization: "Your Organization"
  app_name: "Transcription"

model:
  whisper_model: "large-v3"   # large-v3-turbo uses less VRAM (~3 GB)
  device: "cuda"

watchdog:
  poll_interval: 30
  stuck_job_timeout: 3600

priority:
  manual_upload: 10
  watchfolder_default: 5
  max_retries: 3
```

---

## Step 9 — Watchfolders (watchfolders.yaml)

Edit to match your folder structure:

```bash
nano /opt/transcription/watchfolders.yaml
```

The default single-mode entry monitors `/opt/transcription/watchfolder/`.
Add batch-mode entries for NAS/ingest systems as needed.

---

## Step 10 — First test

```bash
cd /opt/transcription
source venv/bin/activate
export $(cat .env | xargs)

python transcribe.py /path/to/test.mp3
```

On first run, Whisper downloads the model (~3 GB, one-time only).

Expected output:
```
[1/5] Reading timecode metadata: test.mp3
[2/5] Loading Whisper model (large-v3, cuda)
[3/5] Transcribing…
[4/5] Word alignment…
[5/5] Speaker diarization…
✓ Transcript saved: /opt/transcription/output/test_transcript.txt
```

---

## Step 11 — Systemd services

Four services need to be installed: **worker**, **watchfolder**, **webgui** and **watchdog**.

### Worker (GPU transcription)

```bash
sudo nano /etc/systemd/system/transcription-worker.service
```

```ini
[Unit]
Description=Transcription Worker (GPU)
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/opt/transcription
EnvironmentFile=/opt/transcription/.env
ExecStart=/opt/transcription/venv/bin/python worker.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Watchfolder

```bash
sudo nano /etc/systemd/system/transcription-watchfolder.service
```

```ini
[Unit]
Description=Transcription Watchfolder
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/opt/transcription
EnvironmentFile=/opt/transcription/.env
ExecStart=/opt/transcription/venv/bin/python watchfolder.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Web GUI

```bash
sudo nano /etc/systemd/system/transcription-webgui.service
```

```ini
[Unit]
Description=Transcription Web GUI
After=network.target

[Service]
Type=simple
User=YOUR_USERNAME
WorkingDirectory=/opt/transcription
EnvironmentFile=/opt/transcription/.env
ExecStart=/opt/transcription/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Watchdog

> **Note:** The watchdog runs as `root` so it can restart services via `systemctl`.

```bash
sudo nano /etc/systemd/system/transcription-watchdog.service
```

```ini
[Unit]
Description=Transcription Watchdog
After=network.target transcription-worker.service
Wants=transcription-worker.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/transcription
EnvironmentFile=/opt/transcription/.env
ExecStart=/opt/transcription/venv/bin/python service_watchdog.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Enable and start all services

```bash
sudo systemctl daemon-reload

sudo systemctl enable transcription-worker transcription-watchfolder \
    transcription-webgui transcription-watchdog

sudo systemctl start transcription-worker transcription-watchfolder \
    transcription-webgui transcription-watchdog
```

Verify:
```bash
sudo systemctl status transcription-worker
sudo systemctl status transcription-watchfolder
sudo systemctl status transcription-webgui
sudo systemctl status transcription-watchdog
```

---

## Step 12 — HTTPS via nginx

Generate a self-signed certificate (valid 10 years, includes IP SAN):

```bash
sudo mkdir -p /etc/nginx/certs
sudo openssl req -x509 -nodes -newkey rsa:4096 \
    -keyout /etc/nginx/certs/transcription.key \
    -out /etc/nginx/certs/transcription.crt \
    -days 3650 \
    -subj "/CN=YOUR_SERVER_IP/O=Your Organization" \
    -addext "subjectAltName=IP:YOUR_SERVER_IP"
```

Configure nginx:
```bash
sudo nano /etc/nginx/sites-available/transcription
```

```nginx
server {
    listen 80;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;

    ssl_certificate     /etc/nginx/certs/transcription.crt;
    ssl_certificate_key /etc/nginx/certs/transcription.key;
    ssl_protocols       TLSv1.2 TLSv1.3;

    client_max_body_size 4096M;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_read_timeout 3600;
        proxy_send_timeout 3600;
    }
}
```

Enable and start:
```bash
sudo ln -s /etc/nginx/sites-available/transcription \
           /etc/nginx/sites-enabled/transcription
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl enable nginx
sudo systemctl start nginx
```

---

## Step 13 — NAS mount (optional)

If using a NAS for batch ingest (CIFS/SMB):

```bash
sudo mkdir -p /mnt/Archive
```

Create credentials file:
```bash
sudo nano /etc/cifs-credentials
```
```
username=your-nas-user
password=your-nas-password
domain=WORKGROUP
```
```bash
sudo chmod 600 /etc/cifs-credentials
```

Add to `/etc/fstab`:
```
//nas-ip/ShareName  /mnt/Archive  cifs
  credentials=/etc/cifs-credentials,
  uid=YOUR_USERNAME,gid=YOUR_USERNAME,
  iocharset=utf8,file_mode=0664,dir_mode=0775,
  _netdev,nofail  0  0
```

Test:
```bash
sudo mount -a
ls /mnt/Archive
```

---

## Accessing the system

| URL | Purpose |
|-----|---------|
| `https://SERVER_IP/` | Upload GUI — manual transcription |
| `https://SERVER_IP/queue` | Queue monitor — all jobs |
| `https://SERVER_IP/system` | System status — GPU, services, disk |

---

## Useful commands

```bash
# View live logs
journalctl -u transcription-worker -f
journalctl -u transcription-watchfolder -f
journalctl -u transcription-webgui -f
journalctl -u transcription-watchdog -f

# Watchdog log file
tail -f /var/log/transcription_watchdog.log

# Restart a service after config change
sudo systemctl restart transcription-worker

# After editing config.yaml — restart all services
sudo systemctl restart transcription-worker transcription-watchfolder \
    transcription-webgui transcription-watchdog

# Check GPU
nvidia-smi

# Manually queue a file via CLI
source venv/bin/activate && export $(cat .env | xargs)
python transcribe.py /path/to/file.mxf
```

---

## Notes

- **First run:** Whisper large-v3 downloads ~3 GB on first use (cached permanently)
- **VRAM:** large-v3 needs ~6 GB VRAM. Use `large-v3-turbo` (~3 GB) in `config.yaml` if needed
- **CIFS polling:** Batch-mode and CIFS single-mode watchfolders use polling (configurable via `BATCH_POLL_INTERVAL`). Set `poll: true` per entry in `watchfolders.yaml` for single-mode CIFS folders.
- **Priority:** Manual uploads always take precedence over watchfolder jobs by default (configurable in `config.yaml`)
- **Language:** Auto-detected per file using a two-pass approach — first pass detects language, second pass transcribes with `task="transcribe"` to prevent silent translation to English. Override per upload in the GUI, per watchfolder entry in `watchfolders.yaml`, or globally via `default_language` in `config.yaml`
- **Multi-delivery:** Use `outputs` (list) instead of `output` (single) in `watchfolders.yaml` to deliver transcripts to multiple destinations simultaneously
- **Retry:** Failed jobs can be retried via the ↺ button in the Queue GUI. Maximum retries configurable via `priority.max_retries` in `config.yaml`
- **Watchdog:** Monitors all services every 30 seconds by default. Stuck jobs (running longer than `stuck_job_timeout`) are automatically requeued. Logs to `/var/log/transcription_watchdog.log`
- **HF_TOKEN:** Required for diarization. The token is checked on every model load (pyannote behaviour) but no data leaves the server after the initial model download
