# Copyright (c) 2026 Deepa Kumari. All rights reserved. See LICENSE.
"""Interactive web maps (Leaflet via folium) for picking an area and exploring results."""

from __future__ import annotations

import base64
import html
import io

import folium
import matplotlib
import numpy as np
from branca.colormap import LinearColormap
from folium.plugins import Draw
from pyproj import Transformer

from .plots import THEMES, _diverging

ESRI_IMAGERY = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
ESRI_ATTR = "Imagery © Esri, Maxar, Earthstar Geographics"
DIVERGING = ["#8f1d1d", "#e34948", "#f2a3a2", "#f0efec", "#86b6ef", "#2a78d6", "#0d366b"]

# Well-known places where InSAR has documented ground motion; good starting points.
EXAMPLE_PLACES = {
    "Mexico City, Mexico: fast city subsidence (groundwater)": (-99.20, 19.28, -98.95, 19.52),
    "Jakarta, Indonesia: coastal city subsidence": (106.70, -6.28, 107.00, -6.05),
    "Joshimath, Uttarakhand, India: Himalayan town subsidence": (79.45, 30.48, 79.65, 30.62),
    "Delhi NCR, India: groundwater-related subsidence": (76.95, 28.45, 77.15, 28.65),
    "Tehran plain, Iran: aquifer subsidence": (51.10, 35.45, 51.45, 35.72),
    "Central Valley, California, USA: agricultural subsidence": (-119.75, 35.90, -119.40, 36.15),
}


def _base(m: folium.Map) -> folium.Map:
    folium.TileLayer(ESRI_IMAGERY, attr=ESRI_ATTR, name="Satellite image", show=False).add_to(m)
    folium.TileLayer("OpenStreetMap", name="Street map", show=True).add_to(m)
    return m


def _keep_fitted(m: folium.Map, w: float, s: float, e: float, n: float) -> None:
    """Re-measure the map and re-fit the area whenever its frame changes size.

    Inside web-app frames the map is often created while its frame is still tiny or hidden; without this,
    Leaflet keeps the first (wrong) size and the area appears small and off-centre.
    """
    name = m.get_name()
    js = f"""
    (function() {{
      function refit() {{
        var map = window["{name}"];
        if (!map) return;
        map.invalidateSize(false);
        map.fitBounds([[{s}, {w}], [{n}, {e}]], {{padding: [20, 20], animate: false}});
      }}
      var el = document.getElementById("{name}");
      if (window.ResizeObserver && el) {{
        var last = "";
        new ResizeObserver(function() {{
          var key = el.clientWidth + "x" + el.clientHeight;
          if (key !== last && el.clientWidth > 50) {{ last = key; refit(); }}
        }}).observe(el);
      }}
      window.addEventListener("load", refit);
      setTimeout(refit, 400);
    }})();
    """
    m.get_root().script.add_child(folium.Element(js))


def _zoom_for(w: float, s: float, e: float, n: float, width_px: int = 800) -> int:
    """Web-map zoom level at which the box fills most of a map of the given width."""
    import math

    span = max(e - w, (n - s) / max(math.cos(math.radians((s + n) / 2)), 0.2), 1e-4)
    return int(max(2, min(16, math.floor(math.log2(360 * width_px / (256 * span))) - 1)))


NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "OpenSAR-Explorer/0.1 (https://github.com/Dkashkush/opensar-explorer)"  # Nominatim asks for one


def find_place(query: str, limit: int = 5, fetch=None) -> list[dict]:
    """Places matching ``query`` from OpenStreetMap (Nominatim): [{name, lat, lon, bbox (W, S, E, N)}].

    ``fetch(url, params, headers) -> parsed JSON`` can be replaced in tests. Raises OSError when the service
    cannot be reached.
    """
    query = (query or "").strip()
    if not query:
        return []
    if fetch is None:
        import requests

        def fetch(url, params, headers):
            r = requests.get(url, params=params, headers=headers, timeout=15)
            r.raise_for_status()
            return r.json()
    try:
        rows = fetch(NOMINATIM_URL, {"q": query, "format": "jsonv2", "limit": limit},
                     {"User-Agent": USER_AGENT, "Accept-Language": "en"})
    except Exception as e:  # network errors, HTTP errors, bad JSON
        raise OSError(f"place search unavailable ({type(e).__name__})") from e
    out = []
    for r in rows or []:
        try:
            s, n, w, e = (float(v) for v in r["boundingbox"])  # Nominatim order: south, north, west, east
            out.append({"name": r.get("display_name", query), "lat": float(r["lat"]), "lon": float(r["lon"]),
                        "bbox": (w, s, e, n)})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def box_around(lat: float, lon: float, place_bbox=None, max_km2: float = 900.0, default_km: float = 20.0):
    """A starting box for a found place: its own outline if that is small enough, else a box of ``default_km``
    (shrunk to fit ``max_km2``) centred on it. Returns (W, S, E, N)."""
    from sarcompare.aoi import AOI

    if place_bbox:
        w, s, e, n = place_bbox
        if e > w and n > s and 1.0 <= AOI.from_bbox(w, s, e, n).area_km2() <= max_km2:
            return round(w, 4), round(s, 4), round(e, 4), round(n, 4)
    side = min(default_km, max_km2 ** 0.5)
    dlat = side / 2 / 111.0
    dlon = side / 2 / (111.0 * max(np.cos(np.radians(lat)), 0.05))
    return round(lon - dlon, 4), round(lat - dlat, 4), round(lon + dlon, 4), round(lat + dlat, 4)


