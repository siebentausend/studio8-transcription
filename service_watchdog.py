"""
service_watchdog.py
───────────────────
Monitors all three transcription services and restarts them if they fail.
Also detects stuck worker jobs (running for too long without progress).
Also watches config files for changes — waits for any running job to finish
before restarting affected services.

Runs as a fourth systemd service: transcription-watchdog

Log file: /var/log/transcription_watchdog.log
"""

import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from jobstore import get_jobs, init_db, update_job
from settings import cfg

# ── Configuration ─────────────────────────────────────────────────────────────
POLL_INTERVAL     = cfg.watchdog.poll_interval
STUCK_JOB_TIMEOUT = cfg.watchdog.stuck_job_timeout
LOG_FILE          = "/var/log/transcription_watchdog.log"
INSTALL_DIR       = Path(__file__).parent

SERVICES = [
    "transcription-worker",
    "transcription-watchfolder",
    "transcription-webgui",
]

# Config files to monitor — any change triggers a full service restart
# (after waiting for any running job to complete)
CONFIG_FILES = [
    INSTALL_DIR / "config.yaml",
    INSTALL_DIR / "watchfolders.yaml",
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


def restart_service(service: str, reason: str = ""):
    """Restart a systemd service and log the event."""
    msg = f"Restarting '{service}'"
    if reason:
        msg += f" — {reason}"
    log.warning(msg)
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
            restart_service(service, reason="service not active")


# ── Job state ─────────────────────────────────────────────────────────────────

def is_job_running() -> bool:
    """Return True if any job is currently in 'running' state."""
    try:
        jobs = get_jobs(100)
        return any(j.get("status") == "running" for j in jobs)
    except Exception:
        return False


def wait_for_idle(timeout: int = 7200, check_interval: int = 10) -> bool:
    """
    Wait until no job is running, up to timeout seconds.
    Returns True if idle, False if timed out.
    """
    waited = 0
    while is_job_running():
        if waited == 0:
            log.info("Config change detected — waiting for running job to finish before restart…")
        time.sleep(check_interval)
        waited += check_interval
        if waited % 60 == 0:
            log.info(f"Still waiting for job to finish… ({waited}s elapsed)")
        if waited >= timeout:
            log.warning(f"Timed out after {timeout}s waiting for job — restarting anyway")
            return False
    return True


# ── Config file monitoring ────────────────────────────────────────────────────

def get_mtimes() -> dict:
    """Return a dict of {Path: mtime} for all monitored config files."""
    mtimes = {}
    for path in CONFIG_FILES:
        try:
            mtimes[path] = path.stat().st_mtime
        except FileNotFoundError:
            mtimes[path] = 0
    return mtimes


def check_config_changes(last_mtimes: dict) -> dict:
    """
    Compare current mtimes to last known mtimes.
    If any file changed, wait for idle, restart all services.
    Returns updated mtimes dict.
    """
    current_mtimes = get_mtimes()

    changed = [
        p for p in CONFIG_FILES
        if current_mtimes.get(p, 0) != last_mtimes.get(p, 0)
        and last_mtimes.get(p, 0) != 0   # ignore first run
    ]

    if changed:
        for path in changed:
            log.info(f"Config file changed: {path.name}")

        wait_for_idle()

        log.info("Restarting all services after config change…")
        for service in SERVICES:
            restart_service(service, reason=f"config changed: {', '.join(p.name for p in changed)}")

    return current_mtimes


# ── Stuck job detection ───────────────────────────────────────────────────────

def check_stuck_jobs():
    """
    Detect jobs stuck in 'running' state for too long.
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
                restart_service("transcription-worker", reason="stuck job detected")
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
    Path(LOG_FILE).touch(exist_ok=True)

    log.info(f"Watchdog started — checking every {POLL_INTERVAL}s")
    log.info(f"Monitoring services: {', '.join(SERVICES)}")
    log.info(f"Monitoring config files: {', '.join(p.name for p in CONFIG_FILES)}")
    log.info(f"Stuck job timeout: {STUCK_JOB_TIMEOUT}s ({STUCK_JOB_TIMEOUT//60} min)")

    # Record initial mtimes — changes are only detected after first poll
    last_mtimes = get_mtimes()

    while True:
        try:
            check_services()
            check_database()
            check_stuck_jobs()
            last_mtimes = check_config_changes(last_mtimes)
        except Exception as e:
            log.error(f"Unexpected error in watchdog loop: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()