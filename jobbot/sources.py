"""Job sources.

Only public, documented job-board APIs that employers publish on purpose are used:
  * Greenhouse  https://boards-api.greenhouse.io/v1/boards/{company}/jobs
  * Lever       https://api.lever.co/v0/postings/{company}
  * Ashby       https://api.ashbyhq.com/posting-api/job-board/{company}
  * Remotive    https://remotive.com/api/remote-jobs   (discovery only; apply manually)

LinkedIn, Indeed, etc. are deliberately NOT scraped: their terms forbid bots and
accounts get banned.
"""
from __future__ import annotations

import html
import logging
import re
from typing import Callable, Iterable

import requests

from jobbot.models import Job

log = logging.getLogger(__name__)
TIMEOUT = 30
HEADERS = {"User-Agent": "jobbot/0.1 (personal job search tool)"}


def html_to_text(s: str) -> str:
    if not s:
        return ""
    s = html.unescape(s)  # Greenhouse double-escapes content
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n• ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


def _get_json(url: str, params: dict | None = None):
    r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- parsers
# Parsers are separate from fetchers so they can be tested offline.

def parse_greenhouse(company: str, data: dict) -> list[Job]:
    jobs = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        jid = str(j["id"])
        jobs.append(Job(
            source="greenhouse", source_id=jid, company=company,
            title=j.get("title", ""), location=loc,
            url=j.get("absolute_url", ""),
            # The canonical hosted form works even when absolute_url points at a company site.
            apply_url=f"https://job-boards.greenhouse.io/{company}/jobs/{jid}",
            description=html_to_text(j.get("content", "")),
            remote="remote" in loc.lower(),
            posted_at=j.get("updated_at", "") or j.get("first_published", ""),
        ))
    return jobs


def parse_lever(company: str, data: list) -> list[Job]:
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        parts = [j.get("descriptionPlain", "")]
        for lst in j.get("lists") or []:
            parts.append(f"{lst.get('text', '')}\n{html_to_text(lst.get('content', ''))}")
        parts.append(j.get("additionalPlain", ""))
        loc = cats.get("location", "") or ", ".join(cats.get("allLocations") or [])
        sal = j.get("salaryRange") or {}
        jobs.append(Job(
            source="lever", source_id=j["id"], company=company,
            title=j.get("text", ""), location=loc,
            url=j.get("hostedUrl", ""),
            apply_url=j.get("applyUrl") or (j.get("hostedUrl", "") + "/apply"),
            description="\n\n".join(p for p in parts if p).strip(),
            remote=j.get("workplaceType") == "remote" or "remote" in loc.lower(),
            posted_at=str(j.get("createdAt", "")),
            salary=f"{sal.get('min')}-{sal.get('max')} {sal.get('currency', '')}".strip() if sal else "",
        ))
    return jobs


def parse_ashby(company: str, data: dict) -> list[Job]:
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        comp = (j.get("compensation") or {}).get("compensationTierSummary", "") or ""
        jobs.append(Job(
            source="ashby", source_id=j["id"], company=company,
            title=j.get("title", ""), location=j.get("location", ""),
            url=j.get("jobUrl", ""),
            apply_url=j.get("applyUrl") or (j.get("jobUrl", "") + "/application"),
            description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
            remote=bool(j.get("isRemote")) or j.get("workplaceType") == "Remote",
            posted_at=j.get("publishedAt", ""),
            salary=comp,
        ))
    return jobs


def parse_remotive(data: dict) -> list[Job]:
    jobs = []
    for j in data.get("jobs", []):
        jobs.append(Job(
            source="remotive", source_id=str(j["id"]), company=j.get("company_name", ""),
            title=j.get("title", ""), location=j.get("candidate_required_location", ""),
            url=j.get("url", ""), apply_url=j.get("url", ""),
            description=html_to_text(j.get("description", "")),
            remote=True, posted_at=j.get("publication_date", ""),
            salary=j.get("salary", ""),
        ))
    return jobs


# ---------------------------------------------------------------- fetchers

def fetch_greenhouse(company: str) -> list[Job]:
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs",
                     {"content": "true"})
    return parse_greenhouse(company, data)


def fetch_lever(company: str) -> list[Job]:
    return parse_lever(company, _get_json(f"https://api.lever.co/v0/postings/{company}",
                                          {"mode": "json"}))


def fetch_ashby(company: str) -> list[Job]:
    return parse_ashby(company, _get_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{company}",
        {"includeCompensation": "true"}))


def fetch_remotive(search: str = "", category: str = "") -> list[Job]:
    params = {k: v for k, v in {"search": search, "category": category}.items() if v}
    return parse_remotive(_get_json("https://remotive.com/api/remote-jobs", params))


def fetch_all(cfg: dict) -> list[Job]:
    src = cfg["sources"]
    tasks: list[tuple[str, Callable[[], Iterable[Job]]]] = []
    for c in src.get("greenhouse") or []:
        tasks.append((f"greenhouse/{c}", lambda c=c: fetch_greenhouse(c)))
    for c in src.get("lever") or []:
        tasks.append((f"lever/{c}", lambda c=c: fetch_lever(c)))
    for c in src.get("ashby") or []:
        tasks.append((f"ashby/{c}", lambda c=c: fetch_ashby(c)))
    rem = src.get("remotive") or {}
    if rem.get("enabled"):
        searches = rem.get("searches") or [" ".join(cfg["search"]["keywords"][:1])]
        for s in searches:
            tasks.append((f"remotive/{s}", lambda s=s: fetch_remotive(s, rem.get("category", ""))))

    jobs: list[Job] = []
    for name, fn in tasks:
        try:
            got = list(fn())
            log.info("%-35s %4d jobs", name, len(got))
            jobs.extend(got)
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            hint = " (check the company slug)" if code == 404 else ""
            log.warning("%s: HTTP %s%s", name, code, hint)
        except requests.RequestException as e:
            log.warning("%s: %s", name, e)
    return jobs
