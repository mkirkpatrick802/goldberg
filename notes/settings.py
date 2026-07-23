"""
Settings for the notes pipeline.

Separate from config.py on purpose. config.py requires BOT_TOKEN, SERVER_ID,
the Taiga credentials and more at import time, so importing it on a dev machine
that only has an Anthropic key blows up before anything runs. This module reads
the same .env file but only insists on the one key the pipeline actually needs,
which is what lets the CLI harness run anywhere.
"""

import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv

# utf-8-sig, not utf-8: Windows editors happily save .env with a BOM, and that
# invisible byte gets glued onto the first key name — ANTHROPIC_API_KEY becomes
# ﻿ANTHROPIC_API_KEY and every lookup for it silently returns None.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", encoding="utf-8-sig")


def require_api_key() -> str:
    """
    Fetch the Anthropic key, failing loudly if it's missing.

    Checked here rather than at import time so transcription — which needs no
    key at all — still works on a machine that hasn't been given one yet.
    """
    val = os.getenv("ANTHROPIC_API_KEY")
    if not val:
        raise RuntimeError(
            "Missing required environment variable: ANTHROPIC_API_KEY "
            "(add it to goldberg/.env — see .env.example)"
        )
    return val


# Anthropic
ANTHROPIC_MODEL         = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")

# Transcription. Both machines run CUDA; only the compute type differs in
# practice (the server's Pascal card has crippled FP16, so int8 is the default
# that works everywhere). Never hardcode these — they're the only things that
# differ between the Windows dev box and the Linux server.
TRANSCRIBE_DEVICE       = os.getenv("TRANSCRIBE_DEVICE", "cuda")
WHISPER_MODEL           = os.getenv("WHISPER_MODEL", "small")
WHISPER_COMPUTE_TYPE    = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_CPU_THREADS     = int(os.getenv("WHISPER_CPU_THREADS", "4"))

# Where per-speaker audio gets staged. Blank means a goldberg_notes folder in
# the platform temp dir — no hardcoded /tmp, since this also runs on Windows.
NOTES_TMP_DIR           = Path(
    os.getenv("NOTES_TMP_DIR") or Path(tempfile.gettempdir()) / "goldberg_notes"
)
