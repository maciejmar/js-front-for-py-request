"""One-command local dashboard for the existing read_inbox.py pipeline."""
from __future__ import annotations

import os
import mimetypes
import sys
import time
import threading
from pathlib import Path
from datetime import datetime
from collections import deque
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

# Windows registry can map .css/.js to text/plain, which makes browsers ignore the stylesheet.
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("text/javascript", ".js")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import read_inbox as pipeline
import credit_assessment as assessment

INBOX = Path(pipeline.INBOX_DIR)
OUTBOX = Path(pipeline.OUTBOX_DIR)
POLL = float(getattr(pipeline, "POLL_SECONDS", 5))
STABLE = float(getattr(pipeline, "STABLE_SECONDS", 10))
# A step-1 outbox folder is complete when it holds both of these (see read_inbox.process_folder).
STEP1_INPUT = "combined_input.txt"
# Give up on an outbox folder that never receives both files (e.g. OneDrive sync stalled).
OUTBOX_INCOMPLETE_TIMEOUT = 15 * 60
# Stages that persist on screen until the next event, instead of falling back to "waiting".
RESTING_STAGES = {"waiting", "extracted", "assessing", "completed", "failed"}

lock = threading.RLock()
stop = threading.Event()
state = {"stage": "waiting", "folder": None, "since": None, "error": None, "events": deque(maxlen=50), "started": time.time()}


def event(stage: str, folder=None, error=None):
    with lock:
        state.update(stage=stage, folder=folder, since=time.time(), error=error)
        state["events"].appendleft({"stage": stage, "folder": folder, "time": datetime.now().isoformat(timespec="seconds"), "error": error})


def snapshot(folder: Path):
    # Use original snapshot semantics, but avoid a disappearing directory crashing watcher.
    try:
        return pipeline.folder_snapshot(folder)
    except (FileNotFoundError, OSError):
        return ()


def watch():
    """Cooperatively stoppable version of the visible read_inbox.watch() logic."""
    try:
        INBOX.mkdir(parents=True, exist_ok=True)
        OUTBOX.mkdir(parents=True, exist_ok=True)
        known = {p.name for p in INBOX.iterdir() if p.is_dir()}
        pending = {}
        event("waiting")
        while not stop.is_set():
            try:
                for p in INBOX.iterdir():
                    if p.is_dir() and p.name not in known and p.name not in pending:
                        pending[p.name] = (snapshot(p), time.monotonic())
                        event("detected", p.name)
                for name, (old, since) in list(pending.items()):
                    folder = INBOX / name
                    current = snapshot(folder)
                    if not folder.is_dir():
                        pending.pop(name, None)
                        continue
                    if current != old or not current:
                        pending[name] = (current, time.monotonic())
                        event("stabilizing", name)
                    elif time.monotonic() - since >= STABLE:
                        del pending[name]
                        known.add(name)
                        try:
                            event("processing", name)
                            pipeline.process_folder(folder)
                            # The outbox watcher picks the result up and runs the credit assessment.
                            event("extracted", name)
                        except Exception as exc:
                            event("failed", name, str(exc)[:300])
                if not pending and state["stage"] not in RESTING_STAGES:
                    event("waiting")
            except Exception as exc:
                event("failed", error=f"Watcher: {exc}"[:300])
            stop.wait(max(0.5, POLL))
    except Exception as exc:
        event("failed", error=f"Watcher startup: {exc}"[:300])


def step1_result(folder: Path):
    """The step-1 markdown in an outbox folder, or None until both step-1 files are present."""
    try:
        files = [p for p in folder.iterdir() if p.is_file()]
    except OSError:
        return None
    if not any(p.name == STEP1_INPUT for p in files):
        return None
    return next((p for p in files if assessment.is_step1_result(p)), None)


