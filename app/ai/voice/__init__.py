"""Speech: transcription (STT) on the way in, synthesis (TTS) on the way out.

Providers are selected from settings (`STT_PROVIDER` / `TTS_PROVIDER`): OpenAI when a key is
configured, an in-process fake in `AI_FAKE_MODE` (scriptable transcripts, a tone as speech),
otherwise disabled — endpoints then answer 503 `voice_unavailable`.
"""
