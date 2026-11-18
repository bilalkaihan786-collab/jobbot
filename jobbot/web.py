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
        "can_auto_apply": j.can_auto_apply, "note": r["note"], "updated": r["updated"],
        "reasons": a.get("reasons", []),
        "concerns": a.get("missing_requirements", []) + a.get("red_flags", []),
        "resume": _rel(r["resume_path"], out_root),
        "cover_letter": _rel(r["cover_letter_path"], out_root),
    }


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
