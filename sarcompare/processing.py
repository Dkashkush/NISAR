"""Put both sensors on one grid, stack pairs into mean rates, and reference them to a common point."""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import xarray as xr
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from scipy import ndimage
from shapely.ops import transform

from .aoi import AOI
from .notes import GOOD, INFO, WARN, Note
from .readers.base import Interferogram


@dataclass
class RateMap:
    """Mean displacement rate for one sensor on the common grid (metres/year)."""

    sensor: str
    rate: xr.DataArray  # LOS or vertical rate, m/yr, positive = up / toward the satellite
    coherence: xr.DataArray  # mean coherence over the pairs
    n_valid: xr.DataArray  # number of pairs contributing at each pixel
    n_pairs: int
    total_days: int
    date_start: date
    date_end: date
    incidence_deg: float
    wavelength: float
    component: str = "LOS"
    noise_rate: float = float("nan")  # estimated 1-sigma rate noise (m/yr) from pair-to-pair scatter
    notes: list[Note] = field(default_factory=list)
    pair_qc: list[dict] = field(default_factory=list)  # per pair: tilt and spread (see pair_tilt)
    direction: str | None = None  # ASCENDING / DESCENDING for a single viewing geometry, None if combined
    geometry: str = ""  # e.g. "ascending track 34", or "ascending track 34 + descending track 41"
    two_direction_fraction: float = 0.0  # share of valid pixels where both directions were combined


def common_grid(aoi: AOI, resolution: float) -> xr.DataArray:
    """Empty template grid covering the AOI in its UTM zone."""
    crs = aoi.utm_crs()
    w, s, e, n = aoi.bounds_in(crs)
    x = np.arange(w + resolution / 2, e, resolution)
    y = np.arange(n - resolution / 2, s, -resolution)
    tmpl = xr.DataArray(np.full((y.size, x.size), np.nan, dtype="float32"), dims=("y", "x"),
                        coords={"y": y, "x": x})
    return tmpl.rio.write_crs(crs).rio.write_nodata(np.nan)


def aoi_mask(aoi: AOI, template: xr.DataArray) -> np.ndarray:
    """True for grid cells inside the AOI polygon."""
    tr = Transformer.from_crs("EPSG:4326", template.rio.crs, always_xy=True)
    geom = transform(tr.transform, aoi.geometry)
    return ~geometry_mask([geom], out_shape=template.shape, transform=template.rio.transform())


def regrid(da: xr.DataArray, template: xr.DataArray) -> xr.DataArray:
    out = da.rio.reproject_match(template, resampling=Resampling.average)
    return out.where(np.isfinite(out))


def pair_tilt(los: np.ndarray, x: np.ndarray, y: np.ndarray) -> dict:
    """Best-fitting plane of one pair (metres, LOS): its change across the grid east-west and north-south, and
    the spread left after removing it, all in mm. Real ground motion builds up pair after pair; a tilt that shows
    up in only one or two pairs is usually ionosphere or troposphere."""
    ok = np.isfinite(los)
    if ok.sum() < 10:
        return {"tilt_ew_mm": float("nan"), "tilt_ns_mm": float("nan"), "spread_mm": float("nan")}
    X, Y = np.meshgrid((x - x.mean()) / 1000, (y - y.mean()) / 1000)
    A = np.column_stack([np.ones(ok.sum()), X[ok], Y[ok]])
    coef, *_ = np.linalg.lstsq(A, los[ok], rcond=None)
    resid = los[ok] - A @ coef
    return {"tilt_ew_mm": float(coef[1] * (np.ptp(X)) * 1000), "tilt_ns_mm": float(coef[2] * (np.ptp(Y)) * 1000),
            "spread_mm": float(np.std(resid) * 1000)}


def independent_pieces(ifgs) -> int:
    """Number of gapless chains the pairs form (each pair starting on the date the previous one ended)."""
    pairs = sorted((i.date1, i.date2) for i in ifgs)
    return 1 + sum(1 for (_, end), (start, _) in zip(pairs, pairs[1:]) if start != end)


