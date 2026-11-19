"""All AI calls: fit scoring, resume tailoring, cover letters, form answers.

Two backends share the same prompts and safety checks:
  * LLM (Claude, via the Anthropic API) - needs ANTHROPIC_API_KEY.
  * OllamaLLM (a free model running on your own computer via https://ollama.com).
make_llm() picks one from config.yaml's `provider` setting.

Truthfulness is enforced two ways:
  1. Prompts forbid inventing anything not in the profile.
  2. Employers, titles, dates, education and contact details are copied from the
     profile by code; the model only chooses/rephrases bullets and writes summaries.
"""
from __future__ import annotations

import json
import logging
import os
from typing import TypeVar

import anthropic
import requests
import yaml
from pydantic import BaseModel, ValidationError

from jobbot.models import (CoverLetter, FitAssessment, FormAnswers, FormField, Job,
                           TailoredResume)

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

SYSTEM = """You are a meticulous career assistant working for one candidate. \
Their full profile is below and is the ONLY source of facts about them.

Hard rules:
- Never invent or inflate experience, employers, titles, dates, degrees, \
certifications, skills, metrics, or results. Rephrasing and reordering true facts is fine; \
adding new ones is not.
- If the profile does not support something, leave it out (or flag it when asked).
- Write in plain, specific, professional language. No buzzword soup, no clichés \
("I am writing to express my interest", "passionate", "synergy", "rockstar").

<candidate_profile>
{profile}
</candidate_profile>"""


class RefusedError(RuntimeError):
    pass


