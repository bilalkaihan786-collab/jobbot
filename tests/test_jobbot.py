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
    return load_profile(FIX / "profile_engineer.yaml")


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
    assert body["options"]["num_ctx"] == 8192 and body["keep_alive"] == "30m"
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
    shutil.copy(FIX / "config_engineer.yaml", tmp_path / "config.yaml")
    shutil.copy(FIX / "profile_engineer.yaml", tmp_path / "profile.yaml")
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
    assert "at least one employer" in c.post("/api/preferences", json=none, headers=H).get_json()["error"]


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


# ------------------------------------------------------------ student / Adzuna setup

ADZUNA_SAMPLE = {"results": [
    {"id": "4410", "title": " Research Assistant - <strong>Kinesiology</strong> Lab ",
     "company": {"display_name": "University of Ottawa"},
     "location": {"area": ["Canada", "Ontario", "Ottawa"], "display_name": "Ottawa, Ontario"},
     "description": "Bilingual (English/French) student to support exercise studies...",
     "created": "2026-11-10T12:00:00Z", "redirect_url": "https://www.adzuna.ca/land/ad/4410",
     "contract_time": "part_time", "salary_min": 18, "salary_max": 22, "salary_is_predicted": "0"},
    {"id": "4411", "title": "French Tutor", "company": {},
     "location": {"area": ["Canada", "Quebec"], "display_name": "Quebec"},
     "description": "Remote tutoring", "created": "2026-11-12T00:00:00Z",
     "redirect_url": "https://www.adzuna.ca/land/ad/4411", "salary_min": 40000,
     "salary_is_predicted": "1"},
]}


def test_parse_adzuna():
    from jobbot.sources import parse_adzuna
    a, b = parse_adzuna(ADZUNA_SAMPLE, "ca")
    assert a.source == "adzuna" and a.key == "adzuna:University of Ottawa:4410"
    assert a.title == "Research Assistant - Kinesiology Lab"           # HTML and padding removed
    assert a.location == "Ottawa, Ontario, Canada"
    assert a.description.startswith("[part-time] Bilingual")
    assert a.salary == "18-22 CAD" and a.url == a.apply_url and not a.can_auto_apply
    assert b.company == "Employer not listed" and b.location == "Quebec, Canada"
    assert b.salary == "" and b.remote                                   # predicted salary skipped


def test_fetch_all_adzuna(monkeypatch, caplog):
    import jobbot.sources as src
    from jobbot.config import DEFAULTS
    calls = []

    def fake_get(url, params=None):
        calls.append((url, params))
        return ADZUNA_SAMPLE
    monkeypatch.setattr(src, "_get_json", fake_get)
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    ad = {"enabled": True, "country": "ca", "where": "Ottawa", "app_id": "", "app_key": "",
          "searches": ["research assistant", "french tutor"]}
    cfg = {**DEFAULTS, "sources": {**DEFAULTS["sources"], "adzuna": ad}}
    assert src.fetch_all(cfg) == [] and not calls
    assert "developer.adzuna.com" in caplog.text
    ad.update(app_id="id1", app_key="key1")
    jobs = src.fetch_all(cfg)
    assert len(calls) == 2 and len(jobs) == 2                            # duplicates dropped
    url, params = calls[0]
    assert url == "https://api.adzuna.com/v1/api/jobs/ca/search/1"
    assert params["what"] == "research assistant" and params["where"] == "Ottawa"
    assert params["app_id"] == "id1" and params["max_days_old"] == 30


def test_students_focus_filters():
    s = {"keywords": ["research", "french", "clinic"], "exclude_keywords": ["senior", "physician"],
         "locations": [], "remote_ok": True, "max_age_days": 0, "focus": "students"}
    assert passes(make_job(title="Clinical Research Assistant"), s)[0]       # no "intern" needed
    assert passes(make_job(title="Summer Camp Counsellor", description="Bilingual French camp"), s)[0]
    assert not passes(make_job(title="Senior Research Scientist"), s)[0]
    assert not passes(make_job(title="Physician, Clinic"), s)[0]
    assert not passes(make_job(title="Cashier", description="retail"), s)[0]
    from jobbot.filters import normalize_focus
    assert normalize_focus("both") == "students" and normalize_focus(None) == "jobs"


def test_student_prompt_mentions_licences_and_languages(profile, monkeypatch):
    from jobbot.llm import OllamaLLM
    good = ('{"score": 70, "worth_applying": true, "reasons": ["r"], '
            '"missing_requirements": [], "red_flags": []}')
    sent = _fake_ollama(monkeypatch, [good])
    OllamaLLM(profile, focus="students").assess_fit(make_job(source="adzuna"))
    prompt = sent[0]["messages"][1]["content"]
    assert "STUDENT" in prompt and "licence" in prompt and "languages" in prompt
    assert "snippet" in prompt                                           # Adzuna descriptions are partial