def stack_rates(ifgs: list[Interferogram], template: xr.DataArray, coherence_threshold: float,
                min_fraction: float = 0.5) -> RateMap:
    """Mean LOS rate by interferogram stacking: sum(displacement) / sum(time) per pixel.

    Each pair is first shifted so its median over the pixels valid in *every* pair is zero,
    which removes the arbitrary per-pair phase offset before the pairs are combined.
    """
    if not ifgs:
        raise ValueError("No interferograms to stack")
    sensor = ifgs[0].sensor
    notes: list[Note] = []
    los = np.stack([regrid(i.los, template).values for i in ifgs])
    coh = np.stack([regrid(i.coherence, template).values for i in ifgs])
    years = np.array([i.span_years for i in ifgs])

    valid = np.isfinite(los) & (np.nan_to_num(coh, nan=0.0) >= coherence_threshold)
    common = valid.all(axis=0)
    if common.sum() < 0.01 * common.size:
        common = None
    for k in range(len(ifgs)):
        sel = common if common is not None else valid[k]
        if sel.any():
            los[k] -= np.nanmedian(los[k][sel])
    los = np.where(valid, los, np.nan)
    xs, ys = template.x.values, template.y.values
    pair_qc = []
    for k, i in enumerate(ifgs):
        q = {"date1": str(i.date1), "date2": str(i.date2), "valid": float(valid[k].mean()), **pair_tilt(los[k], xs, ys)}
        it = (i.extras or {}).get("iono_tilt")
        if it:  # tilt of the ionosphere screen that was removed, as line-of-sight displacement (mm)
            q.update(iono_tilt_ew_mm=it["tilt_ew_mm"], iono_tilt_ns_mm=it["tilt_ns_mm"])
        pair_qc.append(q)

    n_valid = valid.sum(axis=0)
    need = max(1, int(np.ceil(min_fraction * len(ifgs))))
    t_sum = np.where(valid, years[:, None, None], 0.0).sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        rate = np.nansum(los, axis=0) / t_sum
    rate = np.where(n_valid >= need, rate, np.nan)

    # Noise: scatter of the pairs about the stacked rate, propagated to the rate estimate. It has two parts:
    # * pixel-to-pixel noise (decorrelation), independent in every pair, measured from neighbouring-pixel
    #   differences;
    # * spatially smooth noise (atmosphere, ionosphere), which belongs to the acquisition dates: in a gapless chain
    #   each in-between date enters one pair with + and the next with -, so only the end dates remain.
    noise = float("nan")
    if len(ifgs) >= 2:
        resid = los - rate[None] * years[:, None, None]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN pixels
            spread = np.nanstd(resid, axis=0)
            white = np.nanmedian([np.nanstd(np.diff(r, axis=1)) / np.sqrt(2) for r in resid])
        sigma_pair = np.nanmedian(spread[n_valid >= 2]) if (n_valid >= 2).any() else np.nan
        if np.isfinite(sigma_pair):
            white = min(float(white), float(sigma_pair)) if np.isfinite(white) else 0.0
            smooth2 = sigma_pair ** 2 - white ** 2
            noise = float(np.sqrt(independent_pieces(ifgs) * smooth2 + len(ifgs) * white ** 2) / years.sum())

    mean_coh = np.nanmean(coh, axis=0) if np.isfinite(coh).any() else coh[0]
    d0, d1 = min(i.date1 for i in ifgs), max(i.date2 for i in ifgs)
    for i in ifgs:
        notes.extend(i.notes)
    da = template.copy(data=rate.astype("float32"))
    return RateMap(
        sensor=sensor, rate=da.rename("rate"), coherence=template.copy(data=mean_coh.astype("float32")),
        n_valid=template.copy(data=n_valid.astype("float32")), n_pairs=len(ifgs),
        total_days=int(sum(i.span_days for i in ifgs)), date_start=d0, date_end=d1,
        incidence_deg=float(np.mean([i.incidence_deg for i in ifgs])),
        wavelength=float(ifgs[0].wavelength), noise_rate=noise, notes=notes, pair_qc=pair_qc,
    )


