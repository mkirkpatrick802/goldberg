"""
faster-whisper wrapper. Device, model size and compute type all come from
settings so the same code runs on the Windows dev box and the Linux server.
"""

import ctypes
import importlib.util
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from notes.settings import (
    TRANSCRIBE_DEVICE,
    WHISPER_COMPUTE_TYPE,
    WHISPER_CPU_THREADS,
    WHISPER_MODEL,
)

_model = None
_libs_ready = False
# Only one transcription may touch the model at a time. Two meetings can
# finish close together — the first is still transcribing when the second
# stops — and concurrent calls would contend for the same model and VRAM.
_model_lock = threading.Lock()
_load_lock = threading.Lock()


@dataclass
class Segment:
    """One chunk of speech: seconds from the start of the file, plus the text."""
    start: float
    end: float
    text: str


def _ensure_cuda_libs() -> None:
    """
    Make the pip-installed CUDA libraries loadable, on either platform.

    The nvidia-cublas-cu12 / nvidia-cudnn-cu12 wheels drop their libraries
    inside site-packages without putting them anywhere the loader looks, so
    CTranslate2 fails to find them and model construction dies.

    Windows keeps them in nvidia/<pkg>/bin and needs the directory registered.
    Linux keeps them in nvidia/<pkg>/lib and normally needs LD_LIBRARY_PATH —
    but that's read by the dynamic linker at exec, so a process can't set it for
    itself. Loading each .so here with RTLD_GLOBAL achieves the same thing
    without requiring anything of whatever launches the bot, which matters
    because start_maple.sh runs it under nohup with no environment setup.
    """
    global _libs_ready
    if _libs_ready:
        return
    _libs_ready = True

    spec = importlib.util.find_spec("nvidia")
    if spec is None or not spec.submodule_search_locations:
        print("[Notes] WARNING: nvidia CUDA wheels not installed - CUDA will fail.")
        return

    loaded = 0
    for root in spec.submodule_search_locations:
        if sys.platform == "win32":
            for bin_dir in Path(root).glob("*/bin"):
                if any(bin_dir.glob("*.dll")):
                    os.add_dll_directory(str(bin_dir))
                    loaded += 1
        else:
            # cuBLAS first: cuDNN links against it, so the reverse order fails.
            for pattern in ("*/lib/libcublas*.so*", "*/lib/libcudnn*.so*"):
                for so in sorted(Path(root).glob(pattern)):
                    try:
                        ctypes.CDLL(str(so), mode=ctypes.RTLD_GLOBAL)
                        loaded += 1
                    except OSError as e:
                        print(f"[Notes] Could not preload {so.name}: {e}")

    if loaded == 0:
        print("[Notes] WARNING: found no CUDA libraries in the nvidia wheels.")


def get_model():
    """
    Lazily build the WhisperModel and hang onto it.

    Loading costs seconds and roughly a gig of VRAM, so building one per file
    would be painful in the CLI and unacceptable in the cog. Guarded so two
    threads can't race to build a second copy and double the VRAM.
    """
    global _model
    if _model is not None:
        return _model

    with _load_lock:
        if _model is not None:  # another thread won the race
            return _model
        return _build_model()


def _build_model():
    global _model
    _ensure_cuda_libs()

    from faster_whisper import WhisperModel  # imported late so the DLL fix runs first

    print(
        f"[Notes] Loading whisper '{WHISPER_MODEL}' on {TRANSCRIBE_DEVICE} "
        f"({WHISPER_COMPUTE_TYPE})..."
    )
    _model = WhisperModel(
        WHISPER_MODEL,
        device=TRANSCRIBE_DEVICE,
        compute_type=WHISPER_COMPUTE_TYPE,
        cpu_threads=WHISPER_CPU_THREADS,
    )
    return _model


def transcribe_file(path: Path) -> list[Segment]:
    """
    Transcribe one audio file into timestamped segments.

    vad_filter drops silence before it reaches the model, which matters a lot
    for per-speaker streams — those are mostly dead air while someone else talks.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"No such audio file: {path}")

    model = get_model()
    # segments is a lazy generator — the real work happens while consuming it,
    # so the lock has to cover consumption too, not just the transcribe() call.
    with _model_lock:
        segments, info = model.transcribe(str(path), beam_size=1, vad_filter=True)
        result = [
            Segment(start=s.start, end=s.end, text=s.text.strip())
            for s in segments
            if s.text.strip()
        ]
    print(
        f"[Notes] {path.name}: {len(result)} segments, "
        f"{info.duration:.0f}s audio, language={info.language}"
    )
    return result
