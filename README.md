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

## Step 2: credit assessment

`credit_assessment.py` sends a step-1 result (`outbox/<stamp>/<stamp>.md`) plus three Word templates to FOMO. The model classifies the request (renovation loan / credit line / regular loan), estimates the maximum loan amount or limit, and fills the matching template. The prompt lives in `assessment_prompt.py`: request types and their cues in `LOAN_TYPES`, general lending rules (DSTI, stress rate, DTI, LTV, terms, living costs) in `DEFAULT_RULES`. Rules written in a template take precedence.

Put the three templates (one per request type) in `templates/` next to the scripts. File names do not matter: every file in the folder is sent, and the model decides from each template's content which one fits the request. The first two lines of the answer name the detected request type and the chosen template file; both are shown in the draft header. On start the script logs each template's first line, so you can see what was loaded.

```powershell
python -m pip install python-docx openpyxl
python credit_assessment.py                      # newest result in outbox
python credit_assessment.py "<outbox>\<stamp>"   # a specific case
```

**Automatic run from the dashboard.** `python dashboard.py` starts a second watcher on `OUTBOX_DIR`. When a new outbox folder contains both `combined_input.txt` and `<stamp>.md` and has stopped changing for `STABLE_SECONDS`, it runs the assessment once. This also covers folders written by a standalone `read_inbox.py` or synced in by OneDrive. Folders that existed at startup, or that already contain `*_assessment.md`, are skipped; a folder still missing one of the two files after 15 minutes is reported as failed. The manual command above stays available for re-running a single case.

Output next to the source: `assessment_input.txt` (exact prompt) and `<stamp>_assessment.md` (detected request type + filled template). Copy both `.py` files next to `read_inbox.py`.
