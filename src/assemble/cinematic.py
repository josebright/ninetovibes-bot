"""Cinematic video assembler.

Structure:
  [Bamboo INTRO]  →  [Stock footage story scenes]  →  [End card/final scene]

- Bamboo appears ONLY at the start (hook delivery)
- Story scenes are unique stock footage, character-consistent
- Video ends on the story's emotional impact — no Bamboo at the end
- Voiceover + lo-fi instrumental play across the entire video
- Visual cuts every 3-5 seconds keep viewers engaged
"""
from __future__ import annotations
import random
import subprocess
from pathlib import Path

from src.core.config import settings
from src.core.logging import get_logger
from src.core.paths import DATA

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


def _normalize_clip(src: Path, w: int, h: int, fps: int, out: Path,
                    color_grade: str = "") -> Path:
    """Scale + crop + optional color grading. Each video gets a random grade
    so even if clips are reused across videos, they look different."""
    vf = (
        f"scale='if(gt(a,{w}/{h}),-2,{w})':"
        f"'if(gt(a,{w}/{h}),{h},-2)',"
        f"crop={w}:{h},fps={fps},setsar=1"
    )
    if color_grade:
        vf += f",{color_grade}"
    subprocess.run(
        [FFMPEG, "-y", "-i", str(src), "-vf", vf,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-an", str(out)],
        check=True, capture_output=True,
    )
    return out


def _pick_color_grade() -> str:
    """Pick a random subtle color grade for this video.
    Ensures each video has a slightly different visual feel."""
    grades = [
        "eq=saturation=1.1:brightness=0.02:contrast=1.02",     # warm, slightly bright
        "eq=saturation=1.05:brightness=-0.01:contrast=1.05",   # moody, punchy
        "eq=saturation=0.95:brightness=0.03:contrast=1.0",     # desaturated, clean
        "eq=saturation=1.08:brightness=0.0:contrast=1.03",     # vivid
        "eq=saturation=1.0:brightness=-0.02:contrast=1.06",    # dark, dramatic
    ]
    return random.choice(grades)


def _trim_clip(src: Path, duration: float, out: Path) -> Path:
    subprocess.run(
        [FFMPEG, "-y", "-i", str(src), "-t", f"{duration:.3f}",
         "-c", "copy", str(out)],
        check=True, capture_output=True,
    )
    return out


def _concat(clips: list[Path], out: Path) -> Path:
    list_file = out.with_suffix(".txt")
    list_file.write_text("\n".join(f"file '{p.resolve()}'" for p in clips))
    subprocess.run(
        [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
         "-c", "copy", str(out)],
        check=True, capture_output=True,
    )
    list_file.unlink(missing_ok=True)
    return out


def assemble_cinematic(
    bamboo_clips: list[Path],
    story_scene_clips: list[Path],
    audio_path: Path,
    out_path: Path,
    fmt: str = "short",
) -> Path:
    """Build a cinematic video: Bamboo intro → story scenes → end on impact.

    bamboo_clips: 1+ clips (only the first one is used for intro)
    story_scene_clips: unique clips for each story beat
    audio_path: processed voiceover
    """
    v = settings()["video"][fmt]
    w, h, fps = v["width"], v["height"], v["fps"]
    audio_dur = _probe_duration(audio_path)

    log.info("cinematic: %dx%d, %.1fs, %d bamboo, %d scenes",
             w, h, audio_dur, len(bamboo_clips), len(story_scene_clips))

    workdir = out_path.parent / "_work"
    workdir.mkdir(parents=True, exist_ok=True)

    # Pick a color grade for this entire video (consistent look throughout)
    grade = _pick_color_grade()
    log.info("color grade: %s", grade)

    # 1. Bamboo INTRO — pick one clip randomly, show for ~12% of duration
    intro_src = random.choice(bamboo_clips)
    intro_dur = min(audio_dur * 0.12, 5.0)

    intro_norm = workdir / "intro.mp4"
    _normalize_clip(intro_src, w, h, fps, intro_norm, color_grade=grade)
    intro_final = workdir / "intro_t.mp4"
    _trim_clip(intro_norm, intro_dur, intro_final)

    # 2. Story scenes — fill the remaining time
    story_dur = audio_dur - intro_dur
    n_scenes = len(story_scene_clips)

    if n_scenes == 0:
        raise RuntimeError("no story scenes available")

    # Each scene gets equal time, clamped to 3-5 seconds for good pacing
    raw_per_scene = story_dur / n_scenes
    per_scene = max(3.0, min(raw_per_scene, 5.5))

    scene_parts: list[Path] = []
    for i, src in enumerate(story_scene_clips):
        norm = workdir / f"s_{i:02d}.mp4"
        try:
            _normalize_clip(src, w, h, fps, norm, color_grade=grade)
            trimmed = workdir / f"s_{i:02d}_t.mp4"
            this_dur = per_scene
            # Last scene gets whatever time remains
            if i == n_scenes - 1:
                used = intro_dur + per_scene * i
                this_dur = max(2.0, audio_dur - used)
            _trim_clip(norm, this_dur, trimmed)
            scene_parts.append(trimmed)
        except subprocess.CalledProcessError as e:
            log.warning("scene %d norm failed, skipping", i)

    # If we don't have enough scenes to fill the time, extend the last scene
    total_scene_time = sum(_probe_duration(p) for p in scene_parts)
    if total_scene_time < story_dur - 1.0 and scene_parts:
        # Repeat some scenes to fill
        fill_needed = story_dur - total_scene_time
        fill_idx = 0
        while fill_needed > 2.0 and fill_idx < len(story_scene_clips):
            src = story_scene_clips[fill_idx]
            fn = workdir / f"fill_{fill_idx}.mp4"
            try:
                _normalize_clip(src, w, h, fps, fn, color_grade=grade)
                fd = min(fill_needed, per_scene)
                ft = workdir / f"fill_{fill_idx}_t.mp4"
                _trim_clip(fn, fd, ft)
                scene_parts.append(ft)
                fill_needed -= fd
            except Exception:
                pass
            fill_idx += 1

    # 3. Build the full sequence: intro → scenes
    sequence = [intro_final] + scene_parts

    # 4. Concat
    concat_out = workdir / "concat.mp4"
    _concat(sequence, concat_out)

    # Trim to exact audio duration
    trimmed_video = workdir / "trimmed.mp4"
    _trim_clip(concat_out, audio_dur, trimmed_video)

    # 5. Mix voiceover + lo-fi background into ONE audio file
    bgm_path = next(
        (p for p in [DATA / "music" / "lofi_real.mp3", DATA / "music" / "lofi_beat.mp3"]
         if p.exists()), None
    )

    if bgm_path:
        mixed_audio = workdir / "mixed.mp3"
        mix_result = subprocess.run(
            [FFMPEG, "-y",
             "-i", str(audio_path),
             "-i", str(bgm_path),
             "-filter_complex",
             "[0:a]volume=1.0[v];"
             f"[1:a]volume=0.55,"
             f"afade=t=in:st=0:d=1.5,"
             f"afade=t=out:st={max(0, audio_dur - 2.5):g}:d=2.5,"
             "lowpass=f=2500,highpass=f=40[b];"
             "[v][b]amix=inputs=2:duration=first:dropout_transition=2[a]",
             "-map", "[a]", "-ar", "44100", "-ac", "2",
             str(mixed_audio)],
            capture_output=True,
        )
        if mix_result.returncode == 0 and mixed_audio.exists() and mixed_audio.stat().st_size > 1000:
            final_audio = mixed_audio
            log.info("lo-fi mixed OK (%.0fKB)", mixed_audio.stat().st_size / 1024)
        else:
            log.warning("lo-fi mix failed (rc=%d), using voice only", mix_result.returncode)
            final_audio = audio_path
    else:
        log.warning("no lo-fi music file found")
        final_audio = audio_path

    # 6. Mux video + final audio into the output MP4
    mux_result = subprocess.run(
        [FFMPEG, "-y",
         "-i", str(trimmed_video),
         "-i", str(final_audio),
         "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "libx264", "-preset", "medium", "-crf", "19",
         "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         "-movflags", "+faststart", "-shortest",
         str(out_path)],
        capture_output=True,
    )
    if mux_result.returncode != 0:
        stderr = (mux_result.stderr or b"").decode(errors="replace")[-500:]
        raise RuntimeError(f"mux failed: {stderr}")

    # 7. Verify final video has audio
    probe = subprocess.run(
        [FFPROBE, "-v", "error", "-show_streams", "-of", "json", str(out_path)],
        capture_output=True, text=True,
    )
    import json
    streams = json.loads(probe.stdout).get("streams", [])
    has_audio = any(s["codec_type"] == "audio" for s in streams)
    if not has_audio:
        raise RuntimeError("final video has no audio stream!")
    log.info("audio verified in final video")

    # 8. Cleanup
    for p in workdir.glob("*"):
        p.unlink(missing_ok=True)
    workdir.rmdir()

    size_mb = out_path.stat().st_size / 1024 / 1024
    log.info("cinematic done: %s (%.1fMB, %.1fs)", out_path.name, size_mb, audio_dur)
    return out_path
