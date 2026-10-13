"""Rule-based interpretation: turn numbers into plain-language findings with the physics behind them.

Every rule here is deliberately simple and explainable. The notes are a starting point for your
own interpretation, not a substitute for it: always check them against local geology and ground truth.
"""

from __future__ import annotations

import math

import numpy as np

from .compare import Comparison
from .notes import GOOD, INFO, WARN, Note
from .processing import RateMap
from .readers.base import Interferogram


def _fmt(v: float, nd: int = 1) -> str:
    return "n/a" if v is None or not math.isfinite(v) else f"{v:.{nd}f}"


def pair_notes(ifg: Interferogram, threshold: float) -> list[Note]:
    """Quick look at a single pair as it is loaded."""
    coh = ifg.coherence.values
    good = np.nanmean(coh >= threshold) if np.isfinite(coh).any() else 0.0
    valid = np.isfinite(ifg.los.values).mean()
    lvl = GOOD if good > 0.6 else INFO if good > 0.3 else WARN
    text = (f"{ifg.sensor} {ifg.date1}→{ifg.date2} ({ifg.span_days} d): {good:.0%} of pixels above coherence "
            f"{threshold}, {valid:.0%} unwrapped; median coherence {_fmt(float(np.nanmedian(coh)), 2)}.")
    if good < 0.3:
        text += " Mostly decorrelated; vegetation, snow, soil moisture or a long time gap can cause this."
    return [Note("load", lvl, text)]


def stack_notes(rm: RateMap) -> list[Note]:
    notes = []
    fringe_mm = rm.wavelength / 2 * 1000
    notes.append(Note("stack", INFO,
                      f"{rm.sensor}: {rm.n_pairs} pairs covering {rm.total_days} days ({rm.date_start}→{rm.date_end}) "
                      f"stacked into a mean {rm.component} rate. Wavelength {rm.wavelength * 100:.1f} cm, so one "
                      f"colour cycle (fringe) = {fringe_mm:.0f} mm of line-of-sight motion; mean incidence "
                      f"{rm.incidence_deg:.1f}°."))
    if math.isfinite(rm.noise_rate):
        n_mm = rm.noise_rate * 1000
        lvl = GOOD if n_mm < 10 else INFO if n_mm < 30 else WARN
        notes.append(Note("stack", lvl,
                          f"{rm.sensor}: estimated rate noise ≈ {n_mm:.0f} mm/yr (1σ, from pair-to-pair scatter). "
                          f"Signals smaller than ~{2 * n_mm:.0f} mm/yr are not reliably detectable. Noise falls "
                          "as the total time span grows; add more or longer pairs to push it down."))
    return notes


def _combined(rm: RateMap) -> bool:
    """True when most of the map's vertical rate comes from ascending + descending passes (sideways motion separated)."""
    return getattr(rm, "two_direction_fraction", 0.0) > 0.5


def motion_words(peak_mm_yr: float, mode: str = "area") -> str:
    """How to describe a moving area, depending on what zero means (see Reference.mode)."""
    if mode == "gnss":
        return "subsiding / moving away" if peak_mm_yr < 0 else "uplifting / moving toward the satellite"
    if mode == "point":
        return ("moving down relative to your reference point" if peak_mm_yr < 0
                else "moving up relative to your reference point")
    return ("moving down faster than the area as a whole" if peak_mm_yr < 0
            else "moving up relative to the area as a whole (it may still be sinking, only more slowly)")


def _in_europe(lon: float, lat: float) -> bool:
    return -25 <= lon <= 45 and 34 <= lat <= 72  # roughly the area covered by EGMS


