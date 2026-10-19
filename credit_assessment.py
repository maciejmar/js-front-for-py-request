"""Step 2: classify the step-1 loan request and fill the matching bank template with a maximum-loan estimate.

Usage:
    python credit_assessment.py                 # newest <stamp>.md in OUTBOX_DIR
    python credit_assessment.py <file.md>       # a specific step-1 result
    python credit_assessment.py <outbox folder> # the step-1 result inside that folder

Every template file in TEMPLATES_DIR (default: ./templates, override with env CREDIT_TEMPLATES_DIR
or --templates) is sent with every request, whatever its name; the model classifies the request,
picks the matching template from its content and reports both in the first two lines of the answer.

Writes next to the source markdown:
    assessment_input.txt      - the exact prompt sent to the model
    <stamp>_assessment.md     - the filled template
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import read_inbox as pipeline
from assessment_prompt import LOAN_TYPES, build_assessment_prompt

ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = Path(os.getenv("CREDIT_TEMPLATES_DIR", ROOT / "templates"))
EXPECTED_TEMPLATES = 3  # one per loan type in assessment_prompt.LOAN_TYPES
ASSESSMENT_SUFFIX = "_assessment.md"
ASSESSMENT_MAX_TOKENS = 8192
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".xml", ".log"}
MAX_XLSX_ROWS = 2000
MARKER = re.compile(r"^(RODZAJ_WNIOSKU|SZABLON)\s*:\s*(.+?)$", re.IGNORECASE)

log = logging.getLogger("fomo.assessment")


# ---------------------------------------------------------------- Templates

def read_docx(path: Path) -> str:
    """Word -> markdown-like text that keeps headings, lists and tables, so the model can mirror the layout."""
    try:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError:
        raise RuntimeError("python-docx is not installed: pip install python-docx")

    def paragraph(p: Paragraph) -> str | None:
        text = p.text.strip()
        if not text:
            return None
        style = (p.style.name if p.style is not None else "").lower()
        level = re.search(r"(heading|otsikko|nagłówek)\s*(\d)", style)
        if level:
            return "#" * min(int(level.group(2)), 6) + " " + text
        if style == "title":
            return "# " + text
        if "list" in style or "luettelo" in style or "lista" in style or p._p.pPr is not None and p._p.pPr.numPr is not None:
            return "- " + text
        return text

    def table(t: Table) -> list[str]:
        rows = []
        for row in t.rows:
            cells, seen = [], set()
            for c in row.cells:
                # Merged cells are returned once per grid column; keep each physical cell once.
                if id(c._tc) in seen:
                    continue
                seen.add(id(c._tc))
                cells.append(" ".join(c.text.split()))
            rows.append("| " + " | ".join(cells) + " |")
        return rows

    doc = docx.Document(str(path))
    out = []
    # Walk the body in document order so tables stay next to the paragraphs that describe them.
    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            line = paragraph(Paragraph(child, doc))
            if line:
                out.append(line)
        elif child.tag.endswith("}tbl"):
            out.extend(table(Table(child, doc)))
    return "\n".join(out)


def read_xlsx(path: Path) -> str:
    from openpyxl import load_workbook
    values = load_workbook(path, data_only=True, read_only=True)
    formulas = load_workbook(path, data_only=False, read_only=True)
    out = []
    for ws_v, ws_f in zip(values.worksheets, formulas.worksheets):
        out.append(f"### Arkusz: {ws_v.title}")
        for i, (row_v, row_f) in enumerate(zip(ws_v.iter_rows(), ws_f.iter_rows())):
            if i >= MAX_XLSX_ROWS:
                out.append(f"... (obcięto po {MAX_XLSX_ROWS} wierszach)")
                break
            cells = []
            for cv, cf in zip(row_v, row_f):
                if cv.value is None and cf.value is None:
                    continue
                text = "" if cv.value is None else str(cv.value)
                # Formulas carry the bank's calculation rules, so keep them alongside the cached value.
                if isinstance(cf.value, str) and cf.value.startswith("="):
                    text = f"{text} [{cf.value}]".strip()
                cells.append(f"{cv.coordinate}: {text}")
            if cells:
                out.append(" | ".join(cells))
    values.close()
    formulas.close()
    return "\n".join(out)


def read_template(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    if suffix in {".html", ".htm"}:
        return pipeline.html_to_text(path.read_text(encoding="utf-8", errors="replace"))
    if suffix == ".docx":
        return read_docx(path)
    if suffix in {".xlsx", ".xlsm"}:
        return read_xlsx(path)
    if suffix in {".doc", ".xls"}:
        raise RuntimeError(f"{path.name}: old Office format, save it as .docx / .xlsx")
    # PDF (text layer + OCR of image pages) and images (OCR) via the step-1 readers.
    return pipeline.read_document(path)


def load_templates(folder: Path) -> list[tuple[str, str]]:
    folder = pipeline.long_path(folder)
    if not folder.is_dir():
        raise RuntimeError(f"Templates folder not found: {folder}")
    # Skip Word lock files (~$name.docx) and hidden files.
    files = sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith(("~$", ".")))
    templates = []
    for path in files:
        text = read_template(path)
        if not text:
            log.warning("Template %s is empty or unsupported - skipped", path.name)
            continue
        first_line = next((l.lstrip("# ") for l in text.splitlines() if l.strip()), "")
        log.info("Template %s: %s", path.name, first_line[:80])
        templates.append((path.name, text))
    if not templates:
        raise RuntimeError(f"No templates in {folder}")
    if len(templates) != EXPECTED_TEMPLATES:
        log.warning("Expected %d templates (one per loan type), found %d in %s", EXPECTED_TEMPLATES, len(templates), folder)
    return templates


# ---------------------------------------------------------------- Source markdown

def is_step1_result(p: Path) -> bool:
    return p.suffix.lower() == ".md" and not p.name.endswith(ASSESSMENT_SUFFIX)


def find_source(arg: str | None) -> Path:
    if arg:
        p = Path(arg)
        if p.is_file():
            return p
        if p.is_dir():
            candidates = [c for c in p.glob("*.md") if is_step1_result(c)]
            if candidates:
                return max(candidates, key=lambda c: c.stat().st_mtime)
        raise RuntimeError(f"No step-1 markdown found at {p}")
    candidates = [c for c in Path(pipeline.OUTBOX_DIR).glob("*/*.md") if is_step1_result(c)]
    if not candidates:
        raise RuntimeError(f"No step-1 markdown found in {pipeline.OUTBOX_DIR}")
    return max(candidates, key=lambda c: c.stat().st_mtime)


# ---------------------------------------------------------------- Pipeline

def split_markers(answer: str) -> tuple[dict[str, str], str]:
    """Pull the leading 'RODZAJ_WNIOSKU: …' / 'SZABLON: …' lines off the answer."""
    lines = answer.strip().splitlines()
    markers, i = {}, 0
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        # Tolerate markdown decoration the model may add around the markers (bold, code, heading).
        m = MARKER.match(re.sub(r"[*`#>]", "", lines[i]).strip())
        if not m:
            break
        markers[m.group(1).upper()] = m.group(2).strip()
        i += 1
    return markers, "\n".join(lines[i:]).strip()


def assess(source_md: Path, templates_dir: Path = TEMPLATES_DIR) -> Path:
    source_md = pipeline.long_path(source_md)
    log.info("Assessing %s", source_md.name)
    application = source_md.read_text(encoding="utf-8", errors="replace")
    templates = load_templates(templates_dir)

    prompt = build_assessment_prompt(application, templates)
    out_dir = source_md.parent
    # Save the prompt before the call, so a failed FOMO request still leaves it for inspection.
    (out_dir / "assessment_input.txt").write_text(prompt, encoding="utf-8")
    log.info("Sending assessment prompt to FOMO (%d chars)", len(prompt))
    answer = pipeline.call_fomo([{"text": prompt}], ASSESSMENT_MAX_TOKENS)

    markers, body = split_markers(answer)
    loan_type = markers.get("RODZAJ_WNIOSKU", "").lower()
    loan_type = loan_type if loan_type in LOAN_TYPES else None
    template = markers.get("SZABLON")
    template = template if template in {name for name, _ in templates} else None
    log.info("Request type: %s, template: %s",
             LOAN_TYPES[loan_type]["label"] if loan_type else "not recognised", template or "not recognised")

    out_file = out_dir / f"{source_md.stem}{ASSESSMENT_SUFFIX}"
    header = [
        f"# Ocena zdolności kredytowej – {out_dir.name}",
        "",
        f"- **Rodzaj wniosku:** {LOAN_TYPES[loan_type]['label'] if loan_type else 'nieustalony'}",
        f"- **Szablon:** {f'`{template}`' if template else 'nieustalony'}",
        f"- **Oceniono:** {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- **Wniosek:** `{source_md.name}`",
        f"- **Model:** `{pipeline.MODEL_ID}`",
        "",
        "---",
        "",
    ]
    out_file.write_text("\n".join(header) + body + "\n", encoding="utf-8")
    log.info("Saved %s", out_file)
    return out_file


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Step-1 markdown + 3 bank templates -> FOMO -> filled template with maximum loan estimate")
    parser.add_argument("source", nargs="?", help="step-1 .md file or outbox folder (default: newest in outbox)")
    parser.add_argument("--templates", default=str(TEMPLATES_DIR), metavar="FOLDER", help="folder with the template files")
    args = parser.parse_args()
    assess(find_source(args.source), Path(args.templates))


if __name__ == "__main__":
    main()
