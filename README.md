# FOMO Intelligence Dashboard

Copy `dashboard.py` and the `static/` folder into your existing project, **next to** `read_inbox.py` and `prompt.py`.

## Windows PowerShell

```powershell
python -m pip install fastapi uvicorn
python dashboard.py
```

Open: http://127.0.0.1:8000

The single command starts both the FastAPI server and the existing pipeline's `process_folder()` worker in a background thread. Do **not** simultaneously start `read_inbox.py` manually. Existing `.env` credentials remain managed by `read_inbox.py`.

### Important behavior

- The existing watcher operates on **new subdirectories** of `INBOX_DIR`, not individual loose files.
- Existing folders at startup are ignored, matching the original code.
- The dashboard polls every 2 seconds and visualizes existing root-level inbox/outbox entries, processing stages, and the latest Markdown output from `outbox/<timestamp>/*.md`.
- The status "Processing" represents the whole `process_folder` call. Internal OCR/FOMO stages require instrumentation of that function and are **not** claimed in this version.
- Keep the app bound to localhost (`127.0.0.1`). It has no user authentication.
- The watcher is cooperatively stopped between jobs. An ongoing external FOMO API call may continue until its existing timeout.
- No auto-retry of failed jobs. Restarting does not reprocess existing folders, as in the original code.
- This local PoC assumes a single process. It does not provide cross-process locking; do not launch duplicate instances or use Uvicorn workers/reload.

### Notes

The app imports `read_inbox.py`, so that script must keep its `if __name__ == '__main__': main()` guard (visible in your screenshots). The dashboard depends on its `INBOX_DIR`, `OUTBOX_DIR`, `folder_snapshot`, `process_folder`, `POLL_SECONDS`, and `STABLE_SECONDS` symbols.
