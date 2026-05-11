"""
watchdog.py
───────────
Monitors all three transcription services and restarts them if they fail.
Also detects stuck worker jobs (running for too long without progress).

Runs as a fourth systemd service: transcription-watchdog

Log file: /var/log/transcription_watchdog.log
"""

import logging
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from jobstore import get_jobs, init_db, update_job
from settings import cfg

# ── Configuration ─────────────────────────────────────────────────────────────
POLL_INTERVAL     = int(os.environ.get("WATCHDOG_POLL", str(cfg.watchdog.poll_interval)))
STUCK_JOB_TIMEOUT = int(os.environ.get("WATCHDOG_STUCK", str(cfg.watchdog.stuck_job_timeout)))
LOG_FILE          = "/var/log/transcription_watchdog.log"

SERVICES = [
    "transcription-worker",
    "transcription-watchfolder",
    "transcription-webgui",
]

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WATCHDOG] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE),
    ]
)
log = logging.getLogger(__name__)


# ── Service checks ────────────────────────────────────────────────────────────

def is_active(service: str) -> bool:
    """Return True if the service is active according to systemctl."""
    try:
        r = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip() == "active"
    except Exception:
        return False


def restart_service(service: str):
    """Restart a systemd service and log the event."""
    log.warning(f"Service '{service}' is not active — restarting…")
    try:
        subprocess.run(
            ["systemctl", "restart", service],
            timeout=30, check=True
        )
        time.sleep(3)
        if is_active(service):
            log.info(f"Service '{service}' restarted successfully")
        else:
            log.error(f"Service '{service}' failed to restart — manual intervention required")
    except subprocess.CalledProcessError as e:
        log.error(f"Failed to restart '{service}': {e}")
    except Exception as e:
        log.error(f"Unexpected error restarting '{service}': {e}")


def check_services():
    """Check all services and restart any that are not active."""
    for service in SERVICES:
        if not is_active(service):
            restart_service(service)


# ── Stuck job detection ───────────────────────────────────────────────────────

def check_stuck_jobs():
    """
    Detect jobs that have been in 'running' state for longer than
    STUCK_JOB_TIMEOUT seconds — likely the worker crashed mid-job.
    Requeues them and restarts the worker.
    """
    try:
        jobs = get_jobs(100)
    except Exception as e:
        log.error(f"Could not read job queue: {e}")
        return

    now = datetime.now()
    for job in jobs:
        if job.get("status") != "running":
            continue

        updated_at_str = job.get("updated_at", "")
        if not updated_at_str:
            continue

        try:
            updated_at = datetime.fromisoformat(updated_at_str)
        except ValueError:
            continue

        age = (now - updated_at).total_seconds()
        if age > STUCK_JOB_TIMEOUT:
            log.warning(
                f"Job '{job['filename']}' [{job['id']}] has been running for "
                f"{int(age/60)} minutes — requeuing and restarting worker"
            )
            try:
                update_job(job["id"], "queued", step=0, total=5,
                           message="Requeued by watchdog after timeout")
                restart_service("transcription-worker")
            except Exception as e:
                log.error(f"Could not requeue stuck job [{job['id']}]: {e}")


# ── Database check ────────────────────────────────────────────────────────────

def check_database():
    """Verify the job database is reachable."""
    try:
        init_db()
        get_jobs(1)
    except Exception as e:
        log.error(f"Database check failed: {e}")

# ── Main loop ─────────────────────────────────────────────────────────────────

def run():
    # Ensure log file exists and is writable
    Path(LOG_FILE).touch(exist_ok=True)

    log.info(f"Watchdog started — checking every {POLL_INTERVAL}s")
    log.info(f"Monitoring: {', '.join(SERVICES)}")
    log.info(f"Stuck job timeout: {STUCK_JOB_TIMEOUT}s ({STUCK_JOB_TIMEOUT//60} min)")

    while True:
        try:
            check_services()
            check_database()
            check_stuck_jobs()
        except Exception as e:
            log.error(f"Unexpected error in watchdog loop: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()