"""Shared reader utilities and the sensor-independent Interferogram container."""

from __future__ import annotations

from dataclasses import dataclass, field
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
