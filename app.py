# Copyright (c) 2026 Deepa Kumari. All rights reserved. See LICENSE.
"""Streamlit entry point. Easiest: run ``sarcompare gui``; or ``streamlit run app.py``.

On Streamlit Community Cloud new commits are pulled into the running app's folder, but the server keeps the
``sarcompare`` modules it imported earlier, so a page load could still show the old version until the app was
rebooted. ``fresh_code`` re-imports the package whenever its files have changed since it was imported.
"""

import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().with_name("sarcompare")


def code_stamp() -> int:
    """Fingerprint of the package's source files (names and change times): any pulled change alters it."""
    return hash(tuple(sorted((str(p), p.stat().st_mtime_ns) for p in PACKAGE.rglob("*.py"))))


def fresh_code() -> None:
    """Forget the sarcompare modules imported by an earlier page load if their files have changed since."""
    loaded = sys.modules.get("sarcompare")
    stamp = code_stamp()
    before = getattr(loaded, "_code_stamp", None)
    if loaded is not None and before is not None and before != stamp:
        for name in [m for m in sys.modules if m == "sarcompare" or m.startswith("sarcompare.")]:
            del sys.modules[name]
    import sarcompare

    sarcompare._code_stamp = stamp


if __name__ == "__main__":  # Streamlit runs the app as __main__; importing this file only defines the helpers
    fresh_code()
    from sarcompare.gui import main

    main()
