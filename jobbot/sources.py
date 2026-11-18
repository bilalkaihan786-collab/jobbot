"""Job sources.

Only public, documented job-board APIs that employers publish on purpose are used:
  * Greenhouse  https://boards-api.greenhouse.io/v1/boards/{company}/jobs
  * Lever       https://api.lever.co/v0/postings/{company}
  * Ashby       https://api.ashbyhq.com/posting-api/job-board/{company}
  * Remotive    https://remotive.com/api/remote-jobs   (discovery only; apply manually)
  * Adzuna      https://api.adzuna.com/v1/api/jobs/{country}/search/{page}
                Job search across many employers (hospitals, universities, governments...).
                Needs a free app_id/app_key from https://developer.adzuna.com. Discovery only:
                you apply on the employer's site. Adzuna returns only a description snippet.

LinkedIn, Indeed, etc. are deliberately NOT scraped: their terms forbid bots and
accounts get banned.
"""
from __future__ import annotations

import html
import logging
import os
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


CURRENCY = {"ca": "CAD", "us": "USD", "gb": "GBP", "fr": "EUR", "de": "EUR", "au": "AUD",
            "nz": "NZD", "in": "INR", "sg": "SGD", "za": "ZAR", "br": "BRL", "mx": "MXN"}


def parse_adzuna(data: dict, country: str = "ca") -> list[Job]:
    jobs = []
    for j in data.get("results", []):
        loc_obj = j.get("location") or {}
        area = [a for a in (loc_obj.get("area") or []) if a]
        loc = (loc_obj.get("display_name") or "").strip()
        # display_name is e.g. "Ottawa, Ontario"; add province/country so filters like "Canada" work.
        for a in area[1:2] + area[:1]:
            if a.lower() not in loc.lower():
                loc = f"{loc}, {a}" if loc else a
        desc = html_to_text(j.get("description", ""))
        details = [x.replace("_", "-") for x in (j.get("contract_time"), j.get("contract_type")) if x]
        if details:
            desc = f"[{', '.join(details)}] {desc}"
        lo, hi = j.get("salary_min"), j.get("salary_max")
        salary = ""
        if (lo or hi) and not int(j.get("salary_is_predicted") or 0):
            cur = CURRENCY.get(country, "")
            salary = (f"{lo:,.0f}-{hi:,.0f} {cur}" if lo and hi and lo != hi
                      else f"{(lo or hi):,.0f} {cur}").strip()
        title = html_to_text(j.get("title", ""))
        url = j.get("redirect_url", "")
        jobs.append(Job(
            source="adzuna", source_id=str(j.get("id", "")),
            company=((j.get("company") or {}).get("display_name") or "Employer not listed").strip(),
            title=title, location=loc, url=url, apply_url=url, description=desc,
            remote="remote" in f"{title} {loc} {desc[:300]}".lower(),
            posted_at=j.get("created", ""), salary=salary,
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


def adzuna_keys(ad: dict) -> tuple[str, str]:
    return (str(ad.get("app_id") or os.environ.get("ADZUNA_APP_ID") or "").strip(),
            str(ad.get("app_key") or os.environ.get("ADZUNA_APP_KEY") or "").strip())


def fetch_adzuna(ad: dict, what: str, max_age_days: int | None = None) -> list[Job]:
    app_id, app_key = adzuna_keys(ad)
    country = (ad.get("country") or "ca").lower()
    params = {"app_id": app_id, "app_key": app_key, "what": what,
              "results_per_page": int(ad.get("results_per_page") or 50),
              "content-type": "application/json"}
    if ad.get("where"):
        params["where"] = ad["where"]
    if max_age_days:
        params["max_days_old"] = int(max_age_days)
    data = _get_json(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1", params)
    return parse_adzuna(data, country)


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

    ad = src.get("adzuna") or {}
    if ad.get("enabled") and ad.get("searches"):
        if all(adzuna_keys(ad)):
            for s in ad["searches"]:
                tasks.append((f"adzuna/{s}",
                              lambda s=s: fetch_adzuna(ad, s, cfg["search"].get("max_age_days"))))
        else:
            log.warning("Adzuna is on but has no app_id/app_key. Get a free key at "
                        "https://developer.adzuna.com and add it on the Preferences tab.")

    jobs: list[Job] = []
    seen: set[str] = set()
    for name, fn in tasks:
        try:
            got = list(fn())
            log.info("%-35s %4d jobs", name, len(got))
            for j in got:           # several searches can return the same posting
                if j.key not in seen:
                    seen.add(j.key)
                    jobs.append(j)
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            hint = (" (check your Adzuna app_id/app_key)" if name.startswith("adzuna") and code in (401, 403)
                    else " (check the company slug)" if code == 404 else "")
            log.warning("%s: HTTP %s%s", name, code, hint)
        except requests.RequestException as e:
            log.warning("%s: %s", name, e)
    return jobs