class BaseLLM:
    """Prompts and output checks shared by every backend. Subclasses implement _parse()."""

    def __init__(self, profile: dict, focus: str = "jobs"):
        from jobbot.filters import normalize_focus
        self.profile = profile
        self.focus = normalize_focus(focus)
        public = {k: v for k, v in profile.items() if k not in ("eeo",)}
        self.system_text = SYSTEM.format(
            profile=yaml.safe_dump(public, sort_keys=False, allow_unicode=True))

    def _parse(self, prompt: str, schema: type[T], effort: str = "medium",
               max_tokens: int = 16000) -> T:
        raise NotImplementedError

    max_description_chars = 20000

    def _job_block(self, job: Job) -> str:
        return (f"<job>\nCompany: {job.company}\nTitle: {job.title}\nLocation: {job.location}"
                f"{' (remote)' if job.remote else ''}\n"
                f"{'Salary: ' + job.salary + chr(10) if job.salary else ''}"
                f"Description:\n{job.description[:self.max_description_chars]}\n</job>")

    @staticmethod
    def _snippet_note(job: Job) -> str:
        if job.source == "adzuna":
            return ("Only a short snippet of this posting's description is available. Judge from "
                    "what is shown and don't penalise details you can't see.\n\n")
        return ""

    def _student_note(self, job: Job) -> str:
        from jobbot.filters import is_internship
        if self.focus == "students":
            sched = (self.profile.get("preferences") or {}).get("schedule") or []
            sched = ", ".join(map(str, sched)) if isinstance(sched, list) else str(sched)
            return ("The candidate is a STUDENT looking for student-friendly work"
                    f"{' (' + sched + ')' if sched else ''}: summer jobs, part-time work during "
                    "school, co-ops or internships. Judge them as a student: do not penalise "
                    "limited professional experience. Set worth_applying=false if the role needs a "
                    "completed degree, a professional licence or registration (e.g. a registered "
                    "kinesiologist, nurse or teacher's certificate), or years of experience they "
                    "don't have, or if its schedule clearly can't fit theirs.\n\n")
        if self.focus == "internships":
            return ("The candidate is looking ONLY for internships or co-op positions. Judge them "
                    "as an internship/co-op applicant: do not penalise limited professional "
                    "experience; do check eligibility (enrolment, year of study, graduation date, "
                    "work authorization) and that the term/dates fit their preferences. Set "
                    "worth_applying=false if this is not an internship or co-op.\n\n")
        return ""

    # -------------------------------------------------------------- tasks
    def assess_fit(self, job: Job) -> FitAssessment:
        prefs = json.dumps(self.profile.get("preferences") or {}, ensure_ascii=False)
        # Everything that is the same for every job comes first and the job comes last, so a
        # local model can reuse the work it already did on the shared start of the prompt.
        prompt = (
            f"Candidate preferences: {prefs}\n\n"
            f"{self._student_note(job)}"
            "Assess how well this candidate fits the job below. Score 0-100 where 70+ means a "
            "realistic shot at an interview. Be honest: penalise missing hard requirements "
            "(years, degrees, licences, clearances, required skills) and seniority mismatches. "
            "If the posting requires languages (e.g. bilingual English/French), check them "
            "against the `languages` in the profile: meeting one is a strong plus, missing one "
            "is a hard requirement gap. Use the candidate's `targets` preference to judge how "
            "well the field matches. Set worth_applying=false for any deal-breaker in red_flags. "
            "Keep every list item short (under 12 words).\n\n"
            f"{self._snippet_note(job)}"
            f"{self._job_block(job)}"
        )
        return self._parse(prompt, FitAssessment, effort="low", max_tokens=4000)

    def tailor_resume(self, job: Job) -> TailoredResume:
        from jobbot.filters import is_internship
        student = ("- This is a student application (summer job, part-time, co-op or "
                   "internship): keep the summary to 1-2 sentences on what the student brings, "
                   "include relevant projects, and favour transferable experience (volunteering, "
                   "tutoring, coaching, lab work) that fits the posting.\n"
                   if is_internship(job) or self.focus in ("students", "internships") else "")
        prompt = (
            f"{self._job_block(job)}\n\n"
            "Tailor the candidate's resume for this job.\n"
            f"{student}"
            "- roles: include every experience entry that is relevant (usually all recent "
            "ones), referenced by its `id`, most recent first. For each, write 2-6 bullets by "
            "selecting and rephrasing the profile's bullets for that role so the most relevant "
            "achievements come first and use the posting's terminology where it truthfully "
            "applies. Keep every number exactly as in the profile; do not add numbers.\n"
            "- skills: only skills present in the profile, most relevant first, max 20.\n"
            "- projects: only if the profile has projects relevant to this job.\n"
            "- Resume must fit on one page for <10 years experience, two pages otherwise."
        )
        result = self._parse(prompt, TailoredResume, effort="medium")
        return self._validate_resume(result)

    def _validate_resume(self, r: TailoredResume) -> TailoredResume:
        valid_exp = {e["id"] for e in self.profile["experience"]}
        valid_proj = {p["id"] for p in self.profile.get("projects") or []}
        bad = [x.experience_id for x in r.roles if x.experience_id not in valid_exp]
        if bad:
            log.warning("Dropping unknown experience ids from Claude output: %s", bad)
        r.roles = [x for x in r.roles if x.experience_id in valid_exp]
        r.projects = [p for p in r.projects if p.project_id in valid_proj]
        profile_skills = {s.lower() for s in _flatten_skills(self.profile.get("skills"))}
        if profile_skills:
            extra = [s for s in r.skills if s.lower() not in profile_skills]
            if extra:
                log.warning("Removing skills not in profile: %s", extra)
            r.skills = [s for s in r.skills if s.lower() in profile_skills]
        if not r.roles:
            raise RuntimeError("Tailored resume has no valid roles")
        return r

    def cover_letter(self, job: Job) -> CoverLetter:
        prompt = (
            f"{self._job_block(job)}\n\n"
            "Write a cover letter for this job, 250-350 words. Open with something specific "
            "about why this role/company and what the candidate brings - not a generic opener. "
            "Middle: 2 concrete, true examples from the profile mapped to the job's top needs. "
            "Close briefly. Address the hiring manager by name only if the posting names one; "
            "otherwise use 'Dear Hiring Team,'. sign_off should be e.g. 'Best regards,'."
        )
        return self._parse(prompt, CoverLetter, effort="medium")

    def answer_fields(self, job: Job, fields: list[FormField]) -> FormAnswers:
        answers_hint = self.profile.get("application_answers") or {}
        eeo = self.profile.get("eeo") or {}
        prompt = (
            f"{self._job_block(job)}\n\n"
            f"Pre-written answers from the candidate: {json.dumps(answers_hint, ensure_ascii=False)}\n"
            f"Voluntary self-identification answers the candidate chose to give: "
            f"{json.dumps(eeo, ensure_ascii=False)}\n\n"
            "Fill in these application form fields for the candidate:\n"
            f"{json.dumps([f.describe() for f in fields], indent=1, ensure_ascii=False)}\n\n"
            "Rules:\n"
            "- Return one answer per field_id.\n"
            "- For select/radio/checkbox/combobox, `answer` must be copied exactly from options.\n"
            "- Demographic/EEO questions (gender, race, veteran, disability, etc.): use the "
            "candidate's self-identification answers if given; otherwise pick the option "
            "meaning 'decline to answer'. Never guess demographics.\n"
            "- Legal questions (work authorization, sponsorship, criminal history, non-competes, "
            "relatives at the company): answer only from the profile; else needs_human=true.\n"
            "- Free-text questions ('Why do you want to work here?'): write 2-5 sentence, "
            "specific, truthful answers based on the profile and posting.\n"
            "- Set needs_human=true whenever you would have to guess a fact about the candidate."
        )
        return self._parse(prompt, FormAnswers, effort="medium")


def _flatten_skills(skills) -> list[str]:
    if not skills:
        return []
    if isinstance(skills, dict):
        out = []
        for v in skills.values():
            out.extend(_flatten_skills(v))
        return out
    if isinstance(skills, list):
        out = []
        for s in skills:
            out.extend(_flatten_skills(s) if isinstance(s, (list, dict)) else [str(s)])
        return out
    return [s.strip() for s in str(skills).split(",") if s.strip()]


# ------------------------------------------------------------------ Claude

