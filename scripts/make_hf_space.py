"""Build a ready-to-upload Hugging Face Space folder (and zip) from this repository.

    python scripts/make_hf_space.py          ->  dist/hf-space/  and  dist/hf-space.zip

Upload the *contents* of dist/hf-space/ to a new Docker Space (Files -> Add file -> Upload files).
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dist" / "hf-space"
INCLUDE = ["sarcompare", "app.py", "Dockerfile", ".dockerignore", "requirements.txt", "packages.txt",
           "pyproject.toml", ".streamlit/config.toml", "examples", "docs"]

HEADER = """---
title: NISAR vs Sentinel-1
emoji: 🛰️
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
short_description: Compare NISAR L-band and Sentinel-1 C-band InSAR side by side
---

"""


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    for item in INCLUDE:
        src = ROOT / item
        dst = OUT / item
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    # The Space reads its settings from the YAML header at the top of README.md
    (OUT / "README.md").write_text(HEADER + (ROOT / "README.md").read_text(encoding="utf-8"), encoding="utf-8")
    zip_path = shutil.make_archive(str(OUT), "zip", OUT)
    n = sum(1 for p in OUT.rglob("*") if p.is_file())
    print(f"Wrote {OUT} ({n} files) and {zip_path}")


if __name__ == "__main__":
    main()
