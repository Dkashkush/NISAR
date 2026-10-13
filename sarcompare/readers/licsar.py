"""LiCSAR (COMET) Sentinel-1 interferograms: lon/lat GeoTIFFs, read remotely (only the window over the area).

* ``{pair}.geo.unw.tif``: unwrapped phase in radians, 0 where LiCSAR masked low coherence.
* ``{pair}.geo.cc.tif``: coherence scaled 0-255.
* ``{frame}.geo.U.tif``: up component of the unit vector from the ground to the satellite (cos of incidence).

LiCSAR is processed with GAMMA, like HyP3: positive phase = motion away from the satellite (LiCSBAS converts to
displacement with -wavelength / 4 pi), so the sign is flipped to this app's toward-positive convention.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import numpy as np
import rioxarray

from ..aoi import AOI
from ..notes import WARN, Note
from ..products import SENTINEL1, WAVELENGTH_S1_C
from .base import Interferogram, make_da

LICSAR_PHASE_SIGN = -1.0
DEFAULT_INCIDENCE = 38.0
GDAL_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                GDAL_HTTP_MAX_RETRY="3", GDAL_HTTP_RETRY_DELAY="2", GDAL_HTTP_TIMEOUT="60", VSI_CACHE="TRUE")
_PAIR = re.compile(r"(\d{8})_(\d{8})")
_FRAME = re.compile(r"/(\d{3})([AD])_\d{5}_\d{6}/")


def _open(src: str):
    path = "/vsicurl/" + src if str(src).startswith(("http://", "https://")) else str(src)
    return rioxarray.open_rasterio(path, masked=True).squeeze("band", drop=True)


def _window(da, aoi: AOI, pad: float = 0.05):
    w, s, e, n = aoi.bounds
    px, py = (e - w) * pad, (n - s) * pad
    try:
        return da.rio.clip_box(minx=w - px, miny=s - py, maxx=e + px, maxy=n + py)
    except Exception as e:  # rioxarray raises its own NoDataInBounds / OneDimensionalRaster
        raise ValueError(f"The LiCSAR frame does not overlap the area of interest ({type(e).__name__})") from e


def _like(layer, ref):
    """``layer`` on ``ref``'s grid (LiCSAR layers normally share the frame grid; this guards against offsets)."""
    if layer.shape == ref.shape and np.allclose(layer.x.values, ref.x.values) and \
            np.allclose(layer.y.values, ref.y.values):
        return layer.values
    return layer.rio.reproject_match(ref).values


def read_licsar(unw: str | Path, aoi: AOI, cc: str | Path | None = None, up: str | Path | None = None,
                sign: float | None = None, name: str | None = None) -> Interferogram:
    """One LiCSAR pair. ``unw``/``cc``/``up`` are addresses (read with HTTP range requests) or local files."""
    import rasterio

    src = str(unw)
    m = _PAIR.search(src.rsplit("/", 1)[-1]) or _PAIR.search(src)
    if not m:
        raise ValueError(f"Cannot determine the dates of LiCSAR product {src}")
    date1, date2 = (datetime.strptime(x, "%Y%m%d").date() for x in m.groups())
    fm = _FRAME.search(src)
    direction = {"A": "ASCENDING", "D": "DESCENDING"}.get(fm.group(2)) if fm else None
    notes: list[Note] = []
    with rasterio.Env(**GDAL_ENV):
        u = _window(_open(src), aoi)
        phase = u.values.astype("float64")
        phase[(phase == 0) | ~np.isfinite(phase)] = np.nan  # 0 = masked by LiCSAR (low coherence)
        if cc is not None:
            coh = _like(_window(_open(str(cc)), aoi), u).astype("float64") / 255.0
            coh[(coh <= 0) | ~np.isfinite(coh)] = np.nan
        else:
            coh = np.where(np.isfinite(phase), 0.5, np.nan)
            notes.append(Note("load", WARN, f"LiCSAR {date1}→{date2}: no coherence layer; all unmasked pixels kept."))
        inc = None
        if up is not None:
            try:
                uz = _like(_window(_open(str(up)), aoi), u).astype("float64")
                uz = uz[np.isfinite(uz) & (uz > 0) & (uz <= 1)]
                inc = float(np.degrees(np.arccos(np.mean(uz)))) if uz.size else None
            except Exception as e:  # the geometry file is optional: fall back to a typical angle
                notes.append(Note("load", WARN, f"LiCSAR {date1}→{date2}: line-of-sight file unreadable "
                                                f"({type(e).__name__})."))
    if inc is None:
        inc = DEFAULT_INCIDENCE
        notes.append(Note("load", WARN, f"LiCSAR {date1}→{date2}: no line-of-sight geometry; assuming {inc}° "
                                        "incidence."))
    sgn = LICSAR_PHASE_SIGN if sign is None else sign
    los = sgn * phase * WAVELENGTH_S1_C / (4 * np.pi)
    coh = np.where(np.isfinite(los), coh, np.nan)
    x, y = u.x.values, u.y.values
    label = name or (f"{fm.group(0).strip('/')}_{m.group(0)}" if fm else m.group(0))
    return Interferogram(sensor=SENTINEL1, product_type="LICSAR", name=label, date1=date1, date2=date2,
                         los=make_da(los, x, y, u.rio.crs, "los"), coherence=make_da(coh, x, y, u.rio.crs, "coherence"),
                         wavelength=WAVELENGTH_S1_C, incidence_deg=inc, flight_direction=direction, notes=notes)


def read_licsar_product(product, aoi: AOI, sign: float | None = None) -> Interferogram:
    """Read a product found by sarcompare.licsar.search (its address points to the pair's unwrapped phase)."""
    from .. import licsar

    track, frame = product.extra.get("licsar") or (product.track, product.frame)
    if product.local_path is not None:  # downloaded copies sit side by side: <pair>.geo.unw.tif, <pair>.geo.cc.tif
        unw = Path(product.local_path)
        cc = unw.with_name(unw.name.replace(".geo.unw.tif", ".geo.cc.tif"))
        return read_licsar(unw, aoi, cc if cc.exists() else None, None, sign, product.name)
    unw = licsar.resolve(product.url)
    if unw is None:
        from ..remote import UnavailableError

        raise UnavailableError(f"LiCSAR lists {product.name} but its file is not available (yet)")
    cc = licsar.resolve(product.url.replace(".geo.unw.tif", ".geo.cc.tif"))
    up = licsar.metadata_url(track, frame, "geo.U.tif") if track and frame else None
    return read_licsar(unw, aoi, cc, up, sign, product.name)
