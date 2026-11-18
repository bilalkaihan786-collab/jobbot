"""Fill (and optionally submit) application forms with Playwright.

Works with Greenhouse, Lever and Ashby hosted forms using a generic,
label-driven scanner: every field's question text is read from the page,
standard fields (name, email, phone, links, resume...) are filled from the
profile, and everything else is answered by Claude from the profile.

Modes
  review (default): a visible browser fills everything and STOPS. You check the
                    form, fix anything, click Submit yourself, then confirm in the
                    terminal. Recommended.
  auto:             submits automatically, but only when every required field was
                    answered confidently and no CAPTCHA is present; otherwise it
                    falls back to review (or marks the job needs_manual if headless).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from jobbot.documents import chromium_launch_kwargs
from jobbot.models import FieldAnswer, FormField, Job

log = logging.getLogger(__name__)

SCAN_JS = r"""
() => {
  const forms = [...document.querySelectorAll('form')];
  const count = f => f.querySelectorAll('input:not([type=hidden]),textarea,select').length;
  forms.sort((a, b) => count(b) - count(a));
  const scope = (forms[0] && count(forms[0]) >= 2) ? forms[0] : document.body;

  const raw = s => (s || '').replace(/\s+/g, ' ').trim();
  const clean = s => raw(s).replace(/\s*[*✱]\s*$/, '').replace(/^[*✱]\s*/, '').replace(/\s*\(required\)\s*$/i, '').trim();
  const visible = el => {
    const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const textOf = el => raw(el ? el.innerText || el.textContent : '');

  const ownLabel = el => {
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(/\s+/).map(id => textOf(document.getElementById(id))).join(' '); if (raw(t)) return t; }
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l && textOf(l)) return textOf(l); }
    const wrap = el.closest('label'); if (wrap && textOf(wrap)) return textOf(wrap);
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    return '';
  };
  const containerLabel = (el, exclude) => {
    let p = el.parentElement;
    const mine = new Set([el, ...(exclude || [])].flatMap(x => [x, ...x.querySelectorAll('input,select,textarea')]));
    for (let i = 0; i < 6 && p && p !== scope.parentElement; i++, p = p.parentElement) {
      // Stop once this ancestor also holds other fields: its labels belong to them.
      if ([...p.querySelectorAll('input:not([type=hidden]),select,textarea')].some(x => !mine.has(x))) return '';
      const cands = p.querySelectorAll('legend, .application-label, label, [class*="label"], [class*="Label"], [class*="question"]');
      for (const c of cands) {
        if (c.contains(el) || c.querySelector('input,select,textarea')) continue;
        if (exclude && exclude.some(x => c.contains(x) || x.contains(c))) continue;
        const t = textOf(c); if (t && t.length < 600) return t;
      }
    }
    return '';
  };
  const isRequired = (el, labelRaw) =>
    el.required || el.getAttribute('aria-required') === 'true' || /[*✱]\s*$|\(required\)/i.test(labelRaw)
    || !!el.closest('.required, [class*="required"]');

  const fields = []; let n = 0; const seenGroups = new Set();
  const els = scope.querySelectorAll('input, textarea, select, [role=combobox]');
  for (const el of els) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type)) continue;
    if (el.disabled || el.closest('[aria-hidden=true]') && type !== 'file') continue;
    if (el.dataset.jobbot || el.dataset.jobbotGroup) continue;
    if (type === 'file') {
      const lab = ownLabel(el) || containerLabel(el) || el.name || el.id;
      const id = 'f' + (n++); el.dataset.jobbot = id;
      fields.push({id, label: clean(lab), kind: 'file', required: isRequired(el, raw(lab)), options: [], name: el.name || el.id || ''});
      continue;
    }
    if (!visible(el)) continue;
    if (type === 'radio' || type === 'checkbox') {
      const key = el.name || el.id;
      if (seenGroups.has(key)) continue; seenGroups.add(key);
      const group = el.name ? [...scope.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)] : [el];
      const opts = group.map(g => clean(ownLabel(g) || g.value));
      const id = 'f' + (n++);
      group.forEach((g, i) => { g.dataset.jobbotGroup = id; g.dataset.jobbotOption = opts[i]; });
      let q = '';
      const fs = el.closest('fieldset'); if (fs && fs.querySelector('legend')) q = textOf(fs.querySelector('legend'));
      if (!q && group.length === 1) q = opts[0];
      if (!q) q = containerLabel(el, group.map(g => g.closest('label') || g));
      fields.push({id, label: clean(q), kind: type, required: group.some(g => isRequired(g, raw(q))),
                   options: opts, multi: type === 'checkbox' && group.length > 1, name: el.name || ''});
      continue;
    }
    const role = el.getAttribute('role');
    const kind = tag === 'select' ? 'select' : tag === 'textarea' ? 'textarea'
               : (role === 'combobox' || el.getAttribute('aria-autocomplete') === 'list') ? 'combobox' : 'text';
    const lab = ownLabel(el) || containerLabel(el) || el.placeholder || el.name || '';
    const id = 'f' + (n++); el.dataset.jobbot = id;
    const options = kind === 'select' ? [...el.options].filter(o => o.value !== '' && !/^select|^--|^please select/i.test(o.text.trim())).map(o => raw(o.text)) : [];
    fields.push({id, label: clean(lab), kind, required: isRequired(el, raw(lab)), options,
                 multi: kind === 'select' && el.multiple, name: el.name || el.id || ''});
  }
  return fields;
}
"""

CAPTCHA_SELECTORS = ["iframe[src*='hcaptcha']", "iframe[src*='recaptcha']",
                     "iframe[src*='turnstile']", "iframe[title*='captcha' i]"]
SUBMIT_SELECTORS = ["button[type=submit]", "input[type=submit]",
                    "button:has-text('Submit application')", "button:has-text('Submit Application')",
                    "button:has-text('Submit')"]
SUCCESS_RE = re.compile(r"thank you for (applying|your application)|application (has been )?(received|submitted)"
                        r"|we('ve| have) received your application|successfully submitted", re.I)


@dataclass
class FillReport:
    filled: list[str]
    unanswered: list[str]          # required fields we couldn't fill
    needs_human: list[str]         # fields Claude flagged
    has_cover_letter_field: bool
    captcha: bool


def standard_value(f: FormField, profile: dict) -> Optional[str]:
    """Answer common fields straight from the profile without calling Claude."""
    if f.kind not in ("text", "combobox", "textarea") or len(f.label) > 60:
        return None
    label = f.label.lower().strip()
    links = profile.get("links") or {}
    name = profile["name"].split()
    first = profile.get("first_name") or name[0]
    last = profile.get("last_name") or " ".join(name[1:])
    current = next((e.get("company") for e in profile["experience"]
                    if str(e.get("end", "Present")).lower() in ("present", "current", "")), None)
    rules = [
        (r"^(legal )?first name|^given name", first),
        (r"^preferred (first )?name", profile.get("preferred_name") or first),
        (r"^(legal )?last name|^surname|^family name", last),
        (r"^(full |legal )?name$|^your name$|^full legal name", profile["name"]),
        (r"^e-?mail( address)?$|^your email", profile.get("email")),
        (r"^(phone|mobile|telephone)( number)?$|^your phone", profile.get("phone")),
        (r"linkedin", links.get("linkedin")),
        (r"github", links.get("github")),
        (r"^(personal )?(website|portfolio|blog)|^other website", links.get("website") or links.get("portfolio")),
        (r"^(current )?(company|employer|organization)$|^current company", current),
        (r"^(current )?location$|^location \(city\)|^city$|^where are you (located|based)", profile.get("location")),
    ]
    for pat, val in rules:
        if re.search(pat, label) and val:
            return str(val)
    return None


def _is_resume(f: FormField) -> bool:
    s = f"{f.label} {f.name}".lower()
    return f.kind == "file" and ("resume" in s or re.search(r"\bcv\b", s) is not None) and "cover" not in s


def _is_cover_file(f: FormField) -> bool:
    return f.kind == "file" and "cover" in f"{f.label} {f.name}".lower()


def _is_cover_text(f: FormField) -> bool:
    s = f"{f.label} {f.name}".lower()
    return f.kind == "textarea" and ("cover letter" in s or f.name == "comments"
                                     or "additional information" in s)


class FormFiller:
    def __init__(self, page, profile: dict):
        self.page = page
        self.profile = profile

    def scan(self) -> list[FormField]:
        raw = self.page.evaluate(SCAN_JS)
        fields = []
        for r in raw:
            sel = (f"[data-jobbot-group='{r['id']}']" if r["kind"] in ("radio", "checkbox")
                   else f"[data-jobbot='{r['id']}']")
            fields.append(FormField(field_id=r["id"], label=r["label"] or r.get("name", ""),
                                    kind=r["kind"], required=r["required"],
                                    options=r.get("options") or [], selector=sel,
                                    name=r.get("name"), multi=r.get("multi", False)))
        for f in fields:
            if f.kind == "combobox" and not f.options:
                f.options = self._combobox_options(f)
        return fields

    def _combobox_options(self, f: FormField) -> list[str]:
        """Open a custom dropdown, read its options, close it."""
        try:
            el = self.page.locator(f.selector).first
            el.click(timeout=2000)
            self.page.wait_for_timeout(300)
            opts = self.page.locator("[role=option]").all_inner_texts()
            self.page.keyboard.press("Escape")
            opts = [o.strip() for o in opts if o.strip()]
            # Typeahead lists (e.g. city search) are empty or huge until you type.
            return opts if 0 < len(opts) <= 60 else []
        except Exception:
            return []

    # ------------------------------------------------------------------ fill
    def fill(self, f: FormField, value: str) -> bool:
        page = self.page
        try:
            if f.kind == "file":
                page.locator(f.selector).first.set_input_files(value)
            elif f.kind in ("text", "textarea"):
                page.locator(f.selector).first.fill(value)
            elif f.kind == "select":
                el = page.locator(f.selector).first
                vals = [v.strip() for v in value.split(" | ")] if f.multi else [value]
                el.select_option(label=vals)
            elif f.kind == "combobox":
                return self._fill_combobox(f, value)
            elif f.kind in ("radio", "checkbox"):
                wanted = {v.strip().lower() for v in value.split(" | ")}
                inputs = page.locator(f.selector)
                hit = False
                for i in range(inputs.count()):
                    inp = inputs.nth(i)
                    opt = (inp.get_attribute("data-jobbot-option") or "").lower()
                    single_yes = (f.kind == "checkbox" and inputs.count() == 1
                                  and value.strip().lower() in ("yes", "true", "checked", opt))
                    if opt in wanted or single_yes:
                        inp.check(force=True)
                        hit = True
                return hit
            return True
        except Exception as e:
            log.debug("fill %s (%s) failed: %s", f.label, f.kind, e)
            return False

    def _fill_combobox(self, f: FormField, value: str) -> bool:
        page = self.page
        el = page.locator(f.selector).first
        el.click()
        el.fill("") if el.evaluate("e => 'value' in e") else None
        el.type(value[:40], delay=20)
        page.wait_for_timeout(900)
        opts = page.locator("[role=option]")
        n = opts.count()
        if n == 0:
            page.keyboard.press("Enter")
            return True
        texts = [opts.nth(i).inner_text().strip() for i in range(n)]
        exact = [i for i, t in enumerate(texts) if t.lower() == value.lower()]
        part = [i for i, t in enumerate(texts) if value.lower() in t.lower()]
        idx = (exact or part or [0])[0]
        opts.nth(idx).click()
        return True


def detect_captcha(page) -> bool:
    for sel in CAPTCHA_SELECTORS:
        loc = page.locator(sel)
        for i in range(loc.count()):
            try:
                if loc.nth(i).is_visible():
                    return True
            except Exception:
                pass
    return False


def _goto_form(page, job: Job):
    page.goto(job.apply_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)
    # Some Greenhouse boards redirect to a company page that embeds the form in an iframe.
    frame = page.locator("iframe#grnhse_iframe, iframe[src*='greenhouse.io']")
    if frame.count():
        src = frame.first.get_attribute("src")
        if src:
            page.goto(src, wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
    # Ashby / Lever sometimes need an "Apply" click to reveal the form.
    for label in ("Apply for this job", "Apply now", "Apply"):
        btn = page.locator(f"a:has-text('{label}'), button:has-text('{label}')")
        if btn.count() and page.locator("input[type=file]").count() == 0:
            try:
                btn.first.click(timeout=3000)
                page.wait_for_timeout(1500)
            except Exception:
                pass
            break


def fill_application(page, job: Job, profile: dict, llm, resume_pdf: Path,
                     get_cover_letter: Callable[[], tuple[Path, str]] | None) -> FillReport:
    filler = FormFiller(page, profile)
    fields = filler.scan()
    log.info("Found %d form fields", len(fields))

    has_cover = any(_is_cover_file(f) or _is_cover_text(f) for f in fields)
    cover: tuple[Path, str] | None = None
    if has_cover and get_cover_letter:
        cover = get_cover_letter()

    filled, unanswered, needs_human = [], [], []
    to_ask: list[FormField] = []
    for f in fields:
        value = None
        if _is_resume(f):
            value = str(resume_pdf)
        elif _is_cover_file(f):
            value = str(cover[0]) if cover else None
            if not value:
                continue
        elif _is_cover_text(f) and cover:
            value = cover[1]
        else:
            value = standard_value(f, profile)
        if value is None:
            if f.kind != "file":
                to_ask.append(f)
            elif f.required:
                unanswered.append(f.label)
            continue
        (filled if filler.fill(f, value) else unanswered).append(f.label)

    if to_ask:
        answers = {a.field_id: a for a in llm.answer_fields(job, to_ask).answers}
        for f in to_ask:
            a: FieldAnswer | None = answers.get(f.field_id)
            if a is None or not a.answer.strip() or a.needs_human:
                if a is not None and a.needs_human:
                    needs_human.append(f.label)
                elif f.required:
                    unanswered.append(f.label)
                continue
            if f.options and f.kind != "combobox" and not _option_ok(a.answer, f.options, f.multi):
                log.debug("Answer %r not among options for %r", a.answer, f.label)
                (unanswered if f.required else needs_human).append(f.label)
                continue
            (filled if filler.fill(f, a.answer) else unanswered).append(f.label)

    return FillReport(filled=filled, unanswered=[u for u in unanswered if u],
                      needs_human=needs_human, has_cover_letter_field=has_cover,
                      captcha=detect_captcha(page))


def _option_ok(answer: str, options: list[str], multi: bool) -> bool:
    opts = {o.lower() for o in options}
    parts = [p.strip().lower() for p in answer.split(" | ")] if multi else [answer.strip().lower()]
    return all(p in opts for p in parts) or (len(options) == 1 and answer.lower() in ("yes", "true"))


def submit(page) -> tuple[bool, str]:
    for sel in SUBMIT_SELECTORS:
        btn = page.locator(sel)
        if btn.count() and btn.first.is_visible():
            btn.first.click()
            break
    else:
        return False, "submit button not found"
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    page.wait_for_timeout(2000)
    body = page.inner_text("body")
    if SUCCESS_RE.search(body) or re.search(r"/(thanks|confirmation|success)", page.url):
        return True, "confirmation detected"
    if detect_captcha(page):
        return False, "captcha appeared after submit"
    return False, "no confirmation detected (check for validation errors)"


def run_application(job: Job, profile: dict, llm, resume_pdf: Path,
                    get_cover_letter, mode: str, headless: bool, out_dir: Path,
                    ask_human: Callable[[Job], tuple[str, str]] | None = None) -> tuple[str, str]:
    """Returns (status, note). status in applied | needs_manual | failed | skipped.

    ask_human asks the user whether they submitted the form; defaults to a terminal
    prompt (the web UI passes its own)."""
    from playwright.sync_api import sync_playwright

    if mode == "review":
        headless = False
    with sync_playwright() as p:
        browser = p.chromium.launch(**chromium_launch_kwargs(headless))
        page = browser.new_context(viewport={"width": 1280, "height": 1000}).new_page()
        try:
            _goto_form(page, job)
            report = fill_application(page, job, profile, llm, resume_pdf, get_cover_letter)
            page.screenshot(path=str(out_dir / "filled-form.png"), full_page=True)
            _print_report(job, report)

            clean = not report.unanswered and not report.needs_human and not report.captcha
            if mode == "auto" and clean:
                ok, note = submit(page)
                page.screenshot(path=str(out_dir / "after-submit.png"), full_page=True)
                if ok:
                    return "applied", note
                if headless:
                    return "failed", note
                print(f"  Auto-submit didn't confirm ({note}). Please finish in the browser.")
            elif mode == "auto" and headless:
                return "needs_manual", _issues(report)

            return (ask_human or _ask_human)(job)
        except Exception as e:
            log.exception("Application failed for %s", job.title)
            try:
                page.screenshot(path=str(out_dir / "error.png"), full_page=True)
            except Exception:
                pass
            return "failed", str(e)[:300]
        finally:
            browser.close()


def _issues(r: FillReport) -> str:
    parts = []
    if r.unanswered:
        parts.append("unanswered: " + "; ".join(r.unanswered))
    if r.needs_human:
        parts.append("needs you: " + "; ".join(r.needs_human))
    if r.captcha:
        parts.append("captcha")
    return " | ".join(parts)


def _print_report(job: Job, r: FillReport):
    print(f"\n  Filled {len(r.filled)} fields for {job.title} @ {job.company}")
    if r.unanswered:
        print("  ⚠ Required fields NOT filled:\n    - " + "\n    - ".join(r.unanswered))
    if r.needs_human:
        print("  ⚠ Questions that need your answer:\n    - " + "\n    - ".join(r.needs_human))
    if r.captcha:
        print("  ⚠ CAPTCHA present - solve it in the browser.")


def _ask_human(job: Job) -> tuple[str, str]:
    print("\n  Review the form in the browser window. Fix anything wrong, then click Submit.")
    while True:
        ans = input("  Did you submit it? [y]es / [n]o, skip / [l]ater: ").strip().lower()
        if ans in ("y", "yes"):
            return "applied", "submitted by user in review mode"
        if ans in ("n", "no", "s", "skip"):
            return "skipped", "skipped by user"
        if ans in ("l", "later"):
            return "needs_manual", "deferred by user"
        time.sleep(0.1)
