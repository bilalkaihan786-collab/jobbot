"""Data models shared across the app."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- jobs

@dataclass
class Job:
    source: str            # greenhouse | lever | ashby | remotive
    source_id: str         # id within the source
    company: str
    title: str
    location: str
    url: str               # human-readable posting URL
    apply_url: str         # where the application form lives
    description: str       # plain text
    remote: bool = False
    posted_at: str = ""
    salary: str = ""
    query: str = ""        # which search found it (e.g. "adzuna/french tutor"); used to mix results

    @property
    def key(self) -> str:
        return f"{self.source}:{self.company}:{self.source_id}"

    @property
    def can_auto_apply(self) -> bool:
        return self.source in ("greenhouse", "lever", "ashby")


# ------------------------------------------------- Claude structured outputs

class FitAssessment(BaseModel):
    score: int = Field(description="0-100: how well the candidate fits this job")
    worth_applying: bool
    reasons: list[str] = Field(description="Main reasons for the score, 2-4 short items")
    missing_requirements: list[str] = Field(
        description="Hard requirements in the posting the candidate does not show"
    )
    red_flags: list[str] = Field(
        description="Deal-breakers vs. the candidate's preferences (location, seniority, visa, etc.)"
    )


class TailoredRole(BaseModel):
    experience_id: str = Field(description="The `id` of the experience entry from the profile")
    bullets: list[str] = Field(
        description="Rewritten bullets for this role, using ONLY facts from the profile"
    )


class TailoredProject(BaseModel):
    project_id: str
    description: str


class TailoredResume(BaseModel):
    headline: str = Field(description="One-line professional headline aimed at this job")
    summary: str = Field(description="2-4 sentence summary, truthful, targeted at this job")
    skills: list[str] = Field(description="Skills from the profile, most relevant first")
    roles: list[TailoredRole] = Field(description="Experience entries to include, in order")
    projects: list[TailoredProject] = Field(default_factory=list)


class CoverLetter(BaseModel):
    greeting: str
    paragraphs: list[str] = Field(description="3-4 paragraphs, no greeting or sign-off")
    sign_off: str


class FieldAnswer(BaseModel):
    field_id: str
    answer: str = Field(
        description="The value to enter. For choice fields, copy one option exactly "
        "(for multi-select, join options with ' | '). Empty if unknown."
    )
    needs_human: bool = Field(
        description="True if the profile does not contain enough information to answer "
        "truthfully, or the question is sensitive/legal and not covered by the profile"
    )


class FormAnswers(BaseModel):
    answers: list[FieldAnswer]


# ---------------------------------------------------------------- forms

FieldKind = Literal["text", "textarea", "select", "radio", "checkbox", "combobox", "file"]


@dataclass
class FormField:
    field_id: str                 # our id (stable within a page scan)
    label: str
    kind: FieldKind
    required: bool = False
    options: list[str] = field(default_factory=list)
    selector: str = ""            # CSS selector for the element / group
    name: Optional[str] = None    # HTML name attribute
    multi: bool = False

    def describe(self) -> dict:
        d = {"field_id": self.field_id, "question": self.label, "type": self.kind,
             "required": self.required}
        if self.options:
            d["options"] = self.options
        if self.multi:
            d["multiple_allowed"] = True
        return d
