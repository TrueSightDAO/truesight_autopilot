"""Text-to-speech synthesis for autopilot voice responses.

Uses edge-tts (Microsoft Edge TTS) — free, no API key, no subscription.
Outputs MP3 files to /tmp/voice_responses/ with auto-cleanup.

Designated voices per language:
  - English (default): en-US-AriaNeural (Aria) — Positive, Confident
  - Mandarin (Chinese): zh-CN-XiaoxiaoNeural (Xiaoxiao) — Warm, Natural
  - Portuguese (Brazilian): pt-BR-FranciscaNeural (Francisca) — Friendly, Positive
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

logger = logging.getLogger("autopilot.voice_output")

# ── Language → voice mapping ───────────────────────────────────────────────

VOICE_MAP: dict[str, str] = {
    "en": "en-US-AriaNeural",       # English — Positive, Confident
    "zh": "zh-CN-XiaoxiaoNeural",   # Mandarin — Warm, Natural
    "pt": "pt-BR-FranciscaNeural",  # Portuguese — Friendly, Positive
}

# Output directory for generated voice files
_OUTPUT_DIR = Path("/tmp/voice_responses")
_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Max age for cleanup (seconds)
_CLEANUP_MAX_AGE = 3600  # 1 hour

# ── Language detection ─────────────────────────────────────────────────────

# Range of CJK Unified Ideographs
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")

# Portuguese-specific character sequences (common digraphs and words)
_PORTUGUESE_PATTERNS = re.compile(
    r"\b(?:não|sim|obrigado|por|favor|como|está|tudo|bem|você|ele|ela|isso|aqui|"
    r"gente|coisa|casa|mundo|vida|tempo|dia|noite|hoje|amanhã|"
    r|obrigada|pra|pro|tá|vou|faz|ser|mais|mas|era|foi|são|só|"
    r|então|depois|antes|sempre|nunca|já|ainda|também|muito|"
    r|pode|deve|saber|falar|ver|dar|ter|vir|ir|ler|ão|õe|ães)\b",
    re.IGNORECASE,
)


def detect_language(text: str) -> str:
    """Detect the language of a text string.

    Returns one of 'zh', 'pt', or 'en' (default).
    Uses CJK character detection for Mandarin and Portuguese-specific
    word patterns for Portuguese. Falls back to English.
    """
    if not text or not text.strip():
        return "en"

    # Check for CJK characters (Mandarin)
    cjk_matches = _CJK_RE.findall(text)
    if len(cjk_matches) >= 3:
        return "zh"

    # Check for Portuguese patterns
    pt_matches = _PORTUGUESE_PATTERNS.findall(text)
    if len(pt_matches) >= 2:
        return "pt"

    return "en"


# ── Synthesis ──────────────────────────────────────────────────────────────

def _cleanup_old_files() -> None:
    """Remove voice response files older than _CLEANUP_MAX_AGE."""
    now = time.time()
    try:
        for f in _OUTPUT_DIR.iterdir():
            if f.is_file() and f.suffix == ".mp3":
                age = now - f.stat().st_mtime
                if age > _CLEANUP_MAX_AGE:
                    f.unlink(missing_ok=True)
                    logger.debug("Cleaned up old voice file: %s", f.name)
    except Exception as e:
        logger.warning("Voice cleanup failed: %s", e)


def synthesize_voice(text: str, language: str = "en") -> str | None:
    """Synthesize text to speech using edge-tts.

    Args:
        text: The text to speak.
        language: Language code ('en', 'zh', 'pt'). Defaults to 'en'.

    Returns:
        Absolute path to the generated MP3 file, or None on failure.
    """
    if not text or not text.strip():
        return None

    voice = VOICE_MAP.get(language, VOICE_MAP["en"])
    filename = f"{uuid.uuid4().hex}.mp3"
    output_path = _OUTPUT_DIR / filename

    try:
        import edge_tts

        async def _synthesize():
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(str(output_path))

        asyncio.run(_synthesize())

        if output_path.exists() and output_path.stat().st_size > 0:
            logger.info(
                "Synthesized voice response: lang=%s voice=%s size=%d bytes",
                language, voice, output_path.stat().st_size,
            )
            _cleanup_old_files()
            return str(output_path)
        else:
            logger.error("Voice synthesis produced empty file")
            return None

    except Exception as e:
        logger.error("Voice synthesis failed: %s", e)
        return None


def get_voice_name(language: str) -> str:
    """Get the voice name for a language code."""
    return VOICE_MAP.get(language, VOICE_MAP["en"])
