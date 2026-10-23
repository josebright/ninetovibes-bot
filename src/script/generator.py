"""Source-grounded script generator (Ollama + Llama 3.1 8B).

Flow:
  1. Gather snippets from the niche's allowlisted fetchers.
  2. Assemble a prompt with the fixed system rules (see config/prompts.yaml)
     plus the persistent banlist (BANNED_OPENINGS, BANNED_TITLES, BANNED_NAMES,
     BANNED_NUMBERS) injected as hard "do NOT use" constraints.
  3. Ask the local LLM to emit JSON in the exact shape required by Format.
  4. Validate:
       - Parses as JSON
       - Required keys present
       - sources_used ⊆ URLs we actually fetched
       - No "studies show..."-style fabricated attributions
       - HOOK, TITLE, NAMES, NUMBERS are not already in the banlist
  5. If the LLM replies INSUFFICIENT_SOURCES, we abort — do not write scripts
     on thin evidence.
  6. If a banlist collision is detected, raise BannedContentError so the
     orchestrator can regenerate with a stronger nudge.

Return value: ScriptPayload ready for the TTS stage.
"""
from __future__ import annotations
import json
import re
from typing import Any

import ollama

from src.core.config import env, prompts
from src.core.logging import get_logger
from src.core.models import Format, Niche, ScriptPayload, SourceSnippet
from src.sources.manager import enough_to_write, gather
from src.trends import banlist

log = get_logger(__name__)


class InsufficientSourcesError(RuntimeError):
    pass


class ScriptValidationError(RuntimeError):
    pass


class BannedContentError(RuntimeError):
    """Raised when generated content collides with the persistent banlist."""
    def __init__(self, collisions: list[str]):
        self.collisions = collisions
        super().__init__("; ".join(collisions))


# Phrases that often signal fabricated authority — if the script contains any
# of these WITHOUT a cited URL on the same line/sentence, reject it.
# Only reject blatant source-quoting in speech (the script should state facts
# with authority, never cite sources while talking). Authoritative phrasing like
# "studies show" is fine — it's the source NAMING that we block.
FABRICATION_PATTERNS = [
    r"\baccording to the [A-Z]",        # "according to the SEC" — cite in description only
    r"\b[A-Z]{2,}\.(gov|org|com)\b",    # speaking a URL out loud
    r"\bcitation\b",                     # meta-reference to citation
]


# Compact, easily-banned defaults seeded for day-one so the LLM never reaches
# for the obvious mode-collapse hooks even before the channel has shipped much.
SEED_BANNED_HOOKS = [
    "my neighbor bought a porsche",
    "let me tell you about a guy",
    "let me tell you about a girl",
]
SEED_BANNED_NAMES = ["sarah", "john", "mike", "jessica"]
SEED_BANNED_NUMBERS = ["$340,000", "$200k", "$14"]


def _hook_signature(text: str) -> str:
    """First 10 words of the spoken opening — used for hook dedup."""
    words = re.findall(r"\S+", text.strip())
    return " ".join(words[:10]).lower()


def _extract_numbers(text: str) -> list[str]:
    return re.findall(r"\$[\d,]+(?:\.\d+)?[KkMm]?|\d{2,}%|\b\d{3,}\b", text)


def _extract_names(text: str) -> list[str]:
    """Capitalized single-word tokens that look like first names. Ignore the
    first word of sentences and common non-name capitalizations.
    """
    # Tokens after the first sentence position, preceded by a lowercase word
    candidates = re.findall(r"(?<=[a-z]\s)([A-Z][a-z]{2,15})\b", text)
    stop = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
            "Sunday", "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
            "Porsche", "Chipotle", "America", "American", "New", "York",
            "California", "Texas", "Bentley", "BMW", "Lamborghini",
            "Christmas", "Thanksgiving"}
    return [c for c in candidates if c not in stop]


def _render_sources_block(snips: list[SourceSnippet]) -> str:
    lines = []
    for i, s in enumerate(snips, 1):
        lines.append(
            f"[{i}] {s.citation_prefix}\n"
            f"    Title: {s.title}\n"
            f"    URL: {s.url}\n"
            f"    Snippet: {s.snippet}\n"
            f"    License: {s.license}"
        )
    return "\n\n".join(lines)


