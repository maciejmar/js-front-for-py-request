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

Put the templates in `templates/` next to the scripts, named as in `TEMPLATE_FILES` in `credit_assessment.py`:

| Request type | File |
|---|---|
| Renovation loan (`remontowy`) | `kredyt_remontowy.docx` |
| Credit line (`linia_kredytowa`) | `linia_kredytowa.docx` |
| Regular loan (`zwykly`) | `kredyt.docx` |

```powershell
python -m pip install python-docx openpyxl
python credit_assessment.py                      # newest result in outbox
python credit_assessment.py "<outbox>\<stamp>"   # a specific case
```

Output next to the source: `assessment_input.txt` (exact prompt) and `<stamp>_assessment.md` (detected request type + filled template). Copy both `.py` files next to `read_inbox.py`.