def missing_sensor_notes(present: str, missing: str, center: tuple[float, float] | None = None,
                         hyp3_counts: dict | None = None) -> list[Note]:
    """Why one satellite is missing from the run, what is lost, and how to add it.

    ``hyp3_counts``: status of the HyP3 jobs already ordered for the area (status -> number), if checked."""
    notes: list[Note] = []
    add = lambda lvl, text: notes.append(Note("compare", lvl, text))  # noqa: E731
    add(WARN, f"No usable {missing} data were found for this area and period, so ground motion is measured with "
              f"{present} alone. The rate map, the moving areas and the checks against GNSS and published studies "
              f"all still work. What is lost is the second, independent satellite: a moving area can no longer be "
              "confirmed by both, so check it against GNSS, published studies or field evidence.")
    busy = sum((hyp3_counts or {}).get(k, 0) for k in ("PENDING", "RUNNING"))
    if missing == "Sentinel-1" and busy:
        add(INFO, f"{busy} Sentinel-1 interferogram(s) ordered from ASF HyP3 for this area are still processing. "
                  "Run this area again when they are done (usually within an hour or two): they are then used "
                  "automatically.")
    elif missing == "Sentinel-1":
        add(INFO, "Why: Sentinel-1 images almost all land every 12 days, but ready-made interferograms are only "
                  "published for some places (ARIA for selected regions, OPERA DISP-S1 for North America). For "
                  "anywhere else, ASF's free HyP3 service makes them on request from the raw images. Use "
                  "\"Order Sentinel-1 from ASF HyP3\" below the results (or sarcompare hyp3 config.yaml --order): "
                  "the app picks Sentinel-1 pairs on the NISAR dates and orders them with your Earthdata login. "
                  "Processing usually takes under an hour; then run the same area again and they are used "
                  "automatically (HyP3 keeps them for 14 days).")
        if center is not None and _in_europe(*center):
            add(INFO, "This area is in Europe, where the European Ground Motion Service (EGMS) maps Sentinel-1 "
                      "ground motion everywhere. Download the EGMS vertical tile for this area (see Checks) and "
                      "add it as a published map: it serves as the independent Sentinel-1 check.")
    else:
        add(INFO, "Why: NISAR science products have been published since late 2025 and coverage is still being "
                  "filled in. Try a later period, or check ASF Vertex (search.asf.data.nasa.gov) for NISAR GUNW "
                  "products over this area.")
    return notes


