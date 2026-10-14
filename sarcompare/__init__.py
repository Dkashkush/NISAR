# Copyright (c) 2026 Deepa Kumari. All rights reserved. See LICENSE.
"""OpenSAR Explorer (package ``sarcompare``): satellite radar analysis. Copyright (c) 2026 Deepa Kumari.
All rights reserved; see LICENSE.

Module 1, Ground Motion: compare NISAR and Sentinel-1 InSAR over the same region.
"""

__version__ = "0.1.0"
__author__ = "Deepa Kumari"
__copyright__ = "© 2026 Deepa Kumari. All rights reserved."


def provenance() -> dict:
    """What made a result file: written into GeoTIFF tags, figure metadata and metrics.json, so results can be
    traced to the software that produced them (the data inside stay the providers'; see THIRD_PARTY_NOTICES.md)."""
    return {"software": f"OpenSAR Explorer {code_version()}", "software_copyright": __copyright__,
            "software_url": "https://github.com/Dkashkush/OpenSAR-Explorer"}


def code_version() -> str:
    """'0.1.0 (a3ef815)': the version plus the git commit of the running code, when it runs from a checkout
    (as on Streamlit Community Cloud), so anyone can see which version an app is running."""
    from pathlib import Path

    git = Path(__file__).resolve().parents[1] / ".git"
    commit = ""
    try:
        head = (git / "HEAD").read_text().strip()
        if head.startswith("ref: "):
            ref = head[5:]
            if (git / ref).is_file():
                commit = (git / ref).read_text().strip()
            elif (git / "packed-refs").is_file():
                for line in (git / "packed-refs").read_text().splitlines():
                    if line.endswith(" " + ref):
                        commit = line.split()[0]
        else:
            commit = head
    except OSError:
        pass
    return f"{__version__} ({commit[:7]})" if commit else __version__
