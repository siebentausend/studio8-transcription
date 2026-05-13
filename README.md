# Studio 8, Washington — Transcription System

Self-hosted audio/video transcription with speaker diarization and SMPTE timecodes.
Built on WhisperX and pyannote, running on a local GPU server.

---

## What it does

- Transcribes audio and video files (MXF, MP4, MOV, MP3, WAV, and more)
- Outputs plain-text transcripts with SMPTE timecodes and speaker labels
- Two workflows: manual upload via web GUI, and automated ingest from a NAS via watchfolder
- Batch mode: all clips from a camera card ingested into one combined transcript
- Supports 99 languages with per-file auto-detection (two-pass to prevent silent translation)
- Priority queue — manual uploads always jump ahead of automated jobs
- Service watchdog — monitors all services, restarts on failure, detects stuck jobs
- Config file monitoring — automatically restarts affected services when config changes

---

## Stack

- **Transcription:** WhisperX large-v3
- **Diarization:** pyannote/speaker-diarization-3.1
- **Web GUI:** FastAPI + nginx (HTTPS)
- **Queue:** SQLite (WAL mode, multi-process safe)
- **Platform:** Ubuntu Server 24.04 · NVIDIA GPU (tested on Quadro RTX 4000)

---

## Getting started

See [SETUP.md](SETUP.md) for the full installation guide, or use the interactive installer:

```bash
git clone https://github.com/siebentausend/studio8-transcription.git /opt/transcription
cd /opt/transcription
cp config.yaml.example config.yaml
cp watchfolders.yaml.example watchfolders.yaml
cp .env.example .env
# Edit config.yaml and .env with your values
chmod +x install.sh
sudo ./install.sh
```


---

## Repository structure

| File | Purpose |
|---|---|
| `app.py` | Web GUI (FastAPI) — Upload, Queue, Status pages |
| `worker.py` | GPU transcription worker |
| `watchfolder.py` | Watchfolder daemon — single and batch mode |
| `transcribe.py` | Core transcription pipeline |
| `service_watchdog.py` | Service monitor and config file watcher |
| `jobstore.py` | SQLite job queue interface |
| `settings.py` | Config loader — typed access to config.yaml |
| `install.sh` | Interactive installation script |
| `requirements.txt` | Python dependencies |
| `config.yaml.example` | Configuration template |
| `watchfolders.yaml.example` | Watchfolder template |
| `.env.example` | Secrets template |
| `SETUP.md` | Full setup guide |

---

## Configuration files

| File | Purpose |
|---|---|
| `config.yaml` | Branding, colors, model, priorities, runtime settings |
| `watchfolders.yaml` | Watchfolder sources, modes, output paths |
| `.env` | Secrets only — `HF_TOKEN` and `WEBHOOK_SECRET` |

---

## Web interface
<img width="1917" height="907" alt="s8-trans01" src="https://github.com/user-attachments/assets/d1708729-2a42-43b0-927b-9c41708b4534" />
<img width="1914" height="905" alt="s8-trans02" src="https://github.com/user-attachments/assets/35500f9b-a908-4443-870e-fc028bf9197e" />
<img width="1902" height="907" alt="s8-trans03" src="https://github.com/user-attachments/assets/6f60a15b-c4d9-4564-9489-000ff5f6b607" />
| URL | Purpose |
|---|---|
| `https://server/` | Upload GUI — manual transcription |
| `https://server/queue` | Queue monitor — all jobs |
| `https://server/system` | System status — GPU, services, disk |

---

## Version

v0.9.2-beta
