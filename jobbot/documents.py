"""Render resumes and cover letters to PDF (via headless Chromium) and DOCX."""
from __future__ import annotations

import os
import re
from datetime import date
from html import escape
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

from jobbot.models import CoverLetter, Job, TailoredResume


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def build_resume_data(profile: dict, t: TailoredResume, education_first: bool = False) -> dict:
    """Merge Claude's tailored text with facts copied verbatim from the profile."""
    exp_by_id = {e["id"]: e for e in profile["experience"]}
    proj_by_id = {p["id"]: p for p in profile.get("projects") or []}
    roles = []
    for r in t.roles:
        e = exp_by_id[r.experience_id]
        roles.append({
            "title": e.get("title", ""), "company": e.get("company", ""),
            "location": e.get("location", ""),
            "dates": f"{e.get('start', '')} – {e.get('end', 'Present')}".strip(" –"),
            "bullets": r.bullets,
        })
    projects = [{"name": proj_by_id[p.project_id].get("name", ""),
                 "url": proj_by_id[p.project_id].get("url", ""),
                 "description": p.description} for p in t.projects]
    links = [l for l in (profile.get("links") or {}).values() if l]
    return {
        "name": profile["name"],
        "contact": " · ".join(x for x in [profile.get("location"), profile.get("email"),
                                          profile.get("phone"), *links] if x),
        "headline": t.headline, "summary": t.summary, "skills": t.skills,
        "roles": roles, "projects": projects,
        "education": profile.get("education") or [],
        "certifications": profile.get("certifications") or [],
        "education_first": education_first,   # student resumes lead with education
        "languages": _str_items(profile.get("languages")),   # copied verbatim from the profile
    }


def _str_items(v) -> list[str]:
    if not v:
        return []
    if isinstance(v, dict):
        return [f"{k}: {val}" if val else str(k) for k, val in v.items()]
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if x]
    return [str(v)]


# ------------------------------------------------------------------ HTML

CSS = """
@page { size: Letter; margin: 0.55in 0.6in; }
body { font-family: 'Helvetica Neue', Arial, sans-serif; font-size: 10.3pt; color: #222;
       line-height: 1.32; margin: 0; }
h1 { font-size: 20pt; margin: 0 0 2px; letter-spacing: .3px; }
.contact { color: #555; font-size: 9.2pt; margin-bottom: 4px; word-break: break-word; }
.headline { font-weight: 600; color: #333; margin-bottom: 8px; }
h2 { font-size: 10.5pt; text-transform: uppercase; letter-spacing: 1px; color: #1f4e79;
     border-bottom: 1px solid #c9d3df; padding-bottom: 2px; margin: 12px 0 5px; }
.role { margin-bottom: 7px; page-break-inside: avoid; }
.row { display: flex; justify-content: space-between; gap: 12px; }
.row b { font-weight: 600; } .muted { color: #555; white-space: nowrap; }
ul { margin: 3px 0 0 16px; padding: 0; } li { margin-bottom: 2px; }
p { margin: 0 0 9px; }
.letter { font-size: 11pt; line-height: 1.5; }
"""


def resume_html(d: dict) -> str:
    e = escape
    parts = [f"<h1>{e(d['name'])}</h1><div class=contact>{e(d['contact'])}</div>",
             f"<div class=headline>{e(d['headline'])}</div>"]
    if d["summary"]:
        parts.append(f"<h2>Summary</h2><p>{e(d['summary'])}</p>")
    edu = []
    if d["education"]:
        edu.append("<h2>Education</h2>")
        for ed in d["education"]:
            line = ", ".join(x for x in [ed.get("degree"), ed.get("school")] if x)
            edu.append(f"<div class=row><span><b>{e(line)}</b>"
                       f"{' — ' + e(ed['details']) if ed.get('details') else ''}</span>"
                       f"<span class=muted>{e(str(ed.get('year', '')))}</span></div>")
    if d.get("education_first"):
        parts.extend(edu)
    if d["skills"]:
        parts.append(f"<h2>Skills</h2><p>{e(' · '.join(d['skills']))}</p>")
    if d.get("languages"):
        parts.append(f"<h2>Languages</h2><p>{e(' · '.join(d['languages']))}</p>")
    parts.append("<h2>Experience</h2>")
    for r in d["roles"]:
        where = ", ".join(x for x in [r["company"], r["location"]] if x)
        parts.append(
            f"<div class=role><div class=row><span><b>{e(r['title'])}</b> — {e(where)}</span>"
            f"<span class=muted>{e(r['dates'])}</span></div><ul>"
            + "".join(f"<li>{e(b)}</li>" for b in r["bullets"]) + "</ul></div>")
    if d["projects"]:
        parts.append("<h2>Projects</h2>")
        for p in d["projects"]:
            url = f" <span class=muted>{e(p['url'])}</span>" if p["url"] else ""
            parts.append(f"<p><b>{e(p['name'])}</b>{url} — {e(p['description'])}</p>")
    if not d.get("education_first"):
        parts.extend(edu)
    if d["certifications"]:
        parts.append("<h2>Certifications</h2><p>" +
                     e(" · ".join(map(str, d["certifications"]))) + "</p>")
    return f"<!doctype html><html><head><meta charset=utf-8><style>{CSS}</style></head><body>{''.join(parts)}</body></html>"


