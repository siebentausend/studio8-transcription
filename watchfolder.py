"""
watchfolder.py
──────────────
Reads watchfolders.yaml and monitors all configured watchfolder sources.

Single mode: watchdog monitors a flat folder, each file → own transcript.
Batch mode:  polls a parent folder every N seconds for .done marker files
             inside card subfolders. Used for CIFS/NFS mounts that don't
             deliver reliable inotify events.

Usage:
    python watchfolder.py
"""

import logging
import os
import shutil
import time
import uuid
from fnmatch import fnmatch
from pathlib import Path

import yaml
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from jobstore import init_db, submit_job
from transcribe import SUPPORTED_EXTENSIONS

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "./watchfolders.yaml"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WATCHFOLDER] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ─── Config loader ────────────────────────────────────────────────────────────

def load_config() -> list[dict]:
    if not CONFIG_PATH.exists():
        log.warning(f"Config not found: {CONFIG_PATH} — using defaults")
        return [{
            "name":     "General Intake",
            "mode":     "single",
            "path":     "./watchfolder",
            "output":   "./output",
            "priority": 5,
            "enabled":  True,
        }]

    with open(CONFIG_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    valid = []
    for e in data.get("watchfolders", []):
        if not e.get("enabled", True):
            continue
        if e.get("mode", "single") not in ("single", "batch"):
            log.warning(f"Unknown mode '{e.get('mode')}' for '{e.get('name')}' — skipping")
            continue
        valid.append(e)
    return valid


def get_batch_config(folder_path: str) -> dict | None:
    """Find the batch config entry whose path is a parent of folder_path."""
    folder = Path(folder_path).resolve()
    for entry in load_config():
        if entry.get("mode") != "batch":
            continue
        base = Path(entry["path"]).resolve()
        try:
            folder.relative_to(base)
            if fnmatch(folder.name, entry.get("subfolder_glob", "*")):
                return entry
        except ValueError:
            continue
    return None


# ─── File settle tracking ─────────────────────────────────────────────────────

class PendingFiles:
    def __init__(self, settle_time: int = 5):
        self._files: dict[str, float] = {}
        self._settle = settle_time

    def touch(self, path: str):
        self._files[path] = time.time()

    def settled(self) -> list[str]:
        now   = time.time()
        ready = [p for p, t in self._files.items() if now - t >= self._settle]
        for p in ready:
            del self._files[p]
        return ready


# ─── Single-mode handler (watchdog) ──────────────────────────────────────────

class SingleModeHandler(FileSystemEventHandler):
    """Watches a flat folder and enqueues individual media files."""

    def __init__(self, config: dict):
        self.config   = config
        self.pending  = PendingFiles(int(os.environ.get("SETTLE_TIME", "5")))
        self.submitted: set[str] = set()
        self._staging = Path(os.environ.get("OUTPUT_DIR", "./output")) / "staging"

    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._handle(event.dest_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._handle(event.src_path)

    def _handle(self, path: str):
        if Path(path).suffix.lower() not in SUPPORTED_EXTENSIONS:
            return
        if path in self.submitted:
            return
        log.info(f"[{self.config['name']}] New file: {Path(path).name}")
        self.pending.touch(path)

    def process_settled(self):
        for path in self.pending.settled():
            self._enqueue(path)

    def _enqueue(self, path: str):
        if path in self.submitted:
            return

        src    = Path(path)
        name   = src.name

        # Resolve output directory first so we can check for existing transcript
        raw_output = self.config.get("output", os.environ.get("OUTPUT_DIR", "./output"))
        if raw_output == "same_as_source":
            output_dir = str(src.parent)
        else:
            output_dir = raw_output

        # Skip if transcript already exists — avoids re-processing after restart
        expected_transcript = Path(output_dir) / f"{src.stem}_transcript.txt"
        if expected_transcript.exists():
            self.submitted.add(path)
            log.info(f"[{self.config['name']}] Skipping (transcript exists): {name}")
            return

        self.submitted.add(path)

        job_id = str(uuid.uuid4())[:8]
        self._staging.mkdir(parents=True, exist_ok=True)
        staged = self._staging / f"{job_id}_{name}"
        shutil.copy2(str(src), str(staged))

        priority = self.config.get("priority", 5)
        language = self.config.get("language") or None

        submit_job(
            job_id,
            filename=name,
            filepath=str(staged),
            source="watchfolder",
            priority=priority,
            output_dir=output_dir,
            language=language,
        )
        log.info(f"[{self.config['name']}] Queued: {name} [{job_id}] (priority {priority}, lang={language or 'auto'}, output={output_dir})")

    def scan_existing(self):
        watch_path = Path(self.config["path"])
        if not watch_path.exists():
            return
        files = [
            str(p) for p in watch_path.iterdir()
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        if files:
            log.info(f"[{self.config['name']}] {len(files)} existing file(s) found")
            for f in files:
                self._enqueue(f)


# ─── Batch-mode poller (polling) ──────────────────────────────────────────────

class BatchModePoller:
    """
    Polls a parent folder every poll_interval seconds for .done marker files.
    Designed for CIFS/NFS mounts that don't deliver inotify events.

    Expected structure:
        <path>/
        └── 20240101_ProjectName_Card01/
            ├── clip001.mxf
            ├── clip002.mxf
            └── 20240101_ProjectName_Card01.done   ← trigger
    """

    def __init__(self, config: dict, poll_interval: int = 10):
        self.config        = config
        self.done_ext      = config.get("done_extension", ".done")
        self.delete_done   = config.get("delete_done_file", True)
        self.poll_interval = poll_interval
        self.submitted: set[str] = set()

    def scan(self):
        watch_path = Path(self.config["path"])
        if not watch_path.exists():
            log.warning(f"[{self.config['name']}] Path not reachable: {watch_path}")
            return

        for done_file in watch_path.rglob(f"*{self.done_ext}"):
            key = str(done_file.resolve())
            if key in self.submitted:
                continue
            self._enqueue_batch(done_file)

    def _enqueue_batch(self, done_file: Path):
        key         = str(done_file.resolve())
        card_folder = done_file.parent      # .done lives inside the card folder
        folder_name = card_folder.name

        if not card_folder.is_dir():
            log.warning(
                f"[{self.config['name']}] Marker '{done_file.name}' found "
                f"but folder '{card_folder}' does not exist — skipping"
            )
            self.submitted.add(key)
            return

        media_files = [
            p for p in card_folder.iterdir()
            if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        if not media_files:
            log.warning(
                f"[{self.config['name']}] No media files in '{folder_name}' — skipping"
            )
            self.submitted.add(key)
            return

        self.submitted.add(key)

        job_id     = str(uuid.uuid4())[:8]
        priority   = self.config.get("priority", 7)
        language   = self.config.get("language") or None

        # Support both 'output' (singular) and 'outputs' (list)
        raw_outputs = self.config.get("outputs") or []
        if not raw_outputs and self.config.get("output"):
            raw_outputs = [self.config["output"]]
        if not raw_outputs:
            raw_outputs = [os.environ.get("OUTPUT_DIR", "./output")]

        # Primary output_dir is the first entry
        primary_output = raw_outputs[0]
        extra_outputs  = raw_outputs[1:]

        submit_job(
            job_id,
            filename=folder_name,
            filepath=str(card_folder),
            source="watchfolder",
            priority=priority,
            mode="batch",
            output_dir=primary_output,
            output_dirs=extra_outputs,
            language=language,
        )

        log.info(
            f"[{self.config['name']}] Batch queued: '{folder_name}' "
            f"({len(media_files)} clips) [{job_id}] "
            f"(priority {priority}, lang={language or 'auto'}, "
            f"outputs={len(raw_outputs)})"
        )

        if self.delete_done:
            # Wait until the file is no longer locked (IN2IT may still have it open)
            # Try up to 10 times with 1 second between attempts
            removed = False
            for attempt in range(10):
                try:
                    done_processed = done_file.with_suffix(".done.processed")
                    done_file.rename(done_processed)
                    done_processed.unlink()
                    log.info(f"[{self.config['name']}] Removed marker: {done_file.name}")
                    removed = True
                    break
                except OSError as e:
                    import errno
                    if e.errno == errno.EBUSY or e.errno == errno.EACCES:
                        log.debug(
                            f"[{self.config['name']}] Marker still locked, "
                            f"retrying in 1s (attempt {attempt+1}/10)"
                        )
                        time.sleep(1)
                    else:
                        log.warning(f"[{self.config['name']}] Could not remove marker: {e}")
                        break
            if not removed:
                log.warning(
                    f"[{self.config['name']}] Marker still locked after 10s — "
                    f"will be ignored on next poll (already in submitted set)"
                )


# ─── Single-mode poller (polling) ────────────────────────────────────────────

class SingleModePoller:
    """
    Polls a flat folder every poll_interval seconds for new media files.
    Used instead of watchdog for CIFS/NFS mounts that don't deliver inotify events.

    Configure per entry in watchfolders.yaml:
        poll: true
        poll_interval: 10   # optional, defaults to BATCH_POLL_INTERVAL env var
    """

    def __init__(self, config: dict, poll_interval: int = 10):
        self.config        = config
        self.poll_interval = config.get("poll_interval", poll_interval)
        self.submitted: set[str] = set()
        self._staging = Path(os.environ.get("OUTPUT_DIR", "./output")) / "staging"

    def scan(self):
        watch_path = Path(self.config["path"])
        if not watch_path.exists():
            log.warning(f"[{self.config['name']}] Path not reachable: {watch_path}")
            return

        for p in watch_path.iterdir():
            if not p.is_file():
                continue
            if p.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            if str(p) in self.submitted:
                continue
            self._enqueue(str(p))

    def _enqueue(self, path: str):
        src  = Path(path)
        name = src.name

        raw_output = self.config.get("output", os.environ.get("OUTPUT_DIR", "./output"))
        if raw_output == "same_as_source":
            output_dir = str(src.parent)
        else:
            output_dir = raw_output

        # Skip if transcript already exists
        expected_transcript = Path(output_dir) / f"{src.stem}_transcript.txt"
        if expected_transcript.exists():
            self.submitted.add(path)
            log.info(f"[{self.config['name']}] Skipping (transcript exists): {name}")
            return

        self.submitted.add(path)

        job_id = str(uuid.uuid4())[:8]
        self._staging.mkdir(parents=True, exist_ok=True)
        staged = self._staging / f"{job_id}_{name}"
        shutil.copy2(str(src), str(staged))

        priority = self.config.get("priority", 5)
        language = self.config.get("language") or None

        submit_job(
            job_id,
            filename=name,
            filepath=str(staged),
            source="watchfolder",
            priority=priority,
            output_dir=output_dir,
            language=language,
        )
        log.info(
            f"[{self.config['name']}] Queued: {name} [{job_id}] "
            f"(priority {priority}, lang={language or 'auto'}, output={output_dir})"
        )


# ─── Main loop ────────────────────────────────────────────────────────────────

def run():
    init_db()
    config_entries = load_config()

    single_entries = [e for e in config_entries if e.get("mode", "single") == "single"]
    batch_entries  = [e for e in config_entries if e.get("mode") == "batch"]
    poll_interval  = int(os.environ.get("BATCH_POLL_INTERVAL", "10"))

    log.info(
        f"Loaded {len(single_entries)} single-mode, "
        f"{len(batch_entries)} batch-mode entries"
    )

    # Single-mode: watchdog (inotify) or polling depending on config
    observer        = Observer()
    single_handlers = []   # watchdog-based
    single_pollers  = []   # polling-based

    for entry in single_entries:
        watch_path = Path(entry["path"])

        if entry.get("poll", False):
            # Polling mode — for CIFS/NFS mounts
            poller = SingleModePoller(entry, poll_interval=poll_interval)
            poller.scan()   # check for existing files at startup
            single_pollers.append(poller)
            log.info(
                f"  Single (poll): '{entry['name']}' → {watch_path} "
                f"(every {poller.poll_interval}s)"
            )
        else:
            # Watchdog mode — for local filesystems
            if not watch_path.exists():
                watch_path.mkdir(parents=True, exist_ok=True)
            handler = SingleModeHandler(entry)
            handler.scan_existing()
            observer.schedule(handler, str(watch_path), recursive=False)
            single_handlers.append(handler)
            log.info(f"  Single (watch): '{entry['name']}' → {watch_path}")

    # Batch-mode: always polling (CIFS doesn't deliver inotify events)
    batch_pollers = []

    for entry in batch_entries:
        poller = BatchModePoller(entry, poll_interval=poll_interval)
        poller.scan()
        batch_pollers.append(poller)
        done_ext = entry.get("done_extension", ".done")
        log.info(
            f"  Batch: '{entry['name']}' → {entry['path']} "
            f"(polling every {poll_interval}s, trigger: *{done_ext})"
        )

    observer.start()
    log.info("Watchfolder running (Ctrl+C to stop)")

    tick = 0
    try:
        while True:
            time.sleep(1)
            tick += 1

            # Watchdog-based single handlers: process settled files
            for h in single_handlers:
                h.process_settled()

            # Polling-based handlers: scan on interval
            if tick % poll_interval == 0:
                for p in single_pollers:
                    p.scan()
                for p in batch_pollers:
                    p.scan()

    except KeyboardInterrupt:
        log.info("Watchfolder shutting down…")
        observer.stop()
    observer.join()


if __name__ == "__main__":
    run()
