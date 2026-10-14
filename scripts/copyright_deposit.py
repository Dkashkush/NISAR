# Copyright (c) 2026 Deepa Kumari. All rights reserved. See LICENSE.
"""Build a source-code deposit (PDF) for registering the copyright of OpenSAR Explorer.

    python scripts/copyright_deposit.py                 # whole source -> dist/copyright-deposit-<version>.pdf
    python scripts/copyright_deposit.py --identifying   # first and last 25 pages only (what the US Copyright
                                                        # Office usually asks for)

The PDF starts with a title page (title, author, version, git commit, date, notice) and every page carries the
copyright notice and a page number, so the deposit identifies the work and its version. Check your copyright
office's current deposit rules before filing; some ask for the whole source, some for an excerpt.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LINES_PER_PAGE = 88
WIDTH = 104  # characters per line before wrapping


def source_files() -> list[Path]:
    files = [ROOT / "app.py", *sorted((ROOT / "sarcompare").rglob("*.py")), *sorted((ROOT / "scripts").glob("*.py"))]
    return [f for f in files if f.is_file() and "__pycache__" not in f.parts]


_MISSING: dict[str, bool] = {}


def _printable(line: str) -> str:
    """Characters the typewriter font cannot draw (emoji) as their Unicode code, so nothing is lost."""
    from matplotlib import font_manager
    from matplotlib.ft2font import FT2Font

    font = _printable.__dict__.setdefault("font", FT2Font(font_manager.findfont("DejaVu Sans Mono")))
    out = []
    for ch in line:
        if ch not in _MISSING:
            _MISSING[ch] = ord(ch) > 127 and font.get_char_index(ord(ch)) == 0
        out.append(f"[U+{ord(ch):04X}]" if _MISSING[ch] else ch)
    return "".join(out)


def _pages(files: list[Path]) -> list[list[str]]:
    pages, page = [], []
    for f in files:
        lines = [f"==== {f.relative_to(ROOT).as_posix()} " + "=" * max(0, WIDTH - 6 - len(str(f.relative_to(ROOT))))]
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            wrapped = textwrap.wrap(_printable(line.expandtabs(4)), WIDTH - 6, drop_whitespace=False,
                                    subsequent_indent="      ") or [""]
            lines.append(f"{n:5d} {wrapped[0]}")
            lines += [f"      {w}" for w in wrapped[1:]]
        lines.append("")
        for line in lines:
            page.append(line)
            if len(page) == LINES_PER_PAGE:
                pages.append(page)
                page = []
    if page:
        pages.append(page)
    return pages


def build(out: Path, files: list[Path] | None = None, identifying: bool = False) -> int:
    """Write the deposit PDF; returns the number of source pages included."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    from sarcompare import __author__, __copyright__, code_version

    files = files or source_files()
    pages = _pages(files)
    total = len(pages)
    chosen = list(enumerate(pages, 1))
    if identifying and total > 50:
        chosen = chosen[:25] + chosen[-25:]
    title = "OpenSAR Explorer (Python package sarcompare)"
    notice = f"OpenSAR Explorer {code_version()}  ·  {__copyright__}"
    out.parent.mkdir(parents=True, exist_ok=True)
    with PdfPages(out, metadata={"Title": f"{title}: source code deposit", "Author": __author__,
                                 "Subject": notice, "Creator": "scripts/copyright_deposit.py"}) as pdf:
        fig = plt.figure(figsize=(8.27, 11.69))
        lines = [title, "", "Source code deposit for copyright registration", "",
                 f"Author and copyright holder: {__author__}", f"Version: {code_version()}",
                 f"Deposit prepared: {date.today():%d %B %Y}", "", __copyright__, "",
                 f"Files: {len(files)}   Source pages: {total}"
                 + (f"   Included here: first 25 and last 25 pages" if identifying and total > 50 else ""), "",
                 "Each page shows the file path and line numbers of the source code it contains."]
        for i, text in enumerate(lines):
            fig.text(0.1, 0.85 - i * 0.03, text, fontsize=14 if i == 0 else 10.5, family="DejaVu Sans",
                     weight="bold" if i == 0 else "normal")
        pdf.savefig(fig)
        plt.close(fig)
        for number, page in chosen:
            fig = plt.figure(figsize=(8.27, 11.69))
            fig.text(0.06, 0.975, notice, fontsize=7.5, family="DejaVu Sans")
            fig.text(0.94, 0.975, f"page {number} of {total}", fontsize=7.5, family="DejaVu Sans", ha="right")
            fig.text(0.06, 0.955, "\n".join(page), fontsize=7.4, family="DejaVu Sans Mono", va="top",
                     linespacing=1.18)
            pdf.savefig(fig)
            plt.close(fig)
    return len(chosen)


def main() -> int:
    from sarcompare import __version__

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--identifying", action="store_true", help="only the first and last 25 pages")
    ap.add_argument("--out", type=Path, default=ROOT / "dist" / f"copyright-deposit-{__version__}.pdf")
    a = ap.parse_args()
    n = build(a.out, identifying=a.identifying)
    print(f"Wrote {a.out} ({n} source pages plus a title page).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