def letter_html(profile: dict, job: Job, cl: CoverLetter) -> str:
    e = escape
    contact = " · ".join(x for x in [profile.get("email"), profile.get("phone"),
                                     profile.get("location")] if x)
    body = "".join(f"<p>{e(p)}</p>" for p in cl.paragraphs)
    return (f"<!doctype html><html><head><meta charset=utf-8><style>{CSS}</style></head>"
            f"<body class=letter><h1>{e(profile['name'])}</h1><div class=contact>{e(contact)}</div>"
            f"<p style='margin-top:18px'>{date.today():%B %d, %Y}</p>"
            f"<p>{e(job.company)}<br>Re: {e(job.title)}</p>"
            f"<p>{e(cl.greeting)}</p>{body}<p>{e(cl.sign_off)}<br>{e(profile['name'])}</p></body></html>")


def letter_text(profile: dict, cl: CoverLetter) -> str:
    return "\n\n".join([cl.greeting, *cl.paragraphs, f"{cl.sign_off}\n{profile['name']}"])


# ------------------------------------------------------------------ renderers

def chromium_launch_kwargs(headless: bool = True) -> dict:
    kw: dict = {"headless": headless}
    if os.environ.get("JOBBOT_CHROMIUM_PATH"):
        kw["executable_path"] = os.environ["JOBBOT_CHROMIUM_PATH"]
    return kw


def html_to_pdf(html: str, out: Path) -> Path:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(**chromium_launch_kwargs())
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        page.pdf(path=str(out), format="Letter", print_background=True,
                 prefer_css_page_size=True)
        browser.close()
    return out


def resume_docx(d: dict, out: Path) -> Path:
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name, st.font.size = "Calibri", Pt(10.5)
    for s in doc.sections:
        s.left_margin = s.right_margin = s.top_margin = s.bottom_margin = Pt(40)
    h = doc.add_paragraph(); r = h.add_run(d["name"]); r.bold = True; r.font.size = Pt(18)
    doc.add_paragraph(d["contact"])
    p = doc.add_paragraph(); p.add_run(d["headline"]).bold = True

    def section(title):
        p = doc.add_paragraph(); run = p.add_run(title.upper()); run.bold = True
        run.font.size = Pt(11)

    if d["summary"]:
        section("Summary"); doc.add_paragraph(d["summary"])

    def education():
        if d["education"]:
            section("Education")
            for ed in d["education"]:
                doc.add_paragraph(", ".join(str(x) for x in [ed.get("degree"), ed.get("school"),
                                                             ed.get("year"), ed.get("details")] if x))

    if d.get("education_first"):
        education()
    if d["skills"]:
        section("Skills"); doc.add_paragraph(" · ".join(d["skills"]))
    if d.get("languages"):
        section("Languages"); doc.add_paragraph(" · ".join(d["languages"]))
    section("Experience")
    for role in d["roles"]:
        p = doc.add_paragraph()
        p.add_run(role["title"]).bold = True
        p.add_run(f" — {', '.join(x for x in [role['company'], role['location']] if x)}")
        p.add_run(f"    {role['dates']}").italic = True
        for b in role["bullets"]:
            doc.add_paragraph(b, style="List Bullet")
    if d["projects"]:
        section("Projects")
        for pr in d["projects"]:
            p = doc.add_paragraph(); p.add_run(pr["name"]).bold = True
            p.add_run(f" — {pr['description']}")
    if not d.get("education_first"):
        education()
    if d["certifications"]:
        section("Certifications"); doc.add_paragraph(" · ".join(map(str, d["certifications"])))
    doc.save(out)
    return out


def letter_docx(profile: dict, job: Job, cl: CoverLetter, out: Path) -> Path:
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)
    p = doc.add_paragraph(); r = p.add_run(profile["name"]); r.bold = True; r.font.size = Pt(16)
    doc.add_paragraph(" · ".join(x for x in [profile.get("email"), profile.get("phone")] if x))
    doc.add_paragraph(f"{date.today():%B %d, %Y}")
    doc.add_paragraph(f"{job.company}\nRe: {job.title}")
    doc.add_paragraph(cl.greeting)
    for para in cl.paragraphs:
        doc.add_paragraph(para).alignment = WD_ALIGN_PARAGRAPH.LEFT
    doc.add_paragraph(f"{cl.sign_off}\n{profile['name']}")
    doc.save(out)
    return out


def job_dir(output_dir: str, job: Job) -> Path:
    d = Path(output_dir) / f"{slug(job.company)}__{slug(job.title)}__{job.source_id[:12]}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_resume(profile: dict, job: Job, t: TailoredResume, output_dir: str,
                 formats: list[str], education_first: bool | None = None) -> Path:
    from jobbot.filters import is_internship
    if education_first is None:
        education_first = is_internship(job)
    d = build_resume_data(profile, t, education_first=education_first)
    folder = job_dir(output_dir, job)
    base = f"{slug(profile['name'])}-resume"
    pdf = folder / f"{base}.pdf"
    (folder / f"{base}.html").write_text(resume_html(d))
    if "docx" in formats:
        resume_docx(d, folder / f"{base}.docx")
    html_to_pdf(resume_html(d), pdf)  # PDF is always produced: it's what gets uploaded
    return pdf


def write_cover_letter(profile: dict, job: Job, cl: CoverLetter, output_dir: str,
                       formats: list[str]) -> Path:
    folder = job_dir(output_dir, job)
    base = f"{slug(profile['name'])}-cover-letter"
    (folder / f"{base}.txt").write_text(letter_text(profile, cl))
    if "docx" in formats:
        letter_docx(profile, job, cl, folder / f"{base}.docx")
    pdf = folder / f"{base}.pdf"
    html_to_pdf(letter_html(profile, job, cl), pdf)
    return pdf
