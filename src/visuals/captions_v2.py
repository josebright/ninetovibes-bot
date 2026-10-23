"""Two-tier caption system matching NineToVibes production style.

Tier 1 — HOOK TEXT (upper region):
  Bold white sans-serif, one keyword highlighted in magenta/pink.
  Appears at the start of each major beat/section.

Tier 2 — SUBTITLES (lower region):
  Italic white text on a dark semi-transparent pill.
  Synced to the voiceover, one phrase at a time.

Both tiers are rendered as transparent PNGs at full canvas size,
composited via FFmpeg overlay with time-based enable.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from src.core.logging import get_logger

log = get_logger(__name__)


# ---- Data structures -------------------------------------------------------

@dataclass
class SubtitleCue:
    """A spoken phrase synced to audio timing."""
    start: float
    end: float
    text: str


@dataclass
class HookCue:
    """A large hook/headline that appears during a section."""
    start: float
    end: float
    text: str
    keyword: str  # the word to highlight in magenta


# ---- Font loading ----------------------------------------------------------

_FONT_PATHS_BOLD = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Helvetica Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
_FONT_PATHS_ITALIC = [
    "/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf",
    "/System/Library/Fonts/Supplemental/Arial Italic.ttf",
    "/System/Library/Fonts/Supplemental/Helvetica Oblique.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
_FONT_PATHS_REGULAR = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]

def _load_font(candidates: list[str], px: int) -> ImageFont.FreeTypeFont:
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, px)
            except Exception:
                continue
    return ImageFont.load_default()


# ---- Text wrapping ---------------------------------------------------------

def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_px: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    for w in words:
        trial = (cur + " " + w).strip()
        if probe.textlength(trial, font=font) <= max_px or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


# ---- Cue generation from script -------------------------------------------

def _split_sentences(text: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return [s.strip() for s in sentences if s.strip()]


def build_subtitle_cues(text: str, total_duration: float, max_chars: int = 45) -> list[SubtitleCue]:
    """Distribute text across duration proportionally to WORD count (more accurate
    than character count since speaking pace is word-driven, not char-driven).

    Each phrase gets a small padding gap (0.08s) between cues so captions don't
    feel like they slam together.
    """
    sentences = _split_sentences(text)
    phrases: list[str] = []
    for s in sentences:
        if len(s) <= max_chars:
            phrases.append(s)
        else:
            # Break at commas/semicolons/dashes
            parts = re.split(r"(?<=[,;:\-])\s+", s)
            buf = ""
            for p in parts:
                if len(buf) + 1 + len(p) <= max_chars:
                    buf = (buf + " " + p).strip()
                else:
                    if buf:
                        phrases.append(buf)
                    buf = p
            if buf:
                phrases.append(buf)

    if not phrases:
        return []

    # Word-based timing (more natural than character-based)
    total_words = sum(len(p.split()) for p in phrases)
    gap = 0.08  # small gap between cues for visual breathing room
    usable_duration = total_duration - gap * (len(phrases) - 1)

    cues: list[SubtitleCue] = []
    t = 0.0
    for i, p in enumerate(phrases):
        word_count = len(p.split())
        dur = usable_duration * (word_count / total_words) if total_words else 0
        cues.append(SubtitleCue(start=round(t, 3), end=round(t + dur, 3), text=p))
        t += dur + gap

    # Pin last cue's end to total_duration
    if cues:
        cues[-1] = SubtitleCue(start=cues[-1].start, end=total_duration, text=cues[-1].text)
    return cues


def build_hook_cues(script_json: dict, total_duration: float, fmt: str = "short") -> list[HookCue]:
    """Extract 1-3 hook headlines from the script's structure.

    For Shorts: one hook from the 'hook' field.
    For long-form: one per act beat.
    The keyword is the single most impactful word (noun/verb) — we pick the
    longest capitalized-or-important word.
    """
    hooks: list[HookCue] = []

    if fmt == "short":
        hook_text = script_json.get("hook", "") or script_json.get("title", "")
        if hook_text:
            kw = _pick_keyword(hook_text)
            hooks.append(HookCue(start=0.0, end=min(5.0, total_duration * 0.3),
                                 text=hook_text[:60], keyword=kw))
    else:
        acts = []
        for key in ["act_1", "act_2", "act_3"]:
            act = script_json.get(key) or {}
            beat = act.get("beat", "")
            if beat:
                acts.append(beat)
        segment_dur = total_duration / max(len(acts), 1)
        for i, beat in enumerate(acts):
            kw = _pick_keyword(beat)
            hooks.append(HookCue(
                start=i * segment_dur, end=(i + 0.4) * segment_dur,
                text=beat[:60], keyword=kw,
            ))

    return hooks


def _pick_keyword(text: str) -> str:
    """Pick the single most impactful word to highlight in magenta."""
    # Skip common small words, pick the longest remaining word
    stopwords = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "can", "shall", "to", "of", "in", "for",
        "on", "with", "at", "by", "from", "as", "into", "about", "like",
        "and", "but", "or", "nor", "not", "so", "yet", "both", "it", "its",
        "this", "that", "these", "those", "your", "you", "we", "they", "i",
    }
    words = re.findall(r"[A-Za-z]+", text)
    candidates = [w for w in words if w.lower() not in stopwords and len(w) > 2]
    if not candidates:
        candidates = words
    return max(candidates, key=len) if candidates else text.split()[0]


# ---- PNG rendering ---------------------------------------------------------

MAGENTA = (255, 50, 180, 255)
WHITE = (255, 255, 255, 255)
PILL_BG = (0, 0, 0, 170)


def render_hook_png(cue: HookCue, canvas_w: int, canvas_h: int, out_path: Path) -> Path:
    """Render a hook headline with one keyword in magenta, rest in white.
    Positioned in the upper third of the frame.
    """
    font_px = int(canvas_h * 0.038) if canvas_h >= 1920 else int(canvas_h * 0.05)
    font_px = max(font_px, 36)
    font = _load_font(_FONT_PATHS_BOLD, font_px)
    max_px = int(canvas_w * 0.85)

    lines = _wrap_text(cue.text, font, max_px)
    line_h = int(font_px * 1.3)

    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Position: upper region, ~18% from top
    block_h = line_h * len(lines)
    start_y = int(canvas_h * 0.18)

    for line_idx, line in enumerate(lines):
        # Find the keyword in this line and render word-by-word
        words = line.split()
        # Measure total line width to center it
        total_w = draw.textlength(line, font=font)
        x = (canvas_w - total_w) / 2
        y = start_y + line_idx * line_h

        for word in words:
            color = MAGENTA if word.lower().rstrip(".,!?;:") == cue.keyword.lower() else WHITE
            draw.text((x, y), word, font=font, fill=color)
            x += draw.textlength(word + " ", font=font)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path


def render_subtitle_png(cue: SubtitleCue, canvas_w: int, canvas_h: int, out_path: Path) -> Path:
    """Render a subtitle phrase on a dark semi-transparent pill in the lower third."""
    font_px = int(canvas_h * 0.026) if canvas_h >= 1920 else int(canvas_h * 0.035)
    font_px = max(font_px, 24)
    font = _load_font(_FONT_PATHS_ITALIC, font_px)
    max_px = int(canvas_w * 0.88)

    lines = _wrap_text(cue.text, font, max_px)
    line_h = int(font_px * 1.25)

    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    block_h = line_h * len(lines)
    block_w = int(max(draw.textlength(l, font=font) for l in lines)) if lines else 0

    pad_x = int(font_px * 0.7)
    pad_y = int(font_px * 0.45)
    pill_w = block_w + pad_x * 2
    pill_h = block_h + pad_y * 2
    pill_x = (canvas_w - pill_w) // 2
    bottom_offset = int(canvas_h * 0.10)
    pill_y = canvas_h - bottom_offset - pill_h
    radius = int(pill_h * 0.3)

    draw.rounded_rectangle(
        [pill_x, pill_y, pill_x + pill_w, pill_y + pill_h],
        radius=radius, fill=PILL_BG,
    )

    y = pill_y + pad_y
    for line in lines:
        tw = draw.textlength(line, font=font)
        x = (canvas_w - tw) // 2
        draw.text((x, y), line, font=font, fill=WHITE)
        y += line_h

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path


# ---- Batch render for assembly ---------------------------------------------

@dataclass
class CaptionLayer:
    """A timed PNG overlay — either hook or subtitle."""
    start: float
    end: float
    png_path: Path
    tier: str  # "hook" or "subtitle"


def render_all_captions(
    script_json: dict,
    spoken_text: str,
    total_duration: float,
    canvas_w: int,
    canvas_h: int,
    fmt: str,
    out_dir: Path,
) -> list[CaptionLayer]:
    """Render ONLY bottom subtitles. No hook overlay — user feedback confirmed
    double text is distracting and ugly. One clean subtitle bar at the bottom
    is all we need.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    layers: list[CaptionLayer] = []

    for i, cue in enumerate(build_subtitle_cues(spoken_text, total_duration)):
        p = out_dir / f"sub_{i:03d}.png"
        render_subtitle_png(cue, canvas_w, canvas_h, p)
        layers.append(CaptionLayer(start=cue.start, end=cue.end, png_path=p, tier="subtitle"))

    layers.sort(key=lambda l: l.start)
    log.info("captions: %d subtitle PNGs", len(layers))
    return layers