def merge_frames(maps: list[RateMap], min_overlap: int = 50) -> tuple[RateMap, list[Note]]:
    """Join rate maps of neighbouring frames (processed separately, so each has its own zero).

    The frame covering most pixels is the base; each other frame is shifted by the median difference where both
    have data, then fills the gaps. A frame that does not overlap the joined map cannot be lined up and is left out.
    """
    maps = sorted(maps, key=lambda m: -int(np.isfinite(m.rate.values).sum()))
    base = maps[0]
    rate, coh, nv = base.rate.values.copy(), base.coherence.values.copy(), base.n_valid.values.copy()
    notes: list[Note] = []
    used = [base]
    for m in maps[1:]:
        r = m.rate.values
        both = np.isfinite(rate) & np.isfinite(r)
        if both.sum() < min_overlap:
            notes.append(Note("stack", WARN, f"{m.sensor}: one frame ({m.date_start}→{m.date_end}) does not overlap "
                                             "the others, so it cannot be lined up and was left out."))
            continue
        shift = float(np.median(rate[both] - r[both]))
        spread = float(np.std(rate[both] - r[both] - shift)) * 1000
        fill = ~np.isfinite(rate) & np.isfinite(r)
        rate[fill], coh[fill], nv[fill] = r[fill] + shift, m.coherence.values[fill], m.n_valid.values[fill]
        used.append(m)
        notes.append(Note("stack", INFO if spread < 2 * max(base.noise_rate, m.noise_rate, 0.005) * 1000 else WARN,
                          f"{m.sensor}: joined a neighbouring frame (shift {shift * 1000:+.0f} mm/yr, where the frames "
                          f"overlap they differ by {spread:.0f} mm/yr (1σ) over {int(both.sum()):,} pixels)."))
    noises = [m.noise_rate for m in used if np.isfinite(m.noise_rate)]
    merged = RateMap(
        sensor=base.sensor, rate=base.rate.copy(data=rate), coherence=base.coherence.copy(data=coh),
        n_valid=base.n_valid.copy(data=nv), n_pairs=sum(m.n_pairs for m in used),
        total_days=max(m.total_days for m in used), date_start=min(m.date_start for m in used),
        date_end=max(m.date_end for m in used), incidence_deg=float(np.mean([m.incidence_deg for m in used])),
        wavelength=base.wavelength, component=base.component,
        noise_rate=float(max(noises)) if noises else float("nan"),
        notes=[n for m in used for n in m.notes], pair_qc=[q for m in used for q in m.pair_qc])
    return merged, notes


# Typical heading (flight direction, degrees clockwise from north) of near-polar, sun-synchronous SAR orbits such as
# Sentinel-1 and NISAR at low and middle latitudes. The east component of the look vector scales with cos(heading),
# so a few degrees of error change the result by about 1%.
NOMINAL_HEADING = {"ASCENDING": -12.0, "DESCENDING": -168.0}


def los_vector(incidence_deg: float, heading_deg: float) -> tuple[float, float, float]:
    """Unit vector (east, north, up) from the ground to a right-looking satellite. Motion along it counts as
    positive (toward the satellite), matching the sign of our line-of-sight rates."""
    th, a = np.radians(incidence_deg), np.radians(heading_deg)
    return float(-np.sin(th) * np.cos(a)), float(np.sin(th) * np.sin(a)), float(np.cos(th))


