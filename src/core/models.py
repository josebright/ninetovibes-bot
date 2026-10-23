from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Literal
from pydantic import BaseModel, Field


class Niche(str, Enum):
    career = "career"
    finance = "finance"
    love = "love"


class Format(str, Enum):
    short = "short"
    long = "long"


class SourceSnippet(BaseModel):
    """A fetched fact with a verifiable URL. The LLM can only cite these."""
    source_id: str
    citation_prefix: str
    url: str
    title: str
    snippet: str = Field(description="Short factual paragraph pulled from the source.")
    license: str
    fetched_at: datetime = Field(default_factory=datetime.utcnow)


class TrendingTopic(BaseModel):
    niche: Niche
    query: str
    rationale: str = ""
    score: float = 0.0


class ScriptPayload(BaseModel):
    title: str
    description: str
    hashtags: list[str]
    sources_used: list[str]
    raw_json: dict
    text_for_tts: str


class VideoDraft(BaseModel):
    id: str
    niche: Niche
    format: Format
    topic: str
    script: ScriptPayload
    audio_path: str
    video_path: str
    thumbnail_path: str | None = None
    duration_seconds: float
    voice: str
    sources: list[SourceSnippet]
    created_at: datetime = Field(default_factory=datetime.utcnow)
    status: Literal["draft", "approved", "rejected", "uploaded", "published"] = "draft"
    youtube_video_id: str | None = None
