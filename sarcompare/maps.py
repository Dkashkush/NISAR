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
    "Mexico City, Mexico: fast city subsidence (groundwater)": (-99.25, 19.25, -98.95, 19.55),
    "Jakarta, Indonesia: coastal city subsidence": (106.70, -6.28, 107.00, -6.05),
    "Joshimath, Uttarakhand, India: Himalayan town subsidence": (79.45, 30.48, 79.65, 30.62),
    "Delhi NCR, India: groundwater-related subsidence": (76.95, 28.45, 77.15, 28.65),
    "Tehran plain, Iran: aquifer subsidence": (51.00, 35.40, 51.50, 35.75),
    "Central Valley, California, USA: agricultural subsidence": (-119.80, 35.90, -119.40, 36.20),
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


def picker_map(bbox: tuple[float, float, float, float] | None, height_hint: str = "") -> folium.Map:
    """Map with a rectangle tool; shows the current area if one is set."""
    if bbox:
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
    a, b, c = result.nisar_rate, result.s1_rate, result.comparison
    w, s, e, n = result.config.get_aoi().bounds
    # No zoom/fade animation: when the map first renders below the visible part of the page, an unfinished
    # animation can leave overlays drawn at the wrong zoom.
    m = folium.Map(location=[(s + n) / 2, (w + e) / 2], zoom_start=_zoom_for(w, s, e, n, 1000), tiles=None,
                   control_scale=True, zoom_animation=False, fade_animation=False, marker_zoom_animation=False)
    _base(m)
    vals = np.concatenate([x[np.isfinite(x)] for x in (a.rate.values * 1000, b.rate.values * 1000)])
    lim = max(float(np.nanpercentile(np.abs(vals), 98)) if vals.size else 10.0, 5.0)
    cmap = _diverging(THEMES["light"])
    for name, da, show in ((f"{a.sensor} (L-band)", a.rate, True), (f"{b.sensor} (C-band)", b.rate, False),
                           (f"Difference {a.sensor} − {b.sensor}", c.difference, False)):
        url, bounds = _overlay(da, -lim, lim, cmap)
        folium.raster_layers.ImageOverlay(url, bounds=bounds, name=name, show=show, interactive=False,
                                          zindex=2).add_to(m)
    folium.Rectangle([[s, w], [n, e]], color="#1f2933", weight=2, fill=False, tooltip="Analysis area").add_to(m)

    moving = folium.FeatureGroup(name="Moving areas", show=True).add_to(m)
    for h in c.hotspots:
        verb = "Sinking" if h.peak_mm_yr < 0 else "Rising"
        seen = {"yes": "Seen by both satellites", "no data": "Other satellite has no data here",
                "no": "Not seen by the other satellite"}[h.seen_by_other]
        popup = (f"<b>{verb} area</b><br>Detected by {html.escape(h.sensor)}<br>Peak {h.peak_mm_yr:+.0f} mm/yr"
                 f"<br>Area {h.area_km2:.1f} km²<br>{seen}")
        folium.Marker([h.lat, h.lon], popup=folium.Popup(popup, max_width=260), tooltip=f"{verb}: {h.peak_mm_yr:+.0f} mm/yr",
                      icon=folium.Icon(color="red" if h.peak_mm_yr < 0 else "blue", icon="info-sign")).add_to(moving)
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
    legend = LinearColormap(DIVERGING, vmin=-lim, vmax=lim)
    legend.caption = "Ground motion, mm/yr (red = sinking, blue = rising)"
    legend.add_to(m)
    folium.LayerControl(position="topright", collapsed=False).add_to(m)
    _keep_fitted(m, w, s, e, n)
    return m