def comparison_notes(c: Comparison, a: RateMap, b: RateMap | None, mode: str = "area") -> list[Note]:
    """``mode`` is the reference mode: "area" (zero = area median), "point" or "gnss" (absolute).

    ``b`` is None when only one satellite has data: then only ``a``'s own findings are described."""
    if b is None:
        return _single_notes(c, a, mode)
    A, B = a.sensor, b.sensor
    notes: list[Note] = []
    add = lambda lvl, text: notes.append(Note("compare", lvl, text))  # noqa: E731

    # --- Coverage & coherence: the wavelength story
    add(INFO, f"Usable coverage of the AOI: {A} {c.coverage_a:.0%}, {B} {c.coverage_b:.0%}, both "
              f"{c.coverage_both:.0%}. Mean coherence {A} {_fmt(c.coh_mean_a, 2)} vs {B} {_fmt(c.coh_mean_b, 2)}.")
    if c.only_a - c.only_b > 0.1:
        add(GOOD, f"{A} measures {c.only_a:.0%} of the AOI where {B} cannot. This is the classic L-band advantage: "
                  "the 24 cm wave penetrates vegetation and scatters from stable trunks, branches and ground, "
                  "while 5.6 cm C-band scatters from leaves that move between passes. Check whether those "
                  "areas are forest, crops or seasonally wet ground.")
    elif c.only_b - c.only_a > 0.1:
        add(INFO, f"{B} covers {c.only_b:.0%} of the AOI that {A} does not. Unusual for vegetated terrain; likely "
                  f"causes are fewer or shorter-span {A} pairs, {A} unwrapping gaps, or ionospheric disturbance.")
    if c.coverage_both < 0.1:
        add(WARN, "Less than 10% of the AOI is measured by both sensors, so the statistics below rest on few "
                  "pixels. Lower coherence_threshold or choose pairs from a drier / leaf-off season.")

    # --- Agreement
    if c.sign_suspect:
        add(WARN, f"The rates are anti-correlated (r = {c.pearson_r:.2f}). This almost always means a sign-convention "
                  "mismatch between products, not real opposite motion. Check against a feature you know (e.g. a "
                  "subsiding city should be negative) and set nisar_sign or s1_sign to flip one product.")
    elif math.isfinite(c.pearson_r):
        if c.pearson_r > 0.7:
            add(GOOD, f"Strong agreement where both measure: r = {c.pearson_r:.2f}, slope {_fmt(c.slope, 2)}, "
                      f"RMS difference {_fmt(c.rmsd_mm_yr)} mm/yr over {c.n_joint:,} pixels. Two independent "
                      "satellites at different wavelengths seeing the same pattern is strong evidence it is real "
                      "ground motion, not atmosphere or processing artefacts.")
        elif c.pearson_r > 0.4:
            add(INFO, f"Moderate agreement: r = {c.pearson_r:.2f}, RMS difference {_fmt(c.rmsd_mm_yr)} mm/yr. The "
                      "large-scale pattern is shared but noise (mainly tropospheric water vapour, different on "
                      "each acquisition date) is comparable to the signal.")
        else:
            add(WARN, f"Weak agreement: r = {c.pearson_r:.2f}. Either there is little real motion (so both maps "
                      "are mostly noise), the two periods differ in behaviour, or one dataset has artefacts. "
                      f"Compare with the noise estimates ({A} ≈ {_fmt(c.noise_a_mm_yr, 0)}, {B} ≈ "
                      f"{_fmt(c.noise_b_mm_yr, 0)} mm/yr).")
        if math.isfinite(c.slope) and c.pearson_r > 0.4:
            if c.slope < 0.75 or c.slope > 1.33:
                horiz = ("" if _combined(a) and _combined(b) else
                         "horizontal motion (a single viewing direction cannot tell it from vertical motion, and the "
                         "two satellites look from different angles), ")
                add(INFO, f"The amplitude differs (slope {_fmt(c.slope, 2)}: {B} ≈ {_fmt(c.slope, 2)} × {A}). "
                          f"Causes: {horiz}non-steady motion over non-matching time windows, smooth atmospheric "
                          "tilts, or incidence-angle assumptions.")

    if math.isfinite(c.ramp_mm_yr):
        lvl = WARN if c.ramp_mm_yr > max(10, 2 * c.rmsd_detrended_mm_yr) else INFO
        add(lvl, f"The difference map contains a planar ramp of {c.ramp_mm_yr:.0f} mm/yr across the AOI; after "
                 f"removing it the RMS difference is {_fmt(c.rmsd_detrended_mm_yr)} mm/yr. Long-wavelength "
                 "differences typically come from residual ionosphere (strong at L-band), orbit errors, or "
                 "large-scale tropospheric gradients, not from local ground motion.")
    if math.isfinite(c.bias_mm_yr) and abs(c.bias_mm_yr) > 5:
        add(INFO, f"Mean offset {A} − {B} = {c.bias_mm_yr:+.1f} mm/yr. Offsets depend on the reference point; "
                  "only differences in spatial pattern are meaningful.")

    # --- Hotspots
    _no_hotspots(c, add)
    for h in c.hotspots:
        kind = motion_words(h.peak_mm_yr, mode)
        other = B if h.sensor == A else A
        base = (f"{h.sensor} detects an area {kind}: {h.area_km2:.1f} km² centred at {h.lat:.4f}°N, "
                f"{h.lon:.4f}°E, on average {h.mean_mm_yr:+.0f} mm/yr and up to {h.peak_mm_yr:+.0f} mm/yr (the fastest 5% of it).")
        if h.seen_by_other == "yes":
            add(GOOD, base + f" {other} sees it too, so it is confirmed by two independent sensors.")
        elif h.seen_by_other == "no data":
            add(INFO, base + f" {other} has no coherent data there, so this is new information only "
                             f"{h.sensor} can provide. Validate with field observation, GNSS, or optical imagery "
                             "(e.g. scarps or cracks for a landslide).")
        else:
            add(WARN, base + f" {other} does not see it. Treat it with caution: it could be an atmospheric or "
                             "unwrapping artefact, or motion that happened only in one sensor's time window.")
    _bowl(c, add)

    # --- Caveats
    zero = _zero_words(mode)
    both = [rm.sensor for rm in (a, b) if _combined(rm)]
    single = [rm.sensor for rm in (a, b) if not _combined(rm)]
    if not single:
        geo = ("Both vertical maps come from ascending + descending passes, so sideways (east-west) motion is "
               "separated; north-south motion is not measured.")
    elif both:
        geo = (f"{both[0]}'s vertical map comes from ascending + descending passes (sideways motion separated); "
               f"{single[0]}'s assumes the ground moves only up and down.")
    else:
        geo = ("The vertical maps assume the ground moves only up and down; with ascending and descending passes "
               "the app separates vertical from east-west motion.")
    add(INFO, f"Caveats: {zero}. {geo} Nonlinear (e.g. seasonal) motion is averaged out by rate stacking.")
    return notes


