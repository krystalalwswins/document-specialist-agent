"""ParseDocumentTool: turn a document in the sandbox into markdown/text.

The point is that the model should not have to write (and re-write) parsing code
for every PDF or workbook. The conversion script below is authored here, runs in
the sandbox against libraries the image already ships (PyMuPDF, openpyxl,
python-pptx, docx2txt), and the tool only exposes a filename and a size budget.

Full markdown is written next to the source as ``<name>.md`` so nothing is lost
when the preview returned to the model is truncated.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from sandbox.client import SandboxClient
from tools.base_tool import BaseTool, ErrorType, ToolResult
from tools.sandbox_tool import execution_to_tool_result

SUPPORTED_SUFFIXES = (
    ".pdf",
    ".xlsx",
    ".xlsm",
    ".csv",
    ".tsv",
    ".txt",
    ".md",
    ".json",
    ".docx",
    ".pptx",
)

# Runs inside the sandbox. __SOURCE__ / __MARKDOWN__ / __TARGET__ are replaced
# with json-encoded literals, so no submitted value is ever interpolated raw.
DOCUMENT_SCRIPT = r"""
import json, os, sys

source = __SOURCE__
markdown_path = __MARKDOWN__

def as_markdown_table(rows):
    if not rows:
        return "_(empty)_"
    width = max(len(r) for r in rows)
    rows = [list(r) + [""] * (width - len(r)) for r in rows]
    head, *body = rows
    lines = ["| " + " | ".join(str(c) for c in head) + " |",
             "| " + " | ".join("---" for _ in head) + " |"]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in body]
    return "\n".join(lines)

def parse_csv(path):
    import csv
    with open(path, newline="", encoding="utf-8", errors="replace") as handle:
        rows = list(csv.reader(handle))
    return as_markdown_table(rows[:200]) + ("" if len(rows) <= 200 else f"\n\n_(first 200 of {len(rows)} rows)_")

def parse_xlsx(path):
    import openpyxl
    book = openpyxl.load_workbook(path, data_only=True, read_only=True)
    out = []
    for sheet in book.worksheets:
        rows = []
        for index, row in enumerate(sheet.iter_rows(values_only=True)):
            if index >= 200:
                break
            rows.append(["" if cell is None else cell for cell in row])
        out.append(f"## Sheet: {sheet.title}\n\n{as_markdown_table(rows)}")
    return "\n\n".join(out)

def parse_pdf(path):
    import fitz
    document = fitz.open(path)
    parts = []
    for number, page in enumerate(document, start=1):
        parts.append(f"## Page {number}\n\n{page.get_text().strip()}")
        if number >= 50:
            parts.append(f"\n_(first 50 of {document.page_count} pages)_")
            break
    return "\n\n".join(parts)

def parse_docx(path):
    import docx2txt
    return docx2txt.process(path)

def parse_pptx(path):
    from pptx import Presentation
    deck = Presentation(path)
    parts = []
    for number, slide in enumerate(deck.slides, start=1):
        texts = [shape.text for shape in slide.shapes if hasattr(shape, "text") and shape.text]
        parts.append(f"## Slide {number}\n\n" + "\n".join(texts))
    return "\n\n".join(parts)

def parse_text(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()

suffix = os.path.splitext(source)[1].lower()
parsers = {".csv": parse_csv, ".tsv": parse_csv, ".xlsx": parse_xlsx, ".xlsm": parse_xlsx,
           ".pdf": parse_pdf, ".docx": parse_docx, ".pptx": parse_pptx,
           ".txt": parse_text, ".md": parse_text, ".json": parse_text}
parser = parsers.get(suffix)
if parser is None:
    print(json.dumps({"ok": False, "error": "unsupported_type", "suffix": suffix}))
else:
    if not os.path.isfile(source):
        print(json.dumps({"ok": False, "error": "not_found", "path": source}))
    else:
        try:
            text = parser(source)
            with open(markdown_path, "w", encoding="utf-8") as handle:
                handle.write(text)
            print(json.dumps({"ok": True, "markdown": text, "markdown_path": os.path.abspath(markdown_path),
                              "chars": len(text)}))
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
"""


class ParseDocumentTool(BaseTool):
    name = "parse_document"
    description = (
        "Convert a document already in the sandbox to markdown/text. "
        "Supports pdf, xlsx, xlsm, csv, tsv, docx, pptx, txt, md, json. "
        "Prefer this over writing parsing code yourself."
    )
    required_permissions = frozenset({"file.read", "sandbox.execute"})
    file_parameters = ("filename",)
    task_scoped_cwd = True
    retry_safe = True  # read-only conversion, safe to replay

    def __init__(self, client: SandboxClient, max_chars: int = 20000) -> None:
        self._client = client
        self._max_chars = max_chars

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "filename": {
                    "type": "string",
                    "minLength": 1,
                    "description": "document path relative to the task directory",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": self._max_chars,
                    "description": "preview budget returned to the model",
                },
            },
            "required": ["filename"],
        }

    def execute(
        self,
        filename: str,
        max_chars: Optional[int] = None,
        cwd: Optional[str] = None,
    ) -> ToolResult:
        budget = self._max_chars if max_chars is None else max_chars
        markdown_name = _markdown_name(filename)
        script = (
            DOCUMENT_SCRIPT.replace("__SOURCE__", json.dumps(filename))
            .replace("__MARKDOWN__", json.dumps(markdown_name))
        )
        result = self._client.execute_python(script, cwd=cwd)
        if result.status != "ok":
            return execution_to_tool_result(result)

        payload = _last_json_line(result.text)
        if payload is None:
            return ToolResult(
                success=False,
                output=result.text,
                error="document parser returned no result",
                error_type=ErrorType.EXECUTION,
            )
        if not payload.get("ok"):
            reason = payload.get("error", "parse failed")
            if reason == "unsupported_type":
                reason = f"unsupported file type '{payload.get('suffix', '')}'; supported: {', '.join(SUPPORTED_SUFFIXES)}"
            return ToolResult(
                success=False,
                error=reason,
                error_type=ErrorType.INVALID_ARGUMENT,
            )

        markdown = payload.get("markdown", "")
        preview = markdown[:budget]
        if len(markdown) > budget:
            preview += f"\n\n_(truncated: {len(markdown)} chars total, full markdown at {payload['markdown_path']})_"
        return ToolResult(
            success=True,
            output=preview,
            metadata={
                "source": filename,
                "markdown_path": payload.get("markdown_path"),
                "chars": payload.get("chars", len(markdown)),
                "truncated": len(markdown) > budget,
            },
        )


def _markdown_name(filename: str) -> str:
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0] or "document"
    return f"{stem}.md"


def _last_json_line(text: str) -> Optional[dict[str, Any]]:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None
