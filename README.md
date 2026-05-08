# Studio 8, Washington — Transcription System

Self-hosted audio/video transcription with speaker diarization and SMPTE timecodes.
Built on WhisperX and pyannote, running on a local GPU server.

## What it does

- Transcribes audio and video files (MXF, MP4, MOV, MP3, WAV, and more)
- Outputs plain-text transcripts with SMPTE timecodes and speaker labels
- Two workflows: manual upload via web GUI, and automated ingest from a NAS via watchfolder
- Supports 99 languages with per-file auto-detection
- Priority queue — manual uploads always jump ahead of automated jobs

## Stack

- **Transcription:** WhisperX large-v3
- **Diarization:** pyannote/speaker-diarization-3.1
- **Web GUI:** FastAPI + nginx (HTTPS)
- **Platform:** Ubuntu Server 24.04 · NVIDIA Quadro RTX 4000

## Getting started

See [SETUP.md](SETUP.md) for the full installation guide.

Quick overview:
1. Clone the repository
2. Copy the configuration templates and fill in your values:
   - `cp config.yaml.example config.yaml`
   - `cp watchfolders.yaml.example watchfolders.yaml`
   - `cp .env.example .env`
3. Follow the steps in SETUP.md

## Repository structure

| File | Purpose |
|------|---------|
| `app.py` | Web GUI (FastAPI) |
| `worker.py` | GPU transcription worker |
| `watchfolder.py` | Watchfolder daemon |
| `transcribe.py` | Transcription pipeline |
| `jobstore.py` | SQLite job queue |
| `settings.py` | Config loader |
| `requirements.txt` | Python dependencies |
| `config.yaml.example` | Configuration template |
| `watchfolders.yaml.example` | Watchfolder template |
| `.env.example` | Environment variables template |
| `SETUP.md` | Full setup guide |

## Configuration files

| File | Purpose | In Git |
|------|---------|--------|
| `config.yaml.example` | Branding, model, colors, priorities | ✅ |
| `watchfolders.yaml.example` | Watchfolder paths and modes | ✅ |
| `.env.example` | Tokens, secrets, paths | ✅ |
| `config.yaml` | Your actual configuration | ❌ |
| `watchfolders.yaml` | Your actual watchfolder setup | ❌ |
| `.env` | Your actual secrets | ❌ |

## Version

v0.9.1-beta
