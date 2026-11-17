"""Offline tests: no network, no API key. Claude is replaced with a stub."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jobbot import documents
from jobbot.apply import fill_application, submit
from jobbot.config import load_profile
from jobbot.filters import passes
from jobbot.llm import LLM
from jobbot.models import (CoverLetter, FieldAnswer, FormAnswers, Job, TailoredProject,
                           TailoredResume, TailoredRole)
from jobbot.sources import parse_ashby, parse_greenhouse, parse_lever

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def profile():
    return load_profile(ROOT / "profile.example.yaml")


def make_job(**kw):
    d = dict(source="greenhouse", source_id="1", company="exampleco", title="Senior Backend Engineer",
             location="New York, NY", url="u", apply_url="u", description="Python, Kafka, AWS.")
    d.update(kw)
    return Job(**d)


# ------------------------------------------------------------ sources

def test_parse_greenhouse():
    data = {"jobs": [{"id": 42, "title": "Backend Engineer", "location": {"name": "Remote - US"},
                      "absolute_url": "https://x.com/jobs/42",
                      "content": "&lt;p&gt;We use &lt;b&gt;Python&lt;/b&gt;&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Kafka&lt;/li&gt;&lt;/ul&gt;",
                      "updated_at": "2026-11-01T00:00:00-04:00"}]}
    [j] = parse_greenhouse("acme", data)
    assert j.apply_url == "https://job-boards.greenhouse.io/acme/jobs/42"
    assert "We use Python" in j.description and "• Kafka" in j.description
    assert j.remote and "<" not in j.description


def test_parse_lever_and_ashby():
    [l] = parse_lever("plaid", [{"id": "abc", "text": "SWE", "categories": {"location": "SF"},
                                 "descriptionPlain": "Desc", "lists": [{"text": "Reqs", "content": "<li>Go</li>"}],
                                 "hostedUrl": "https://jobs.lever.co/plaid/abc",
                                 "applyUrl": "https://jobs.lever.co/plaid/abc/apply",
                                 "workplaceType": "remote", "createdAt": 1762000000000}])
    assert l.remote and "Reqs" in l.description and l.apply_url.endswith("/apply")
    [a] = parse_ashby("ramp", {"jobs": [{"id": "z", "title": "Eng", "location": "NYC", "isRemote": False,
                                         "jobUrl": "https://jobs.ashbyhq.com/ramp/z", "descriptionPlain": "d"},
                                        {"id": "hidden", "title": "x", "isListed": False}]})
    assert a.apply_url == "https://jobs.ashbyhq.com/ramp/z/application"


def test_filters():
    s = {"keywords": ["backend", "python"], "exclude_keywords": ["staff", "intern"],
         "locations": ["new york"], "remote_ok": True, "max_age_days": 30}
    assert passes(make_job(), s)[0]
    assert not passes(make_job(title="Staff Backend Engineer"), s)[0]
    assert not passes(make_job(title="Accountant", description="excel"), s)[0]
    assert not passes(make_job(location="Berlin"), s)[0]
    assert passes(make_job(location="Berlin", remote=True), s)[0]
    assert not passes(make_job(posted_at="2020-01-01T00:00:00Z"), s)[0]
    # "intern" must not match "internal"
    assert passes(make_job(title="Backend Engineer, Internal Tools"), s)[0]


# ------------------------------------------------------------ truthfulness guard

def test_resume_validation_drops_fabrications(profile, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    llm = LLM(profile)
    r = TailoredResume(headline="h", summary="s", skills=["Python", "Rust", "Kafka"],
                       roles=[TailoredRole(experience_id="acme", bullets=["b"]),
                              TailoredRole(experience_id="google", bullets=["made up"])],
                       projects=[TailoredProject(project_id="nope", description="x")])
    out = llm._validate_resume(r)
    assert [x.experience_id for x in out.roles] == ["acme"]
    assert out.skills == ["Python", "Kafka"]        # Rust not in profile
    assert out.projects == []
    assert "eeo" not in llm.system[0]["text"]       # EEO answers not sent for scoring/writing


# ------------------------------------------------------------ documents

def sample_tailored():
    return TailoredResume(headline="Backend engineer: Python, Kafka, AWS", summary="Six years...",
                          skills=["Python", "Kafka", "AWS"],
                          roles=[TailoredRole(experience_id="acme", bullets=["Led migration ...", "Kafka pipeline ..."]),
                                 TailoredRole(experience_id="brightdata", bullets=["Billing API ..."])],
                          projects=[TailoredProject(project_id="tinyq", description="Task queue")])


def test_documents(profile, tmp_path):
    job = make_job()
    pdf = documents.write_resume(profile, job, sample_tailored(), str(tmp_path), ["pdf", "docx"])
    assert pdf.exists() and pdf.read_bytes()[:4] == b"%PDF"
    assert pdf.with_suffix(".docx").exists()
    html = pdf.with_suffix(".html").read_text()
    # facts come from the profile, not from Claude
    assert "Acme Logistics" in html and "Mar 2022 – Present" in html and "Rutgers" in html
    cl = CoverLetter(greeting="Dear Hiring Team,", paragraphs=["P1", "P2"], sign_off="Best regards,")
    cpdf = documents.write_cover_letter(profile, job, cl, str(tmp_path), ["pdf"])
    assert cpdf.read_bytes()[:4] == b"%PDF"


# ------------------------------------------------------------ form filling

class StubLLM:
    """Answers like Claude would, from the example profile."""
    def __init__(self):
        self.asked = []

    def answer_fields(self, job, fields):
        self.asked.extend(f.label for f in fields)
        ans = []
        for f in fields:
            l = f.label.lower()
            if "authorized" in l:
                a = FieldAnswer(field_id=f.field_id, answer="Yes", needs_human=False)
            elif "why do you want" in l:
                a = FieldAnswer(field_id=f.field_id, answer="Because ...", needs_human=False)
            elif "clearance" in l:
                a = FieldAnswer(field_id=f.field_id, answer="", needs_human=True)
            elif "gender" in l:
                a = FieldAnswer(field_id=f.field_id, answer="Decline to self-identify", needs_human=False)
            elif "privacy" in l:
                a = FieldAnswer(field_id=f.field_id, answer="Yes", needs_human=False)
            elif "sponsorship" in l:
                a = FieldAnswer(field_id=f.field_id, answer="No", needs_human=False)
            elif "languages" in l:
                a = FieldAnswer(field_id=f.field_id, answer="Python | Go", needs_human=False)
            else:
                a = FieldAnswer(field_id=f.field_id, answer="", needs_human=True)
            ans.append(a)
        return FormAnswers(answers=ans)


@pytest.fixture
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(**documents.chromium_launch_kwargs())
        yield b
        b.close()


def test_greenhouse_style_form(browser, profile, tmp_path):
    resume = tmp_path / "r.pdf"; resume.write_bytes(b"%PDF-1.4 test")
    cover = tmp_path / "c.pdf"; cover.write_bytes(b"%PDF-1.4 cl")
    page = browser.new_page()
    page.goto((FIX / "greenhouse_form.html").as_uri())
    llm = StubLLM()
    rep = fill_application(page, make_job(), profile, llm, resume, lambda: (cover, "letter text"))

    v = lambda sel: page.locator(sel).input_value()
    assert v("#first_name") == "Alex" and v("#last_name") == "Rivera"
    assert v("#email") == profile["email"] and v("#phone") == profile["phone"]
    assert v("#q1") == profile["links"]["linkedin"]
    assert page.locator("#q2 option:checked").inner_text() == "Yes"
    assert v("#q3") == "Because ..."
    assert v("#q4") == ""                                    # left for the human
    assert page.locator("input[name=gender][value=d]").is_checked()
    assert v("#loc") == "Brooklyn, NY, United States"        # picked from autocomplete
    assert page.locator("input[name=consent]").is_checked()
    assert page.evaluate("document.getElementById('resume').files[0].name") == "r.pdf"
    assert page.evaluate("document.getElementById('cover_letter').files[0].name") == "c.pdf"
    assert rep.has_cover_letter_field and not rep.captcha
    assert rep.needs_human == ["Do you have an active security clearance?"]
    assert not any("first name" in a.lower() or "email" in a.lower() for a in llm.asked)  # no LLM for basics

    page.fill("#q4", "No")
    ok, note = submit(page)
    assert ok, note


def test_lever_style_form(browser, profile, tmp_path):
    resume = tmp_path / "r.pdf"; resume.write_bytes(b"%PDF-1.4 test")
    page = browser.new_page()
    page.goto((FIX / "lever_form.html").as_uri())
    rep = fill_application(page, make_job(source="lever"), profile, StubLLM(), resume,
                           lambda: (resume, "Dear Hiring Team, ..."))
    v = lambda sel: page.locator(sel).input_value()
    assert v("input[name=name]") == "Alex Rivera"
    assert v("input[name=org]") == "Acme Logistics"
    assert v("input[name='urls[GitHub]']") == profile["links"]["github"]
    assert page.locator("input[name='cards[abc][field0]'][value=No]").is_checked()
    assert page.locator("input[value=Python]").is_checked() and page.locator("input[value=Go]").is_checked()
    assert not page.locator("input[value=Rust]").is_checked()
    assert v("textarea[name=comments]").startswith("Dear Hiring Team")   # cover letter as text
    assert rep.captcha                                                  # auto mode would stop here