def combine_directions(asc: RateMap, desc: RateMap, inside: np.ndarray | None = None
                       ) -> tuple[RateMap, RateMap, list[Note]]:
    """Separate vertical and east-west motion from ascending and descending line-of-sight rates.

    Each pixel seen from both directions gives two equations, d_asc = e_a E + u_a U and d_desc = e_d E + u_d U
    (north-south motion is ignored: near-polar orbits are almost blind to it), solved exactly for the vertical (U)
    and east-west (E) rates. Pixels seen from one direction only keep that direction's vertical estimate, which
    assumes no sideways motion. Both inputs are first given a common zero (their median where both have data).
    Returns (vertical, east-west, notes); east-west is NaN where only one direction has data.
    """
    ra, rd = asc.rate.values.astype("float64"), desc.rate.values.astype("float64")
    both = np.isfinite(ra) & np.isfinite(rd)
    zone = both & inside if inside is not None and (both & inside).any() else both
    if zone.any():
        ra, rd = ra - np.median(ra[zone]), rd - np.median(rd[zone])
    ea, _, ua = los_vector(asc.incidence_deg, NOMINAL_HEADING["ASCENDING"])
    ed, _, ud = los_vector(desc.incidence_deg, NOMINAL_HEADING["DESCENDING"])
    det = ea * ud - ed * ua
    up = np.where(both, (ea * rd - ed * ra) / det,
                  np.where(np.isfinite(ra), ra / ua, rd / ud))  # one direction only: assume no sideways motion
    east = np.where(both, (ra * ud - ua * rd) / det, np.nan)
    na = asc.noise_rate if np.isfinite(asc.noise_rate) else desc.noise_rate
    nd = desc.noise_rate if np.isfinite(desc.noise_rate) else asc.noise_rate
    noise_up = float(np.hypot(ed / det * na, ea / det * nd))
    noise_east = float(np.hypot(ud / det * na, ua / det * nd))
    coh = np.nanmean(np.stack([asc.coherence.values, desc.coherence.values]), axis=0) if both.any() \
        else np.where(np.isfinite(asc.coherence.values), asc.coherence.values, desc.coherence.values)
    nv = np.nan_to_num(asc.n_valid.values) + np.nan_to_num(desc.n_valid.values)
    valid = np.isfinite(up)
    frac = float(both.sum() / valid.sum()) if valid.any() else 0.0
    common = dict(sensor=asc.sensor, coherence=asc.coherence.copy(data=coh.astype("float32")),
                  n_valid=asc.n_valid.copy(data=nv.astype("float32")), n_pairs=asc.n_pairs + desc.n_pairs,
                  total_days=max(asc.total_days, desc.total_days), date_start=min(asc.date_start, desc.date_start),
                  date_end=max(asc.date_end, desc.date_end),
                  incidence_deg=float(np.mean([asc.incidence_deg, desc.incidence_deg])), wavelength=asc.wavelength,
                  notes=asc.notes + desc.notes, pair_qc=asc.pair_qc + desc.pair_qc,
                  geometry=f"{asc.geometry} + {desc.geometry}".strip(" +"), two_direction_fraction=frac)
    vertical = RateMap(rate=asc.rate.copy(data=up.astype("float32")).rename("rate"), component="vertical",
                       noise_rate=noise_up, **common)
    east_west = RateMap(rate=asc.rate.copy(data=east.astype("float32")).rename("rate"), component="east-west",
                        noise_rate=noise_east, **common)
    notes = [Note("stack", GOOD if frac > 0.5 else INFO,
                  f"{asc.sensor}: ascending and descending passes combined ({common['geometry']}). At {frac:.0%} of "
                  "the measured area, vertical and east-west motion are separated exactly instead of assuming the "
                  "ground only moves up and down; elsewhere only one direction has data and that assumption remains. "
                  f"Noise: vertical ≈ {noise_up * 1000:.0f} mm/yr, east-west ≈ {noise_east * 1000:.0f} mm/yr (1σ).")]
    # Real sideways motion (e.g. toward the centre of a subsidence bowl) is smooth over hundreds of metres or more,
    # so judge it on a map smoothed over ~600 m, where pixel noise largely averages out.
    res = abs(float(asc.rate.x[1] - asc.rate.x[0])) if asc.rate.x.size > 1 else 90.0
    size = max(3, int(round(600 / res)) | 1)
    ok = np.isfinite(east)
    if ok.sum() > size * size:
        num = ndimage.uniform_filter(np.where(ok, east, 0.0), size)
        den = ndimage.uniform_filter(ok.astype(float), size)
        smooth = np.where(ok & (den > 0.5), num / np.maximum(den, 1e-9), np.nan) * 1000
        lo, hi = np.nanpercentile(smooth, [2, 98])
        if max(abs(lo), abs(hi)) > max(2 * noise_east * 1000, 10):  # smooth noise does not average out
            notes.append(Note("stack", INFO,
                              f"{asc.sensor}: east-west motion reaches {lo:+.0f} to {hi:+.0f} mm/yr (2nd-98th percentile, "
                              f"smoothed over ~{size * res:.0f} m; + = eastward, relative to the area as a whole). "
                              "Around subsidence bowls the ground also moves sideways toward the centre; with one "
                              "direction only, that motion would have been mistaken for extra sinking or rising."))
        else:
            notes.append(Note("stack", GOOD,
                              f"{asc.sensor}: no significant east-west motion ({lo:+.0f} to {hi:+.0f} mm/yr, 2nd-98th "
                              f"percentile smoothed over ~{size * res:.0f} m, within the noise). The motion is "
                              "essentially vertical."))
    return vertical, east_west, notes


