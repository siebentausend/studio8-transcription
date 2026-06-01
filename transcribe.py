"""
transcribe.py
─────────────
Core pipeline: ffprobe → WhisperX → Diarization → Timecode → TXT

Dependencies:
    pip install whisperx ffmpeg-python
    Hugging Face token with access to pyannote/speaker-diarization-3.1
"""

import gc
import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path

import torch
import whisperx
from pyannote.audio import Pipeline

from settings import cfg

# ─── Configuration ────────────────────────────────────────────────────────────

HF_TOKEN     = os.environ.get("HF_TOKEN", "")
DEVICE       = os.environ.get("DEVICE", cfg.model.device)
COMPUTE_TYPE = cfg.model.compute_type if cfg.model.compute_type else ("float16" if DEVICE == "cuda" else "int8")
MODEL_SIZE   = os.environ.get("WHISPER_MODEL", cfg.model.whisper_model)
LANGUAGE     = os.environ.get("LANGUAGE", cfg.model.default_language)
OUTPUT_DIR   = Path(cfg.runtime.output_dir)

SUPPORTED_EXTENSIONS = {
    ".mp3", ".mp4", ".wav", ".m4a", ".aac", ".flac", ".ogg",
    ".mxf", ".mov", ".mts", ".m2ts", ".avi", ".mkv", ".webm",
}

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ─── Timecode helpers ─────────────────────────────────────────────────────────

