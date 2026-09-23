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


def comparison_notes(c: Comparison, a: RateMap, b: RateMap) -> list[Note]:
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
                add(INFO, f"The amplitude differs (slope {_fmt(c.slope, 2)}: {B} ≈ {_fmt(c.slope, 2)} × {A}). "
                          "Causes: horizontal motion (the vertical projection assumes none, and the two satellites "
                          "look from different angles), non-steady motion over non-matching time windows, or "
                          "incidence-angle assumptions.")

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
    if not c.hotspots:
        add(INFO, "No coherent area moves faster than the detection threshold. The AOI appears stable at the "
                  "precision these pairs allow.")
    for h in c.hotspots:
        kind = "subsiding / moving away" if h.peak_mm_yr < 0 else "uplifting / moving toward the satellite"
        other = B if h.sensor == A else A
        base = (f"{h.sensor} detects an area {kind}: {h.area_km2:.1f} km² centred at {h.lat:.4f}°N, "
                f"{h.lon:.4f}°E, peak {h.peak_mm_yr:+.0f} mm/yr.")
        if h.seen_by_other == "yes":
            add(GOOD, base + f" {other} sees it too, so it is confirmed by two independent sensors.")
        elif h.seen_by_other == "no data":
            add(INFO, base + f" {other} has no coherent data there, so this is new information only "
                             f"{h.sensor} can provide. Validate with field observation, GNSS, or optical imagery "
                             "(e.g. scarps or cracks for a landslide).")
        else:
            add(WARN, base + f" {other} does not see it. Treat it with caution: it could be an atmospheric or "
                             "unwrapping artefact, or motion that happened only in one sensor's time window.")
    if any(h.peak_mm_yr < 0 and h.area_km2 > 5 for h in c.hotspots):
        add(INFO, "A broad, bowl-shaped subsidence area in sediment-filled basins most often means groundwater "
                  "extraction and aquifer-system compaction. Compare with well-level records and land use.")

    # --- Caveats
    add(INFO, "Caveats: rates are relative to the reference point. The vertical projection assumes no "
              "horizontal motion. Combining ascending and descending tracks separates vertical from east–west "
              "motion. Nonlinear (e.g. seasonal) motion is averaged out by rate stacking.")
    return notes