def watch_outbox():
    """Step 2: run the credit assessment for each new outbox folder once it holds combined_input.txt + <stamp>.md.

    Watching the outbox (rather than chaining after process_folder) also covers folders produced by
    a standalone read_inbox.py run or synced in by OneDrive from another machine.
    """
    try:
        OUTBOX.mkdir(parents=True, exist_ok=True)
        known = {p.name for p in OUTBOX.iterdir() if p.is_dir()}
        pending = {}  # name -> (snapshot, stable since, first seen)
        while not stop.is_set():
            try:
                now = time.monotonic()
                for p in OUTBOX.iterdir():
                    if p.is_dir() and p.name not in known and p.name not in pending:
                        pending[p.name] = (snapshot(p), now, now)
                for name, (old, since, first) in list(pending.items()):
                    folder = OUTBOX / name
                    if not folder.is_dir():
                        pending.pop(name, None)
                        continue
                    current = snapshot(folder)
                    source = step1_result(folder)
                    if source is None:
                        if now - first >= OUTBOX_INCOMPLETE_TIMEOUT:
                            del pending[name]
                            known.add(name)
                            event("failed", name, f"Outbox folder incomplete: expected {STEP1_INPUT} and <stamp>.md")
                        else:
                            pending[name] = (current, now, first)
                        continue
                    if current != old:
                        pending[name] = (current, now, first)
                    elif now - since >= STABLE:
                        del pending[name]
                        known.add(name)
                        if any(p.name.endswith(assessment.ASSESSMENT_SUFFIX) for p in folder.iterdir()):
                            continue  # already assessed (e.g. by a manual credit_assessment.py run)
                        try:
                            event("assessing", name)
                            assessment.assess(source)
                            event("completed", name)
                        except Exception as exc:
                            event("failed", name, f"Assessment: {exc}"[:300])
            except Exception as exc:
                event("failed", error=f"Outbox watcher: {exc}"[:300])
            stop.wait(max(0.5, POLL))
    except Exception as exc:
        event("failed", error=f"Outbox watcher startup: {exc}"[:300])


def entries(root: Path, limit=200):
    if not root.exists():
        return []
    out = []
    try:
        for p in root.iterdir():
            try:
                st = p.stat()
                out.append({"name": p.name, "kind": "folder" if p.is_dir() else "file", "size": st.st_size if p.is_file() else None, "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")})
            except OSError:
                continue
    except OSError:
        return []
    return sorted(out, key=lambda x: x["modified"], reverse=True)[:limit]


def latest_result():
    if not OUTBOX.exists():
        return None
    # Output layout seen in read_inbox.py: outbox/<timestamp>/<timestamp>.md
    candidates = []
    try:
        for p in OUTBOX.glob("*/*.md"):
            if p.is_file():
                candidates.append(p)
        for p in OUTBOX.glob("*.md"):
            if p.is_file():
                candidates.append(p)
    except OSError:
        return None
    if not candidates:
        return None
    p = max(candidates, key=lambda x: x.stat().st_mtime)
    try:
        # Prevent huge results from bogging down the browser.
        text = p.read_text(encoding="utf-8", errors="replace")[:250_000]
        return {"name": p.name, "folder": p.parent.name, "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"), "text": text}
    except OSError:
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Avoid running the same watcher via uvicorn reload/multiple workers.
    stop.clear()
    threads = [threading.Thread(target=watch, name="fomo-watcher", daemon=True),
               threading.Thread(target=watch_outbox, name="fomo-assessment-watcher", daemon=True)]
    for t in threads:
        t.start()
    yield
    stop.set()
    for t in threads:
        t.join(timeout=3)


app = FastAPI(title="FOMO Intelligence", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def homepage():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/state")
def api_state():
    with lock:
        status = {k: (list(v) if isinstance(v, deque) else v) for k, v in state.items()}
    inbox = entries(INBOX)
    outbox = entries(OUTBOX)
    latest = latest_result()
    return {"status": status, "inbox": inbox, "outbox": outbox, "latest": latest,
            "counts": {"inbox": len(inbox), "outbox": len(outbox)}, "server_time": time.time()}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False, workers=1)
