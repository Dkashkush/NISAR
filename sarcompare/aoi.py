"""Area of interest (AOI) handling: parse bbox / WKT / GeoJSON and pick a UTM grid."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pyproj import CRS, Transformer
from shapely import wkt as shapely_wkt
from shapely.geometry import box, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform


@dataclass
class AOI:
    """A region on the ground, always stored in lon/lat (EPSG:4326)."""

    geometry: BaseGeometry
    name: str = "aoi"

    @classmethod
    def from_bbox(cls, west: float, south: float, east: float, north: float, name: str = "aoi") -> "AOI":
        if west >= east or south >= north:
            raise ValueError(f"Invalid bbox: west<east and south<north required, got {(west, south, east, north)}")
        if not (-180 <= west <= 180 and -180 <= east <= 180 and -90 <= south <= 90 and -90 <= north <= 90):
            raise ValueError("bbox must be in degrees longitude/latitude")
        return cls(box(west, south, east, north), name)

    @classmethod
    def from_wkt(cls, text: str, name: str = "aoi") -> "AOI":
        return cls(shapely_wkt.loads(text), name)

    @classmethod
    def from_geojson(cls, source: str | Path | dict, name: str = "aoi") -> "AOI":
        if isinstance(source, dict):
            data = source
        else:
            text = str(source)
            data = json.loads(text) if text.lstrip().startswith("{") else json.loads(Path(text).read_text())
        if data.get("type") == "FeatureCollection":
            data = data["features"][0]
        if data.get("type") == "Feature":
            data = data["geometry"]
        return cls(shape(data), name)

    @classmethod
    def parse(cls, value, name: str = "aoi") -> "AOI":
        """Accept a bbox list, a WKT string, a GeoJSON dict/string, or a path to a GeoJSON file."""
        if isinstance(value, AOI):
            return value
        if isinstance(value, (list, tuple)) and len(value) == 4:
            return cls.from_bbox(*map(float, value), name=name)
        if isinstance(value, dict):
            return cls.from_geojson(value, name)
        if isinstance(value, str):
            s = value.strip()
            if s.upper().startswith(("POLYGON", "MULTIPOLYGON", "POINT")):
                return cls.from_wkt(s, name)
            if "," in s and not s.startswith("{") and not Path(s).exists():
                parts = [float(p) for p in s.split(",")]
                return cls.from_bbox(*parts, name=name)
            return cls.from_geojson(s, name)
        raise ValueError(f"Cannot interpret AOI: {value!r}")

    @property
    def wkt(self) -> str:
        return self.geometry.wkt

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self.geometry.bounds

    @property
    def center(self) -> tuple[float, float]:
        c = self.geometry.centroid
        return c.x, c.y

    def utm_crs(self) -> CRS:
        """UTM zone CRS containing the AOI centroid."""
        lon, lat = self.center
        zone = int((lon + 180) // 6) + 1
        epsg = (32600 if lat >= 0 else 32700) + zone
        return CRS.from_epsg(epsg)

    def bounds_in(self, crs) -> tuple[float, float, float, float]:
        """AOI bounds transformed to another CRS."""
        crs = CRS.from_user_input(crs)
        if crs.to_epsg() == 4326:
            return self.bounds
        tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        return transform(tr.transform, self.geometry).bounds

    def area_km2(self) -> float:
        tr = Transformer.from_crs("EPSG:4326", self.utm_crs(), always_xy=True)
        return transform(tr.transform, self.geometry).area / 1e6
