"""
worker.py
─────────
Single GPU worker. Polls the SQLite queue and processes
one job at a time — single files or batch folders.

Supports job cancellation: if a running job is deleted via the GUI,
the worker detects this and cleans up GPU memory before moving on.

Start:
    python worker.py
"""

import logging
import os
import time
from pathlib import Path

import torch

from jobstore import claim_next_job, get_job, init_db, reset_stale_jobs, update_job
from settings import cfg

POLL_INTERVAL = int(os.environ.get("WORKER_POLL", str(cfg.runtime.worker_poll)))
GPU_SETTLE    = 3   # seconds to wait after each job for VRAM to settle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WORKER] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


class JobCancelledError(Exception):
    """Raised when a running job is deleted via the GUI."""
    pass


def is_cancelled(job_id: str) -> bool:
    """Return True if the job no longer exists in the database."""
    return get_job(job_id) is None


def make_progress(job_id: str):
    """Return a progress callback that raises JobCancelledError if job was deleted."""
    def progress(step: int, total: int, msg: str):
        if is_cancelled(job_id):
            raise JobCancelledError(f"Job {job_id} was cancelled via GUI")
        update_job(job_id, "running", step=step, total=total, message=msg)
    return progress


def deliver(out: Path, job: dict, filepath: str):
    """Copy the transcript to all additional output destinations."""
    import json as _json
    import shutil as _shutil

    raw = job.get("output_dirs", "[]")
    try:
        extra_dirs = _json.loads(raw) if isinstance(raw, str) else (raw or [])
    except Exception:
        extra_dirs = []

    for dest_dir in extra_dirs:
        try:
            if dest_dir == "same_as_source":
                dest = Path(filepath) / out.name if Path(filepath).is_dir() \
                       else Path(filepath).parent / out.name
            else:
                dest = Path(dest_dir) / out.name
                dest.parent.mkdir(parents=True, exist_ok=True)

            if dest.resolve() != out.resolve():
                _shutil.copy2(str(out), str(dest))
                log.info(f"Delivered to: {dest}")
        except Exception as e:
            log.error(f"Delivery failed for {dest_dir}: {e}")
    """Free all CUDA memory and wait for GPU to settle."""
    try:
        torch.cuda.empty_cache()
        torch.cuda.synchronize()
    except Exception:
        pass
    time.sleep(GPU_SETTLE)


def process(job: dict):
    job_id     = job["id"]
    filename   = job["filename"]
    filepath   = job["filepath"]
    mode       = job.get("mode", "single")
    output_dir = job.get("output_dir") or cfg.runtime.output_dir
    language   = job.get("language") or None

    log.info(f"Starting [{mode}]: {filename} [{job_id}] lang={language or 'auto'}")

    # Failsafe: clear GPU memory before every job regardless of previous state
    import gc
    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.synchronize()
    log.info(f"GPU cleared before job start")

    progress = make_progress(job_id)

    try:
        if mode == "batch":
            if not Path(filepath).is_dir():
                raise FileNotFoundError(f"Batch folder not found: {filepath}")

            if output_dir == "same_as_source":
                out_path = Path(filepath) / f"{Path(filepath).name}_transcript.txt"
            else:
                out_path = Path(output_dir) / f"{Path(filepath).name}_transcript.txt"

            out = batch_transcribe(
                folder=filepath,
                output_path=str(out_path),
                progress=progress,
            )

        else:
            if not Path(filepath).exists():
                raise FileNotFoundError(f"File not found: {filepath}")

            out_path = Path(output_dir) / f"{Path(filename).stem}_transcript.txt"
            out = transcribe(
                filepath,
                original_name=filename,
                output_path=str(out_path),
                progress=progress,
                language=language,
            )

            # Clean up staging file
            try:
                if "/staging/" in filepath:
                    Path(filepath).unlink()
            except Exception:
                pass

        # Final check: job may have been deleted while last step was running
        if is_cancelled(job_id):
            raise JobCancelledError(f"Job {job_id} was cancelled via GUI")

        # Deliver to additional output destinations
        deliver(out, job, filepath)

        update_job(job_id, "done", step=5, total=5,
                   message="Transcript complete", output=str(out))
        log.info(f"Done: {out.name}")

    except JobCancelledError:
        log.info(f"Cancelled: {filename} [{job_id}] — cleaning up GPU memory")
        # Job is already deleted from DB — nothing to update, just clean up

    except Exception as e:
        # Only update DB if job still exists (wasn't deleted mid-run)
        if not is_cancelled(job_id):
            update_job(job_id, "error", message="Failed", error=str(e))
        log.error(f"Error processing {filename}: {e}")

    finally:
        cleanup_gpu()
        log.info(f"GPU memory released after: {filename}")


def run():
    init_db()
    reset_stale_jobs()

    log.info("Worker started — polling every %ds", POLL_INTERVAL)
    log.info("Exclusive GPU access guaranteed (one job at a time)")

    while True:
        job = claim_next_job()
        if job:
            process(job)
        else:
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()