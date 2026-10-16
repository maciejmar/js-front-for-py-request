"""One-command local dashboard for the existing read_inbox.py pipeline."""
from __future__ import annotations

import os
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

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import read_inbox as pipeline

INBOX = Path(pipeline.INBOX_DIR)
OUTBOX = Path(pipeline.OUTBOX_DIR)
POLL = float(getattr(pipeline, "POLL_SECONDS", 5))
STABLE = float(getattr(pipeline, "STABLE_SECONDS", 10))

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
                            event("completed", name)
                        except Exception as exc:
                            event("failed", name, str(exc)[:300])
                if not pending and state["stage"] not in {"waiting", "completed", "failed"}:
                    event("waiting")
            except Exception as exc:
                event("failed", error=f"Watcher: {exc}"[:300])
            stop.wait(max(0.5, POLL))
    except Exception as exc:
        event("failed", error=f"Watcher startup: {exc}"[:300])


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
    thread = threading.Thread(target=watch, name="fomo-watcher", daemon=True)
    thread.start()
    yield
    stop.set()
    thread.join(timeout=3)


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
