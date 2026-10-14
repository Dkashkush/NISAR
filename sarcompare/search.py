# Copyright (c) 2026 Deepa Kumari. All rights reserved. See LICENSE.
"""Search ASF for NISAR and Sentinel-1 interferometric products and pick comparable pairs."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time
from pathlib import Path

from .aoi import AOI
from .notes import GOOD, INFO, WARN, Note
from .products import NISAR, SENTINEL1, Product, pair_dates_from_name, parse_name

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


def _cover(result, aoi: AOI | None) -> dict:
    """The part of the AOI inside the product footprint (lon/lat), to choose geometries and frames by coverage."""
    geom = getattr(result, "geometry", None)
    if aoi is None or not geom:
        return {}
    try:
        from shapely.geometry import shape

        part = shape(geom).intersection(aoi.geometry)
    except (ValueError, TypeError, AttributeError):  # missing or invalid footprint
        return {}
    return {"cover": part, "aoi_area": aoi.geometry.area}


def coverage(products: list[Product]) -> float | None:
    """Fraction of the AOI covered by the union of the products' footprints (None when footprints are unknown)."""
    parts = [p.extra["cover"] for p in products if "cover" in p.extra]
    if not parts:
        return None
    from shapely.ops import unary_union

    area = next(p.extra["aoi_area"] for p in products if "cover" in p.extra)
    return float(unary_union(parts).area / area) if area else None


def _to_product(result, sensor: str, product_type: str, aoi: AOI | None = None) -> Product:
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
    info = parse_name(Path(url).name if url else name) or parse_name(name)
    return Product(
        sensor=sensor,
        product_type=product_type,
        name=name,
        url=url,
        date1=dates[0] if dates else None,
        date2=dates[1] if dates else None,
        flight_direction=(p.get("flightDirection") or "").upper() or None,
        track=p.get("pathNumber"),
        frame=info.get("frame") or p.get("frameNumber"),  # OPERA frame IDs are in the name (F36542)
        size_mb=_size_mb(p),
        extra=_cover(result, aoi),
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
    return [_to_product(r, NISAR, "GUNW", aoi) for r in results]


def search_sentinel1(aoi: AOI, source: str, start=None, end=None, flight_direction=None,
                     max_results=500) -> list[Product]:
    """Sentinel-1 interferometric products: ARIA S1 GUNW (selected regions) or OPERA DISP-S1 (North America)."""
    asf = _asf()
    kw = dict(intersectsWith=aoi.wkt, maxResults=max_results, **_window(start, end))
    if flight_direction:
        kw["flightDirection"] = flight_direction
    if source == "ARIA_S1_GUNW":
        results = asf.search(dataset=asf.DATASET.ARIA_S1_GUNW, **kw)
        return [_to_product(r, SENTINEL1, "ARIA_GUNW", aoi) for r in results]
    if source == "OPERA_DISP_S1":
        results = asf.search(dataset=asf.DATASET.OPERA_S1, processingLevel="DISP-S1", **kw)
        return [_to_product(r, SENTINEL1, "DISP-S1", aoi) for r in results]
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
    """Products in a folder. Viewing geometry is read from official file names so that ascending and
    descending products are never mixed; a HyP3 zip and its unzipped folder count once."""
    folder = Path(folder)
    if not folder.exists():
        raise FileNotFoundError(f"Local product folder not found: {folder}")
    out, seen = [], set()
    paths = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name))  # prefer unzipped folders
    for path in paths:
        kind = classify_file(path)
        if not kind or kind[0] != sensor:
            continue
        stem = path.name[:-4] if path.name.lower().endswith(".zip") else path.stem if path.is_file() else path.name
        if stem in seen:
            continue
        seen.add(stem)
        info = parse_name(path.name)
        dates = (info["date1"], info["date2"]) if info else pair_dates_from_name(path.name)
        out.append(Product(sensor=kind[0], product_type=kind[1], name=stem, local_path=path,
                           date1=dates[0] if dates else None, date2=dates[1] if dates else None,
                           flight_direction=info.get("direction"), track=info.get("track"),
                           frame=info.get("frame"), size_mb=_local_size_mb(path)))
    return out