def _ffprobe_meta(filepath: str) -> dict:
    """Read metadata via ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_entries",
        "format_tags=timecode,creation_time:stream_tags=timecode:format=duration",
        filepath,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return json.loads(result.stdout)
    except Exception:
        return {}


def _parse_timecode(tc_str: str) -> tuple[int, int, int, int] | None:
    """
    Parse timecode strings in various formats:
      HH:MM:SS:FF  (SMPTE)
      HH:MM:SS;FF  (drop-frame)
      HH:MM:SS
    Returns (h, m, s, frames) or None.
    """
    if not tc_str:
        return None
    m = re.match(r"(\d{2}):(\d{2}):(\d{2})[:;](\d{2,3})$", tc_str.strip())
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
    m = re.match(r"(\d{2}):(\d{2}):(\d{2})$", tc_str.strip())
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3)), 0
    return None


def _detect_fps(meta: dict) -> float:
    """Detect FPS from ffprobe metadata, fallback to 25."""
    try:
        for s in meta.get("streams", []):
            r = s.get("r_frame_rate", "")
            if r and r != "0/0":
                return float(Fraction(r))
    except Exception:
        pass
    return 25.0


def _tc_to_seconds(tc: tuple, fps: float) -> float:
    h, m, s, f = tc
    return h * 3600 + m * 60 + s + f / fps


def _seconds_to_smpte(total_seconds: float, fps: float) -> str:
    total_frames = round(total_seconds * fps)
    fps_int = round(fps)
    frames   = total_frames % fps_int
    total_sec = total_frames // fps_int
    s = total_sec % 60
    total_min = total_sec // 60
    m = total_min % 60
    h = total_min // 60
    return f"{h:02d}:{m:02d}:{s:02d}:{frames:02d}"


def _seconds_to_hhmmss(total_seconds: float) -> str:
    td = timedelta(seconds=round(total_seconds))
    total = int(td.total_seconds())
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def extract_timecode_info(filepath: str) -> dict:
    """
    Read timecode and FPS from file metadata.
    Returns dict with:
      - start_seconds: float
      - fps: float
      - tc_string: str or None
      - has_timecode: bool
    """
    meta = _ffprobe_meta(filepath)
    fps  = _detect_fps(meta)

    tc_str = None
    for location in [
        meta.get("format", {}).get("tags", {}),
        *[s.get("tags", {}) for s in meta.get("streams", [])],
    ]:
        tc_str = location.get("timecode") or location.get("TIMECODE")
        if tc_str:
            break

    tc = _parse_timecode(tc_str) if tc_str else None
    start_seconds = _tc_to_seconds(tc, fps) if tc else 0.0

    return {
        "start_seconds": start_seconds,
        "fps": fps,
        "tc_string": tc_str,
        "has_timecode": tc is not None,
    }


def format_timestamp(relative_seconds: float, tc_info: dict) -> str:
    absolute = tc_info["start_seconds"] + relative_seconds
    if tc_info["has_timecode"]:
        return _seconds_to_smpte(absolute, tc_info["fps"])
    return _seconds_to_hhmmss(absolute)


# ─── Transcription pipeline ───────────────────────────────────────────────────

def transcribe(
    filepath: str,
    original_name: str | None = None,
    output_path: str | None = None,
    progress=None,
    language: str | None = None,
) -> Path:
    """
    Transcribe an audio/video file and write a TXT transcript.

    Args:
        filepath:      Path to the input file (may be a temp file)
        original_name: Original filename to use for the output TXT name
        output_path:   Optional explicit output path
        progress:      Optional callback(step, total, message)
        language:      Optional language code (e.g. 'en', 'de', 'es').
                       None = auto-detect.
    """
    def report(step: int, msg: str):
        print(f"[{step}/5] {msg}")
        if progress:
            progress(step, 5, msg)

    filepath   = str(filepath)
    input_path = Path(filepath)

    # Use job-level language if provided, otherwise fall back to env var
    lang = language or LANGUAGE

    # Use original_name for output naming if provided
    name_stem = Path(original_name).stem if original_name else input_path.stem

    report(1, f"Reading timecode metadata: {original_name or input_path.name}")
    tc_info = extract_timecode_info(filepath)
    if tc_info["has_timecode"]:
        print(f"      Timecode found: {tc_info['tc_string']} @ {tc_info['fps']:.2f} fps")
    else:
        print("      No timecode found — using 00:00:00 as fallback")

    report(2, f"Loading Whisper model ({MODEL_SIZE}, {DEVICE})")
    model = whisperx.load_model(
        MODEL_SIZE, DEVICE, compute_type=COMPUTE_TYPE, language=lang
    )

    report(3, "Transcribing…")
    audio  = whisperx.load_audio(filepath)

    # First pass: detect language if not explicitly set
    if not lang:
        detect_result     = model.transcribe(audio, batch_size=2)
        detected_language = detect_result.get("language", "unknown")
        print(f"      Language detected: {detected_language}")
        result = model.transcribe(audio, batch_size=cfg.model.batch_size,
                                  language=detected_language, task="transcribe")
    else:
        detected_language = lang
        result = model.transcribe(audio, batch_size=cfg.model.batch_size,
                                  language=lang, task="transcribe")
        print(f"      Language forced: {detected_language}")

    # Unload Whisper immediately — frees ~6 GB VRAM
    del model
    gc.collect()
    torch.cuda.empty_cache()

    report(4, "Word alignment…")
    model_a, metadata = whisperx.load_align_model(
        language_code=detected_language, device=DEVICE
    )
    result = whisperx.align(
        result["segments"], model_a, metadata, audio, DEVICE,
        return_char_alignments=False
    )
    del model_a
    gc.collect()
    torch.cuda.empty_cache()

    report(5, "Speaker diarization…")
    if not HF_TOKEN:
        print("      WARNING: No HF_TOKEN set — diarization skipped.")
        segments_with_speakers = result["segments"]
        for seg in segments_with_speakers:
            seg["speaker"] = cfg.transcript.fallback_speaker
    else:
        import pandas as pd
        diarize_model = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            token=HF_TOKEN
        )
        diarize_model.to(torch.device(DEVICE))
        waveform = torch.from_numpy(audio).unsqueeze(0)
        diarize_segments = diarize_model({
            "waveform": waveform,
            "sample_rate": 16000
        })
        diarize_df = pd.DataFrame([
            {"start": turn.start, "end": turn.end, "speaker": speaker}
            for turn, _, speaker in diarize_segments.speaker_diarization.itertracks(yield_label=True)
        ])
        result = whisperx.assign_word_speakers(diarize_df, result)
        segments_with_speakers = result["segments"]
        del diarize_model, waveform, diarize_segments
        gc.collect()
        torch.cuda.empty_cache()

    del audio, result

    # ─── Build TXT ────────────────────────────────────────────────────────────

    duration_sec = float(
        subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", filepath],
            capture_output=True, text=True
        ).stdout.strip() or "0"
    )
    duration_str = _seconds_to_hhmmss(duration_sec)
    date_str     = datetime.now().strftime("%Y-%m-%d")
    t            = cfg.transcript

    if tc_info["has_timecode"]:
        tc_header = f"{t.timecode_label}: {tc_info['tc_string']} ({tc_info['fps']:.2f} fps)"
    else:
        tc_header = f"{t.timecode_label}: 00:00:00 (no TC in file)"

    lines = [
        f"{t.date_label}:     {date_str}",
        f"{t.file_label}:     {original_name or input_path.name}",
        f"{t.duration_label}: {duration_str}",
        f"{t.language_label}: {detected_language}",
        tc_header,
        t.separator,
        "",
    ]

    # Group consecutive segments from the same speaker.
    # Force a new timecode marker every TC_INTERVAL seconds for single speaker.
    TC_INTERVAL = cfg.model.tc_interval

    current_speaker    = None
    current_start      = None
    last_tc_at         = None
    current_text_parts = []

    def flush_segment():
        if current_speaker and current_text_parts:
            tc = format_timestamp(current_start, tc_info)
            lines.append(f"[{tc}] {current_speaker}")
            lines.append(" ".join(current_text_parts).strip())
            lines.append("")

    for seg in segments_with_speakers:
        speaker = seg.get("speaker", "SPEAKER_?").upper().replace(" ", "_")
        text    = seg.get("text", "").strip()
        start   = seg.get("start", 0.0)

        if not text:
            continue

        if speaker != current_speaker:
            flush_segment()
            current_speaker    = speaker
            current_start      = start
            last_tc_at         = start
            current_text_parts = [text]
        elif last_tc_at is not None and (start - last_tc_at) >= TC_INTERVAL:
            flush_segment()
            current_start      = start
            last_tc_at         = start
            current_text_parts = [text]
        else:
            current_text_parts.append(text)

    flush_segment()

    # ─── Write file ───────────────────────────────────────────────────────────

    if output_path is None:
        out_file = OUTPUT_DIR / f"{name_stem}_transcript.txt"
    else:
        out_file = Path(output_path)

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text("\n".join(lines), encoding="utf-8")

    print(f"\n✓ Transcript saved: {out_file}")
    return out_file


# ─── Batch transcription pipeline ────────────────────────────────────────────

def _transcribe_single_for_batch(
    filepath: str,
    model,
    diarize_model,
    progress_label: str = "",
    progress=None
) -> tuple[list, dict, str, float]:
    """
    Transcribe one file using pre-loaded models.
    Returns (segments_with_speakers, tc_info, detected_language, duration_sec).
    """
    def report(msg: str):
        print(f"    {msg}")
        if progress:
            progress(msg)

    report(f"Timecode: {Path(filepath).name}")
    tc_info = extract_timecode_info(filepath)

    report(f"Transcribing: {Path(filepath).name}")
    audio  = whisperx.load_audio(filepath)
    result = model.transcribe(audio, batch_size=2)
    torch.cuda.empty_cache()
    detected_language = result.get("language", "unknown")

    report("Aligning…")
    model_a, metadata = whisperx.load_align_model(
        language_code=detected_language, device=DEVICE
    )
    result = whisperx.align(
        result["segments"], model_a, metadata, audio, DEVICE,
        return_char_alignments=False
    )
    del model_a
    torch.cuda.empty_cache()

    report("Diarizing…")
    if diarize_model is None:
        segments = result["segments"]
        for seg in segments:
            seg["speaker"] = "SPEAKER_A"
    else:
        import pandas as pd
        waveform = torch.from_numpy(audio).unsqueeze(0)
        diarize_segments = diarize_model({
            "waveform": waveform,
            "sample_rate": 16000
        })
        diarize_df = pd.DataFrame([
            {"start": turn.start, "end": turn.end, "speaker": speaker}
            for turn, _, speaker in diarize_segments.speaker_diarization.itertracks(yield_label=True)
        ])
        result = whisperx.assign_word_speakers(diarize_df, result)
        segments = result["segments"]

    del audio, result
    torch.cuda.empty_cache()

    duration_sec = float(
        subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", filepath],
            capture_output=True, text=True
        ).stdout.strip() or "0"
    )

    return segments, tc_info, detected_language, duration_sec


def batch_transcribe(
    folder: str,
    output_path: str | None = None,
    progress=None
) -> Path:
    """
    Transcribe all media files in a folder into one combined TXT transcript.
    Files are sorted alphabetically by filename.
    Models are loaded and unloaded per clip to stay within VRAM limits.
    """
    folder_path = Path(folder)
    folder_name = folder_path.name

    def report(step: int, total: int, msg: str):
        print(f"[{step}/{total}] {msg}")
        if progress:
            progress(step, total, msg)

    # Collect and sort files alphabetically
    files = sorted([
        p for p in folder_path.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    ], key=lambda p: p.name)

    if not files:
        raise ValueError(f"No supported media files found in {folder}")

    total_steps = len(files) + 1
    report(1, total_steps, f"Found {len(files)} file(s) — starting")

    # ─── Build combined TXT ───────────────────────────────────────────────────

    date_str   = datetime.now().strftime("%Y-%m-%d")
    total_dur  = 0.0
    all_languages: list[str] = []
    t = cfg.transcript

    lines = [
        f"{t.date_label}:     {date_str}",
        f"Folder:   {folder_name}",
        f"Files:    {len(files)} clips (alphabetical)",
        f"{t.duration_label}: —",
        f"{t.language_label}: —",
        t.separator,
        "",
    ]

    TC_INTERVAL = cfg.model.tc_interval

    for i, filepath in enumerate(files):
        step = i + 2
        report(step, total_steps, f"Processing {filepath.name} ({i+1}/{len(files)})")

        try:
            # ── Step 1: Transcribe ────────────────────────────────────────────
            print(f"    Timecode: {filepath.name}")
            tc_info = extract_timecode_info(str(filepath))

            print(f"    Loading Whisper model…")
            model  = whisperx.load_model(
                MODEL_SIZE, DEVICE, compute_type=COMPUTE_TYPE, language=LANGUAGE
            )
            audio  = whisperx.load_audio(str(filepath))

            # Detect language first if not set, then transcribe explicitly
            # with task="transcribe" to prevent silent translation
            if not LANGUAGE:
                detect_result     = model.transcribe(audio, batch_size=2)
                detected_language = detect_result.get("language", "unknown")
                print(f"    Language detected: {detected_language}")
                result = model.transcribe(audio, batch_size=cfg.model.batch_size,
                                          language=detected_language, task="transcribe")
            else:
                detected_language = LANGUAGE
                result = model.transcribe(audio, batch_size=cfg.model.batch_size,
                                          language=LANGUAGE, task="transcribe")
                print(f"    Language forced: {detected_language}")
            del model
            torch.cuda.empty_cache()
            print(f"    Transcription done — Whisper unloaded")

            # ── Step 2: Align ─────────────────────────────────────────────────
            print(f"    Aligning…")
            model_a, metadata = whisperx.load_align_model(
                language_code=detected_language, device=DEVICE
            )
            result = whisperx.align(
                result["segments"], model_a, metadata, audio, DEVICE,
                return_char_alignments=False
            )
            del model_a
            torch.cuda.empty_cache()
            print(f"    Alignment done — model unloaded")

            # ── Step 3: Diarize ───────────────────────────────────────────────
            print(f"    Diarizing…")
            if not HF_TOKEN:
                segments = result["segments"]
                for seg in segments:
                    seg["speaker"] = cfg.transcript.fallback_speaker
            else:
                import pandas as pd
                diarize_model = Pipeline.from_pretrained(
                    "pyannote/speaker-diarization-3.1",
                    token=HF_TOKEN
                )
                diarize_model.to(torch.device(DEVICE))
                waveform = torch.from_numpy(audio).unsqueeze(0)
                diarize_segments = diarize_model({
                    "waveform": waveform,
                    "sample_rate": 16000
                })
                diarize_df = pd.DataFrame([
                    {"start": turn.start, "end": turn.end, "speaker": speaker}
                    for turn, _, speaker in
                    diarize_segments.speaker_diarization.itertracks(yield_label=True)
                ])
                result   = whisperx.assign_word_speakers(diarize_df, result)
                segments = result["segments"]
                del diarize_model
                torch.cuda.empty_cache()
                print(f"    Diarization done — model unloaded")

            del audio, result
            torch.cuda.empty_cache()

        except Exception as e:
            print(f"    ERROR: {filepath.name}: {e}")
            torch.cuda.empty_cache()
            sep_label = f"── {filepath.name} "
            lines.append(sep_label + "─" * max(0, 44 - len(sep_label)))
            lines.append(f"[ERROR] {e}")
            lines.append("")
            continue

        duration_sec = float(
            subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(filepath)],
                capture_output=True, text=True
            ).stdout.strip() or "0"
        )
        total_dur += duration_sec
        all_languages.append(detected_language)

        # File separator
        sep_label = f"── {filepath.name} "
        lines.append(sep_label + "─" * max(0, 44 - len(sep_label)))
        lines.append("")

        # Segments
        current_speaker    = None
        current_start      = None
        last_tc_at         = None
        current_text_parts = []

        def flush_segment():
            if current_speaker and current_text_parts:
                tc = format_timestamp(current_start, tc_info)
                lines.append(f"[{tc}] {current_speaker}")
                lines.append(" ".join(current_text_parts).strip())
                lines.append("")

        for seg in segments:
            speaker = seg.get("speaker", "SPEAKER_?").upper().replace(" ", "_")
            text    = seg.get("text", "").strip()
            start   = seg.get("start", 0.0)
            if not text:
                continue
            if speaker != current_speaker:
                flush_segment()
                current_speaker    = speaker
                current_start      = start
                last_tc_at         = start
                current_text_parts = [text]
            elif last_tc_at is not None and (start - last_tc_at) >= TC_INTERVAL:
                flush_segment()
                current_start      = start
                last_tc_at         = start
                current_text_parts = [text]
            else:
                current_text_parts.append(text)

        flush_segment()

    # Fill in totals
    dominant_lang = max(set(all_languages), key=all_languages.count) if all_languages else "unknown"
    lines[3] = f"{t.duration_label}: {_seconds_to_hhmmss(total_dur)} (total)"
    lines[4] = f"{t.language_label}: {dominant_lang}"

    # Write file
    if output_path is None:
        out_dir  = Path(cfg.runtime.output_dir)
        out_file = out_dir / f"{folder_name}_transcript.txt"
    else:
        out_file = Path(output_path)

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text("\n".join(lines), encoding="utf-8")

    print(f"\n✓ Batch transcript saved: {out_file}")
    return out_file


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python transcribe.py <file> [output.txt]")
        sys.exit(1)
    out = sys.argv[2] if len(sys.argv) > 2 else None
    transcribe(sys.argv[1], original_name=sys.argv[1], output_path=out)
