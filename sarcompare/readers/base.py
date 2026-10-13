"""Shared reader utilities and the sensor-independent Interferogram container."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from datetime import date

import numpy as np
import rioxarray  # noqa: F401  (registers the .rio accessor)
import xarray as xr

from ..notes import Note


@dataclass
class Interferogram:
    """One interferometric pair, cropped to the AOI.

    ``los`` is line-of-sight displacement in metres, **positive = toward the satellite**
    (uplift or motion toward the sensor), regardless of the source product's convention.
    """

    sensor: str
    product_type: str
    name: str
    date1: date
    date2: date
    los: xr.DataArray
    coherence: xr.DataArray
    wavelength: float
    incidence_deg: float
    flight_direction: str | None = None
    notes: list[Note] = field(default_factory=list)
    extras: dict = field(default_factory=dict)

    @property
    def span_days(self) -> int:
        return (self.date2 - self.date1).days

    @property
    def span_years(self) -> float:
        return self.span_days / 365.25


def make_da(values: np.ndarray, x: np.ndarray, y: np.ndarray, crs, name: str) -> xr.DataArray:
    da = xr.DataArray(np.asarray(values, dtype="float32"), dims=("y", "x"),
                      coords={"y": np.asarray(y, dtype="float64"), "x": np.asarray(x, dtype="float64")}, name=name)
    da = da.rio.write_crs(crs)
    da = da.rio.write_nodata(np.nan)
    return da


def aoi_slices(x: np.ndarray, y: np.ndarray, bounds: tuple[float, float, float, float],
               pad_frac: float = 0.05) -> tuple[slice, slice]:
    """Row/column slices of a regular grid covering ``bounds`` (+ padding). Raises if no overlap."""
    w, s, e, n = bounds
    px, py = (e - w) * pad_frac, (n - s) * pad_frac
    w, e, s, n = w - px, e + px, s - py, n + py
    cols = np.where((x >= w) & (x <= e))[0]
    rows = np.where((y >= s) & (y <= n))[0]
    if cols.size == 0 or rows.size == 0:
        raise ValueError("The product does not overlap the area of interest")
    return slice(rows.min(), rows.max() + 1), slice(cols.min(), cols.max() + 1)


def apply_fill(arr: np.ndarray, fill) -> np.ndarray:
    arr = np.asarray(arr, dtype="float32")
    if fill is not None:
        fill = np.asarray(fill).ravel()[0]
        if np.isfinite(fill):
            arr = np.where(arr == fill, np.nan, arr)
    return arr


def decode(value) -> str:
    if isinstance(value, np.ndarray):
        value = value.ravel()[0] if value.size else ""
    if isinstance(value, bytes):
        return value.decode(errors="ignore")
    return str(value)


def source_name(source, name: str | None = None) -> str:
    """File name used for dates/geometry: explicit ``name``, else the path's name (sources may be file objects)."""
    if name:
        return name
    if isinstance(source, (str, Path)):
        return Path(source).name
    return getattr(source, "name", None) or "remote.h5"


def fix_components(los: np.ndarray, labels: np.ndarray, cycle: float | None,
                   min_fraction: float = 0.002) -> tuple[np.ndarray, dict]:
    """Make the separately unwrapped pieces ("connected components") of one pair consistent.

    Phase unwrapping is reliable within a piece, but each piece can be off from the others by a whole number of
    cycles. With ``cycle`` (one cycle in the units of ``los``), every piece is shifted by the whole number of
    cycles that best matches its median to the largest piece. That is safe when the real motion differs by less
    than half a cycle between pieces during the pair (true for L-band over weeks: half a cycle is ~6 cm). With
    ``cycle=None`` (C-band, where half a cycle is only ~1.4 cm), only the largest piece is kept. Pieces smaller
    than ``min_fraction`` of the valid pixels are dropped either way.
    """
    from scipy import ndimage

    out = np.array(los, dtype="float32", copy=True)
    lab = np.where(np.isfinite(out), np.nan_to_num(np.asarray(labels, dtype="float64"), nan=0), 0).astype(np.int64)
    ids, counts = np.unique(lab[lab > 0], return_counts=True)
    info = {"pieces": int(ids.size), "shifted": 0, "dropped_fraction": 0.0, "main_fraction": 1.0}
    if ids.size <= 1:
        return out, info
    total = counts.sum()
    main = int(np.argmax(counts))
    keep = counts >= max(20, min_fraction * total)
    shifts = np.zeros(ids.size)
    if cycle:
        meds = np.asarray(ndimage.median(out, lab, ids), dtype="float64")
        shifts = np.round((meds - meds[main]) / cycle) * cycle
    else:
        keep[:] = False
    keep[main], shifts[main] = True, 0.0
    idx = np.searchsorted(ids, lab)
    labelled = lab > 0
    idx = np.where(labelled, idx, 0)
    out = np.where(labelled & ~keep[idx], np.nan, out - np.where(labelled, shifts[idx], 0)).astype("float32")
    info.update(shifted=int(np.count_nonzero(shifts[keep])), dropped_fraction=float(counts[~keep].sum() / total),
                main_fraction=float(counts[main] / total))
    return out, info


def component_note(sensor: str, date1, date2, info: dict, aligned: bool) -> Note | None:
    """Plain-language note on what fix_components did (None when the pair was unwrapped in one piece)."""
    from ..notes import INFO, WARN

    if info["pieces"] <= 1:
        return None
    head = f"{sensor} {date1}→{date2}: the unwrapped phase comes in {info['pieces']} separately unwrapped pieces"
    if aligned:
        text = (f"{head}; {info['shifted']} were shifted by whole cycles to line up with the largest piece "
                f"({info['main_fraction']:.0%} of the pixels), and {info['dropped_fraction']:.0%} of the pixels "
                "in tiny pieces were dropped.")
    else:
        text = (f"{head}; only the largest ({info['main_fraction']:.0%} of the pixels) is kept, because the others "
                "may be offset by whole cycles (C-band cycles are too small to line them up safely).")
    level = WARN if info["dropped_fraction"] > 0.3 else INFO
    return Note("load", level, text)
