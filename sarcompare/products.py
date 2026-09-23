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
