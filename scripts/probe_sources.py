"""Check that external data sources answer, and show how their archives are laid out (run on GitHub's servers).

    python scripts/probe_sources.py --tracks 28 35

LiCSAR (COMET) moved to new storage in October 2025, so both the old and the new product folders are listed.
Nothing here needs a login; no token is used.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime

import requests

LICSAR_BASES = [
    "https://gws-access.jasmin.ac.uk/public/nceo_geohazards/LiCSAR_products",
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


def covers(url: str, lon: float, lat: float) -> bool:
    """Point-in-polygon test against a LiCSAR frame outline (lon lat per line)."""
    try:
        r = S.get(url, timeout=60)
        pts = [tuple(map(float, ln.split()[:2])) for ln in r.text.splitlines() if len(ln.split()) >= 2]
    except (requests.RequestException, ValueError):
        return False
    inside = False
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracks", nargs="*", type=int, default=[28, 35])
    ap.add_argument("--frames", type=int, default=4, help="frames to inspect per track")
    ap.add_argument("--point", nargs=2, type=float, metavar=("LON", "LAT"),
                    help="only inspect frames whose outline contains this point")
    a = ap.parse_args()
    a.wanted = []
    for base in LICSAR_BASES:
        print(f"\n=== LiCSAR base {base}")
        root = get(base + "/")
        print(f"    {len(links(root))} entries; first: {links(root)[:8]}")
        for track in a.tracks:
            r = get(f"{base}/{track}/")
            frames = [f.strip("/") for f in links(r)]
            print(f"    track {track}: {len(frames)} frames: {frames[:30]}")
            if a.point:
                frames = [f for f in frames if covers(f"{base}/{track}/{f}/metadata/{f}-poly.txt", *a.point)]
                print(f"    frames containing {a.point}: {frames}")
                a.wanted += frames
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
                for pair in sorted(pairs)[-1:]:  # recent pair; folders may not list
                    for b in (base, base + ".future"):
                        for suffix in ("geo.unw.tif", "geo.cc.tif", "geo.diff_pha.tif", "geo.diff.png"):
                            url = f"{b}/{track}/{frame}/interferograms/{pair}/{pair}.{suffix}"
                            h = S.head(url, timeout=60, allow_redirects=True)
                            print(f"      {url.split('nceo_geohazards/')[1]}: HTTP {h.status_code}, "
                                  f"{h.headers.get('Content-Length')} bytes, ranges {h.headers.get('Accept-Ranges')}")
                spans = {}
                for pair in pairs:
                    try:
                        d1, d2 = (datetime.strptime(x, "%Y%m%d") for x in pair.split("_"))
                    except ValueError:
                        continue
                    spans[(d2 - d1).days] = spans.get((d2 - d1).days, 0) + 1
                print(f"    pair spans (days: count): {dict(sorted(spans.items())[:12])}")
                recent = sorted(p for p in pairs if p[:4] >= "2026")
                print(f"    2026 pairs ({len(recent)}): {recent[:60]}")
                bl = get(f"{base}/{track}/{frame}/metadata/baselines", n=300)
                ep = get(f"{base}/{track}/{frame}/epochs/")
                eps = [e.strip("/") for e in links(ep)]
                print(f"    epochs: {len(eps)}; last {sorted(eps)[-3:]}")
                for ep_ in sorted(eps)[-1:] + sorted(eps)[-20:-19]:
                    for b in (base, base + ".future"):
                        for suffix in ("geo.mli.tif", "sltd.geo.tif", "ztd.geo.tif", "geo.iono.code.tif",
                                       "geo.sltd.tif"):
                            url = f"{b}/{track}/{frame}/epochs/{ep_}/{ep_}.{suffix}"
                            h = S.head(url, timeout=60, allow_redirects=True)
                            if h.status_code == 200:
                                print(f"      {url.split('nceo_geohazards/')[1]}: {h.headers.get('Content-Length')} bytes")
                    print(f"      (epoch {ep_} checked)")
                for name in ("geo.U.tif", "geo.E.tif", "geo.hgt.tif"):
                    url = f"{base}/{track}/{frame}/metadata/{frame}.{name}"
                    h = S.head(url, timeout=60, allow_redirects=True)
                    print(f"      {name}: HTTP {h.status_code}, {h.headers.get('Content-Length')} bytes, "
                          f"ranges {h.headers.get('Accept-Ranges')}")
                for sub in links(fr):
                    if sub.strip("/") not in ("metadata", "interferograms", "epochs"):
                        x = get(f"{base}/{track}/{frame}/{sub}")
                        print(f"    {sub}: {links(x)[:30]}")
    ceda = "https://dap.ceda.ac.uk/neodc/comet/data/licsar_products"
    print(f"\n=== LiCSAR on CEDA {ceda}")
    root = get(ceda + "/")
    names = [x.strip("/") for x in links(root)]
    print(f"    {len(names)} entries: {names[:12]} ... {names[-6:]}")
    for track in a.tracks:
        for t in dict.fromkeys([str(track), f"{track:02d}", f"{track:03d}"]):
            if t not in names:
                continue
            r = get(f"{ceda}/{t}/")
            frames = [f.strip("/") for f in links(r)]
            print(f"    track folder {t}: {len(frames)} frames: {frames[:25]}")
            want = [f for f in frames if a.point is None or f in a.wanted]
            for frame in (want or frames)[: a.frames]:
                fr = get(f"{ceda}/{t}/{frame}/")
                print(f"    {frame}: {links(fr)}")
                ifg = get(f"{ceda}/{t}/{frame}/interferograms/")
                pairs = sorted(x.strip("/") for x in links(ifg))
                print(f"    interferograms: {len(pairs)}; first {pairs[:2]}; last {pairs[-4:]}")
                for pair in pairs[-2:]:
                    one = get(f"{ceda}/{t}/{frame}/interferograms/{pair}/")
                    print(f"      files: {links(one)}")
                    for name in links(one):
                        if name.endswith(".tif"):
                            h = S.head(f"{ceda}/{t}/{frame}/interferograms/{pair}/{name}", timeout=60,
                                       allow_redirects=True)
                            print(f"      {name}: HTTP {h.status_code}, {h.headers.get('Content-Length')} bytes, "
                                  f"ranges {h.headers.get('Accept-Ranges')}, final host "
                                  f"{h.url.split('/')[2]}")
                ep = get(f"{ceda}/{t}/{frame}/epochs/")
                eps = sorted(x.strip("/") for x in links(ep))
                print(f"    epochs: {len(eps)}; last {eps[-3:]}")
                if eps:
                    one = get(f"{ceda}/{t}/{frame}/epochs/{eps[-1]}/")
                    print(f"      files in {eps[-1]}: {links(one)}")
                md = get(f"{ceda}/{t}/{frame}/metadata/")
                print(f"    metadata: {links(md)[:20]}")

    print("\n=== Other sources (no login)")
    for url in OTHER:
        get(url, n=400)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
