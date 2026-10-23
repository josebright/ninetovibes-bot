"""ElevenLabs TTS — premium voice synthesis for NineToVibes characters.

Replaces Edge-TTS with dramatically better voice quality. Each character
(Bamboo, Theodore) gets a dedicated ElevenLabs voice configured in their
character YAML under voice.elevenlabs_voice_id.

If no custom voice is configured yet, falls back to the best-matching
preset voice from ElevenLabs' library.

Character budget at 1 video/day:
  ~1000 chars/video × 30 days = 30,000 chars/month → fits Starter plan ($5/mo)
"""
from __future__ import annotations
import subprocess
from pathlib import Path

from elevenlabs.client import ElevenLabs
from elevenlabs import play, save, VoiceSettings

from src.core.config import env
from src.core.logging import get_logger
from src.core.paths import DRAFTS

log = get_logger(__name__)

FFPROBE = "/opt/homebrew/bin/ffprobe"

# Best-matching ElevenLabs preset voices for each character.
# Brian: deep, resonant, comforting — perfect laid-back radio host energy.
# Daniel: British broadcaster, authoritative — matches Theodore's reporter persona.
FALLBACK_VOICES = {
    "bamboo": "Brian",     # deep, resonant, comforting — laid-back radio host
    "theodore": "Daniel",  # British broadcaster — authoritative reporter
}


def _client() -> ElevenLabs:
    return ElevenLabs(api_key=env("ELEVENLABS_API_KEY", required=True))


def _get_voice_id(character_id: str) -> str:
    """Resolve the voice ID for a character.

    Priority:
    1. Explicit voice_id from character YAML
    2. Lookup preset voice by name from FALLBACK_VOICES
    3. Search ElevenLabs library for the fallback name
    """
    import yaml
    from src.core.paths import ROOT
    char_path = ROOT / "config" / "characters" / f"{character_id}.yaml"
    if char_path.exists():
        with open(char_path) as f:
            char = yaml.safe_load(f)
        voice_cfg = char.get("voice", {})
        explicit = voice_cfg.get("elevenlabs_voice_id")
        if explicit:
            return explicit

    # Search for the fallback voice name in the ElevenLabs library
    fallback_name = FALLBACK_VOICES.get(character_id, "Chris")
    client = _client()
    response = client.voices.get_all()
    for v in response.voices:
        if v.name.lower() == fallback_name.lower():
            log.info("using preset voice %r (id=%s) for %s", v.name, v.voice_id, character_id)
            return v.voice_id

    # Last resort: use the first available voice
    if response.voices:
        v = response.voices[0]
        log.warning("no matching voice for %s, using first available: %s", character_id, v.name)
        return v.voice_id
    raise RuntimeError("no voices available in ElevenLabs account")


def _get_voice_settings(character_id: str) -> VoiceSettings | None:
    import yaml
    from src.core.paths import ROOT
    char_path = ROOT / "config" / "characters" / f"{character_id}.yaml"
    if not char_path.exists():
        return None
    with open(char_path) as f:
        char = yaml.safe_load(f)
    s = char.get("voice", {}).get("elevenlabs_settings", {})
    if not s:
        return None
    return VoiceSettings(
        stability=s.get("stability", 0.55),
        similarity_boost=s.get("similarity_boost", 0.75),
        style=s.get("style", 0.3),
        use_speaker_boost=s.get("use_speaker_boost", True),
    )


def _probe_duration_seconds(path: Path) -> float:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(r.stdout.strip())


