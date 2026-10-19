"""Step 2: classify the step-1 loan request and fill the matching bank template with a maximum-loan estimate.

Usage:
    python credit_assessment.py                 # newest <stamp>.md in OUTBOX_DIR
    python credit_assessment.py <file.md>       # a specific step-1 result
    python credit_assessment.py <outbox folder> # the step-1 result inside that folder

All three templates (TEMPLATE_FILES) are sent with every request; the model picks the one matching
the request type. Templates are read from TEMPLATES_DIR (default: ./templates, override with
env CREDIT_TEMPLATES_DIR or --templates).

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
# Loan type key (see assessment_prompt.LOAN_TYPES) -> template file name inside TEMPLATES_DIR.
TEMPLATE_FILES = {
    "remontowy": "kredyt_remontowy.docx",
    "linia_kredytowa": "linia_kredytowa.docx",
    "zwykly": "kredyt.docx",
}
ASSESSMENT_SUFFIX = "_assessment.md"
ASSESSMENT_MAX_TOKENS = 8192
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".xml", ".log"}
MAX_XLSX_ROWS = 2000
TYPE_MARKER = re.compile(r"^RODZAJ_WNIOSKU\s*:\s*([a-z_]+)$", re.IGNORECASE)

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


def load_templates(folder: Path) -> dict[str, tuple[str, str]]:
    folder = pipeline.long_path(folder)
    missing = [name for name in TEMPLATE_FILES.values() if not (folder / name).is_file()]
    if missing:
        raise RuntimeError(f"Missing templates in {folder}: {', '.join(missing)}")
    templates = {}
    for key, name in TEMPLATE_FILES.items():
        log.info("Reading template %s (%s)", name, LOAN_TYPES[key]["label"])
        text = read_template(folder / name)
        if not text:
            raise RuntimeError(f"Template {name} is empty")
        templates[key] = (name, text)
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

def split_type(answer: str) -> tuple[str | None, str]:
    """Pull the 'RODZAJ_WNIOSKU: <key>' marker off the first non-empty line of the answer."""
    lines = answer.strip().splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        # Tolerate markdown decoration the model may add around the marker (bold, code, heading).
        m = TYPE_MARKER.match(re.sub(r"[*`#>]", "", line).strip())
        if m and m.group(1).lower() in LOAN_TYPES:
            return m.group(1).lower(), "\n".join(lines[i + 1:]).strip()
        break
    return None, answer.strip()


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

    loan_type, body = split_type(answer)
    if loan_type:
        log.info("Request type: %s", LOAN_TYPES[loan_type]["label"])
    else:
        log.warning("Model did not return a valid RODZAJ_WNIOSKU marker")

    out_file = out_dir / f"{source_md.stem}{ASSESSMENT_SUFFIX}"
    header = [
        f"# Ocena zdolności kredytowej – {out_dir.name}",
        "",
        f"- **Rodzaj wniosku:** {LOAN_TYPES[loan_type]['label'] if loan_type else 'nieustalony'}",
        f"- **Szablon:** `{templates[loan_type][0] if loan_type else '—'}`",
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
