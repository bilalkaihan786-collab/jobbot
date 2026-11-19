"""Command line interface.

  python -m jobbot find      fetch new jobs, filter, score them with Claude
  python -m jobbot prepare   write tailored resumes (+ cover letters) for good matches
  python -m jobbot apply     fill / submit applications for prepared jobs
  python -m jobbot run       find + prepare + apply
  python -m jobbot status    summary and list of jobs
  python -m jobbot mark KEY STATUS   manually set a job's status (e.g. after applying by hand)
  python -m jobbot web       open the local web page (http://localhost:8000)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

def _stopped(args) -> bool:
    """The web page's Stop button sets this between jobs."""
    fn = getattr(args, "should_stop", None)
    if fn and fn():
        print("\nStopped. Finished jobs are saved; the rest will be picked up next time.")
        return True
    return False


from jobbot import documents
from jobbot.config import load_config, load_profile
from jobbot.db import DB
from jobbot.filters import passes, pick_diverse, student_focus

log = logging.getLogger("jobbot")

EXAMPLE_PROFILE_MSG = (
    "Your profile is still the example (profile.yaml has `example_profile: true`).\n"
    "Replace the example details with yours (Settings tab > Your profile on the web page),\n"
    "then delete the `example_profile: true` line.")


def _is_example(profile) -> bool:
    return bool(profile.get("example_profile"))


def _llm(cfg, profile):
    from jobbot.llm import make_llm
    return make_llm(cfg, profile)


# -------------------------------------------------------------------- find

def cmd_find(cfg, profile, db: DB, args):
    from jobbot.sources import fetch_all
    jobs = fetch_all(cfg)
    new = db.add_new(jobs)
    print(f"Fetched {len(jobs)} jobs, {len(new)} new.")

    candidates = []
    for j in new:
        ok, why = passes(j, cfg["search"])
        if ok:
            candidates.append(j)
        else:
            db.update(j.key, status="filtered", note=why)
    print(f"{len(candidates)} passed keyword/location filters.")

    # Preferences may have changed (e.g. switched to internships): give earlier
    # filtered-out postings another look. Revived jobs are scored with the backlog below.
    revived = 0
    for r in db.by_status("filtered", order="first_seen"):
        j = db.job(r)
        if passes(j, cfg["search"])[0]:
            db.update(j.key, status="new", note=None)
            revived += 1
    if revived:
        print(f"{revived} earlier postings now match your preferences.")

    if _is_example(profile):
        print("\nSkipping scoring: " + EXAMPLE_PROFILE_MSG)
        return

    limit = cfg["matching"]["max_jobs_to_score"]
    if len(candidates) > limit:
        print(f"Scoring {limit} (Jobs to score per run), mixed across your searches; the rest stay 'new'.")
    min_score = cfg["matching"]["min_score"]
    # Also score jobs left 'new' from earlier runs.
    backlog = [db.job(r) for r in db.by_status("new", order="first_seen")
               if r["key"] not in {c.key for c in candidates}]
    # A mix across your searches (best of each first), max 2 per employer, so one kind of job
    # can't take the whole run.
    todo = pick_diverse(candidates + backlog, limit, cfg["search"])
    if not todo:
        print(f"\nNothing new to score. Good matches waiting: {len(db.by_status('scored'))}")
        return
    llm = _llm(cfg, profile)
    if type(llm).__name__ == "OllamaLLM":
        print(f"\nScoring {len(todo)} jobs with the local AI on this computer. Each takes about "
              "30 seconds to a few minutes (the first one also loads the model).\n"
              "Good matches show up on the page as each one finishes. To go faster, lower "
              "'Jobs to score per run' on the Preferences tab.")
    else:
        print(f"\nScoring {len(todo)} jobs.")
    for i, j in enumerate(todo, 1):
        if _stopped(args):
            break
        print(f"  [{i}/{len(todo)}] scoring: {j.title} @ {j.company} ...")
        t0 = time.time()
        try:
            a = llm.assess_fit(j)
        except Exception as e:
            log.warning("Scoring failed for %s: %s", j.title, e)
            continue
        good = a.worth_applying and a.score >= min_score
        db.update(j.key, status="scored" if good else "rejected", score=a.score,
                  assessment=a.model_dump())
        mark = "✓ good match" if good else "· not a fit"
        print(f"        {mark}: {a.score}/100  ({time.time() - t0:.0f}s)")
    print(f"\nGood matches waiting: {len(db.by_status('scored'))}")


# -------------------------------------------------------------------- prepare

def _make_cover_letter(cfg, profile, llm, job):
    cl = llm.cover_letter(job)
    pdf = documents.write_cover_letter(profile, job, cl, cfg["output_dir"],
                                       cfg["documents"]["formats"])
    return pdf, documents.letter_text(profile, cl)