def test_example_profile_and_languages():
    from types import SimpleNamespace
    import jobbot.cli as cli
    p = load_profile(ROOT / "profile.example.yaml")
    assert p["example_profile"] and "French (fluent, spoken and written)" in p["languages"]
    cfg = load_config_example()
    assert cfg["search"]["focus"] == "students" and cfg["sources"]["adzuna"]["enabled"]
    assert "engineer" in cfg["search"]["exclude_keywords"]
    d = documents.build_resume_data(p, TailoredResume(
        headline="h", summary="s", skills=[], roles=[TailoredRole(experience_id="clinic", bullets=["b"])]),
        education_first=True)
    html = documents.resume_html(d)
    assert "<h2>Languages</h2>" in html and "French (fluent" in html
    assert html.index("Education") < html.index("Experience")


def load_config_example():
    from jobbot.config import load_config
    return load_config(ROOT / "config.example.yaml")


def test_example_profile_blocks_scoring_and_resumes(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    import jobbot.cli as cli
    import jobbot.sources as sources
    from jobbot.db import DB
    p = load_profile(ROOT / "profile.example.yaml")
    cfg = load_config_example()
    db = DB(str(tmp_path / "j.db"))
    monkeypatch.setattr(sources, "fetch_all", lambda c: [make_job(title="Clinical Research Assistant")])
    monkeypatch.setattr(cli, "_llm", lambda *a: (_ for _ in ()).throw(AssertionError("no AI calls")))
    cli.cmd_find(cfg, p, db, SimpleNamespace())
    cli.cmd_prepare(cfg, p, db, SimpleNamespace(limit=None))
    out = capsys.readouterr().out
    assert "Skipping scoring" in out and out.count("still the example") == 2
    assert db.counts() == {"new": 1}


def test_web_adzuna_preferences(web):
    app, c, tmp = web
    p = c.get("/api/preferences").get_json()
    p.update(focus="students", schedule=["summer", "part-time", "bogus"], targets="research, french teaching",
             adzuna_enabled=True, adzuna_country="ca", adzuna_where="Ottawa", adzuna_app_id="abc",
             adzuna_app_key="xyz", adzuna_searches="research assistant, french tutor",
             greenhouse="", lever="", ashby="")
    assert c.post("/api/preferences", json=p, headers=H).status_code == 200
    p2 = c.get("/api/preferences").get_json()
    assert p2["focus"] == "students" and p2["schedule"] == ["summer", "part-time"]
    assert p2["adzuna_searches"] == ["research assistant", "french tutor"] and p2["adzuna_app_key"] == "xyz"
    s = c.get("/api/summary").get_json()
    assert s["adzuna"] == {"enabled": True, "has_keys": True, "country": "ca", "where": "Ottawa",
                           "searches": ["research assistant", "french tutor"]}
    assert s["example_profile"] is False
    r = c.post("/api/preferences", json=dict(p2, adzuna_searches=""), headers=H)
    assert "Adzuna search" in r.get_json()["error"]


# ------------------------------------------------------------ progress, stop, missing packages

def test_find_shows_progress_and_stops(tmp_path, monkeypatch, capsys, profile):
    from types import SimpleNamespace
    import jobbot.cli as cli
    import jobbot.sources as sources
    from jobbot.config import DEFAULTS
    from jobbot.db import DB
    from jobbot.models import FitAssessment
    db = DB(str(tmp_path / "j.db"))
    jobs = [make_job(title=f"Backend Engineer {i}", source_id=str(i), company=f"Org {i}") for i in range(3)]
    monkeypatch.setattr(sources, "fetch_all", lambda c: jobs)
    calls = []

    class FakeLLM:
        def assess_fit(self, j):
            calls.append(j.title)
            return FitAssessment(score=80, worth_applying=True, reasons=[], missing_requirements=[],
                                 red_flags=[])
    monkeypatch.setattr(cli, "_llm", lambda *a: FakeLLM())
    cfg = {**DEFAULTS, "search": {**DEFAULTS["search"], "keywords": ["backend"]}}
    stop_after = {"n": 1}
    cli.cmd_find(cfg, profile, db, SimpleNamespace(should_stop=lambda: len(calls) >= stop_after["n"]))
    out = capsys.readouterr().out
    assert "[1/3] scoring: Backend Engineer 0" in out and "good match: 80/100" in out
    assert "Stopped." in out and len(calls) == 1
    assert db.counts() == {"scored": 1, "new": 2}


def test_web_stop_and_missing_packages(web, monkeypatch):
    import time
    import jobbot.web as webmod
    app, c, tmp = web
    runner = app.config["runner"]
    assert c.post("/api/stop", headers=H).status_code == 409
    seen = {}

    def task():
        while not runner.stop_requested:
            time.sleep(0.01)
        seen["stopped"] = True
    runner.start("find", task)
    assert c.post("/api/stop", headers=H).status_code == 200
    for _ in range(200):
        if not c.get("/api/task").get_json()["running"]:
            break
        time.sleep(0.01)
    assert seen.get("stopped")
    monkeypatch.setattr(webmod, "missing_packages", lambda: ["ruamel"])
    s = c.get("/api/summary").get_json()
    assert s["missing_packages"] == ["ruamel"] and "pip install -r requirements.txt" in s["fix_packages"]

    def boom(*a):
        raise ModuleNotFoundError("No module named 'ruamel'", name="ruamel")
    monkeypatch.setattr(webmod, "write_preferences", boom)
    p = c.get("/api/preferences").get_json()
    r = c.post("/api/preferences", json=p, headers=H)
    assert r.status_code == 500 and "pip install -r requirements.txt" in r.get_json()["error"]


def test_old_large_ollama_context_is_reduced(profile, monkeypatch):
    from jobbot.config import DEFAULTS
    from jobbot.llm import make_llm
    _fake_ollama(monkeypatch, [])
    cfg = {**DEFAULTS, "provider": "ollama", "ollama": {**DEFAULTS["ollama"], "num_ctx": 16384}}
    assert make_llm(cfg, profile).num_ctx == 8192
    cfg["ollama"]["num_ctx"] = 4096
    assert make_llm(cfg, profile).num_ctx == 4096


def test_restart_needed_after_code_changes(web, monkeypatch):
    import jobbot.web as webmod
    app, c, tmp = web
    assert c.get("/api/summary").get_json()["restart_needed"] is False
    monkeypatch.setattr(webmod, "code_fingerprint", lambda: ("changed",))
    s = c.get("/api/summary").get_json()
    assert s["restart_needed"] is True and "Ctrl+C" in s["restart_how"]


def test_serve_refuses_busy_port(monkeypatch, tmp_path, capsys):
    import jobbot.web as webmod
    monkeypatch.setattr(webmod, "port_in_use", lambda port: True)
    with pytest.raises(SystemExit):
        webmod.serve(str(tmp_path / "config.yaml"), port=8000, open_browser=False)
    assert "already running at http://localhost:8000" in capsys.readouterr().out


# ------------------------------------------------------------ speed + choosing resumes

def test_scoring_prompt_puts_job_last_for_reuse(profile, monkeypatch):
    from jobbot.llm import OllamaLLM
    good = ('{"score": 70, "worth_applying": true, "reasons": ["r"], '
            '"missing_requirements": [], "red_flags": []}')
    sent = _fake_ollama(monkeypatch, [good, good])
    llm = OllamaLLM(profile, focus="students")
    llm.assess_fit(make_job(title="Job A", description="aaa"))
    llm.assess_fit(make_job(title="Job B", description="bbb", source="adzuna"))
    a, b = (s["messages"][1]["content"] for s in sent)
    assert a.startswith("Reply with only a JSON object") and a.rstrip().endswith("</job>")
    # everything before the snippet note / job block is identical between jobs
    shared = a[:a.index("<job>")]
    assert b.startswith(shared) and "snippet" in b[len(shared):]


def test_find_scores_most_relevant_first(tmp_path, monkeypatch, profile):
    from types import SimpleNamespace
    import jobbot.cli as cli
    import jobbot.sources as sources
    from jobbot.config import DEFAULTS
    from jobbot.db import DB
    from jobbot.models import FitAssessment
    db = DB(str(tmp_path / "j.db"))
    weak = make_job(title="Clinic Receptionist", source_id="1", description="front desk")
    strong = make_job(title="Clinical Research Assistant, Kinesiology", source_id="2",
                      description="research kinesiology clinic")
    monkeypatch.setattr(sources, "fetch_all", lambda c: [weak, strong])
    order = []

    class FakeLLM:
        def assess_fit(self, j):
            order.append(j.title)
            return FitAssessment(score=80, worth_applying=True, reasons=[], missing_requirements=[], red_flags=[])
    monkeypatch.setattr(cli, "_llm", lambda *a: FakeLLM())
    cfg = {**DEFAULTS, "search": {**DEFAULTS["search"], "keywords": ["clinic", "research", "kinesiology"]},
           "matching": {"min_score": 70, "max_jobs_to_score": 1}}
    cli.cmd_find(cfg, profile, db, SimpleNamespace())
    assert order == ["Clinical Research Assistant, Kinesiology"]       # best one scored within the limit


def test_prepare_only_chosen_jobs(tmp_path, monkeypatch, profile):
    from types import SimpleNamespace
    import jobbot.cli as cli
    from jobbot.config import DEFAULTS
    from jobbot.db import DB
    db = DB(str(tmp_path / "j.db"))
    a, b = make_job(title="A", source_id="1"), make_job(title="B", source_id="2")
    db.add_new([a, b])
    for j in (a, b):
        db.update(j.key, status="scored", score=80)
    made = []

    class FakeLLM:
        def tailor_resume(self, j):
            made.append(j.title)
            return sample_tailored()
    monkeypatch.setattr(cli, "_llm", lambda *x: FakeLLM())
    monkeypatch.setattr(documents, "write_resume", lambda *x, **k: tmp_path / "r.pdf")
    cfg = {**DEFAULTS, "output_dir": str(tmp_path), "documents": {"cover_letter": "never", "formats": ["pdf"]}}
    cli.cmd_prepare(cfg, profile, db, SimpleNamespace(limit=None, keys=[b.key]))
    assert made == ["B"] and db.get(a.key)["status"] == "scored" and db.get(b.key)["status"] == "ready"


def test_web_prepare_needs_a_choice(web):
    app, c, tmp = web
    r = c.post("/api/run/prepare", json={}, headers=H)
    assert r.status_code == 400 and "Pick at least one job" in r.get_json()["error"]


def test_missing_pdf_browser_explains_fix(monkeypatch, tmp_path):
    import playwright.sync_api as pw

    class FakeChromium:
        def launch(self, **k):
            raise Exception("BrowserType.launch: Executable doesn't exist at /x/chrome")

    class FakePW:
        chromium = FakeChromium()
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(pw, "sync_playwright", lambda: FakePW())
    with pytest.raises(RuntimeError, match="python -m playwright install chromium"):
        documents.html_to_pdf("<p>x</p>", tmp_path / "x.pdf")


def test_allow_any_host_opt_in(web, monkeypatch):
    app, c, tmp = web
    assert c.get("/api/summary", headers={"Host": "preview.example"}).status_code == 403
    monkeypatch.setenv("JOBBOT_ALLOW_ANY_HOST", "1")
    assert c.get("/api/summary", headers={"Host": "preview.example"}).status_code == 200
    assert c.post("/api/mark", json={}, headers={"Host": "preview.example"}).status_code == 403  # header still needed


# ------------------------------------------------------------ diverse results

def test_pick_diverse_mixes_searches_and_caps_employers():
    from jobbot.filters import pick_diverse
    s = {"keywords": ["french", "tutor", "teacher", "teaching", "bilingual", "student", "research",
                      "kinesiology", "recreation"]}
    tutoring = [make_job(title="French Tutor / Teacher, Bilingual Student Teaching", company="TutorCo",
                         source_id=f"t{i}", description="french tutor teacher teaching bilingual")
                for i in range(10)]
    for j in tutoring:
        j.query = "adzuna/french tutor"
    others = []
    for i, (q, t, c) in enumerate([("adzuna/research assistant", "Research Assistant", "UHN"),
                                   ("adzuna/kinesiology", "Kinesiology Student", "Clinic A"),
                                   ("adzuna/recreation", "Recreation Leader", "City")]):
        j = make_job(title=t, company=c, source_id=f"o{i}", description="")
        j.query = q
        others.append(j)
    picked = pick_diverse(tutoring + others, 5, s)
    queries = [j.query for j in picked]
    assert len(picked) == 5
    assert {"adzuna/research assistant", "adzuna/kinesiology", "adzuna/recreation"} <= set(queries)
    assert sum(j.company == "TutorCo" for j in picked) == 2                # per-employer cap


def test_jobs_remember_their_search(monkeypatch):
    import jobbot.sources as src
    from jobbot.config import DEFAULTS
    monkeypatch.setattr(src, "_get_json", lambda url, params=None: ADZUNA_SAMPLE)
    ad = {"enabled": True, "country": "ca", "app_id": "a", "app_key": "b", "searches": ["kinesiology"]}
    jobs = src.fetch_all({**DEFAULTS, "sources": {**DEFAULTS["sources"], "adzuna": ad}})
    assert jobs and all(j.query == "adzuna/kinesiology" for j in jobs)


def test_preferences_offer_broad_searches(web):
    app, c, tmp = web
    sug = c.get("/api/preferences").get_json()["suggested"]
    assert len(sug["adzuna_searches"]) >= 20 and "recreation" in sug["adzuna_searches"]
    assert "fitness" in sug["keywords"]