def to_vertical(rm: RateMap) -> RateMap:
    """Project LOS rate to vertical assuming the ground moves only up/down."""
    f = 1.0 / np.cos(np.radians(rm.incidence_deg))
    rm.rate = rm.rate * f
    rm.noise_rate = rm.noise_rate * f
    rm.component = "vertical"
    return rm


@dataclass
class Reference:
    """Common reference. ``mode`` is "point" (a user-given location, e.g. bedrock or a GNSS station) or
    "area" (automatic: the median over all pixels valid in both datasets)."""

    mode: str
    mask: np.ndarray  # pixels whose median defines zero
    x: float | None = None
    y: float | None = None
    lon: float | None = None
    lat: float | None = None
    row: int | None = None
    col: int | None = None
    notes: list[Note] = field(default_factory=list)

    @property
    def auto(self) -> bool:
        return self.mode == "area"


def choose_reference(a: RateMap, b: RateMap, lonlat: tuple[float, float] | None = None,
                     inside: np.ndarray | None = None, window: int = 7) -> Reference:
    """Shared reference for both maps.

    Automatic mode deliberately does *not* search for a "best" pixel: with rate noise of several mm/yr,
    searching thousands of pixels for one that looks typical selects noise (and coherent cities, which
    often subside). The median over every pixel valid in both datasets assumes only that most of the area
    is stable, and averages the noise over the whole scene.
    """
    ra, rb = a.rate.values, b.rate.values
    both = np.isfinite(ra) & np.isfinite(rb)
    if inside is not None and (both & inside).any():
        both = both & inside
    if not both.any():
        raise ValueError("The two sensors have no valid pixels in common; cannot compare. "
                         "Try a lower coherence_threshold or a different AOI/date window.")
    if lonlat is None:
        return Reference(mode="area", mask=both)
    crs = a.rate.rio.crs
    xs, ys = a.rate.x.values, a.rate.y.values
    notes: list[Note] = []
    x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(*lonlat)
    col, row = int(np.argmin(np.abs(xs - x))), int(np.argmin(np.abs(ys - y)))
    if not both[row, col]:
        idx = ndimage.distance_transform_edt(~both, return_indices=True)[1]
        row, col = int(idx[0, row, col]), int(idx[1, row, col])
        notes.append(Note("reference", WARN, "Your reference point is not valid in both datasets; moved to the "
                                             "nearest pixel that is."))
    h = window // 2
    mask = np.zeros_like(both)
    mask[max(0, row - h):row + h + 1, max(0, col - h):col + h + 1] = True
    lon, lat = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform(xs[col], ys[row])
    return Reference(mode="point", mask=mask & both, x=float(xs[col]), y=float(ys[row]), lon=float(lon),
                     lat=float(lat), row=row, col=col, notes=notes)


def apply_reference(rm: RateMap, ref: Reference) -> float:
    """Subtract the median rate over the reference pixels; returns the value removed (m/yr)."""
    offset = float(np.nanmedian(rm.rate.values[ref.mask]))
    rm.rate = rm.rate - offset
    return offset


def reference_notes(ref: Reference, a: RateMap, b: RateMap) -> list[Note]:
    notes = list(ref.notes)
    if ref.mode == "area":
        notes.append(Note("reference", INFO,
                          f"Both rate maps are referenced to their median over the {int(ref.mask.sum()):,} pixels "
                          "valid in both, i.e. zero = the typical motion of the area. This assumes most of the AOI is "
                          "stable. InSAR only measures relative motion; for absolute rates, set reference_lonlat "
                          "to bedrock or a GNSS station (the GNSS check reports how far off zero the area is)."))
        return notes
    notes.append(Note("reference", INFO, f"Both rate maps are referenced to your point at {ref.lat:.4f}°N, "
                                         f"{ref.lon:.4f}°E (7×7-pixel median). Everything is relative to it."))
    ca, cb = float(a.coherence.values[ref.row, ref.col]), float(b.coherence.values[ref.row, ref.col])
    lvl = GOOD if min(ca, cb) > 0.5 else WARN
    notes.append(Note("reference", lvl, f"Coherence at the reference: {a.sensor} {ca:.2f}, {b.sensor} {cb:.2f}."))
    return notes