def _apply_character_voice_fx(raw_path: Path, out_path: Path, character_id: str) -> Path:
    """Post-process ElevenLabs audio to sound like each animal character.

    Based on research into real gorilla/bear vocal acoustics:
    - Gorillas vocalize at fundamentals ~27-80 Hz (3x lower than human males)
    - Laryngeal air sacs act as low-frequency resonators (like built-in subwoofers)
    - Formants are lower due to longer vocal tract
    - Significant broadband noise (breathy, turbulent quality)
    - Subharmonics from dual vocal fold vibration (growl texture)

    Bamboo (gorilla):
      pitch down 5 semitones (formants shift too = sounds like larger creature)
      + heavy bass boost (air sac resonance) + sub-bass harmonics (growl)
      + cut upper mids (less distinctly human) + slight breathiness
      + subtle tremolo at gorilla pulse rate (14 Hz)

    Theodore (bear):
      pitch down 3 semitones + chest resonance boost + slight growl
    """
    FFMPEG = "/opt/homebrew/bin/ffmpeg"
    if character_id == "bamboo":
        # NO pitch-shifting — it ruins natural speed and sounds robotic.
        # Instead: bass boost + sub-bass + chest resonance + studio reverb.
        # The deep quality comes from ElevenLabs "Brian" voice (already deep)
        # plus heavy low-end processing that makes it feel LARGE.
        af = (
            "bass=gain=10:frequency=100:width_type=o:width=2.0,"   # chest boom you can feel
            "equalizer=f=200:t=o:w=1.0:g=4,"                      # low-mid warmth (gorilla body)
            "equalizer=f=3500:t=o:w=2.0:g=-3,"                    # slight upper-mid cut (less tinny)
            "asubboost=dry=0.65:wet=0.35:boost=4:feedback=0.35:cutoff=80:slope=0.5,"  # sub-bass growl
            "aecho=0.8:0.88:12:0.06,"                              # tight studio reverb
            "loudnorm=I=-14:TP=-1:LRA=11"                         # broadcast-standard loudness
        )
    elif character_id == "theodore":
        # Bear: no pitch-shift, bass + authority + outdoor reverb
        af = (
            "bass=gain=8:frequency=90:width_type=o:width=1.5,"
            "equalizer=f=250:t=o:w=1.0:g=3,"
            "equalizer=f=3500:t=o:w=1.5:g=-2,"
            "asubboost=dry=0.75:wet=0.25:boost=3:feedback=0.25:cutoff=70:slope=0.5,"
            "aecho=0.8:0.85:20:0.08,"
            "loudnorm=I=-14:TP=-1:LRA=11"
        )
    else:
        af = "anull"

    cmd = [
        FFMPEG, "-y", "-i", str(raw_path),
        "-af", af,
        "-ar", "44100", "-ac", "1",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out_path


def synthesize(
    text: str,
    draft_id: str,
    character_id: str = "bamboo",
) -> tuple[Path, str, float]:
    """Generate voiceover via ElevenLabs + character voice FX.
    Returns (path, voice_name, duration_seconds).
    """
    out_dir = DRAFTS / draft_id
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / "voiceover_raw.mp3"
    out_path = out_dir / "voiceover.mp3"

    voice_id = _get_voice_id(character_id)
    voice_settings = _get_voice_settings(character_id)

    log.info("elevenlabs tts: draft=%s character=%s chars=%d", draft_id, character_id, len(text))

    client = _client()
    audio = client.text_to_speech.convert(
        voice_id=voice_id,
        text=text,
        model_id="eleven_multilingual_v2",
        voice_settings=voice_settings,
    )

    with open(raw_path, "wb") as f:
        for chunk in audio:
            f.write(chunk)

    # Apply character-specific voice FX (pitch, reverb, bass)
    _apply_character_voice_fx(raw_path, out_path, character_id)
    raw_path.unlink(missing_ok=True)  # clean up raw

    duration = _probe_duration_seconds(out_path)
    log.info("elevenlabs done: %s (%.2fs, %d bytes)", out_path.name, duration, out_path.stat().st_size)
    return out_path, character_id, duration


if __name__ == "__main__":
    import sys
    text = sys.argv[1] if len(sys.argv) > 1 else "Welcome to NineToVibes. Let's talk about money moves that actually matter."
    char = sys.argv[2] if len(sys.argv) > 2 else "bamboo"
    p, v, d = synthesize(text, draft_id="__smoke_el__", character_id=char)
    print(f"wrote {p}  character={v}  duration={d:.2f}s")
