"""Sentinel-1 interferograms from LiCSAR (COMET), free and ready-made for many regions without ARIA or OPERA.

LiCSAR (Lazecký et al., 2020, https://doi.org/10.3390/rs12152430) processes Sentinel-1 automatically for tectonic
and volcanic regions worldwide, with the densest coverage from the Mediterranean to the Himalaya. Products are
public; no login is needed. Layout (checked October 2026):

    {BASE}/{track}/                                 frames, e.g. 035D_05397_131013 (track 35, descending)
    {BASE}/{track}/{frame}/metadata/{frame}-poly.txt     frame outline (lon lat per line)
    {BASE}/{track}/{frame}/metadata/{frame}.geo.U.tif    up component of the ground-to-satellite unit vector
    {BASE}/{track}/{frame}/interferograms/              index of pairs YYYYMMDD_YYYYMMDD
    {PUBLIC}/{track}/{frame}/interferograms/{pair}/{pair}.geo.unw.tif   unwrapped phase (rad, 0 = masked)
    {PUBLIC}/{track}/{frame}/interferograms/{pair}/{pair}.geo.cc.tif    coherence (0-255)

Since LiCSAR's storage migration (2025-26) the files sit under ``LiCSAR_products.public``; the old address of a
pair (without a trailing slash) answers with a page linking to them, which is the fallback used here.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from functools import lru_cache

from .aoi import AOI
from .notes import INFO, Note
from .products import SENTINEL1, Product

BASE = "https://gws-access.jasmin.ac.uk/public/nceo_geohazards/LiCSAR_products"
PUBLIC = BASE + ".public"
CITATION = ("Lazecký, M., et al. (2020). LiCSAR: An automatic InSAR tool for measuring and monitoring tectonic and "
            "volcanic activity. Remote Sensing, 12(15), 2430. https://doi.org/10.3390/rs12152430")
_PAIR = re.compile(r"^(\d{8})_(\d{8})/?$")
_FRAME = re.compile(r"^(\d{3})([AD])_\d{5}_\d{6}/?$")  # plain frames only (not e.g. '.hires.clipped' variants)


def _session():
    import requests

    s = requests.Session()
    s.headers["User-Agent"] = "OpenSAR-Explorer"
    return s


_S = None


def _get(url: str, timeout: float = 60):
    """GET without any login (LiCSAR is public). Module-level session, created on first use."""
    global _S
    if _S is None:
        _S = _session()
    return _S.get(url, timeout=timeout)


def _links(html: str) -> list[str]:
    return re.findall(r"""href=["']([^"'?#]+)["']""", html)


@lru_cache(maxsize=256)
def _index(url: str) -> tuple[str, ...]:
    r = _get(url)
    if r.status_code != 200:
        return ()
    return tuple(h.rstrip("/").rsplit("/", 1)[-1] for h in _links(r.text))


def frames(track: int) -> list[str]:
    """LiCSAR frames on a Sentinel-1 track (relative orbit)."""
    return sorted({f for f in _index(f"{BASE}/{track}/") if _FRAME.match(f)})


@lru_cache(maxsize=256)
def outline(track: int, frame: str):
    """Frame footprint as a shapely polygon (lon/lat), or None."""
    from shapely.geometry import Polygon

    r = _get(f"{BASE}/{track}/{frame}/metadata/{frame}-poly.txt")
    if r.status_code != 200:
        return None
    pts = []
    for line in r.text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                pts.append((float(parts[0]), float(parts[1])))
            except ValueError:
                continue
    if len(pts) < 3:
        return None
    poly = Polygon(pts)
    return poly if poly.is_valid else poly.buffer(0)


def pairs(track: int, frame: str) -> list[tuple[date, date]]:
    out = []
    for name in _index(f"{BASE}/{track}/{frame}/interferograms/"):
        m = _PAIR.match(name)
        if m:
            d1, d2 = (datetime.strptime(x, "%Y%m%d").date() for x in m.groups())
            if d1 < d2:
                out.append((d1, d2))
    return sorted(set(out))


def pair_name(d1: date, d2: date) -> str:
    return f"{d1:%Y%m%d}_{d2:%Y%m%d}"


def file_url(track: int, frame: str, d1: date, d2: date, suffix: str) -> str:
    """Address of one pair's file, e.g. suffix 'geo.unw.tif' (the public storage address; see resolve)."""
    p = pair_name(d1, d2)
    return f"{PUBLIC}/{track}/{frame}/interferograms/{p}/{p}.{suffix}"