def picker_map(bbox: tuple[float, float, float, float] | None, height_hint: str = "",
               view: tuple[float, float, int] | None = None) -> folium.Map:
    """Map with a rectangle tool; shows the current area if one is set. ``view`` = (lat, lon, zoom) centres the map
    on a searched place instead."""
    if view:
        m = folium.Map(location=[view[0], view[1]], zoom_start=int(view[2]), tiles=None, control_scale=True)
        if bbox:
            w, s, e, n = bbox
            folium.Rectangle([[s, w], [n, e]], color="#1f5f8b", weight=3, fill=True, fill_opacity=0.08,
                             tooltip="Your area").add_to(m)
    elif bbox:
        w, s, e, n = bbox
        m = folium.Map(location=[(s + n) / 2, (w + e) / 2], zoom_start=_zoom_for(w, s, e, n, 600), tiles=None,
                       control_scale=True)
        folium.Rectangle([[s, w], [n, e]], color="#1f5f8b", weight=3, fill=True, fill_opacity=0.08,
                         tooltip="Your area").add_to(m)
        _keep_fitted(m, w, s, e, n)
    else:
        m = folium.Map(location=[20, 0], zoom_start=2, tiles=None, control_scale=True)
    _base(m)
    Draw(draw_options={"polyline": False, "polygon": False, "circle": False, "marker": False,
                       "circlemarker": False, "rectangle": {"shapeOptions": {"color": "#d95926"}}},
         edit_options={"edit": False, "remove": False}).add_to(m)
    folium.LayerControl(position="topright", collapsed=True).add_to(m)
    return m


def bbox_from_drawing(drawing: dict | None) -> tuple[float, float, float, float] | None:
    """Bounds (W, S, E, N) of a GeoJSON feature returned by the map's draw tool."""
    if not drawing or not drawing.get("geometry"):
        return None
    coords = np.array(drawing["geometry"]["coordinates"][0], dtype=float)
    w, s = coords.min(axis=0)
    e, n = coords.max(axis=0)
    if e - w < 1e-6 or n - s < 1e-6:
        return None
    return round(float(w), 5), round(float(s), 5), round(float(e), 5), round(float(n), 5)


def _overlay(da_m, vmin: float, vmax: float, cmap) -> tuple[str, list]:
    """Colour a rate map (m/yr) into a transparent PNG on Web Mercator; returns (data URL, lat/lon bounds)."""
    merc = (da_m * 1000).rio.reproject("EPSG:3857")
    arr = merc.values.astype("float64")
    norm = matplotlib.colors.Normalize(vmin=vmin, vmax=vmax, clip=True)
    rgba = cmap(norm(np.nan_to_num(arr)))
    rgba[..., 3] = np.where(np.isfinite(arr), 0.85, 0.0)
    buf = io.BytesIO()
    matplotlib.image.imsave(buf, rgba, format="png")
    xs, ys = merc.x.values, merc.y.values
    dx, dy = abs(xs[1] - xs[0]) / 2, abs(ys[1] - ys[0]) / 2
    tr = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    w, s = tr.transform(xs.min() - dx, ys.min() - dy)
    e, n = tr.transform(xs.max() + dx, ys.max() + dy)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(), [[s, w], [n, e]]