def _local_size_mb(path: Path) -> float:
    if path.is_dir():
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6
    return path.stat().st_size / 1e6


# ---------------------------------------------------------------------------
# Pair selection
# ---------------------------------------------------------------------------




def _iou(start: date, end: date, target: tuple[date, date] | None) -> float:
    """Overlap of [start, end] with ``target`` as a fraction of their union (0 when there is no target)."""
    if not target:
        return 0.0
    t0, t1 = target
    overlap = (min(end, t1) - max(start, t0)).days
    union = (max(end, t1) - min(start, t0)).days or 1
    return round(max(overlap, 0) / union, 3)


def date_range(products: list[Product]) -> tuple[date, date] | None:
    dated = [p for p in products if p.date1 and p.date2]
    return (min(p.date1 for p in dated), max(p.date2 for p in dated)) if dated else None


def _best_path(group: list[Product], max_pairs: int, target: tuple[date, date] | None = None) -> list[Product]:
    """Best gapless chain (each pair starts on the date the previous one ends), at most ``max_pairs`` long.

    Chosen by: overlap with ``target`` (e.g. the other satellite's window), then the longest time span, then the
    most recent end, then the fewest pairs. A path search rather than a greedy pass, so it also finds the right
    chain through OPERA DISP-S1 files, where each file runs from a reference date to a later date and the next
    reference date is the last date of the previous group.
    """
    group = sorted(group, key=lambda p: (p.date1, p.date2))
    # paths[(end date, number of pairs)] = {start date: path}; predecessors always sort first (shorter date1)
    paths: dict[tuple[date, int], dict[date, list[Product]]] = defaultdict(dict)
    for p in group:
        new = [(p.date1, [p])]
        for k in range(1, max_pairs):
            for start, path in paths.get((p.date1, k), {}).items():
                new.append((start, path + [p]))
        for start, path in new:
            slot = paths[(p.date2, len(path))]
            if start not in slot:
                slot[start] = path
    best, best_key = [], None
    for (end, k), starts in paths.items():
        for start, path in starts.items():
            key = (_iou(start, end, target), (end - start).days, end.toordinal(), -k)
            if best_key is None or key > best_key:
                best, best_key = path, key
    return best


def _best_window(chain: list[Product], n: int, target: tuple[date, date] | None = None) -> list[Product]:
    """The run of ``n`` chain pairs with the fewest gaps, then the best overlap with ``target``, then the longest
    time span, then the most recent."""
    if len(chain) <= n:
        return chain
    best, best_key = None, None
    for i in range(len(chain) - n + 1):
        w = chain[i:i + n]
        gaps = sum(1 for a, b in zip(w, w[1:]) if b.date1 != a.date2)
        key = (gaps, -_iou(w[0].date1, w[-1].date2, target), -(w[-1].date2 - w[0].date1).days,
               -w[-1].date2.toordinal())
        if best_key is None or key < best_key:
            best, best_key = w, key
    return best