def _no_hotspots(c: Comparison, add) -> None:
    if not c.hotspots:
        add(INFO, "No coherent area moves faster than the detection threshold. The AOI appears stable at the "
                  "precision these pairs allow.")


def _bowl(c: Comparison, add) -> None:
    if any(h.peak_mm_yr < 0 and h.area_km2 > 5 for h in c.hotspots):
        add(INFO, "A broad, bowl-shaped subsidence area in sediment-filled basins most often means groundwater "
                  "extraction and aquifer-system compaction. Compare with well-level records and land use.")


def _zero_words(mode: str) -> str:
    return {"gnss": "rates are tied to GNSS, so their accuracy depends on the stations used",
            "point": "rates are relative to your reference point"}.get(mode, "rates are relative to the area median")


def _single_notes(c: Comparison, a: RateMap, mode: str) -> list[Note]:
    """Findings when only one satellite has data: coverage, moving areas and caveats, without cross-checks."""
    A = a.sensor
    notes: list[Note] = []
    add = lambda lvl, text: notes.append(Note("compare", lvl, text))  # noqa: E731
    add(INFO, f"Usable coverage of the AOI: {A} {c.coverage_a:.0%}, mean coherence {_fmt(c.coh_mean_a, 2)}. "
              f"Rate noise ≈ {_fmt(c.noise_a_mm_yr, 0)} mm/yr (1σ). Only one satellite has data here, so there is "
              "no satellite-to-satellite agreement to report.")
    if c.coverage_a < 0.3:
        add(WARN, f"{A} measures less than a third of the area. Lower coherence_threshold, or use pairs with "
                  "shorter time gaps or from a drier / leaf-off season.")
    _no_hotspots(c, add)
    for h in c.hotspots:
        add(INFO, f"{A} detects an area {motion_words(h.peak_mm_yr, mode)}: {h.area_km2:.1f} km² centred at "
                  f"{h.lat:.4f}°N, {h.lon:.4f}°E, on average {h.mean_mm_yr:+.0f} mm/yr and up to "
                  f"{h.peak_mm_yr:+.0f} mm/yr (the fastest 5% of it). Seen by one satellite only: confirm it with "
                  "GNSS, published studies or field evidence (cracks, damaged well casings, tilted buildings) "
                  "before relying on it.")
    _bowl(c, add)
    geo = ("The vertical map comes from ascending + descending passes, so sideways (east-west) motion is "
           "separated; north-south motion is not measured." if _combined(a) else
           "The vertical map assumes the ground moves only up and down; with ascending and descending passes "
           "the app separates vertical from east-west motion.")
    add(INFO, f"Caveats: {_zero_words(mode)}. {geo} Nonlinear (e.g. seasonal) motion is averaged out by rate "
              "stacking.")
    return notes


