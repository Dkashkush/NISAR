"""Common description of a downloadable/loaded InSAR product, independent of sensor."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

NISAR = "NISAR"
SENTINEL1 = "Sentinel-1"

# Radar wavelengths in metres (used when a file does not carry its own value).
WAVELENGTH_NISAR_L = 0.2384  # L-band, 1257.5 MHz centre frequency
WAVELENGTH_S1_C = 0.05546576  # C-band, 5.405 GHz

_DT_PATTERN = re.compile(r"(?<!\d)(\d{8})(?:T(\d{6}))?(?!\d)")

# Official file-name patterns (NISAR pattern as in opera-utils' NISAR_GUNW_FILE_REGEX). Each yields the two
# acquisition dates plus, where the name carries it, the viewing geometry.
_NAME_PATTERNS = [
    # NISAR_L2_PR_GUNW_004_151_A_011_005_4000_SH_20251108T155041_20251108T155058_20251120T..._..._X05009_N_F_J_001
    re.compile(r"NISAR_L\d_[A-Z]{2}_GUNW_\d{3}_(?P<track>\d{3})_(?P<dir>[AD])_(?P<frame>\d{3})_\d{3}_\d{4}_"
               r"[A-Z]{2,4}_(?P<d1>\d{8})T\d{6}_\d{8}T\d{6}_(?P<d2>\d{8})T\d{6}_"),
    # S1-GUNW-A-R-064-tops-20210723_20210711-015000-00119W_00033N-PP-6267-v2_0_4 (later date first)
    re.compile(r"S1-GUNW-(?P<dir>[AD])-R-(?P<track>\d{3})-tops-(?P<d2>\d{8})_(?P<d1>\d{8})"),
    # OPERA_L3_DISP-S1_IW_F11115_VV_20160705T140755Z_20160729T140756Z_v1.0_...
    re.compile(r"DISP-S1_IW_F(?P<frame>\d+)_[A-Z]{2}_(?P<d1>\d{8})T\d{6}Z_(?P<d2>\d{8})T\d{6}Z"),
    # HyP3 GAMMA: S1AA_20161223T070700_20170116T070658_VVP024_INT80_G_ueF_74C2
    re.compile(r"^S1[ABCD]{2}_(?P<d1>\d{8})T\d{6}_(?P<d2>\d{8})T\d{6}_"),
    # HyP3 ISCE burst / multi-burst: S1_136231_IW2_20200604_20200616_VV_INT80_10C8
    re.compile(r"^S1_.*?_(?P<d1>\d{8})_(?P<d2>\d{8})_[VH]{2}_INT"),
]


def parse_name(name: str) -> dict:
    """Dates and geometry from an official product file name: {'date1','date2','direction','track','frame'}."""
    for pat in _NAME_PATTERNS:
        m = pat.search(name)
        if m:
            g = m.groupdict()
            d1 = datetime.strptime(g["d1"], "%Y%m%d").date()
            d2 = datetime.strptime(g["d2"], "%Y%m%d").date()
            direction = {"A": "ASCENDING", "D": "DESCENDING"}.get(g.get("dir") or "")
            return {"date1": min(d1, d2), "date2": max(d1, d2), "direction": direction,
                    "track": int(g["track"]) if g.get("track") else None,
                    "frame": int(g["frame"]) if g.get("frame") else None}
    return {}


def parse_dates_from_name(name: str) -> list[date]:
    """Return the distinct acquisition dates found in a product file name, in the order they appear.

    Works for NISAR (``..._20251006T120000_20251006T120020_20251018T...``), ARIA
    (``..._20210723_20210711-...``), HyP3 and OPERA names. Numbers that are not
    plausible dates (e.g. frame IDs) are ignored.
    """
    out: list[date] = []
    for m in _DT_PATTERN.finditer(name):
        try:
            d = datetime.strptime(m.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        if not (2000 <= d.year <= 2100):
            continue
        if d not in out:
            out.append(d)
    return out


def pair_dates_from_name(name: str) -> tuple[date, date] | None:
    """(earlier, later) acquisition dates of an interferometric pair, or None if not found."""
    known = parse_name(name)
    if known:
        return known["date1"], known["date2"]
    dates = parse_dates_from_name(name)
    if len(dates) < 2:
        return None
    return min(dates[0], dates[1]), max(dates[0], dates[1])


@dataclass
class Product:
    sensor: str  # NISAR or SENTINEL1
    product_type: str  # e.g. GUNW, ARIA_GUNW, DISP-S1, HYP3_INSAR
    name: str
    url: str | None = None
    date1: date | None = None  # earlier acquisition
    date2: date | None = None  # later acquisition
    flight_direction: str | None = None  # ASCENDING / DESCENDING
    track: int | None = None
    frame: int | None = None
    size_mb: float | None = None
    local_path: Path | None = None
    extra: dict = field(default_factory=dict)

    @property
    def span_days(self) -> int | None:
        if self.date1 and self.date2:
            return (self.date2 - self.date1).days
        return None

    @property
    def geometry_key(self) -> tuple:
        """Products sharing this key share viewing geometry and can be stacked."""
        return (self.flight_direction, self.track)

    def label(self) -> str:
        dates = f"{self.date1}→{self.date2}" if self.date1 else "?"
        return f"{self.sensor} {self.product_type} {dates}"

    def to_row(self) -> dict:
        return {
            "sensor": self.sensor,
            "type": self.product_type,
            "date1": str(self.date1) if self.date1 else "",
            "date2": str(self.date2) if self.date2 else "",
            "span_days": self.span_days,
            "direction": self.flight_direction or "",
            "track": self.track,
            "size_mb": round(self.size_mb, 1) if self.size_mb else None,
            "name": self.name,
        }
