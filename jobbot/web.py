"""Local web page for jobbot: `python -m jobbot web` -> http://localhost:8000

Everything the command line does, behind buttons: find jobs, prepare resumes, apply,
see results, open the generated files, and edit config.yaml / profile.yaml.

It only listens on 127.0.0.1 (your own computer). Requests that change anything must
carry a custom header and come to a localhost Host name, so other websites you have
open can't trigger actions through it.
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import shutil
import threading
import time
import traceback
import webbrowser
from pathlib import Path
from types import SimpleNamespace

import requests
import yaml
from flask import Flask, abort, jsonify, request, send_from_directory

from jobbot.config import REQUIRED_PROFILE_KEYS, load_config, load_profile
from jobbot.db import DB
from jobbot.filters import FOCUSES, is_internship

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
STATUSES = ["new", "filtered", "scored", "rejected", "ready", "needs_manual",
            "applied", "failed", "skipped"]


# ------------------------------------------------------------------ background tasks

class TaskRunner:
    """Runs one long command (find / prepare / apply) at a time in a thread and keeps its log."""

    def __init__(self):
        self.lock = threading.Lock()
        self.name: str | None = None
        self.running = False
        self.error: str | None = None
        self.lines: list[str] = []
        self.started = self.finished = None
        self._partial = ""
        self.review: dict | None = None   # set while waiting for the user to submit a form

    # -- log capture
    def write(self, s: str):
        with self.lock:
            self._partial += s
            *done, self._partial = self._partial.split("\n")
            self.lines.extend(done)

    def _flush(self):
        with self.lock:
            if self._partial:
                self.lines.append(self._partial)
                self._partial = ""

    def start(self, name: str, fn) -> bool:
        with self.lock:
            if self.running:
                return False
            self.name, self.running, self.error = name, True, None
            self.lines, self._partial = [], ""
            self.started, self.finished = time.time(), None
        threading.Thread(target=self._run, args=(fn,), daemon=True).start()
        return True

    def _run(self, fn):
        stream = _Stream(self)
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        handler.setLevel(logging.INFO)
        jlog = logging.getLogger("jobbot")
        jlog.addHandler(handler)
        try:
            with contextlib.redirect_stdout(stream):
                fn()
        except SystemExit as e:          # config / Ollama problems raise SystemExit with a message
            self.error = str(e.code) if e.code not in (None, 0) else None
        except BaseException as e:      # noqa: BLE001 - report anything to the page
            self.error = f"{type(e).__name__}: {e}"
            log.debug(traceback.format_exc())
        finally:
            jlog.removeHandler(handler)
            self._flush()
            if self.error:
                self.lines.append(f"ERROR {self.error}")
            with self.lock:
                self.running, self.finished = False, time.time()
                self.review = None

    def state(self, since: int = 0) -> dict:
        with self.lock:
            rv = self.review
            return {
                "name": self.name, "running": self.running, "error": self.error,
                "lines": self.lines[since:], "total": len(self.lines),
                "review": ({k: rv[k] for k in ("title", "company", "url")} if rv else None),
            }

    # -- review-mode question, answered from the page instead of the terminal
    def ask_human(self, job) -> tuple[str, str]:
        ev = threading.Event()
        with self.lock:
            self.review = {"title": job.title, "company": job.company, "url": job.apply_url,
                           "event": ev, "answer": None}
        print("  Waiting for you: check the form in the browser window, click Submit there, "
              "then answer on the jobbot page.")
        ev.wait()
        with self.lock:
            answer, self.review = self.review["answer"], None
        return {
            "applied": ("applied", "submitted by user in review mode"),
            "skipped": ("skipped", "skipped by user"),
        }.get(answer, ("needs_manual", "deferred by user"))

    def answer_review(self, answer: str) -> bool:
        with self.lock:
            if not self.review:
                return False
            self.review["answer"] = answer
            self.review["event"].set()
            return True


class _Stream(io.TextIOBase):
    def __init__(self, runner: TaskRunner):
        self.runner = runner

    def write(self, s):
        self.runner.write(s)
        return len(s)


# ------------------------------------------------------------------ helpers

def _ensure_files(config_path: Path):
    """First run: copy the example config/profile next to the config path."""
    pairs = [(config_path, config_path.parent / "config.example.yaml")]
    if config_path.exists():
        try:
            prof = (yaml.safe_load(config_path.read_text()) or {}).get("profile", "profile.yaml")
        except yaml.YAMLError:
            prof = "profile.yaml"
    else:
        prof = "profile.yaml"
    prof_path = Path(prof) if Path(prof).is_absolute() else config_path.parent / prof
    pairs.append((prof_path, config_path.parent / "profile.example.yaml"))
    for target, example in pairs:
        if not target.exists() and example.exists():
            shutil.copyfile(example, target)
            print(f"Created {target.name} from {example.name}. Edit it on the Settings tab.")


def ai_status(cfg: dict) -> dict:
    provider = (cfg.get("provider") or "auto").lower()
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if provider == "auto":
        provider = "claude" if has_key else "ollama"
    if provider == "claude":
        return {"provider": "Claude", "model": cfg.get("model"), "ready": has_key,
                "message": "Ready" if has_key else "ANTHROPIC_API_KEY is not set"}
    o = cfg.get("ollama") or {}
    model, host = o.get("model", "qwen2.5:7b"), o.get("host", "http://localhost:11434")
    try:
        names = {m.get("name") for m in
                 requests.get(f"{host.rstrip('/')}/api/tags", timeout=2).json().get("models", [])}
    except (requests.RequestException, ValueError):
        return {"provider": "Ollama", "model": model, "ready": False,
                "message": "Ollama isn't running. Open the Ollama app."}
    wanted = model if ":" in model else f"{model}:latest"
    if wanted not in names:
        return {"provider": "Ollama", "model": model, "ready": False,
                "message": f"Model not downloaded. Run: ollama pull {model}"}
    return {"provider": "Ollama", "model": model, "ready": True, "message": "Ready"}


def _rel(path: str | None, root: Path) -> str | None:
    if not path:
        return None
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _job_row(db: DB, r, out_root: Path) -> dict:
    j = db.job(r)
    a = json.loads(r["assessment"]) if r["assessment"] else {}
    return {
        "key": r["key"], "status": r["status"], "score": r["score"],
        "title": j.title, "company": j.company, "location": j.location,
        "remote": j.remote, "url": j.url, "apply_url": j.apply_url, "source": j.source,
        "internship": is_internship(j), "posted": j.posted_at, "first_seen": r["first_seen"],
        "can_auto_apply": j.can_auto_apply, "note": r["note"], "updated": r["updated"],
        "reasons": a.get("reasons", []),
        "concerns": a.get("missing_requirements", []) + a.get("red_flags", []),
        "resume": _rel(r["resume_path"], out_root),
        "cover_letter": _rel(r["cover_letter_path"], out_root),
    }


# ------------------------------------------------------------------ preferences form
# Reads and writes the job-search settings spread over config.yaml and profile.yaml.
# ruamel.yaml round-trips the files so their comments and layout survive a save.

def _yaml():
    from ruamel.yaml import YAML
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _str_list(v) -> list[str]:
    if isinstance(v, str):
        v = v.replace("\n", ",").split(",")
    return [str(x).strip() for x in (v or []) if str(x).strip()]


def _set_list(parent, key, values):
    from ruamel.yaml.comments import CommentedSeq
    old = parent.get(key)
    seq = CommentedSeq(values)
    if not isinstance(old, CommentedSeq) or old.fa.flow_style():
        seq.fa.set_flow_style()          # keep `[a, b]` style (also for new keys)
    parent[key] = seq


def _section(parent, key):
    from ruamel.yaml.comments import CommentedMap
    if not isinstance(parent.get(key), dict):
        parent[key] = CommentedMap()
    return parent[key]


def read_preferences(cfg: dict, profile: dict) -> dict:
    s, m, src = cfg["search"], cfg["matching"], cfg["sources"]
    prefs = profile.get("preferences") or {}
    rem = src.get("remotive") or {}
    return {
        "focus": s.get("focus") or "jobs",
        "keywords": _str_list(s.get("keywords")),
        "exclude_keywords": _str_list(s.get("exclude_keywords")),
        "locations": _str_list(s.get("locations")),
        "remote_ok": bool(s.get("remote_ok", True)),
        "max_age_days": s.get("max_age_days") or 0,
        "min_score": m.get("min_score", 70),
        "max_jobs_to_score": m.get("max_jobs_to_score", 50),
        "greenhouse": _str_list(src.get("greenhouse")),
        "lever": _str_list(src.get("lever")),
        "ashby": _str_list(src.get("ashby")),
        "remotive_enabled": bool(rem.get("enabled")),
        "remotive_searches": _str_list(rem.get("searches")),
        "seniority": prefs.get("seniority") or "",
        "min_salary_usd": prefs.get("min_salary_usd") or "",
        "work_mode": prefs.get("work_mode") or "",
        "avoid": _str_list(prefs.get("avoid")),
        "internship_term": prefs.get("internship_term") or "",
    }


def validate_preferences(b: dict) -> tuple[dict, str | None]:
    def num(key, lo, hi, allow_blank=False):
        v = b.get(key)
        if allow_blank and v in ("", None):
            return None
        try:
            v = int(float(v))
        except (TypeError, ValueError):
            raise ValueError(key)
        if not lo <= v <= hi:
            raise ValueError(key)
        return v
    labels = {"max_age_days": "Maximum job age (0-365 days)", "min_score": "Minimum fit score (0-100)",
              "max_jobs_to_score": "Jobs to score per run (1-1000)",
              "min_salary_usd": "Minimum salary (a number, or blank)"}
    try:
        out = {
            "focus": b.get("focus") if b.get("focus") in FOCUSES else None,
            "max_age_days": num("max_age_days", 0, 365),
            "min_score": num("min_score", 0, 100),
            "max_jobs_to_score": num("max_jobs_to_score", 1, 1000),
            "min_salary_usd": num("min_salary_usd", 0, 10_000_000, allow_blank=True),
        }
    except ValueError as e:
        return {}, f"Check this field: {labels[e.args[0]]}"
    if not out["focus"]:
        return {}, "Pick what you're looking for: jobs, internships or both."
    for k in ("keywords", "exclude_keywords", "locations", "greenhouse", "lever", "ashby",
              "remotive_searches", "avoid"):
        out[k] = _str_list(b.get(k))
    for k in ("seniority", "work_mode", "internship_term"):
        out[k] = str(b.get(k) or "").strip()
    out["remote_ok"] = bool(b.get("remote_ok"))
    out["remotive_enabled"] = bool(b.get("remotive_enabled"))
    if not (out["greenhouse"] or out["lever"] or out["ashby"] or out["remotive_enabled"]):
        return {}, "Add at least one company to watch, or turn on Remotive."
    return out, None


def write_preferences(config_path: Path, profile_path: Path, p: dict):
    y = _yaml()
    cfg = y.load(config_path.read_text()) if config_path.exists() else None
    if cfg is None:
        from ruamel.yaml.comments import CommentedMap
        cfg = CommentedMap()
    s = _section(cfg, "search")
    s["focus"] = p["focus"]
    for k in ("keywords", "exclude_keywords", "locations"):
        _set_list(s, k, p[k])
    s["remote_ok"] = p["remote_ok"]
    s["max_age_days"] = p["max_age_days"]
    m = _section(cfg, "matching")
    m["min_score"] = p["min_score"]
    m["max_jobs_to_score"] = p["max_jobs_to_score"]
    src = _section(cfg, "sources")
    for k in ("greenhouse", "lever", "ashby"):
        _set_list(src, k, p[k])
    rem = _section(src, "remotive")
    rem["enabled"] = p["remotive_enabled"]
    _set_list(rem, "searches", p["remotive_searches"])

    prof = y.load(profile_path.read_text())
    pr = _section(prof, "preferences")
    pr["seniority"] = p["seniority"]
    if p["min_salary_usd"] is None:
        pr.pop("min_salary_usd", None)
    else:
        pr["min_salary_usd"] = p["min_salary_usd"]
    pr["work_mode"] = p["work_mode"]
    _set_list(pr, "avoid", p["avoid"])
    pr["internship_term"] = p["internship_term"]

    # Write both only after both serialise, so a failure can't leave one half-saved.
    bufs = []
    for data in (cfg, prof):
        buf = io.StringIO()
        y.dump(data, buf)
        bufs.append(buf.getvalue())
    config_path.write_text(bufs[0])
    profile_path.write_text(bufs[1])


# ------------------------------------------------------------------ app

class ConfigError(Exception):
    pass


def _load_config(path: Path) -> dict:
    try:
        return load_config(path)
    except SystemExit as e:  # load_config exits with a readable message
        raise ConfigError(str(e.code)) from None

def create_app(config_path: str | Path) -> Flask:
    config_path = Path(config_path).resolve()
    app = Flask(__name__, static_folder=None)
    runner = TaskRunner()
    app.config["runner"] = runner

    def cfg_profile():
        cfg = load_config(config_path)  # runs in the task thread; SystemExit is reported there
        return cfg, load_profile(cfg["profile"])

    def db_for(cfg):
        return DB(cfg["database"])

    @app.before_request
    def guard():
        host = (request.host or "").split(":")[0]
        if host not in ("localhost", "127.0.0.1", "[::1]", "::1"):
            abort(403)  # blocks DNS-rebinding tricks
        if request.method == "POST" and request.headers.get("X-Jobbot") != "1":
            abort(403)  # a custom header can't be sent cross-site without CORS approval

    @app.errorhandler(ConfigError)
    def config_problem(e):
        return jsonify(error=str(e)), 400

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/api/summary")
    def summary():
        try:
            cfg = load_config(config_path)
        except SystemExit as e:
            return jsonify(error=str(e.code), config_ok=False)
        profile_error = None
        try:
            load_profile(cfg["profile"])
        except SystemExit as e:
            profile_error = str(e.code)
        db = db_for(cfg)
        out_root = Path(cfg["output_dir"])
        want = request.args.get("status")
        statuses = want.split(",") if want else [s for s in STATUSES if s != "filtered"]
        rows = db.by_status(*statuses, order="CASE WHEN score IS NULL THEN 1 ELSE 0 END, "
                                             "score DESC, updated DESC")
        return jsonify(
            config_ok=True, profile_error=profile_error,
            counts=db.counts(), ai=ai_status(cfg),
            apply_mode=cfg["apply"]["mode"], applied_today=db.applied_today(),
            focus=cfg["search"].get("focus") or "jobs",
            jobs=[_job_row(db, r, out_root) for r in rows],
        )

    @app.post("/api/run/<action>")
    def run(action):
        from jobbot import cli
        if action not in ("find", "prepare", "apply"):
            abort(404)
        body = request.get_json(silent=True) or {}
        mode = body.get("mode") if body.get("mode") in ("review", "auto") else None
        limit = body.get("limit")
        args = SimpleNamespace(limit=int(limit) if limit else None, mode=mode,
                               key=body.get("key") or None, ask_human=runner.ask_human)

        def task():
            cfg, profile = cfg_profile()
            Path(cfg["output_dir"]).mkdir(parents=True, exist_ok=True)
            getattr(cli, f"cmd_{action}")(cfg, profile, db_for(cfg), args)

        if not runner.start(action, task):
            return jsonify(error="Another task is still running."), 409
        return jsonify(ok=True)

    @app.get("/api/task")
    def task_state():
        return jsonify(runner.state(int(request.args.get("since", 0))))

    @app.post("/api/review")
    def review():
        answer = (request.get_json(silent=True) or {}).get("answer")
        if answer not in ("applied", "skipped", "later"):
            abort(400)
        if not runner.answer_review(answer):
            return jsonify(error="Nothing is waiting for an answer."), 409
        return jsonify(ok=True)

    @app.post("/api/mark")
    def mark():
        body = request.get_json(silent=True) or {}
        key, status = body.get("key"), body.get("status")
        if status not in STATUSES:
            abort(400)
        cfg = _load_config(config_path)
        db = db_for(cfg)
        if not db.get(key):
            abort(404)
        db.update(key, status=status, note=body.get("note") or "set on the web page")
        return jsonify(ok=True)

    def _settings_path(which: str) -> Path:
        if which == "config":
            return config_path
        if which == "profile":
            return Path(_load_config(config_path)["profile"])
        abort(404)

    @app.get("/api/settings/<which>")
    def get_settings(which):
        p = _settings_path(which)
        return jsonify(text=p.read_text() if p.exists() else "", path=str(p))

    @app.post("/api/settings/<which>")
    def save_settings(which):
        text = (request.get_json(silent=True) or {}).get("text", "")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            return jsonify(error=f"That isn't valid YAML: {e}"), 400
        if not isinstance(data, dict):
            return jsonify(error="The file should be a list of 'key: value' settings."), 400
        if which == "profile":
            missing = [k for k in REQUIRED_PROFILE_KEYS if not data.get(k)]
            if missing:
                return jsonify(error=f"Missing required fields: {', '.join(missing)}"), 400
        p = _settings_path(which)
        p.write_text(text)
        return jsonify(ok=True)

    @app.get("/api/preferences")
    def get_preferences():
        cfg = _load_config(config_path)
        try:
            profile = load_profile(cfg["profile"])
        except SystemExit as e:
            return jsonify(error=str(e.code)), 400
        return jsonify(read_preferences(cfg, profile))

    @app.post("/api/preferences")
    def save_preferences():
        cfg = _load_config(config_path)
        prefs, err = validate_preferences(request.get_json(silent=True) or {})
        if err:
            return jsonify(error=err), 400
        profile_path = Path(cfg["profile"])
        if not profile_path.exists():
            return jsonify(error="profile.yaml is missing. Create it on the Settings tab first."), 400
        try:
            write_preferences(config_path, profile_path, prefs)
        except Exception as e:  # noqa: BLE001 - malformed YAML the user typed by hand
            return jsonify(error=f"Couldn't update your files ({e}). "
                                 "Check them on the Settings tab."), 400
        return jsonify(ok=True)

    @app.get("/files/<path:rel>")
    def files(rel):
        cfg = _load_config(config_path)
        return send_from_directory(Path(cfg["output_dir"]).resolve(), rel)  # refuses ../ escapes

    return app


def serve(config_path: str = "config.yaml", port: int = 8000, open_browser: bool = True):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for noisy in ("httpx", "anthropic", "urllib3", "werkzeug"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    config_path = Path(config_path).resolve()
    _ensure_files(config_path)
    app = create_app(config_path)
    url = f"http://localhost:{port}"
    print(f"\n  jobbot is running at {url}\n  Keep this window open. Press Ctrl+C to stop.\n")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)