def gnss_notes(gv, rates: list[RateMap], tied: bool = False) -> list[Note]:
    """Interpret the InSAR-vs-GNSS comparison. ``tied``: the maps were already shifted to match these stations."""
    notes: list[Note] = []
    add = lambda lvl, text: notes.append(Note("validate", lvl, text))  # noqa: E731
    if not gv.stations:
        add(WARN, f"No GNSS stations with usable data were found inside the AOI ({gv.source}). Enlarge the AOI "
                  "or supply a CSV of published station velocities.")
        return notes
    how = ("The maps were tied to these stations (a shift, plus a tilt where enough stations allow), so the errors "
           "below measure how well the local pattern matches." if tied else
           "One constant offset per sensor is removed because InSAR rates are relative to the reference point.")
    add(INFO, f"{len(gv.stations)} GNSS station(s) from {gv.source}. GNSS measures absolute 3-D motion at a point, "
              f"independently of radar, so it is the standard benchmark for InSAR. {how}")
    for rm in rates:
        s = gv.stats.get(rm.sensor)
        if s is None or s.n == 0:
            add(WARN, f"{rm.sensor}: no GNSS station falls on a pixel with valid {rm.sensor} data.")
            continue
        if s.n < 2:
            st = next(st for st in gv.stations if math.isfinite(st.insar.get(rm.sensor, float("nan"))))
            what = ("the map was tied to it, so it cannot also test it" if tied
                    else f"only the offset ({s.offset_mm_yr:+.1f} mm/yr) can be estimated")
            from .validate import station_rate_full

            g, _, period = station_rate_full(st, rm.sensor)
            add(INFO, f"{rm.sensor}: only 1 station with data ({st.site}, GNSS vertical {g:+.1f} mm/yr over "
                      f"{period}); {what}. At least 2 are needed to measure errors.")
            continue
        noise = rm.noise_rate * 1000 if math.isfinite(rm.noise_rate) else float("nan")
        expected = math.hypot(noise, 2.0) if math.isfinite(noise) else float("nan")
        r_txt = f", r = {s.r:.2f}" if math.isfinite(s.r) else ""
        corr = getattr(gv, "corrections", {}).get(rm.sensor)
        how_err = ("leave-one-out: each station predicted from a GNSS plane fitted without it"
                   if corr is not None and corr.kind == "plane" else f"offset removed {s.offset_mm_yr:+.1f} mm/yr")
        base = f"{rm.sensor} vs GNSS vertical: RMSE {s.rmse_mm_yr:.1f} mm/yr at {s.n} stations{r_txt} ({how_err})."
        if math.isfinite(expected) and s.rmse_mm_yr <= 1.5 * expected:
            add(GOOD, base + f" This is within what the InSAR noise (~{noise:.0f} mm/yr) predicts, so {rm.sensor} "
                             "agrees with GNSS.")
        else:
            horiz = "" if _combined(rm) else "horizontal motion (a single viewing direction cannot separate it), "
            add(WARN, base + f" Larger than the InSAR noise predicts. Usual causes: {horiz}GNSS and InSAR periods "
                             "that differ, very local motion of the GNSS monument, or residual atmosphere or "
                             "ionosphere.")
    offsets = {k: v.offset_mm_yr for k, v in gv.stats.items() if v.n >= 2 and math.isfinite(v.offset_mm_yr)}
    if offsets and not tied and all(abs(o) > 8 for o in offsets.values()):
        mean_off = float(np.mean(list(offsets.values())))
        add(WARN, f"Both sensors need an offset of about {mean_off:+.0f} mm/yr to match GNSS. That offset is the "
                  f"motion of the reference point itself: it is probably {'subsiding' if mean_off > 0 else 'rising'}"
                  f" by ~{abs(mean_off):.0f} mm/yr. Relative rates are still valid, but for absolute values set "
                  "reference_lonlat to a stable GNSS station or bedrock.")
    worst = max((st for st in gv.stations if st.residual), key=lambda st: max(abs(v) for v in st.residual.values()),
                default=None)
    if worst is not None and max(abs(v) for v in worst.residual.values()) > 10:
        add(INFO, f"Largest misfit at station {worst.site} "
                  + ", ".join(f"{k} {v:+.0f} mm/yr" for k, v in worst.residual.items())
                  + ". Look at that station's time series and surroundings before trusting either value there.")
    if any(st.note.startswith("too little") and not st.window for st in gv.stations):
        add(INFO, "Some stations had too little data in the InSAR period, so their long-term rates were used. "
                  "Seasonal or accelerating motion can make those differ from the InSAR rates.")
    for msg in gv.skipped[:5]:
        add(INFO, f"GNSS skipped: {msg}")
    return notes


