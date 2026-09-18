"""Extract plain text from uploaded documents (PDF, DOCX, TXT, MD).

Uses system tools already on PATH:
  - pdftotext (from poppler, brew installed earlier)
  - pandoc (already installed)

Returns None on failure — the caller stores the file anyway; the extracted
text is a nice-to-have for AI prompt injection and search.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional


def extract_text(path: Path) -> Optional[str]:
    """Best-effort text extraction. Returns None if we can't extract.

    Supported: .pdf (via pdftotext), .docx (via pandoc), .txt / .md /
    .markdown (read directly).

    Legacy binary .doc is NOT supported — pandoc's `docx` reader parses
    Office Open XML (a ZIP container), not Microsoft's Compound File
    Binary format from Word 97-2003, so routing .doc through pandoc
    always fails silently. Coaches with legacy .doc files should save-as
    .docx in Word first; storage of the raw file still happens (per this
    module's contract), only extraction is skipped.
    """
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            return _pdf(path)
        if suffix == ".docx":
            return _docx(path)
        if suffix == ".doc":
            # Legacy binary .doc — pandoc can't parse it. Return None
            # rather than routing to _docx where it would fail after
            # a subprocess call.
            return None
        if suffix in (".txt", ".md", ".markdown"):
            return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    return None


def _pdf(path: Path) -> Optional[str]:
    if not shutil.which("pdftotext"):
        return None
    result = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _docx(path: Path) -> Optional[str]:
    if not shutil.which("pandoc"):
        return None
    result = subprocess.run(
        ["pandoc", "-f", "docx", "-t", "plain", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None