def cmd_prepare(cfg, profile, db: DB, args):
    if _is_example(profile):
        print(EXAMPLE_PROFILE_MSG)
        return
    keys = getattr(args, "keys", None)
    rows = db.by_status("scored", "failed") if keys else db.by_status("scored")
    if keys:   # chosen on the web page; "failed" lets you retry a resume that broke
        rows = [r for r in rows if r["key"] in set(keys)]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("Nothing to prepare. Run `find` first, or pick jobs from your good matches.")
        return
    llm = _llm(cfg, profile)
    cl_mode = cfg["documents"]["cover_letter"]
    for r in rows:
        if _stopped(args):
            break
        job = db.job(r)
        print(f"Preparing {job.title} @ {job.company} (score {r['score']})")
        try:
            t = llm.tailor_resume(job)
            pdf = documents.write_resume(
                profile, job, t, cfg["output_dir"], cfg["documents"]["formats"],
                education_first=True if student_focus(cfg["search"].get("focus")) else None)
            fields = {"status": "ready", "resume_path": str(pdf)}
            # 'always' = make one now. 'when_accepted' = made at apply time only if the
            # form has a cover-letter field (or now, for sources we can't auto-apply to).
            if cl_mode == "always" or (cl_mode == "when_accepted" and not job.can_auto_apply):
                cpdf, _ = _make_cover_letter(cfg, profile, llm, job)
                fields["cover_letter_path"] = str(cpdf)
            if not job.can_auto_apply:
                fields["status"] = "needs_manual"
                fields["note"] = f"apply at {job.apply_url}"
            db.update(job.key, **fields)
            print(f"  → {pdf.parent}")
        except Exception as e:
            log.warning("  failed: %s", e)
            db.update(job.key, status="failed", note=f"prepare: {e}"[:300])


# -------------------------------------------------------------------- apply

def cmd_apply(cfg, profile, db: DB, args):
    from jobbot.apply import run_application
    if _is_example(profile):
        print(EXAMPLE_PROFILE_MSG)
        return
    acfg = cfg["apply"]
    mode = args.mode or acfg["mode"]
    rows = [r for r in db.by_status("ready") if db.job(r).can_auto_apply]
    if args.key:
        rows = [r for r in rows if r["key"] == args.key]
    room = acfg["max_per_run"]
    if acfg.get("max_per_day"):
        room = min(room, max(0, acfg["max_per_day"] - db.applied_today()))
    rows = rows[:room]
    if not rows:
        print("No prepared jobs to apply to (or daily limit reached). Run `prepare` first.")
        return
    if mode == "auto":
        print("AUTO mode: applications will be submitted without review when every field "
              "is answered confidently.\n")
    llm = _llm(cfg, profile)
    cl_mode = cfg["documents"]["cover_letter"]

    for i, r in enumerate(rows):
        if _stopped(args):
            break
        job = db.job(r)
        print(f"\n[{i + 1}/{len(rows)}] {job.title} @ {job.company}\n  {job.apply_url}")
        out_dir = Path(r["resume_path"]).parent

        def get_cover(job=job, r=r):
            if cl_mode == "never":
                return None
            if r["cover_letter_path"]:
                txt = next(out_dir.glob("*cover-letter.txt"), None)
                return Path(r["cover_letter_path"]), txt.read_text() if txt else ""
            pdf, text = _make_cover_letter(cfg, profile, llm, job)
            db.update(job.key, cover_letter_path=str(pdf))
            return pdf, text

        status, note = run_application(job, profile, llm, Path(r["resume_path"]),
                                       get_cover if cl_mode != "never" else None,
                                       mode, acfg["headless"], out_dir,
                                       ask_human=getattr(args, "ask_human", None))
        db.update(job.key, status=status, note=note)
        print(f"  → {status}: {note}")
        if i < len(rows) - 1 and mode == "auto":
            time.sleep(acfg["delay_seconds"])


# -------------------------------------------------------------------- misc

def cmd_status(cfg, profile, db: DB, args):
    counts = db.counts()
    print("  ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "No jobs yet.")
    statuses = args.status.split(",") if args.status else [
        "scored", "ready", "needs_manual", "applied", "failed"]
    for r in db.by_status(*statuses, order="status, score DESC"):
        j = db.job(r)
        print(f"\n[{r['status']}] {r['score'] if r['score'] is not None else '-':>3}  "
              f"{j.title} @ {j.company} — {j.location}\n      key: {r['key']}\n      {j.url}")
        if r["note"]:
            print(f"      note: {r['note']}")
        if args.verbose and r["assessment"]:
            a = json.loads(r["assessment"])
            for reason in a.get("reasons", []):
                print(f"      + {reason}")
            for m in a.get("missing_requirements", []) + a.get("red_flags", []):
                print(f"      - {m}")


def cmd_mark(cfg, profile, db: DB, args):
    if not db.get(args.key):
        sys.exit(f"No job with key {args.key}")
    db.update(args.key, status=args.status, note=args.note or "set manually")
    print("ok")


def cmd_run(cfg, profile, db, args):
    cmd_find(cfg, profile, db, args)
    cmd_prepare(cfg, profile, db, args)
    cmd_apply(cfg, profile, db, args)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jobbot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("find")
    p = sub.add_parser("prepare"); p.add_argument("--limit", type=int)
    p = sub.add_parser("apply")
    p.add_argument("--mode", choices=["review", "auto"])
    p.add_argument("--key", help="apply to one job only")
    p = sub.add_parser("run"); p.add_argument("--mode", choices=["review", "auto"])
    p.add_argument("--limit", type=int); p.set_defaults(key=None)
    p = sub.add_parser("status"); p.add_argument("--status", help="comma-separated filter")
    p = sub.add_parser("mark"); p.add_argument("key"); p.add_argument("status")
    p.add_argument("--note")
    p = sub.add_parser("web", help="open the local web page")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    for noisy in ("httpx", "anthropic", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.cmd == "web":
        from jobbot.web import serve
        serve(args.config, port=args.port, open_browser=not args.no_browser)
        return

    cfg = load_config(args.config)
    profile = load_profile(cfg["profile"])
    Path(cfg["output_dir"]).mkdir(parents=True, exist_ok=True)
    db = DB(cfg["database"])
    {"find": cmd_find, "prepare": cmd_prepare, "apply": cmd_apply, "run": cmd_run,
     "status": cmd_status, "mark": cmd_mark}[args.cmd](cfg, profile, db, args)