def published_notes(pv, rates: list[RateMap]) -> list[Note]:
    notes: list[Note] = []
    add = lambda lvl, text: notes.append(Note("validate", lvl, text))  # noqa: E731
    period = f" (period {pv.period})" if pv.period else ""
    what = " East-west motion, compared with this run's east-west maps." if getattr(pv, "component", "") == "east-west" else ""
    add(INFO, f"Published map '{pv.label}'{period}: {pv.reference_mode}.{what}")
    for rm in rates:
        s = pv.stats[rm.sensor]
        if s.n < 3:
            add(WARN, f"{rm.sensor}: too little overlap with '{pv.label}' to compare ({s.n} pixels).")
            continue
        if not math.isfinite(s.r):  # one map has no variation (e.g. stable ground): only the size of the difference
            noise = rm.noise_rate * 1000 if math.isfinite(rm.noise_rate) else 10.0
            lvl = GOOD if s.rmsd_mm_yr <= 2 * noise else WARN
            add(lvl, f"{rm.sensor} vs '{pv.label}': RMS difference {s.rmsd_mm_yr:.1f} mm/yr over {s.n:,} pixels "
                     f"(mean difference {s.bias_mm_yr:+.1f}); one of the maps is uniform, so no correlation is "
                     f"computed. {'Within' if lvl == GOOD else 'More than'} twice the noise (~{noise:.0f} mm/yr).")
            continue
        base = (f"{rm.sensor} vs '{pv.label}': r = {s.r:.2f}, slope {_fmt(s.slope, 2)}, RMS difference "
                f"{s.rmsd_mm_yr:.1f} mm/yr over {s.n:,} pixels ({s.coverage:.0%} of {rm.sensor}'s valid area).")
        if s.r > 0.7 and 0.75 < s.slope < 1.33:
            add(GOOD, base + " The pattern and amplitude match the published result.")
        elif s.r > 0.4:
            add(INFO, base + " The pattern matches but differences remain. Check whether the periods, the "
                             "reference point or the LOS/vertical conversion differ.")
        else:
            add(WARN, base + " Poor match. Check the units, the sign convention (positive = up?) and the component "
                             "(LOS or vertical) of the published map before blaming either dataset.")
    return notes


def plain_summary(result) -> list[tuple[str, str]]:
    """A few short sentences for non-specialists, as (level, text), most important first."""
    c = result.comparison
    maps = [rm for rm in (result.nisar_rate, result.s1_rate) if rm is not None]
    a = maps[0]
    out: list[tuple[str, str]] = []
    moving = []  # one entry per place: the same area seen by both satellites counts once
    for h in sorted(c.hotspots, key=lambda h: -abs(h.peak_mm_yr)):
        near = [m for m in moving if math.hypot((m.lon - h.lon) * 111 * math.cos(math.radians(h.lat)),
                                                (m.lat - h.lat) * 111) < 2.0]
        if not near:
            moving.append(h)
    if moving:
        h = moving[0]
        mode = result.reference.mode if getattr(result, "reference", None) is not None else "area"
        if mode == "gnss":
            verb = "sinking" if h.peak_mm_yr < 0 else "rising"
        else:
            whom = "your reference point" if mode == "point" else "the area as a whole"
            verb = f"{'sinking' if h.peak_mm_yr < 0 else 'rising'} relative to {whom}"
        who = {"yes": "Both satellites see it.",
               "no data": f"Only {h.sensor} can measure there (the other satellite loses the signal).",
               "no": f"Only {h.sensor} sees it, so treat it with caution.",
               "single": f"Measured by {h.sensor} only; check it against GNSS or published studies."}[h.seen_by_other]
        out.append((GOOD if h.seen_by_other == "yes" else INFO,
                    f"The ground is {verb} by up to {abs(h.peak_mm_yr) / 10:.1f} cm per year over about "
                    f"{h.area_km2:.0f} km² near {h.lat:.3f}°N, {h.lon:.3f}°E. {who}"))
        if mode == "area":
            out.append((INFO, "Zero on the map is the typical motion of the whole area, not stable ground. If the "
                              "whole area is sinking (as in many farming basins), use GNSS to see absolute rates."))
        if len(moving) > 1:
            out.append((INFO, f"{len(moving) - 1} more moving area(s) found; see the map and the table."))
    else:
        out.append((INFO, "No area is moving faster than these data can reliably detect."))
    if len(maps) == 1:
        other = "Sentinel-1" if a.sensor == "NISAR" else "NISAR"
        out.append((INFO, f"{other} has no usable data for this area and period, so only {a.sensor} was used. "
                          "The Agreement tab below says how to add it."))
    if math.isfinite(c.pearson_r):
        word = "agree well" if c.pearson_r > 0.7 else "partly agree" if c.pearson_r > 0.4 else "do not agree well"
        out.append((GOOD if c.pearson_r > 0.7 else INFO if c.pearson_r > 0.4 else WARN,
                    f"Where both satellites have data, their measurements {word} "
                    f"(typical difference {c.rmsd_mm_yr:.0f} mm per year)."))
    if c.only_a > 0.05:
        out.append((GOOD, f"NISAR measures {c.only_a:.0%} of the area that Sentinel-1 cannot, usually under "
                          "vegetation. That is new information."))
    g = getattr(result, "gnss", None)
    if g is not None and a.sensor in g.stats and g.stats[a.sensor].n >= 2:
        s = g.stats[a.sensor]
        out.append((GOOD if s.rmse_mm_yr < 15 else WARN,
                    f"Checked against {s.n} GNSS ground stations: {a.sensor} is within about {s.rmse_mm_yr:.0f} mm "
                    "per year."))
    lit = getattr(result, "literature", None) or []
    tested = [(c, s) for c in lit for s in (rm.sensor for rm in maps) if c.verdict.get(s) in ("consistent", "differs")]
    if tested:
        agree = sum(1 for c, s in tested if c.verdict[s] == "consistent")
        out.append((GOOD if agree / len(tested) >= 0.75 else WARN,
                    f"Compared with published studies: {agree} of {len(tested)} comparisons agree."))
    noise = [rm.noise_rate for rm in maps if math.isfinite(rm.noise_rate)]
    if noise:
        out.append((INFO, f"Movements smaller than about {2 * max(noise) * 1000:.0f} mm per "
                          "year cannot be told apart from noise with this amount of data."))
    if c.sign_suspect:
        out.insert(0, (WARN, "The two satellites show opposite motion, which usually means a data-format setting "
                             "is wrong. See the Details tab."))
    return out


