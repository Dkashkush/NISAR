"""Search ASF for NISAR and Sentinel-1 interferometric products and pick comparable pairs."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time
from pathlib import Path

from .aoi import AOI
from .notes import GOOD, INFO, WARN, Note
from .products import NISAR, SENTINEL1, Product, pair_dates_from_name

NISAR_LAUNCH = date(2025, 7, 30)

# ---------------------------------------------------------------------------
# ASF search
# ---------------------------------------------------------------------------


def _asf():
    try:
        import asf_search as asf
    except ImportError as e:  # pragma: no cover - dependency is required
        raise RuntimeError("asf_search is required for searching: pip install asf_search") from e
    return asf


def _size_mb(props: dict) -> float | None:
    b = props.get("bytes")
    if isinstance(b, (int, float)):
        return b / 1e6
    if isinstance(b, dict):  # NISAR: {filename: {'bytes': n, 'format': ...}}
        vals = [v.get("bytes", 0) for v in b.values() if isinstance(v, dict)]
        return sum(vals) / 1e6 if vals else None
    return None


def _to_product(result, sensor: str, product_type: str) -> Product:
    p = result.properties
    name = p.get("sceneName") or p.get("fileID") or Path(p.get("url", "")).stem
    url = p.get("url")
    if url and not url.lower().endswith((".h5", ".nc", ".zip")):
        # Some NISAR/OPERA granules list the data file among the additional URLs.
        for u in p.get("additionalUrls", []) or []:
            if u.lower().endswith((".h5", ".nc")):
                url = u
                break
    dates = pair_dates_from_name(Path(url).name if url else name) or pair_dates_from_name(name)
    return Product(
        sensor=sensor,
        product_type=product_type,
        name=name,
        url=url,
        date1=dates[0] if dates else None,
        date2=dates[1] if dates else None,
        flight_direction=(p.get("flightDirection") or "").upper() or None,
        track=p.get("pathNumber"),
        frame=p.get("frameNumber"),
        size_mb=_size_mb(p),
    )


def _window(start: date | None, end: date | None) -> dict:
    kw = {}
    if start:
        kw["start"] = datetime.combine(start, time.min)
    if end:
        kw["end"] = datetime.combine(end, time.max)
    return kw


def search_nisar(aoi: AOI, start=None, end=None, flight_direction=None, max_results=500) -> list[Product]:
    """NISAR L2 geocoded unwrapped interferograms (GUNW) intersecting the AOI."""
    asf = _asf()
    kw = dict(dataset=asf.DATASET.NISAR, processingLevel="GUNW", intersectsWith=aoi.wkt,
              maxResults=max_results, **_window(start, end))
    if flight_direction:
        kw["flightDirection"] = flight_direction
    results = asf.search(**kw)
    return [_to_product(r, NISAR, "GUNW") for r in results]


def search_sentinel1(aoi: AOI, source: str, start=None, end=None, flight_direction=None,
                     max_results=500) -> list[Product]:
    """Sentinel-1 interferometric products: ARIA S1 GUNW (selected regions) or OPERA DISP-S1 (North America)."""
    asf = _asf()
    kw = dict(intersectsWith=aoi.wkt, maxResults=max_results, **_window(start, end))
    if flight_direction:
        kw["flightDirection"] = flight_direction
    if source == "ARIA_S1_GUNW":
        results = asf.search(dataset=asf.DATASET.ARIA_S1_GUNW, **kw)
        return [_to_product(r, SENTINEL1, "ARIA_GUNW") for r in results]
    if source == "OPERA_DISP_S1":
        results = asf.search(dataset=asf.DATASET.OPERA_S1, processingLevel="DISP-S1", **kw)
        return [_to_product(r, SENTINEL1, "DISP-S1") for r in results]
    raise ValueError(f"Unsupported Sentinel-1 search source {source!r}")


# ---------------------------------------------------------------------------
# Local folders (products you already have, e.g. HyP3 on-demand jobs)
# ---------------------------------------------------------------------------


def classify_file(path: Path) -> tuple[str, str] | None:
    """(sensor, product_type) for a recognised product file/folder, else None."""
    n = path.name
    low = n.lower()
    if low.endswith(".h5") and ("gunw" in low or low.startswith("nisar")):
        return NISAR, "GUNW"
    if low.endswith(".nc") and "s1-gunw" in low:
        return SENTINEL1, "ARIA_GUNW"
    if low.endswith(".nc") and "disp-s1" in low:
        return SENTINEL1, "DISP-S1"
    if low.endswith(".zip") and low.startswith("s1"):
        return SENTINEL1, "HYP3_INSAR"
    if path.is_dir() and any(path.glob("*_unw_phase.tif")):
        return SENTINEL1, "HYP3_INSAR"
    return None


def scan_local(folder: str | Path, sensor: str) -> list[Product]:
    folder = Path(folder)
    if not folder.exists():
        raise FileNotFoundError(f"Local product folder not found: {folder}")
    out = []
    for path in sorted(folder.iterdir()):
        kind = classify_file(path)
        if not kind or kind[0] != sensor:
            continue
        dates = pair_dates_from_name(path.name)
        out.append(Product(sensor=kind[0], product_type=kind[1], name=path.stem, local_path=path,
                           date1=dates[0] if dates else None, date2=dates[1] if dates else None,
                           size_mb=_local_size_mb(path)))
    return out


def _local_size_mb(path: Path) -> float:
    if path.is_dir():
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6
    return path.stat().st_size / 1e6


# ---------------------------------------------------------------------------
# Pair selection
# ---------------------------------------------------------------------------


def _evenly(items: list, n: int) -> list:
    if len(items) <= n:
        return items
    step = (len(items) - 1) / (n - 1) if n > 1 else 0
    return [items[round(i * step)] for i in range(n)]


def select_pairs(products: list[Product], max_pairs: int, min_span: int, max_span: int,
                 flight_direction: str | None = None) -> tuple[list[Product], list[Note]]:
    """Choose a compact, same-geometry set of pairs suitable for rate stacking.

    Preference: a sequential chain of pairs (each starts where the last ended), from the
    single viewing geometry (flight direction + track) with the most products.
    """
    notes: list[Note] = []
    if not products:
        return [], notes
    sensor = products[0].sensor
    cands = [p for p in products if p.span_days is not None and min_span <= p.span_days <= max_span]
    dropped = len(products) - len(cands)
    if dropped:
        notes.append(Note("search", INFO, f"{sensor}: ignored {dropped} product(s) outside the "
                                          f"{min_span}–{max_span}-day pair span (or without dates)."))
    if flight_direction:
        cands = [p for p in cands if (p.flight_direction or "").upper() == flight_direction.upper()]
    if not cands:
        return [], notes

    groups: dict[tuple, list[Product]] = defaultdict(list)
    for p in cands:
        groups[p.geometry_key].append(p)
    key, group = max(groups.items(), key=lambda kv: (len(kv[1]), -min(p.date1 for p in kv[1]).toordinal()))
    if len(groups) > 1:
        notes.append(Note("search", INFO,
                          f"{sensor}: {len(groups)} viewing geometries found; using "
                          f"{key[0] or 'unknown direction'} track {key[1]} ({len(group)} products). "
                          "Mixing geometries would mix different line-of-sight directions."))

    group.sort(key=lambda p: (p.date1, p.date2))
    chain: list[Product] = []
    for p in group:
        if not chain or p.date1 >= chain[-1].date2:
            chain.append(p)
    if len(chain) >= 2:
        selected = _evenly(chain, max_pairs)
        notes.append(Note("search", GOOD, f"{sensor}: built a sequential chain of {len(chain)} pairs "
                                          f"(using {len(selected)}). Sequential pairs add up to a continuous record."))
    else:
        selected = sorted(group, key=lambda p: -p.span_days)[:max_pairs]
        selected.sort(key=lambda p: p.date1)
        notes.append(Note("search", INFO, f"{sensor}: no sequential chain; using the {len(selected)} "
                                          "longest-span pairs (longer spans give better rate estimates)."))
    return selected, notes


def overlap_notes(nisar: list[Product], s1: list[Product], start=None) -> list[Note]:
    """Explain how well the two sensors' time windows line up."""
    notes: list[Note] = []
    if start and start < NISAR_LAUNCH:
        notes.append(Note("search", INFO, f"NISAR launched on {NISAR_LAUNCH}; the part of your window "
                                          "before that can only be covered by Sentinel-1."))
    if not nisar:
        notes.append(Note("search", WARN, "No NISAR pairs selected. Check the AOI/date window, or that "
                                          "GUNW products are published for this area yet."))
    if not s1:
        notes.append(Note("search", WARN, "No Sentinel-1 pairs selected. ARIA GUNW covers selected regions "
                                          "and OPERA DISP-S1 covers North America; elsewhere, order HyP3 "
                                          "InSAR jobs in ASF Vertex and use s1_source: LOCAL."))
    if not nisar or not s1:
        return notes
    n0, n1 = min(p.date1 for p in nisar), max(p.date2 for p in nisar)
    s0, s1_end = min(p.date1 for p in s1), max(p.date2 for p in s1)
    ov = (min(n1, s1_end) - max(n0, s0)).days
    total = (max(n1, s1_end) - min(n0, s0)).days or 1
    if ov > 0:
        lvl = GOOD if ov / total > 0.5 else INFO
        notes.append(Note("search", lvl, f"Time windows overlap for {ov} days ({ov / total:.0%} of the combined "
                                         f"period): NISAR {n0}→{n1}, Sentinel-1 {s0}→{s1_end}. Rates from "
                                         "overlapping periods are directly comparable."))
    else:
        notes.append(Note("search", WARN, f"The time windows do not overlap (NISAR {n0}→{n1}, Sentinel-1 "
                                          f"{s0}→{s1_end}). Rates are only comparable if motion is steady; "
                                          "seasonal signals (e.g. aquifer recharge) will show up as differences."))
    return notes