def results_map(result) -> folium.Map:
    c = result.comparison
    maps = [rm for rm in (result.nisar_rate, result.s1_rate) if rm is not None]
    w, s, e, n = result.config.get_aoi().bounds
    # No zoom/fade animation: when the map first renders below the visible part of the page, an unfinished
    # animation can leave overlays drawn at the wrong zoom.
    m = folium.Map(location=[(s + n) / 2, (w + e) / 2], zoom_start=_zoom_for(w, s, e, n, 1000), tiles=None,
                   control_scale=True, zoom_animation=False, fade_animation=False, marker_zoom_animation=False)
    _base(m)
    vals = np.concatenate([x[np.isfinite(x)] for x in (rm.rate.values * 1000 for rm in maps)])
    lim = max(float(np.nanpercentile(np.abs(vals), 98)) if vals.size else 10.0, 5.0)
    cmap = _diverging(THEMES["light"])
    band = {"NISAR": "L-band", "Sentinel-1": "C-band"}
    layers = [(f"{rm.sensor} ({band.get(rm.sensor, 'radar')})", rm.rate, k == 0) for k, rm in enumerate(maps)]
    if c.difference is not None and len(maps) > 1:
        layers.append((f"Difference {maps[0].sensor} − {maps[1].sensor}", c.difference, False))
    for name, da, show in layers:
        url, bounds = _overlay(da, -lim, lim, cmap)
        folium.raster_layers.ImageOverlay(url, bounds=bounds, name=name, show=show, interactive=False,
                                          zindex=2).add_to(m)
    folium.Rectangle([[s, w], [n, e]], color="#1f2933", weight=2, fill=False, tooltip="Analysis area").add_to(m)

    moving = folium.FeatureGroup(name="Moving areas", show=True).add_to(m)
    for h in c.hotspots:
        verb = "Sinking" if h.peak_mm_yr < 0 else "Rising"
        seen = {"yes": "Seen by both satellites", "no data": "Other satellite has no data here",
                "no": "Not seen by the other satellite",
                "single": "Only one satellite has data for this area"}[h.seen_by_other]
        popup = (f"<b>{verb} area</b><br>Detected by {html.escape(h.sensor)}<br>Peak {h.peak_mm_yr:+.0f} mm/yr"
                 f"<br>Area {h.area_km2:.1f} km²<br>{seen}")
        colour = "#b3261e" if h.peak_mm_yr < 0 else "#1f5f8b"
        folium.CircleMarker([h.lat, h.lon], radius=9, color="#ffffff", weight=2, fill=True, fill_color=colour,
                            fill_opacity=0.95, popup=folium.Popup(popup, max_width=260),
                            tooltip=f"{verb}: {h.peak_mm_yr:+.0f} mm/yr").add_to(moving)
    g = getattr(result, "gnss", None)
    if g is not None and g.stations:
        layer = folium.FeatureGroup(name="GNSS stations", show=True).add_to(m)
        for st in g.stations:
            vals = "".join(f"<br>{k}: {v:+.1f} mm/yr" for k, v in st.insar.items() if np.isfinite(v))
            popup = (f"<b>GNSS {html.escape(st.site)}</b><br>Vertical {st.up_mm_yr:+.1f} ± "
                     f"{st.up_sigma_mm_yr:.1f} mm/yr{vals}<br><small>InSAR values before offset</small>")
            folium.CircleMarker([st.lat, st.lon], radius=7, color="#111", weight=2, fill=True, fill_color="#fff",
                                fill_opacity=1, popup=folium.Popup(popup, max_width=260),
                                tooltip=f"GNSS {st.site}").add_to(layer)
    lit = getattr(result, "literature", None) or []
    if lit:
        layer = folium.FeatureGroup(name="Published studies", show=True).add_to(m)
        for chk in lit:
            pub = (f"{chk.published_min:+.0f} to {chk.published_max:+.0f}" if chk.published_min is not None
                   and chk.published_max is not None else f"{chk.published_mm_yr:+.0f}")
            ours = "".join(f"<br>{k}: {v:+.0f} mm/yr ({chk.verdict.get(k, '')})" for k, v in chk.insar.items()
                           if np.isfinite(v))
            popup = (f"<b>{html.escape(chk.citation)}</b><br>{html.escape(chk.place)} {html.escape(chk.period)}"
                     f"<br>Published: {pub} mm/yr{ours}")
            ok = "consistent" in chk.verdict.values() and "differs" not in chk.verdict.values()
            # plain circles: icon markers need an icon font that did not load on the hosted app (white boxes)
            folium.CircleMarker([chk.lat, chk.lon], radius=8, color="#1f2933", weight=3, fill=True,
                                fill_color="#2e7d32" if ok else "#c77700", fill_opacity=0.95,
                                popup=folium.Popup(popup, max_width=280),
                                tooltip=f"Published: {html.escape(chk.citation)}").add_to(layer)
    legend = LinearColormap(DIVERGING, vmin=-lim, vmax=lim)
    legend.caption = "Ground motion, mm/yr (red = sinking, blue = rising)"
    legend.add_to(m)
    folium.LayerControl(position="topright", collapsed=False).add_to(m)
    _keep_fitted(m, w, s, e, n)
    return m
