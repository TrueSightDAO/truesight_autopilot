"""Local speech-to-text for Telegram voice notes — faster-whisper, no API cost.

Runs the open-source Whisper model on this box (CPU, int8) so audio never leaves
our infra and there is no per-use charge. The model is lazy-loaded + cached for
the process lifetime. Guards against Whisper's silence-hallucination (it emits
\"Obrigado por assistir\" / \"Thanks for watching\" on near-silent audio) via the
per-segment no_speech_prob threshold.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("autopilot.voice")

_MODEL = None
_MODEL_SIZE = "base"          # base/int8 is fast + light; plenty for dictation
_NO_SPEECH_PROB_MAX = 0.8     # drop segments Whisper isn't sure contain speech


def _get_model():
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        logger.info("loading faster-whisper model: %s", _MODEL_SIZE)
        _MODEL = WhisperModel(_MODEL_SIZE, device="cpu", compute_type="int8")
    return _MODEL


def transcribe_voice(path):
    """Transcribe an audio file (Telegram OGG/Opus etc.) to text. '' on failure/silence."""
    if not path:
        return ""
    try:
        segments, _info = _get_model().transcribe(path, vad_filter=True)
        parts = []
        for seg in segments:
            if getattr(seg, "no_speech_prob", 0.0) > _NO_SPEECH_PROB_MAX:
                continue  # silence-hallucination guard
            t = (seg.text or "").strip()
            if t:
                parts.append(t)
        return " ".join(parts).strip()
    except Exception as e:  # noqa: BLE001
        logger.warning("voice transcription failed: %s", e)
        return ""