class LLM(BaseLLM):
    """Claude via the Anthropic API."""

    def __init__(self, profile: dict, model: str = "claude-opus-5-5", focus: str = "jobs"):
        super().__init__(profile, focus)
        self.client = anthropic.Anthropic()
        self.model = model
        self.system = [{
            "type": "text",
            "text": self.system_text,
            # Profile is identical on every call -> cache it.
            "cache_control": {"type": "ephemeral"},
        }]

    def _parse(self, prompt: str, schema: type[T], effort: str = "medium",
               max_tokens: int = 16000) -> T:
        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=self.system,
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
            output_config={"effort": effort},
        )
        if self.model in ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-2",
                          "claude-fable-5-1", "claude-sonnet-5-5"):
            # Server-side fallback if a safety classifier declines (rare for this use).
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
            resp = self.client.beta.messages.parse(**kwargs)
        else:
            resp = self.client.messages.parse(**kwargs)
        if resp.stop_reason == "refusal":
            raise RefusedError(getattr(resp.stop_details, "explanation", None) or "refused")
        if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
            raise RuntimeError(f"Claude returned no parseable output (stop={resp.stop_reason})")
        return resp.parsed_output


# ------------------------------------------------------------------ Ollama (local, free)

class OllamaLLM(BaseLLM):
    """A model running locally in Ollama (https://ollama.com). No API key, no cost.

    Uses Ollama's structured outputs (`format` = JSON schema) so replies come back
    in the same shapes as Claude's, then validates them with the same pydantic models.
    """

    # Long postings are cut so the prompt fits a small context window: a big window makes
    # local models much slower and can run a laptop out of memory.
    max_description_chars = 6000

    def __init__(self, profile: dict, model: str = "qwen2.5:7b",
                 host: str = "http://localhost:11434", num_ctx: int = 8192,
                 timeout: int = 900, focus: str = "jobs"):
        super().__init__(profile, focus)
        self.model = model
        self.host = host.rstrip("/")
        self.num_ctx = num_ctx
        self.timeout = timeout
        self._check_ready()

    def _check_ready(self):
        try:
            r = requests.get(f"{self.host}/api/tags", timeout=10)
            r.raise_for_status()
        except requests.RequestException:
            raise SystemExit(
                f"Can't reach Ollama at {self.host}.\n"
                "  1. Install it from https://ollama.com and open the Ollama app.\n"
                f"  2. Download the model:  ollama pull {self.model}\n"
                "  Or set provider: claude in config.yaml and set ANTHROPIC_API_KEY.")
        names = {m.get("name", "") for m in r.json().get("models", [])}
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        if wanted not in names:
            raise SystemExit(f"Ollama doesn't have the model '{self.model}' yet. "
                             f"Download it with:  ollama pull {self.model}")

    def _parse(self, prompt: str, schema: type[T], effort: str = "medium",
               max_tokens: int = 16000) -> T:
        json_schema = schema.model_json_schema()
        # Schema first: it's the same for every job of a task, so it belongs to the reusable start.
        content = (f"Reply with only a JSON object matching this schema:\n"
                   f"{json.dumps(json_schema)}\n\n{prompt}")
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": self.system_text},
                         {"role": "user", "content": content}],
            "format": json_schema,
            "stream": False,
            "keep_alive": "30m",   # stay loaded between jobs instead of reloading each time
            "options": {"temperature": 0.3, "num_ctx": self.num_ctx,
                        "num_predict": min(max_tokens, 8000)},
        }
        last_err: Exception | None = None
        for _ in range(2):  # small local models occasionally emit invalid JSON; retry once
            try:
                r = requests.post(f"{self.host}/api/chat", json=body, timeout=self.timeout)
            except requests.RequestException as e:
                raise RuntimeError(f"Ollama request failed: {e}") from e
            if r.status_code != 200:
                raise RuntimeError(f"Ollama error {r.status_code}: {r.text[:300]}")
            text = (r.json().get("message") or {}).get("content", "")
            try:
                return schema.model_validate_json(text)
            except ValidationError as e:
                last_err = e
                log.debug("Ollama returned invalid %s, retrying: %s", schema.__name__, e)
        raise RuntimeError(f"Ollama returned no valid {schema.__name__}: {last_err}")


# ------------------------------------------------------------------ factory

def make_llm(cfg: dict, profile: dict) -> BaseLLM:
    """provider: claude | ollama | auto (Claude if ANTHROPIC_API_KEY is set, else Ollama)."""
    provider = (cfg.get("provider") or "auto").lower()
    focus = (cfg.get("search") or {}).get("focus") or "jobs"
    if provider == "auto":
        provider = "claude" if os.environ.get("ANTHROPIC_API_KEY") else "ollama"
        log.info("Using %s (provider: auto)", provider)
    if provider == "claude":
        return LLM(profile, model=cfg["model"], focus=focus)
    if provider == "ollama":
        o = cfg.get("ollama") or {}
        return OllamaLLM(profile, model=o.get("model", "qwen2.5:7b"),
                         host=o.get("host", "http://localhost:11434"),
                         # 16384 was the old example value; it's slow on laptops.
                         num_ctx=8192 if int(o.get("num_ctx", 8192)) == 16384 else int(o.get("num_ctx")),
                         timeout=int(o.get("timeout_seconds", 900)), focus=focus)
    raise SystemExit(f"Unknown provider '{provider}' in config.yaml (use claude, ollama or auto)")