def metadata_url(track: int, frame: str, name: str) -> str:
    return f"{BASE}/{track}/{frame}/metadata/{frame}.{name}"


@lru_cache(maxsize=1024)
def resolve(url: str) -> str | None:
    """The working address of a LiCSAR file: as given, else from the link page at the old address."""
    import requests

    try:
        r = _get_head(url)
        if r.status_code == 200:
            return url
    except requests.RequestException:
        pass
    folder, fname = url.rsplit("/", 1)
    page = folder.replace("LiCSAR_products.public/", "LiCSAR_products/")
    for candidate in (page, page.replace("LiCSAR_products/", "LiCSAR_products.future/")):
        try:
            r = _get(candidate)
        except requests.RequestException:
            continue
        if r.status_code == 200:
            for href in _links(r.text):
                if href.endswith("/" + fname) or href == fname:
                    return href if href.startswith("http") else f"{candidate}/{href}"
    return None


def _get_head(url: str):
    global _S
    if _S is None:
        _S = _session()
    return _S.head(url, timeout=60, allow_redirects=True)


def search(aoi: AOI, start=None, end=None, flight_direction: str | None = None,
           tracks: dict[int, str] | None = None) -> tuple[list[Product], list[Note]]:
    """LiCSAR pairs over the area and period, as Sentinel-1 products read remotely (no login).

    ``tracks``: {track: "A" or "D"} to look in; by default the Sentinel-1 tracks that image the area (from ASF).
    """
    notes: list[Note] = []
    if tracks is None:
        tracks = _tracks_over(aoi, start, end)
    want = {"ASCENDING": "A", "DESCENDING": "D"}.get((flight_direction or "").upper())
    out: list[Product] = []
    looked = []
    for track, letter in sorted(tracks.items()):
        if want and letter != want:
            continue
        for frame in frames(track):
            if frame[3] != letter:
                continue
            poly = outline(track, frame)
            if poly is None or not poly.intersects(aoi.geometry):
                continue
            part = poly.intersection(aoi.geometry)
            cover = part.area / aoi.geometry.area if aoi.geometry.area else 0.0
            if cover < 0.05:
                continue
            listed = pairs(track, frame)
            looked.append(f"{frame} ({len(listed)} pairs, covering {cover:.0%})")
            for d1, d2 in listed:
                if (start and d2 < start) or (end and d1 > end):
                    continue
                out.append(Product(
                    sensor=SENTINEL1, product_type="LICSAR", name=f"{frame}_{pair_name(d1, d2)}",
                    url=file_url(track, frame, d1, d2, "geo.unw.tif"), date1=d1, date2=d2,
                    flight_direction="ASCENDING" if letter == "A" else "DESCENDING", track=track, frame=frame,
                    extra={"cover": part, "aoi_area": aoi.geometry.area, "licsar": (track, frame)}))
    if looked:
        notes.append(Note("search", INFO, "LiCSAR (COMET) frames over the area: " + "; ".join(looked) + "."))
    return out, notes


def _tracks_over(aoi: AOI, start=None, end=None) -> dict[int, str]:
    """Sentinel-1 tracks that image the area (from ASF's catalogue of raw images), {track: 'A' or 'D'}."""
    from .hyp3 import search_slc

    scenes = search_slc(aoi, start, end, max_results=400)
    best: dict[tuple[int, str], float] = {}
    for sc in scenes:
        key = (sc.path, "A" if sc.direction == "ASCENDING" else "D")
        best[key] = max(best.get(key, 0.0), sc.cover)
    return {track: letter for (track, letter), cover in best.items() if cover >= 0.3}
