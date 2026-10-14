# Copyright (c) 2026 Deepa Kumari. All rights reserved. See LICENSE.
"""Print a compact text diagnosis of a finished run (for CI logs, where the maps cannot be looked at).

    python scripts/diagnose_run.py outputs/central_valley

Shows the GNSS stations used (absolute GNSS rate vs the InSAR rate at the station), the spread of each rate map,
and a coarse grid of block medians so the spatial pattern (e.g. a subsidence bowl) can be read as numbers.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import rioxarray
from pyproj import Transformer


def _f(v, nd=0):
    return "  n/a" if v is None or (isinstance(v, float) and not math.isfinite(v)) else f"{v:+.{nd}f}"


def block_grid(path: Path, n: int = 6) -> None:
    da = rioxarray.open_rasterio(path, masked=True).squeeze("band", drop=True)
    v = da.values * 1000  # m/yr -> mm/yr
    ok = np.isfinite(v)
    print(f"\n{path.name}: {ok.mean():.0%} valid; percentiles (mm/yr) "
          + "  ".join(f"p{q}={np.nanpercentile(v, q):+.0f}" for q in (2, 10, 50, 90, 98)) if ok.any()
          else f"\n{path.name}: no valid pixels")
    if not ok.any():
        return
    to_ll = Transformer.from_crs(da.rio.crs, "EPSG:4326", always_xy=True)
    rows = np.array_split(np.arange(v.shape[0]), n)
    cols = np.array_split(np.arange(v.shape[1]), n)
    lons = [to_ll.transform(float(da.x.values[c].mean()), float(da.y.values.mean()))[0] for c in cols]
    print("  block medians, north at top; columns at lon " + " ".join(f"{x:8.3f}" for x in lons))
    for r in rows:
        lat = to_ll.transform(float(da.x.values.mean()), float(da.y.values[r].mean()))[1]
        cells = []
        for c in cols:
            b = v[np.ix_(r, c)]
            cells.append(f"{np.nanmedian(b):+8.0f}" if np.isfinite(b).mean() > 0.2 else "       .")
        print(f"  lat {lat:7.3f} " + " ".join(cells))


def nearby_gnss(m: dict, holdings: Path, buffer_deg: float = 0.4) -> None:
    """NGL stations in and around the area, to judge whether a larger area would give a better GNSS check."""
    aoi = m.get("config", {}).get("aoi")
    if not holdings.exists() or not isinstance(aoi, list) or len(aoi) != 4:
        return
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from sarcompare.validate import read_ngl_site_list

    w, s, e, n = aoi
    starts = [(m.get(k) or {}).get("date_start") for k in ("nisar", "sentinel1")]
    t0 = min((x for x in starts if x), default="")
    rows = [x for x in read_ngl_site_list(holdings)
            if w - buffer_deg <= x["lon"] <= e + buffer_deg and s - buffer_deg <= x["lat"] <= n + buffer_deg]
    print(f"\nNGL stations within {buffer_deg} deg of the area ({len(rows)}); * = inside, + = has data after {t0}:")
    for x in sorted(rows, key=lambda x: (x["lat"], x["lon"])):
        inside = w <= x["lon"] <= e and s <= x["lat"] <= n
        recent = str(x["end"]) >= t0
        print(f"  {x['site']:<5} {x['lon']:9.4f} {x['lat']:8.4f}  {x['start']} -> {x['end']}  "
              f"{'*' if inside else ' '}{'+' if recent else ' '}")


def main(run_dir: str) -> int:
    run = Path(run_dir)
    mpath = run / "metrics.json"
    if not mpath.exists():
        print(f"No metrics.json in {run}; the run did not finish.")
        return 0
    m = json.loads(mpath.read_text())
    ref = m.get("reference", {})
    print(f"Reference mode: {ref.get('mode')}")
    for n in m.get("notes", []):
        if n["stage"] in ("reference",):
            print(f"  [{n['level']}] {n['text']}")
    for key in ("nisar", "sentinel1"):
        s = m.get(key)
        if not s:
            print(f"{key}: not available for this area and period")
            continue
        print(f"{key}: {s.get('n_pairs')} pairs {s.get('date_start')} -> {s.get('date_end')}, "
              f"noise {_f(s.get('noise_rate_mm_yr'))} mm/yr, incidence {s.get('incidence_deg', 0):.1f} deg")
    print("\nPairs:")
    for p in m.get("pairs", []):
        print("  " + "  ".join(str(p.get(k, "")) for k in ("sensor", "date1", "date2", "direction", "track", "name")))
    print("\nPer-pair quality (line of sight, mm): tilt across the area east-west / north-south, spread after the tilt")
    for key in ("nisar", "sentinel1"):
        for q in (m.get(key) or {}).get("pairs_qc", []):
            print(f"  {key:<9} {q['date1']} -> {q['date2']}  valid {q['valid']:4.0%}  tilt E-W {_f(q['tilt_ew_mm']):>5}"
                  f"  N-S {_f(q['tilt_ns_mm']):>5}  spread {q['spread_mm']:5.1f}"
                  + (f"   (ionosphere removed: tilt E-W {_f(q['iono_tilt_ew_mm']):>5} N-S {_f(q['iono_tilt_ns_mm']):>5})"
                     if "iono_tilt_ew_mm" in q else ""))
    g = (m.get("validation") or {}).get("gnss")
    if g:
        print(f"\nGNSS ({g['source']}): absolute vertical rate vs InSAR at the station (mm/yr)")
        print("  site      lon       lat   long-term  NISAR (GNSS same dates)   S1 (GNSS same dates)   period / note")
        for st in g["stations"]:
            ins, win = st.get("insar", {}), st.get("window", {})

            def pair(sn):
                w = win.get(sn)
                gw = f"{_f(w['up_mm_yr'])}±{w['up_sigma_mm_yr']:.0f}" if w else "  n/a"
                return f"{_f(ins.get(sn)):>6} ({gw:>9})"
            print(f"  {st['site']:<6} {st['lon']:9.4f} {st['lat']:8.4f} {_f(st['up_mm_yr']):>6}±{st['up_sigma_mm_yr']:<4.1f}"
                  f"  {pair('NISAR')}        {pair('Sentinel-1')}   {st.get('period', '')} {st.get('note', '')}")
        for k, s in g["stats"].items():
            print(f"  {k}: n={s['n']} offset={_f(s['offset_mm_yr'], 1)} rmse={_f(s['rmse_mm_yr'], 1)}")
        for sk in g.get("skipped", []):
            print(f"  skipped: {sk}")
    nearby_gnss(m, Path(m.get("config", {}).get("gnss_dir") or "data/gnss") / "DataHoldings.txt")
    for tif in sorted(run.glob("*_rate_m_per_yr.tif")) + sorted(run.glob("nisar_minus_s1_m_per_yr.tif")):
        block_grid(tif)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "outputs/central_valley"))
