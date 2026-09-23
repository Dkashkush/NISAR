"""Run configuration, loadable from YAML."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import yaml

from .aoi import AOI

S1_SOURCES = ("ARIA_S1_GUNW", "OPERA_DISP_S1", "LOCAL")
NISAR_SOURCES = ("ASF", "LOCAL")


@dataclass
class Config:
    name: str = "comparison"
    aoi: object = None  # bbox [W,S,E,N], WKT, GeoJSON dict/path
    start: date | None = None
    end: date | None = None

    nisar_source: str = "ASF"  # ASF search+download, or LOCAL folder
    nisar_dir: str | None = None  # used when nisar_source == LOCAL
    nisar_polarization: str | None = None  # None = first available (usually HH)
    nisar_flight_direction: str | None = None  # ASCENDING / DESCENDING / None (any)
    nisar_apply_ionosphere: bool = True  # subtract the GUNW ionospheric phase screen when present

    s1_source: str = "ARIA_S1_GUNW"  # see S1_SOURCES
    s1_dir: str | None = None  # used when s1_source == LOCAL
    s1_flight_direction: str | None = None

    max_pairs: int = 6  # per sensor; keeps downloads manageable
    min_span_days: int = 6
    max_span_days: int = 96
    coherence_threshold: float = 0.35
    resolution_m: float = 90.0  # common comparison grid spacing
    reference_lonlat: tuple[float, float] | None = None  # stable reference point; auto if None
    project_to_vertical: bool = True  # LOS -> vertical assuming purely vertical motion
    s1_sign: float | None = None  # override sign convention (+1 / -1) if a product disagrees
    nisar_sign: float | None = None

    # Validation against published data (see sarcompare/validate.py)
    gnss: str | None = None  # None = off; "NGL" = Nevada Geodetic Lab (auto-download); or path to a CSV table
    gnss_dir: str = "data/gnss"  # cache for NGL files
    gnss_max_stations: int = 30
    published_maps: list = field(default_factory=list)  # [{path, label, units, component, sign, incidence_deg}]

    data_dir: str = "data"
    output_dir: str = "outputs"
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self.start, str):
            self.start = date.fromisoformat(self.start)
        if isinstance(self.end, str):
            self.end = date.fromisoformat(self.end)
        if self.reference_lonlat is not None:
            self.reference_lonlat = tuple(float(v) for v in self.reference_lonlat)
        if self.s1_source not in S1_SOURCES:
            raise ValueError(f"s1_source must be one of {S1_SOURCES}, got {self.s1_source!r}")
        if self.nisar_source not in NISAR_SOURCES:
            raise ValueError(f"nisar_source must be one of {NISAR_SOURCES}, got {self.nisar_source!r}")
        if self.nisar_source == "LOCAL" and not self.nisar_dir:
            raise ValueError("nisar_dir is required when nisar_source is LOCAL")
        if self.s1_source == "LOCAL" and not self.s1_dir:
            raise ValueError("s1_dir is required when s1_source is LOCAL")
        if not 0 < self.coherence_threshold < 1:
            raise ValueError("coherence_threshold must be between 0 and 1")

    def get_aoi(self) -> AOI:
        if self.aoi is None:
            raise ValueError("No AOI configured")
        return AOI.parse(self.aoi, name=self.name)

    @property
    def run_dir(self) -> Path:
        return Path(self.output_dir) / self.name

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Config":
        data = yaml.safe_load(Path(path).read_text()) or {}
        return cls(**data)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"] = str(self.start) if self.start else None
        d["end"] = str(self.end) if self.end else None
        return d