def _render_banlist_block() -> str:
    """Build the BANNED_* injection that goes into every user prompt."""
    hooks = banlist.list_recent("hook", limit=40) + SEED_BANNED_HOOKS
    titles = banlist.list_recent("title", limit=40)
    names = banlist.list_recent("name", limit=40) + SEED_BANNED_NAMES
    numbers = banlist.list_recent("number", limit=40) + SEED_BANNED_NUMBERS

    def fmt(label: str, items: list[str], inline: bool = False) -> str:
        seen = set()
        unique = []
        for x in items:
            if x and x not in seen:
                seen.add(x)
                unique.append(x)
        if not unique:
            return f"{label}: (none)"
        if inline:
            return f"{label}: " + ", ".join(unique)
        return f"{label}:\n" + "\n".join(f"- {x}" for x in unique)

    return "\n\n".join([
        fmt("BANNED_OPENINGS", hooks),
        fmt("BANNED_TITLES", titles),
        fmt("BANNED_NAMES", names, inline=True),
        fmt("BANNED_NUMBERS", numbers, inline=True),
    ])


def _extract_json(text: str) -> dict[str, Any]:
    """LLMs sometimes wrap JSON in code fences or prose. Find the first {...} block."""
    text = text.strip()
    # Strip ```json fences
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise ScriptValidationError(f"no JSON object in LLM output: {text[:200]!r}")
    return json.loads(text[start : end + 1])


def _check_no_fabrication(text: str) -> None:
    for pat in FABRICATION_PATTERNS:
        if re.search(pat, text, re.I):
            raise ScriptValidationError(
                f"script contains un-cited authority phrase matching {pat!r}; regenerate"
            )


def _text_for_tts(fmt: Format, payload: dict[str, Any]) -> str:
    """Turn the structured JSON into a single speakable string for the TTS engine.
    We intentionally skip the description/hashtags — those are metadata, not spoken.
    """
    if fmt is Format.short:
        parts = [payload.get("hook", ""), payload.get("body", ""), payload.get("cta", "")]
    else:
        parts = [
            payload.get("cold_open", ""),
            payload.get("promise", ""),
            (payload.get("act_1") or {}).get("body", ""),
            (payload.get("act_2") or {}).get("body", ""),
            (payload.get("act_3") or {}).get("body", ""),
            payload.get("recap", ""),
            payload.get("cta", ""),
        ]
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def _check_banlist_collisions(fmt: Format, payload: dict[str, Any], tts_text: str) -> list[str]:
    """Return a list of human-readable collisions against the persistent banlist."""
    collisions: list[str] = []

    title = payload.get("title", "")
    if title and banlist.is_banned("title", title):
        collisions.append(f"title collides with banlist: {title!r}")

    opening_field = payload.get("hook") if fmt is Format.short else payload.get("cold_open")
    hook_sig = _hook_signature(opening_field or tts_text)
    if hook_sig and banlist.is_banned("hook", hook_sig):
        collisions.append(f"opening collides with banlist: {hook_sig!r}")
    for seeded in SEED_BANNED_HOOKS:
        if hook_sig.startswith(seeded):
            collisions.append(f"opening matches seeded ban {seeded!r}")
            break

    for name in _extract_names(tts_text):
        if banlist.is_banned("name", name) or name.lower() in SEED_BANNED_NAMES:
            collisions.append(f"character name banned: {name!r}")

    for num in _extract_numbers(tts_text):
        if banlist.is_banned("number", num) or num.lower() in [n.lower() for n in SEED_BANNED_NUMBERS]:
            collisions.append(f"dollar/number banned: {num!r}")

    return collisions


def _build_user_prompt(niche: Niche, topic: str, fmt: Format,
                      snippets: list[SourceSnippet], extra_nudge: str = "") -> str:
    p = prompts()
    template_key = "short_template" if fmt is Format.short else "long_template"
    template = p[template_key]

    nudge_block = f"\n\nADDITIONAL CONSTRAINT FROM PRIOR ATTEMPT:\n{extra_nudge}\n" if extra_nudge else ""

    return (
        f"TOPIC (this is the story you MUST tell — do not drift): {topic}\n"
        f"NICHE: {niche.value}\n"
        f"FORMAT: {fmt.value}\n\n"
        f"SOURCES (your ONLY permitted grounding — cite only these URLs):\n"
        f"{_render_sources_block(snippets)}\n\n"
        f"{_render_banlist_block()}\n\n"
        f"{template}"
        f"{nudge_block}\n\n"
        f"Respond with ONLY the JSON object — no prose before or after."
    )