def select_pairs(products: list[Product], max_pairs: int, min_span: int, max_span: int,
                 flight_direction: str | None = None,
                 target: tuple[date, date] | None = None) -> tuple[list[Product], list[Note]]:
    """Choose a compact, same-geometry set of pairs suitable for rate stacking.

    Preference: a gapless chain of pairs (each starts where the last ended), from the single viewing geometry
    (flight direction + track) with the most products, overlapping ``target`` (the other satellite's time
    window) as much as possible.
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
    # Prefer the geometry that covers most of the area (a track that only clips its edge is of little use), then
    # the one with the most dates (products / frames), then the earliest start.
    def rank(kv):
        cov = coverage(kv[1])
        n_frames = len({p.frame for p in kv[1]}) or 1
        return (round(cov, 1) if cov is not None else 1.0, len(kv[1]) / n_frames,
                -min(p.date1 for p in kv[1]).toordinal())

    key, group = max(groups.items(), key=rank)
    if len(groups) > 1:
        cov = coverage(group)
        cov_txt = f", covering {cov:.0%} of the area" if cov is not None else ""
        notes.append(Note("search", INFO,
                          f"{sensor}: {len(groups)} viewing geometries found; using "
                          f"{key[0] or 'unknown direction'} track {key[1]} ({len(group)} products{cov_txt}). "
                          "Mixing geometries would mix different line-of-sight directions."))

    frames: dict = defaultdict(list)
    for p in group:
        frames[p.frame].append(p)
    if len(frames) == 1:
        selected, chain_notes = _select_chain(group, sensor, max_pairs, target)
        return selected, notes + chain_notes
    # The area spans several frames (fixed processing tiles along the track). Each frame is processed on its
    # own, so each gets its own chain; they are stacked separately and joined where they overlap.
    picks = {f: _select_chain(g, sensor, max_pairs, target) for f, g in frames.items()}
    windows = {f: (sel[0].date1, sel[-1].date2) for f, (sel, _) in picks.items() if sel}
    cov = {f: coverage(frames[f]) for f in windows}
    best = max(windows, key=lambda f: (round(_iou(*windows[f], target or windows[f]), 1),
                                       round(cov[f], 1) if cov[f] is not None else 1.0, len(frames[f])))
    keep = [best]
    for f in sorted(windows, key=lambda f: -(cov[f] or 0)):
        if f == best or _iou(*windows[f], windows[best]) < 0.5:
            continue
        before, after = coverage([p for k in keep for p in frames[k]]), coverage([p for k in keep + [f] for p in frames[k]])
        if before is None or after is None or after - before >= 0.05:  # only frames that add area
            keep.append(f)
    selected = sorted((p for f in keep for p in picks[f][0]), key=lambda p: (p.date1, p.frame or 0))
    notes += picks[best][1]
    names = ", ".join(str(f) for f in sorted(keep, key=lambda f: (f is None, f)))
    notes.append(Note("search", INFO, f"{sensor}: the area spans {len(frames)} frames; using {len(keep)} "
                                      f"(frame {names}) with matching dates. Each frame is stacked on its own and "
                                      "the frames are joined where they overlap."))
    return selected, notes


def _select_chain(group: list[Product], sensor: str, max_pairs: int,
                  target: tuple[date, date] | None) -> tuple[list[Product], list[Note]]:
    """Pairs from one frame: the best gapless chain, else the best window of pairs with gaps, else the longest."""
    notes: list[Note] = []
    latest = {}  # reprocessed versions share frame and dates; names end with the production time, so keep the last
    for p in sorted(group, key=lambda p: p.name):
        latest[(p.date1, p.date2)] = p
    group = sorted(latest.values(), key=lambda p: (p.date1, p.date2))
    path = _best_path(group, max_pairs, target)
    chain: list[Product] = []
    for p in group:
        if not chain or p.date1 >= chain[-1].date2:
            chain.append(p)
    window = _best_window(chain, max_pairs, target) if len(chain) >= 2 else []
    path_days = (path[-1].date2 - path[0].date1).days if path else 0
    if len(path) >= 2:
        selected = path
        notes.append(Note("search", GOOD, f"{sensor}: using {len(selected)} consecutive pairs covering {path_days} "
                                          f"days ({selected[0].date1} → {selected[-1].date2}). Added together, the "
                                          "atmospheric noise of the in-between dates cancels out."))
    elif window and sum(p.span_days for p in window) > path_days:
        selected = window
        gaps = sum(1 for a, b in zip(selected, selected[1:]) if b.date1 != a.date2)
        span = (selected[-1].date2 - selected[0].date1).days
        notes.append(Note("search", INFO, f"{sensor}: using {len(selected)} pairs over {span} days with {gaps} "
                                          "gap(s) between them; consecutive pairs would give less noise."))
    elif path and path[0].product_type == "DISP-S1":
        selected = path
        notes.append(Note("search", INFO, f"{sensor}: using one OPERA file covering {path_days} days "
                                          f"({path[0].date1} → {path[0].date2}); each file holds the total motion "
                                          "since its reference date. With a single file the noise level cannot be "
                                          "estimated."))
    else:
        selected = sorted(group, key=lambda p: -p.span_days)[:max_pairs]
        selected.sort(key=lambda p: p.date1)
        notes.append(Note("search", INFO, f"{sensor}: no sequential chain; using the {len(selected)} "
                                          "longest-span pairs (longer spans give better rate estimates)."))
    return selected, notes


def select_directions(products: list[Product], max_pairs: int, min_span: int, max_span: int,
                      flight_direction: str | None = None, target: tuple[date, date] | None = None,
                      combine: bool = True, min_coverage: float = 0.5) -> tuple[list[Product], list[Note]]:
    """Like select_pairs, but with ``combine`` picks pairs from both flight directions when both cover the area,
    so vertical and east-west motion can be separated (processing.combine_directions). ``max_pairs`` applies to
    each direction."""
    if not combine or flight_direction:
        return select_pairs(products, max_pairs, min_span, max_span, flight_direction, target)
    picks = {}
    for d in ("ASCENDING", "DESCENDING"):
        group = [p for p in products if (p.flight_direction or "").upper() == d]
        cov = coverage(group)
        if group and (cov is None or cov >= min_coverage):
            sel, notes = select_pairs(group, max_pairs, min_span, max_span, d, target)
            if sel:
                picks[d] = (sel, notes)
    if len(picks) < 2:
        return select_pairs(products, max_pairs, min_span, max_span, None, target)
    # both directions should describe the same period, or their difference is time, not geometry
    w = {d: (sel[0].date1, sel[-1].date2) for d, (sel, _) in picks.items()}
    if _iou(*w["ASCENDING"], w["DESCENDING"]) < 0.5:
        sel, notes = select_pairs(products, max_pairs, min_span, max_span, None, target)
        return sel, notes + [Note("search", INFO, f"{products[0].sensor}: both flight directions have data, but "
                                                  "not for matching periods, so only one is used.")]
    selected = picks["ASCENDING"][0] + picks["DESCENDING"][0]
    notes = picks["ASCENDING"][1] + picks["DESCENDING"][1]
    notes.append(Note("search", GOOD, f"{products[0].sensor}: using both ascending and descending passes, so "
                                      "vertical and east-west motion can be measured separately."))
    return selected, notes


def overlap_notes(nisar: list[Product], s1: list[Product], start=None) -> list[Note]:
    """Explain how well the two sensors' time windows line up."""
    notes: list[Note] = []
    if start and start < NISAR_LAUNCH:
        notes.append(Note("search", INFO, f"NISAR launched on {NISAR_LAUNCH}; the part of your window "
                                          "before that can only be covered by Sentinel-1."))
    alone = {True: " The analysis continues with the other satellite alone.", False: ""}[bool(nisar) != bool(s1)]
    if not nisar:
        notes.append(Note("search", WARN, "No NISAR pairs selected. Check the AOI/date window, or that "
                                          "GUNW products are published for this area yet." + alone))
    if not s1:
        notes.append(Note("search", WARN, "No Sentinel-1 pairs selected. Ready-made ARIA interferograms cover "
                                          "selected regions, OPERA DISP-S1 covers North America and LiCSAR many "
                                          "tectonic and volcanic regions; elsewhere, order them free from ASF HyP3 "
                                          "(button below the results, or sarcompare hyp3)." + alone))
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
