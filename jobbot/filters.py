"""Cheap keyword/location pre-filter so we only spend API calls on plausible jobs."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from jobbot.models import Job


def _has(text: str, term: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])", text) is not None


def _posted(job: Job) -> datetime | None:
    s = job.posted_at
    if not s:
        return None
    try:
        if s.isdigit():  # Lever: epoch millis
            return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc)
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def passes(job: Job, search: dict) -> tuple[bool, str]:
    title = job.title.lower()
    text = f"{title}\n{job.description.lower()}"

    for term in search.get("exclude_keywords") or []:
        if _has(title, term):
            return False, f"title contains excluded '{term}'"

    keywords = search.get("keywords") or []
    if keywords and not any(_has(title, k) for k in keywords):
        # Fall back to description, but require two hits so generic pages don't slip in.
        hits = sum(_has(text, k) for k in keywords)
        if hits < 2:
            return False, "no keyword match"

    locations = [l.lower() for l in search.get("locations") or []]
    if locations:
        loc = job.location.lower()
        loc_ok = any(l in loc for l in locations)
        if not loc_ok and not (search.get("remote_ok", True) and job.remote):
            return False, f"location '{job.location}'"

    max_age = search.get("max_age_days")
    posted = _posted(job)
    if max_age and posted and posted < datetime.now(timezone.utc) - timedelta(days=max_age):
        return False, "too old"

    return True, ""
