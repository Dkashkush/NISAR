"""Put both sensors on one grid, stack pairs into mean rates, and reference them to a common point."""

from __future__ import annotations

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

    n_valid = valid.sum(axis=0)
    need = max(1, int(np.ceil(min_fraction * len(ifgs))))
    t_sum = np.where(valid, years[:, None, None], 0.0).sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        rate = np.nansum(los, axis=0) / t_sum
    rate = np.where(n_valid >= need, rate, np.nan)

    # Noise: residual of each pair about the stacked rate, propagated to the rate estimate.
    noise = float("nan")
    if len(ifgs) >= 2:
        resid = los - rate[None] * years[:, None, None]
        sigma_pair = np.nanmedian(np.nanstd(resid, axis=0)[n_valid >= 2]) if (n_valid >= 2).any() else np.nan
        if np.isfinite(sigma_pair):
            noise = float(sigma_pair * np.sqrt(len(ifgs)) / years.sum())

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
        wavelength=float(ifgs[0].wavelength), noise_rate=noise, notes=notes,
    )


def to_vertical(rm: RateMap) -> RateMap:
    """Project LOS rate to vertical assuming the ground moves only up/down."""
    f = 1.0 / np.cos(np.radians(rm.incidence_deg))
    rm.rate = rm.rate * f
    rm.noise_rate = rm.noise_rate * f
    rm.component = "vertical"
    return rm


@dataclass
class Reference:
    x: float
    y: float
    lon: float
    lat: float
    row: int
    col: int
    auto: bool
    notes: list[Note] = field(default_factory=list)


def choose_reference(a: RateMap, b: RateMap, lonlat: tuple[float, float] | None = None,
                     window: int = 5) -> Reference:
    """Pick a shared reference pixel: user-given, or the most coherent, smoothest spot valid in both."""
    ra, rb = a.rate.values, b.rate.values
    both = np.isfinite(ra) & np.isfinite(rb)
    if not both.any():
        raise ValueError("The two sensors have no valid pixels in common; cannot compare. "
                         "Try a lower coherence_threshold or a different AOI/date window.")
    crs = a.rate.rio.crs
    to_ll = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    xs, ys = a.rate.x.values, a.rate.y.values
    notes: list[Note] = []
    if lonlat is not None:
        x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(*lonlat)
        col, row = int(np.argmin(np.abs(xs - x))), int(np.argmin(np.abs(ys - y)))
        if not both[row, col]:
            dist = ndimage.distance_transform_edt(~both, return_indices=True)[1]
            row, col = int(dist[0, row, col]), int(dist[1, row, col])
            notes.append(Note("reference", WARN, "Your reference point is not valid in both datasets; moved to "
                                                 "the nearest pixel that is."))
        auto = False
    else:
        joint_coh = np.fmin(np.nan_to_num(a.coherence.values), np.nan_to_num(b.coherence.values))
        size = (window, window)
        mean_a = ndimage.uniform_filter(np.nan_to_num(ra), size)
        mean_b = ndimage.uniform_filter(np.nan_to_num(rb), size)
        var = (ndimage.uniform_filter(np.nan_to_num(ra) ** 2, size) - mean_a ** 2
               + ndimage.uniform_filter(np.nan_to_num(rb) ** 2, size) - mean_b ** 2)
        frac_valid = ndimage.uniform_filter(both.astype(float), size)
        score = np.where(both & (frac_valid > 0.9), ndimage.uniform_filter(joint_coh, size) - 50.0 * var, -np.inf)
        if not np.isfinite(score).any():
            score = np.where(both, joint_coh, -np.inf)
        row, col = np.unravel_index(int(np.argmax(score)), score.shape)
        auto = True
    lon, lat = to_ll.transform(xs[col], ys[row])
    return Reference(x=float(xs[col]), y=float(ys[row]), lon=float(lon), lat=float(lat), row=int(row),
                     col=int(col), auto=auto, notes=notes)


def apply_reference(rm: RateMap, ref: Reference, window: int = 3) -> float:
    """Subtract the rate around the reference point; returns the value removed (m/yr)."""
    h = window // 2
    patch = rm.rate.values[max(0, ref.row - h):ref.row + h + 1, max(0, ref.col - h):ref.col + h + 1]
    offset = float(np.nanmedian(patch))
    rm.rate = rm.rate - offset
    return offset


def reference_notes(ref: Reference, a: RateMap, b: RateMap) -> list[Note]:
    notes = list(ref.notes)
    how = "chosen automatically (high coherence in both, locally smooth)" if ref.auto else "set by you"
    notes.append(Note("reference", INFO, f"Both rate maps are referenced to the same point at "
                                         f"{ref.lat:.4f}°N, {ref.lon:.4f}°E ({how}). InSAR measures relative "
                                         "motion, so this point is assumed stable; everything is relative to it."))
    ca, cb = float(a.coherence.values[ref.row, ref.col]), float(b.coherence.values[ref.row, ref.col])
    lvl = GOOD if min(ca, cb) > 0.5 else WARN
    notes.append(Note("reference", lvl, f"Coherence at the reference: {a.sensor} {ca:.2f}, {b.sensor} {cb:.2f}. "
                                        "Pick a point on bedrock or an old building known to be stable if you can "
                                        "(set reference_lonlat)."))
    return notes
