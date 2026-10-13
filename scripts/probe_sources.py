"""Check that external data sources answer, and show how their archives are laid out (run on GitHub's servers).

    python scripts/probe_sources.py --tracks 28 35

LiCSAR (COMET) moved to new storage in October 2025, so both the old and the new product folders are listed.
Nothing here needs a login; no token is used.
"""

from __future__ import annotations

import argparse
import re

import requests

LICSAR_BASES = [
    "https://gws-access.jasmin.ac.uk/public/nceo_geohazards/LiCSAR_products",
    "https://gws-access.jasmin.ac.uk/public/nceo_geohazards/LiCSAR_products.future",
]
OTHER = [
    "https://data.ceda.ac.uk/neodc/comet/data/licsar_products",
    "https://dap.ceda.ac.uk/neodc/comet/data/licsar_products/",
    "https://egms.land.copernicus.eu/insar-api/archive/download/EGMS_L3_E52N26_100km_U_2019_2023_1.zip",
]
S = requests.Session()
S.headers["User-Agent"] = "OpenSAR-Explorer source check"


def get(url: str, n: int = 0):
    try:
        r = S.get(url, timeout=60, allow_redirects=True)
    except requests.RequestException as e:
        print(f"  {url}\n    -> {type(e).__name__}: {e}")
        return None
    hops = " -> ".join(f"{h.status_code}" for h in r.history + [r])
    print(f"  {url}\n    -> {hops} {r.headers.get('Content-Type', '')} {len(r.content)} bytes")
    if n:
        print("    " + r.text[:n].replace("\n", "\n    "))
    return r


def links(r) -> list[str]:
    if r is None or r.status_code != 200:
        return []
    return [h for h in re.findall(r'href="([^"?#]+)"', r.text) if not h.startswith(("/", "http", "..")) and h != "./"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracks", nargs="*", type=int, default=[28, 35])
    ap.add_argument("--frames", type=int, default=4, help="frames to inspect per track")
    a = ap.parse_args()
    for base in LICSAR_BASES:
        print(f"\n=== LiCSAR base {base}")
        root = get(base + "/")
        print(f"    {len(links(root))} entries; first: {links(root)[:8]}")
        for track in a.tracks:
            r = get(f"{base}/{track}/")
            frames = [f.strip("/") for f in links(r)]
            print(f"    track {track}: {len(frames)} frames: {frames[:30]}")
            for frame in frames[: a.frames]:
                print(f"\n  --- frame {frame}")
                fr = get(f"{base}/{track}/{frame}/")
                print(f"    contents: {links(fr)}")
                md = get(f"{base}/{track}/{frame}/metadata/")
                print(f"    metadata: {links(md)[:40]}")
                for name in links(md):
                    if name.endswith(("poly.txt", "metadata.txt")):
                        get(f"{base}/{track}/{frame}/metadata/{name}", n=600)
                ifg = get(f"{base}/{track}/{frame}/interferograms/")
                pairs = [p.strip("/") for p in links(ifg)]
                print(f"    interferograms: {len(pairs)}; first {pairs[:3]}; last {pairs[-5:]}")
                if pairs:
                    one = get(f"{base}/{track}/{frame}/interferograms/{pairs[-1]}/")
                    print(f"    files in {pairs[-1]}: {links(one)}")
                    for name in links(one):
                        if name.endswith(".tif"):
                            h = S.head(f"{base}/{track}/{frame}/interferograms/{pairs[-1]}/{name}", timeout=60,
                                       allow_redirects=True)
                            print(f"      {name}: HTTP {h.status_code}, {h.headers.get('Content-Length')} bytes, "
                                  f"ranges: {h.headers.get('Accept-Ranges')}")
                for sub in links(fr):
                    if sub.strip("/") not in ("metadata", "interferograms", "epochs"):
                        x = get(f"{base}/{track}/{frame}/{sub}")
                        print(f"    {sub}: {links(x)[:30]}")
    print("\n=== Other sources (no login)")
    for url in OTHER:
        get(url, n=400)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