def literature_notes(checks, rates: list[RateMap]) -> list[Note]:
    """One note per published value, plus an overall tally."""
    notes: list[Note] = []
    add = lambda lvl, text: notes.append(Note("validate", lvl, text))  # noqa: E731
    if not checks:
        return notes
    sensors = [rm.sensor for rm in rates]
    tested = [(c, s) for c in checks for s in sensors if c.verdict.get(s) in ("consistent", "differs")]
    agree = sum(1 for c, s in tested if c.verdict[s] == "consistent")
    if tested:
        lvl = GOOD if agree / len(tested) >= 0.75 else INFO if agree / len(tested) >= 0.5 else WARN
        add(lvl, f"Published studies: {agree} of {len(tested)} comparisons agree "
                 f"({len(checks)} published value(s) × {len(sensors)} satellites, where data exist).")
    for c in checks:
        pub = (f"{c.published_min:+.0f} to {c.published_max:+.0f}" if c.published_min is not None
               and c.published_max is not None else f"{c.published_mm_yr:+.0f}") + " mm/yr"
        where = f" at {c.place}" if c.place else f" at {c.lat:.4f}°N, {c.lon:.4f}°E"
        if getattr(c, "stat", "median") == "peak":
            pub = "a peak of " + pub
            where += f" (compared with the fastest-moving 2% of this run's pixels within {c.radius_km:g} km)"
        when = f" for {c.period}" if c.period else ""
        ours = ", ".join(f"{s} {c.insar[s]:+.0f}" for s in sensors if math.isfinite(c.insar.get(s, float('nan'))))
        verdicts = {c.verdict.get(s) for s in sensors}
        text = f"{c.citation} report {pub}{where}{when}; this run gives {ours or 'no data there'} mm/yr."
        if verdicts == {"consistent"} or ("consistent" in verdicts and "differs" not in verdicts):
            add(GOOD, text + " Consistent.")
        elif "differs" in verdicts:
            add(WARN, text + " Different. Check first whether the periods match: subsidence rates change "
                             "with droughts, pumping and recharge. Then check that both are vertical rather than "
                             "line-of-sight, and that the reference (stable point) is comparable.")
        else:
            add(INFO, text + " Not compared: " + ", ".join(sorted(v for v in verdicts if v)) + ".")
    if not any(any(c.absolute.values()) for c in checks):
        add(INFO, "These rates are relative to the area median. Most published rates are absolute; turn on the "
                  "GNSS check so the app can tie its rates to GNSS and compare like with like.")
    return notes
