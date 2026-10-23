from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config"
DATA = ROOT / "data"
DRAFTS = DATA / "drafts"
PUBLISHED = DATA / "published"
CACHE = DATA / "cache"
CACHE_SOURCES = CACHE / "sources"
CACHE_TRENDS = CACHE / "trends"
LOGS = DATA / "logs"
SECRETS = ROOT / "secrets"
POLICY_SNAPSHOTS = ROOT / "src" / "compliance" / "policy_snapshots"

for d in (DRAFTS, PUBLISHED, CACHE_SOURCES, CACHE_TRENDS, LOGS, SECRETS, POLICY_SNAPSHOTS):
    d.mkdir(parents=True, exist_ok=True)
