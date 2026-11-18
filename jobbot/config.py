"""Load config.yaml and profile.yaml."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "profile": "profile.yaml",
    "database": "jobbot.db",
    "output_dir": "output",
    "provider": "auto",             # auto | claude | ollama
    "model": "claude-opus-5-5",     # Claude model (provider: claude)
    "ollama": {"model": "qwen2.5:7b", "host": "http://localhost:11434", "num_ctx": 8192,
               "timeout_seconds": 900},
    "search": {
        "keywords": [],
        "exclude_keywords": [],
        "locations": [],
        "remote_ok": True,
        "max_age_days": 30,
        "focus": "jobs",            # jobs | students | internships
    },
    "sources": {"greenhouse": [], "lever": [], "ashby": [], "remotive": {"enabled": False},
                "adzuna": {"enabled": False, "country": "ca", "where": "", "app_id": "",
                           "app_key": "", "searches": [], "results_per_page": 50}},
    "matching": {"min_score": 70, "max_jobs_to_score": 50},
    "documents": {"cover_letter": "when_accepted", "formats": ["pdf", "docx"]},
    "apply": {
        "mode": "review",            # review | auto
        "max_per_run": 10,
        "delay_seconds": 20,
        "headless": False,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"Config file not found: {path}. Copy config.example.yaml to {path}.")
    raw = yaml.safe_load(path.read_text()) or {}
    cfg = _merge(DEFAULTS, raw)
    base = path.parent
    for key in ("profile", "database", "output_dir"):
        p = Path(cfg[key])
        cfg[key] = str(p if p.is_absolute() else base / p)
    return cfg


REQUIRED_PROFILE_KEYS = ("name", "email", "experience")


def load_profile(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise SystemExit(f"Profile not found: {path}. Copy profile.example.yaml to {path}.")
    profile = yaml.safe_load(path.read_text()) or {}
    missing = [k for k in REQUIRED_PROFILE_KEYS if not profile.get(k)]
    if missing:
        raise SystemExit(f"Profile is missing required keys: {', '.join(missing)}")
    for i, exp in enumerate(profile["experience"]):
        exp.setdefault("id", f"exp{i + 1}")
    for i, proj in enumerate(profile.get("projects") or []):
        proj.setdefault("id", f"proj{i + 1}")
    return profile
