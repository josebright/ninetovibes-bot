"""Video assembly v2 — premium quality with visual transforms + two-tier captions.

Key differences from v1:
  - Clips get random zoom/pan/color transforms so reused library clips look fresh
  - Two-tier caption system (hook + subtitles) composited as overlay PNGs
  - Lo-fi background music mixed under the voiceover
  - Output optimized for YouTube (h264 high, AAC 192k, faststart)
"""
from __future__ import annotations
import random
import subprocess
from pathlib import Path

from src.core.config import settings
from src.core.logging import get_logger
from src.core.paths import DATA
from src.visuals.captions_v2 import CaptionLayer

log = get_logger(__name__)

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"


def _probe_duration(p: Path) -> float:
    r = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
        capture_output=True, text=True, check=True,
    )
    return float(r.stdout.strip() or 0)


def _random_transform(target_w: int, target_h: int) -> str:
    """Generate a random zoom/pan FFmpeg filter to make each clip visually unique.

    Picks from: slow zoom in, slow zoom out, slow pan left-to-right, slight
    warm/cool color shift. These are subtle — the viewer feels variety without
    it being jarring.
    """
    transforms = [
        # Slow zoom in (1.0 → 1.15 over the clip)
        (f"scale={int(target_w * 1.15)}:{int(target_h * 1.15)},"
         f"zoompan=z='1+0.0015*on':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=250:s={target_w}x{target_h}:fps=30"),
        # Slow zoom out (1.15 → 1.0)
        (f"scale={int(target_w * 1.15)}:{int(target_h * 1.15)},"
         f"zoompan=z='1.15-0.0015*on':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=250:s={target_w}x{target_h}:fps=30"),
        # Simple scale to fit (no pan/zoom — variety through simplicity)
        (f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
         f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black"),
    ]
    # Weighted: zoom effects 60%, simple 40%
    return random.choices(transforms, weights=[30, 30, 40], k=1)[0]


def _normalize_clip(src: Path, target_w: int, target_h: int, fps: int, out: Path, apply_transform: bool = True) -> Path:
    """Scale+crop clip to target res, apply a random visual transform, strip audio."""
    # Base: scale to cover, crop center
    base_vf = (
        f"scale='if(gt(a,{target_w}/{target_h}),-2,{target_w})':"
        f"'if(gt(a,{target_w}/{target_h}),{target_h},-2)',"
        f"crop={target_w}:{target_h},"
        f"fps={fps},setsar=1"
    )

    # Optionally apply a subtle color warmth shift
    color_shift = random.choice([
        "",  # no shift
        ",eq=saturation=1.1:brightness=0.02",  # slightly warmer
        ",eq=saturation=1.05:contrast=1.03",   # slightly punchier
    ])

    vf = base_vf + color_shift

    cmd = [
        FFMPEG, "-y", "-i", str(src),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-an",
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out


def _concat_clips(clips: list[Path], out: Path) -> Path:
    list_file = out.with_suffix(".txt")
    list_file.write_text("\n".join(f"file '{p.resolve()}'" for p in clips))
    subprocess.run(
        [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
         "-c", "copy", str(out)],
        check=True, capture_output=True,
    )
    list_file.unlink(missing_ok=True)
    return out


def _trim_to(duration: float, src: Path, out: Path) -> Path:
    subprocess.run(
        [FFMPEG, "-y", "-i", str(src), "-t", f"{duration:.3f}",
         "-c", "copy", str(out)],
        check=True, capture_output=True,
    )
    return out


def _build_overlay_filter(caption_layers: list[CaptionLayer], base_input_count: int) -> tuple[list[str], str]:
    """Build FFmpeg filter_complex for overlaying caption PNGs.

    Returns (extra_inputs, filter_complex_string).
    Each CaptionLayer becomes an input and an overlay with time-based enable.
    """
    extra_inputs: list[str] = []
    filter_parts: list[str] = []
    chain_in = "[0:v]"

    for i, layer in enumerate(caption_layers):
        input_idx = base_input_count + i
        extra_inputs.extend(["-i", str(layer.png_path)])

        tag_out = f"[v{i}]" if i < len(caption_layers) - 1 else "[vout]"
        filter_parts.append(
            f"{chain_in}[{input_idx}:v]overlay=0:0:"
            f"enable='between(t,{layer.start:.3f},{layer.end:.3f})'"
            f"{tag_out}"
        )
        chain_in = tag_out

    return extra_inputs, ";".join(filter_parts)


def assemble_v2(
    fmt: str,
    clip_paths: list[Path],
    audio_path: Path,
    caption_layers: list[CaptionLayer],
    out_path: Path,
) -> Path:
    """Assemble a premium-quality video.

    1. Normalize each clip with random visual transform
    2. Concat + loop to cover audio duration
    3. Trim to audio length
    4. Overlay all caption PNGs with time-based enable
    5. Mix audio, output YouTube-optimized MP4
    """
    v = settings()["video"][fmt]
    target_w, target_h, fps = v["width"], v["height"], v["fps"]
    audio_dur = _probe_duration(audio_path)

    log.info("assemble_v2 fmt=%s target=%dx%d audio=%.2fs clips=%d captions=%d",
             fmt, target_w, target_h, audio_dur, len(clip_paths), len(caption_layers))

    if not clip_paths:
        raise RuntimeError("no clips provided")

    workdir = out_path.parent / "_work"
    workdir.mkdir(parents=True, exist_ok=True)

    # 1. Normalize clips with transforms
    normalized: list[Path] = []
    for i, src in enumerate(clip_paths):
        dst = workdir / f"norm_{i:02d}.mp4"
        try:
            _normalize_clip(src, target_w, target_h, fps, dst)
            normalized.append(dst)
        except subprocess.CalledProcessError as e:
            log.warning("clip %s failed: %s", src.name, (e.stderr or b"")[-200:])

    if not normalized:
        raise RuntimeError("no clips survived normalization")

    # 2. Build sequence to cover audio duration
    sequence: list[Path] = []
    acc = 0.0
    idx = 0
    while acc < audio_dur + 1.0:
        clip = normalized[idx % len(normalized)]
        sequence.append(clip)
        acc += _probe_duration(clip)
        idx += 1
        if idx > 80:
            break

    # 3. Concat
    concat_out = workdir / "concat.mp4"
    _concat_clips(sequence, concat_out)

    # 4. Trim
    trimmed = workdir / "trimmed.mp4"
    _trim_to(audio_dur, concat_out, trimmed)

    # 5. Mix lo-fi background music (vinyl crackle) under voiceover
    bgm_candidates = [DATA / "music" / "lofi_real.mp3", DATA / "music" / "lofi_beat.mp3", DATA / "music" / "lofi_vinyl.mp3"]
    bgm_path = next((p for p in bgm_candidates if p.exists()), None)

    if bgm_path:
        mixed_audio = workdir / "mixed_audio.mp3"
        # The voice has been loudnorm'd to -14 LUFS (broadcast standard) so it's
        # quite hot. The lo-fi beat needs to be loud enough to be clearly heard
        # as a background instrumental, not just a whisper.
        mix_cmd = [
            FFMPEG, "-y",
            "-i", str(audio_path),             # voice (processed, loudnorm'd)
            "-i", str(bgm_path),               # lo-fi instrumental
            "-filter_complex",
            "[0:a]volume=1.0[voice];"           # voice at full
            "[1:a]volume=0.35,"                 # audible lo-fi background behind voice
            "afade=t=in:st=0:d=1.5,"            # fade in
            f"afade=t=out:st={max(0, audio_dur-2.5):g}:d=2.5,"  # fade out
            "lowpass=f=2500,"                   # warm, no highs competing with voice
            "highpass=f=40[bgm];"               # clean sub-bass
            "[voice][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]",
            "-map", "[aout]",
            "-ar", "44100", "-ac", "2",
            str(mixed_audio),
        ]
        subprocess.run(mix_cmd, check=True, capture_output=True)
        final_audio = mixed_audio
        log.info("mixed voice + lo-fi background (%s)", bgm_path.name)
    else:
        final_audio = audio_path

    # 6. Build the final command with caption overlays
    cmd = [FFMPEG, "-y", "-i", str(trimmed), "-i", str(final_audio)]
    base_inputs = 2  # video=0, audio=1

    if caption_layers:
        extra_inputs, filter_str = _build_overlay_filter(caption_layers, base_inputs)
        cmd.extend(extra_inputs)
        cmd.extend(["-filter_complex", filter_str, "-map", "[vout]", "-map", "1:a:0"])
    else:
        cmd.extend(["-map", "0:v:0", "-map", "1:a:0"])

    cmd.extend([
        "-c:v", "libx264", "-preset", "medium", "-crf", "19",
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        "-shortest",
        str(out_path),
    ])

    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode(errors="replace")[-500:]
        raise RuntimeError(f"ffmpeg assembly failed (exit {result.returncode}): {stderr}")

    # Cleanup work dir
    for p in workdir.glob("*"):
        p.unlink(missing_ok=True)
    workdir.rmdir()

    log.info("assemble_v2 done: %s (%.1f MB)", out_path.name,
             out_path.stat().st_size / 1024 / 1024)
    return out_path
