"""
settings.py
───────────
Loads config.yaml and provides typed access to all settings.
All other modules import from here instead of reading config directly.

Usage:
    from settings import cfg
    print(cfg.branding.organization)
    print(cfg.colors.bg)
    print(cfg.model.whisper_model)
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "./config.yaml"))


@dataclass
class Branding:
    organization: str = "Studio 8, Washington"
    app_name: str = "Transcription"
    footer: str = "WhisperX · pyannote · Studio 8, Washington"
    system_subtitle: str = "System Status"
    queue_subtitle: str = "Queue"


@dataclass
class Colors:
    bg: str = "#080808"
    surface: str = "#111111"
    surface2: str = "#1a1a1a"
    border: str = "#333333"
    border2: str = "#484848"
    text: str = "#f0f0f0"
    muted: str = "#888888"
    green: str = "#39d98a"
    amber: str = "#ffcc00"
    red: str = "#ff5555"
    blue: str = "#58a6ff"

    def as_css_vars(self) -> str:
        """Return CSS custom properties block."""
        return f"""
    --bg:       {self.bg};
    --surface:  {self.surface};
    --surface2: {self.surface2};
    --border:   {self.border};
    --border2:  {self.border2};
    --text:     {self.text};
    --muted:    {self.muted};
    --green:    {self.green};
    --amber:    {self.amber};
    --red:      {self.red};
    --blue:     {self.blue};
    --mono:     'IBM Plex Mono', monospace;
    --sans:     'IBM Plex Sans', sans-serif;""".strip()


@dataclass
class Model:
    whisper_model: str = "large-v3"
    device: str = "cuda"
    compute_type: str = "float16"
    default_language: Optional[str] = None
    upload_languages: list = field(default_factory=lambda: [
        ["en", "English"],
        ["de", "Deutsch"],
        ["es", "Español"],
    ])
    batch_size: int = 2
    tc_interval: float = 60.0


@dataclass
class Runtime:
    output_dir: str = "/opt/transcription/output"
    settle_time: int = 5
    batch_poll_interval: int = 10
    worker_poll: int = 3
    update_branch: str = "main"


@dataclass
class Watchdog:
    poll_interval: int = 30
    stuck_job_timeout: int = 3600


@dataclass
class Priority:
    manual_upload: int = 10
    watchfolder_default: int = 5
    max_retries: int = 3


@dataclass
class Transcript:
    date_label: str = "Date"
    file_label: str = "File"
    duration_label: str = "Duration"
    language_label: str = "Language"
    timecode_label: str = "Timecode start"
    separator_char: str = "─"
    separator_length: int = 44
    fallback_speaker: str = "SPEAKER_A"

    @property
    def separator(self) -> str:
        return self.separator_char * self.separator_length


@dataclass
class Config:
    version: str = "0.9.2-beta"
    branding: Branding = field(default_factory=Branding)
    colors: Colors = field(default_factory=Colors)
    model: Model = field(default_factory=Model)
    runtime: Runtime = field(default_factory=Runtime)
    watchdog: Watchdog = field(default_factory=Watchdog)
    priority: Priority = field(default_factory=Priority)
    transcript: Transcript = field(default_factory=Transcript)


def _merge(dataclass_instance, data: dict):
    """Recursively merge dict values into a dataclass instance."""
    for key, value in data.items():
        if hasattr(dataclass_instance, key):
            attr = getattr(dataclass_instance, key)
            if hasattr(attr, '__dataclass_fields__') and isinstance(value, dict):
                _merge(attr, value)
            else:
                setattr(dataclass_instance, key, value)


def load_config() -> Config:
    config = Config()
    if not CONFIG_PATH.exists():
        return config
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if "version" in data:
            config.version = str(data["version"])
        if "branding" in data:
            _merge(config.branding, data["branding"])
        if "colors" in data:
            _merge(config.colors, data["colors"])
        if "model" in data:
            _merge(config.model, data["model"])
        if "runtime" in data:
            _merge(config.runtime, data["runtime"])
        if "watchdog" in data:
            _merge(config.watchdog, data["watchdog"])
        if "priority" in data:
            _merge(config.priority, data["priority"])
        if "transcript" in data:
            _merge(config.transcript, data["transcript"])
    except Exception as e:
        print(f"[settings] Warning: could not load {CONFIG_PATH}: {e} — using defaults")
    return config


# Singleton — imported by all other modules
cfg = load_config()