def generate(niche: Niche, topic: str, fmt: Format, extra_nudge: str = "") -> ScriptPayload:
    # 1. Ground with sources
    snippets = gather(niche, topic, per_source_limit=3)
    if not enough_to_write(snippets):
        raise InsufficientSourcesError(
            f"Only {len(snippets)} snippets for {niche.value}:{topic!r}; refusing to write"
        )

    allowed_urls = {s.url for s in snippets}

    # 2. Build the prompt (with banlist injection + optional nudge)
    p = prompts()
    system_rules = p["system_rules"]
    user_prompt = _build_user_prompt(niche, topic, fmt, snippets, extra_nudge=extra_nudge)

    # 3. Call Ollama
    client = ollama.Client(host=env("OLLAMA_HOST", "http://127.0.0.1:11434"))
    model = env("OLLAMA_MODEL", "llama3.1:8b-instruct-q4_K_M")
    log.info("generating %s script for %s:%r via %s%s",
             fmt.value, niche.value, topic, model,
             " (with nudge)" if extra_nudge else "")

    resp = client.chat(
        model=model,
        messages=[
            {"role": "system", "content": system_rules},
            {"role": "user", "content": user_prompt},
        ],
        options={"temperature": 0.9, "num_ctx": 8192},
        format="json",
    )
    raw = resp["message"]["content"]

    if raw.strip().startswith("INSUFFICIENT_SOURCES"):
        raise InsufficientSourcesError("LLM declined — INSUFFICIENT_SOURCES")

    # 4. Validate structure
    try:
        payload = _extract_json(raw)
    except Exception as e:
        raise ScriptValidationError(f"bad JSON from LLM: {e}") from e

    required_common = {"title", "description", "hashtags", "sources_used"}
    required_fmt = (
        {"hook", "body", "cta"} if fmt is Format.short
        else {"cold_open", "promise", "act_1", "act_2", "act_3", "recap", "cta"}
    )
    missing = (required_common | required_fmt) - set(payload)
    if missing:
        raise ScriptValidationError(f"LLM output missing fields: {missing}")

    sources_used = [str(u) for u in payload.get("sources_used", [])]
    extraneous = [u for u in sources_used if u not in allowed_urls]
    if extraneous:
        raise ScriptValidationError(
            f"LLM cited URLs not in allowed set: {extraneous[:3]}"
        )

    # Fabrication check on the spoken text only (description may quote headlines)
    tts_text = _text_for_tts(fmt, payload)
    _check_no_fabrication(tts_text)

    # 5. Banlist enforcement — if the script collides, the caller should regenerate
    collisions = _check_banlist_collisions(fmt, payload, tts_text)
    if collisions:
        raise BannedContentError(collisions)

    return ScriptPayload(
        title=payload["title"][:100],
        description=payload["description"],
        hashtags=[h if h.startswith("#") else f"#{h}" for h in payload.get("hashtags", [])][:15],
        sources_used=sources_used or [s.url for s in snippets[:3]],
        raw_json=payload,
        text_for_tts=tts_text,
    )


def generate_with_retries(niche: Niche, topic: str, fmt: Format,
                          max_attempts: int = 4) -> ScriptPayload:
    """Generate a script. On banlist collision, re-call with a targeted nudge
    listing the collisions so the model can avoid them on the next attempt.
    Raises BannedContentError if we exhaust attempts — orchestrator will then
    skip the day.
    """
    last_err: Exception | None = None
    nudge = ""
    for attempt in range(1, max_attempts + 1):
        try:
            return generate(niche, topic, fmt, extra_nudge=nudge)
        except InsufficientSourcesError:
            raise
        except BannedContentError as e:
            log.warning("attempt %d/%d: banlist collisions: %s",
                        attempt, max_attempts, e.collisions)
            nudge = ("Your previous attempt collided with the banlist. "
                     "Specifically: " + "; ".join(e.collisions) +
                     ". Pick an entirely different opening line, different "
                     "character name, and different dollar amounts.")
            last_err = e
        except ScriptValidationError as e:
            log.warning("attempt %d/%d: %s", attempt, max_attempts, e)
            nudge = f"Your previous attempt was invalid: {e}. Fix it and obey the JSON schema exactly."
            last_err = e

    assert last_err is not None
    raise last_err


def fingerprint_payload(script: ScriptPayload, fmt: Format, topic: str) -> None:
    """Persist the script's fingerprint into the banlist so future runs cannot
    collide. Call AFTER the video assembled successfully — not before.
    """
    title = script.title
    raw = script.raw_json
    opening_field = raw.get("hook") if fmt is Format.short else raw.get("cold_open")
    hook_sig = _hook_signature(opening_field or script.text_for_tts)
    names = _extract_names(script.text_for_tts)
    numbers = _extract_numbers(script.text_for_tts)
    banlist.fingerprint_script(
        title=title, hook=hook_sig, topic=topic,
        numbers=numbers, names=names,
    )
