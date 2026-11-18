"""Offline tests: no network, no API key. Claude and Ollama are replaced with stubs."""
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


# ------------------------------------------------------------ Ollama backend

class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.text = data, status, str(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(self.text)


def _fake_ollama(monkeypatch, replies, models=("qwen2.5:7b",)):
    """Patch requests in jobbot.llm so OllamaLLM talks to a fake server."""
    import jobbot.llm as llm_mod
    sent = []
    monkeypatch.setattr(llm_mod.requests, "get",
                        lambda url, timeout=None: _Resp({"models": [{"name": m} for m in models]}))

    def post(url, json=None, timeout=None):
        sent.append(json)
        return _Resp({"message": {"content": replies.pop(0)}})
    monkeypatch.setattr(llm_mod.requests, "post", post)
    return sent


def test_ollama_scores_and_validates(profile, monkeypatch):
    from jobbot.llm import OllamaLLM
    good = ('{"score": 82, "worth_applying": true, "reasons": ["Python"], '
            '"missing_requirements": [], "red_flags": []}')
    sent = _fake_ollama(monkeypatch, ["not json at all", good])   # first reply is retried
    a = OllamaLLM(profile).assess_fit(make_job())
    assert a.score == 82 and a.worth_applying
    assert len(sent) == 2
    body = sent[0]
    assert body["format"]["title"] == "FitAssessment" and body["stream"] is False
    assert body["options"]["num_ctx"] == 16384
    assert "eeo" not in body["messages"][0]["content"]


def test_ollama_resume_gets_same_truthfulness_guard(profile, monkeypatch):
    from jobbot.llm import OllamaLLM
    reply = TailoredResume(headline="h", summary="s", skills=["Python", "Rust"],
                           roles=[TailoredRole(experience_id="acme", bullets=["b"]),
                                  TailoredRole(experience_id="google", bullets=["made up"])]
                           ).model_dump_json()
    _fake_ollama(monkeypatch, [reply])
    out = OllamaLLM(profile).tailor_resume(make_job())
    assert [r.experience_id for r in out.roles] == ["acme"] and out.skills == ["Python"]


def test_ollama_missing_model_or_server_gives_clear_error(profile, monkeypatch):
    import jobbot.llm as llm_mod
    from jobbot.llm import OllamaLLM
    _fake_ollama(monkeypatch, [], models=("llama3.1:8b",))
    with pytest.raises(SystemExit, match="ollama pull qwen2.5:7b"):
        OllamaLLM(profile)

    def down(url, timeout=None):
        raise llm_mod.requests.ConnectionError("refused")
    monkeypatch.setattr(llm_mod.requests, "get", down)
    with pytest.raises(SystemExit, match="Can't reach Ollama"):
        OllamaLLM(profile)


def test_make_llm_auto_picks_provider(profile, monkeypatch):
    from jobbot.config import DEFAULTS
    from jobbot.llm import LLM as Claude, OllamaLLM, make_llm
    _fake_ollama(monkeypatch, [])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert isinstance(make_llm(DEFAULTS, profile), OllamaLLM)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    assert isinstance(make_llm(DEFAULTS, profile), Claude)
    assert isinstance(make_llm({**DEFAULTS, "provider": "ollama"}, profile), OllamaLLM)


# ------------------------------------------------------------ web page

@pytest.fixture
def web(tmp_path):
    import shutil, threading
    from jobbot.web import create_app
    shutil.copy(ROOT / "config.example.yaml", tmp_path / "config.yaml")
    shutil.copy(ROOT / "profile.example.yaml", tmp_path / "profile.yaml")
    app = create_app(tmp_path / "config.yaml")
    return app, app.test_client(), tmp_path


H = {"X-Jobbot": "1"}


def test_web_summary_and_mark(web):
    from jobbot.config import load_config
    from jobbot.db import DB
    app, c, tmp = web
    db = DB(load_config(tmp / "config.yaml")["database"])
    db.add_new([make_job()])
    key = make_job().key
    db.update(key, status="scored", score=80, assessment={"reasons": ["fits"], "missing_requirements": [],
                                                          "red_flags": ["on-site"]})
    d = c.get("/api/summary").get_json()
    assert d["config_ok"] and d["counts"] == {"scored": 1}
    assert d["jobs"][0]["reasons"] == ["fits"] and d["jobs"][0]["concerns"] == ["on-site"]
    assert c.post("/api/mark", json={"key": key, "status": "applied"}, headers=H).status_code == 200
    assert c.get("/api/summary").get_json()["counts"] == {"applied": 1}


def test_web_blocks_cross_site_and_bad_paths(web):
    app, c, tmp = web
    assert c.post("/api/mark", json={}).status_code == 403                      # no custom header
    assert c.get("/api/summary", headers={"Host": "evil.example"}).status_code == 403
    (tmp / "output").mkdir(exist_ok=True)
    assert c.get("/files/../config.yaml").status_code == 404
    assert c.post("/api/run/rm", headers=H).status_code == 404


def test_web_settings_validate_before_saving(web):
    app, c, tmp = web
    before = (tmp / "profile.yaml").read_text()
    r = c.post("/api/settings/profile", json={"text": "name: [oops"}, headers=H)
    assert r.status_code == 400 and "YAML" in r.get_json()["error"]
    r = c.post("/api/settings/profile", json={"text": "name: Sam\nemail: s@example.com\n"}, headers=H)
    assert r.status_code == 400 and "experience" in r.get_json()["error"]
    assert (tmp / "profile.yaml").read_text() == before
    assert c.post("/api/settings/profile", json={"text": before}, headers=H).status_code == 200


def test_web_review_question_answered_from_page(web):
    import threading, time
    app, c, tmp = web
    runner = app.config["runner"]
    result = {}

    def task():
        result["v"] = runner.ask_human(make_job())
    assert runner.start("apply", task)
    for _ in range(100):
        if c.get("/api/task").get_json()["review"]:
            break
        time.sleep(0.02)
    assert c.get("/api/task").get_json()["review"]["title"] == "Senior Backend Engineer"
    assert c.post("/api/review", json={"answer": "applied"}, headers=H).status_code == 200
    for _ in range(100):
        if not c.get("/api/task").get_json()["running"]:
            break
        time.sleep(0.02)
    assert result["v"][0] == "applied"
    assert any("Waiting for you" in line for line in c.get("/api/task").get_json()["lines"])


# ------------------------------------------------------------ internships / co-ops

def test_internship_focus_filters():
    from jobbot.filters import is_internship
    base = {"keywords": ["software", "backend"], "exclude_keywords": ["senior", "intern"],
            "locations": ["new york"], "remote_ok": True, "max_age_days": 0}
    intern = make_job(title="Software Engineering Intern (Summer 2027)")
    coop = make_job(title="Backend Developer Co-op")
    regular = make_job(title="Backend Engineer")
    assert is_internship(intern) and is_internship(coop) and not is_internship(regular)
    jobs = {**base, "focus": "jobs"}
    assert not passes(intern, jobs)[0] and passes(regular, jobs)[0]      # "intern" excluded as before
    only = {**base, "focus": "internships"}
    assert passes(intern, only)[0] and passes(coop, only)[0]
    assert passes(regular, only) == (False, "not an internship or co-op")
    both = {**base, "focus": "both"}
    assert passes(intern, both)[0] and passes(regular, both)[0]
    assert not passes(make_job(title="Senior Software Intern"), only)[0]  # other exclusions still apply
    # short intern titles pass on a single description hit
    short = make_job(title="Summer Intern", description="Join our software team.")
    assert passes(short, only)[0] and not passes(short, {**only, "keywords": ["marketing"]})[0]


def test_internship_prompts_and_resume_layout(profile, monkeypatch):
    from jobbot.llm import OllamaLLM
    good = ('{"score": 75, "worth_applying": true, "reasons": ["r"], '
            '"missing_requirements": [], "red_flags": []}')
    sent = _fake_ollama(monkeypatch, [good, good])
    OllamaLLM(profile, focus="internships").assess_fit(make_job(title="Data Intern"))
    OllamaLLM(profile, focus="jobs").assess_fit(make_job())
    assert "ONLY for internships" in sent[0]["messages"][1]["content"]
    assert "ONLY for internships" not in sent[1]["messages"][1]["content"]
    d = documents.build_resume_data(profile, sample_tailored(), education_first=True)
    html = documents.resume_html(d)
    assert html.index("Education") < html.index("Experience")
    html = documents.resume_html(documents.build_resume_data(profile, sample_tailored()))
    assert html.index("Experience") < html.index("Education")


def test_web_preferences_round_trip_keeps_comments(web):
    app, c, tmp = web
    p = c.get("/api/preferences").get_json()
    assert p["focus"] == "jobs" and "stripe" in p["greenhouse"]
    p.update(focus="internships", keywords="software, data", internship_term="Summer 2027",
             min_salary_usd="", greenhouse="stripe, figma")
    r = c.post("/api/preferences", json=p, headers=H)
    assert r.status_code == 200, r.get_json()
    p2 = c.get("/api/preferences").get_json()
    assert p2["focus"] == "internships" and p2["keywords"] == ["software", "data"]
    assert p2["internship_term"] == "Summer 2027" and p2["greenhouse"] == ["stripe", "figma"]
    assert p2["min_salary_usd"] == ""
    cfg_text = (tmp / "config.yaml").read_text()
    assert "# Company \"slugs\"" in cfg_text and "focus: internships" in cfg_text   # comments survive
    assert "# What you want" in (tmp / "profile.yaml").read_text()
    assert c.get("/api/summary").get_json()["focus"] == "internships"
    bad = dict(p2, min_score="150")
    assert "Minimum fit score" in c.post("/api/preferences", json=bad, headers=H).get_json()["error"]
    none = dict(p2, greenhouse="", lever="", ashby="", remotive_enabled=False)
    assert "at least one company" in c.post("/api/preferences", json=none, headers=H).get_json()["error"]


def test_find_revives_filtered_jobs_after_focus_change(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    import jobbot.cli as cli
    import jobbot.sources as sources
    from jobbot.config import DEFAULTS
    from jobbot.db import DB
    db = DB(str(tmp_path / "j.db"))
    intern = make_job(title="Software Intern", source_id="9")
    db.add_new([intern]); db.update(intern.key, status="filtered", note="title contains excluded 'intern'")
    monkeypatch.setattr(sources, "fetch_all", lambda cfg: [])
    cfg = {**DEFAULTS, "search": {**DEFAULTS["search"], "keywords": ["software"],
                                  "exclude_keywords": ["intern"], "focus": "internships"},
           "matching": {"min_score": 70, "max_jobs_to_score": 0}}
    cli.cmd_find(cfg, {}, db, SimpleNamespace())
    assert db.get(intern.key)["status"] == "new"
    assert "1 earlier postings now match" in capsys.readouterr().out
