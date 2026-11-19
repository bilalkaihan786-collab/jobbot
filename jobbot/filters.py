"""Cheap keyword/location pre-filter so we only spend API calls on plausible jobs."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from jobbot.models import Job


# Words in a job title that mark it as an internship / co-op / student position.
INTERNSHIP_TERMS = ("intern", "interns", "internship", "internships", "co-op", "co-ops", "coop",
                    "co op", "work term", "student", "apprentice", "apprenticeship")
FOCUSES = ("jobs", "students", "internships")


def normalize_focus(focus) -> str:
    focus = (focus or "jobs").lower()
    return "students" if focus == "both" else focus if focus in FOCUSES else "jobs"


def student_focus(focus) -> bool:
    return normalize_focus(focus) in ("students", "internships")


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


def is_internship(job: Job) -> bool:
    title = job.title.lower()
    return any(_has(title, t) for t in INTERNSHIP_TERMS)


def relevance(job: Job, search: dict) -> int:
    """Cheap keyword score used to decide which postings the AI looks at first."""
    title = job.title.lower()
    text = job.description.lower()
    kws = search.get("keywords") or []
    return 3 * sum(_has(title, k) for k in kws) + sum(_has(text, k) for k in kws)


def passes(job: Job, search: dict) -> tuple[bool, str]:
    """search.focus: jobs (regular roles), students (student-friendly work of any kind), or
    internships (only titles that say internship / co-op / student)."""
    title = job.title.lower()
    text = f"{title}\n{job.description.lower()}"
    focus = normalize_focus(search.get("focus"))
    intern = is_internship(job)

    if focus == "internships" and not intern:
        return False, "not an internship or co-op"

    for term in search.get("exclude_keywords") or []:
        # When internships are wanted, an "intern" exclusion left over from a regular search
        # must not throw them away.
        if focus != "jobs" and intern and term.lower().strip() in INTERNSHIP_TERMS:
            continue
        if _has(title, term):
            return False, f"title contains excluded '{term}'"

    keywords = search.get("keywords") or []
    if keywords and not any(_has(title, k) for k in keywords):
        # Fall back to description, but require two hits so generic pages don't slip in.
        # Internship titles are short ("Engineering Intern"), so one hit is enough for them.
        hits = sum(_has(text, k) for k in keywords)
        if hits < (1 if focus == "students" or (focus != "jobs" and intern) else 2):
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
