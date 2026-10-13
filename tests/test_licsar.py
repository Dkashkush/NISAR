"""LiCSAR (COMET) Sentinel-1 interferograms: discovery against a fake server, reading, and an end-to-end run."""

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from sarcompare import licsar as L
from sarcompare.aoi import AOI
from sarcompare.demo import DEMO_BBOX, Scene
from sarcompare.products import WAVELENGTH_S1_C

INC = 39.0


def _write(path: Path, arr, lon0, lat0, step, dtype):
    import rasterio
    from rasterio.transform import from_origin

    with rasterio.open(path, "w", driver="GTiff", width=arr.shape[1], height=arr.shape[0], count=1, dtype=dtype,
                       crs="EPSG:4326", transform=from_origin(lon0, lat0, step, step)) as dst:
        dst.write(arr.astype(dtype), 1)


def licsar_pair(folder: Path, scene: Scene, d1: date, d2: date, frame="035D_05397_131013", noise=0.0, seed=0):
    """Files laid out as LiCSAR's: <frame>/<pair>/<pair>.geo.unw.tif and .geo.cc.tif, plus <frame>.geo.U.tif."""
    from pyproj import Transformer

    step = 0.001
    w, s, e, n = DEMO_BBOX
    lons = np.arange(w - 0.05, e + 0.05, step) + step / 2
    lats = np.arange(n + 0.05, s - 0.05, -step) - step / 2
    LON, LAT = np.meshgrid(lons, lats)
    X, Y = Transformer.from_crs("EPSG:4326", scene.crs, always_xy=True).transform(LON, LAT)
    years = (d2 - d1).days / 365.25
    los = scene.vertical_rate(X, Y) * years * np.cos(np.radians(INC))  # toward the satellite = positive
    los += np.random.default_rng(seed).normal(0, noise, los.shape)
    phase = -los * 4 * np.pi / WAVELENGTH_S1_C  # GAMMA/LiCSAR: positive phase = away from the satellite
    cc = np.full(los.shape, 200.0)
    phase[:20, :20], cc[:20, :20] = 0.0, 0.0  # a masked corner, as LiCSAR stores it
    pair = f"{d1:%Y%m%d}_{d2:%Y%m%d}"
    out = folder / frame / pair
    out.mkdir(parents=True, exist_ok=True)
    _write(out / f"{pair}.geo.unw.tif", phase, lons[0] - step / 2, lats[0] + step / 2, step, "float32")
    _write(out / f"{pair}.geo.cc.tif", cc, lons[0] - step / 2, lats[0] + step / 2, step, "uint8")
    up = folder / frame / f"{frame}.geo.U.tif"
    if not up.exists():
        _write(up, np.full(los.shape, np.cos(np.radians(INC))), lons[0] - step / 2, lats[0] + step / 2, step,
               "float32")
    return out / f"{pair}.geo.unw.tif", out / f"{pair}.geo.cc.tif", up


def test_read_licsar_sign_scale_mask(tmp_path):
    from sarcompare.readers.licsar import read_licsar

    aoi = AOI.from_bbox(*DEMO_BBOX)
    scene = Scene(aoi)
    d1, d2 = date(2026, 7, 20), date(2026, 8, 13)
    unw, cc, up = licsar_pair(tmp_path, scene, d1, d2)
    ifg = read_licsar(unw, aoi, cc, up)
    assert (ifg.date1, ifg.date2) == (d1, d2) and abs(ifg.incidence_deg - INC) < 0.01
    from pyproj import Transformer

    LON, LAT = np.meshgrid(ifg.los.x.values, ifg.los.y.values)
    X, Y = Transformer.from_crs("EPSG:4326", scene.crs, always_xy=True).transform(LON, LAT)
    truth = scene.vertical_rate(X, Y) * (d2 - d1).days / 365.25 * np.cos(np.radians(INC))
    ok = np.isfinite(ifg.los.values)
    assert np.nanmin(ifg.los.values) < 0  # subsidence comes out negative
    np.testing.assert_allclose(ifg.los.values[ok], truth[ok], atol=1e-6)
    assert np.nanmax(ifg.coherence.values) == pytest.approx(200 / 255)


class FakeServer:
    """Answers LiCSAR's index pages, frame outlines and the post-migration link pages."""

    def __init__(self):
        b = L.BASE
        poly = "\n".join(f"{x} {y}" for x, y in [(77.0, 29.5), (79.0, 29.5), (79.0, 31.0), (77.0, 31.0)])
        far = "\n".join(f"{x} {y}" for x, y in [(10, 10), (11, 10), (11, 11), (10, 11)])
        pairs = ["20260708_20260720", "20260720_20260801", "20260720_20260813", "20260801_20260813",
                 "20240101_20240113"]
        self.pages = {
            f"{b}/35/": self._index(["035A_04908_131313/", "035D_05397_131013/", "035D_05397_131013.hires.clipped/",
                                     "035D_09999_131313/"]),
            f"{b}/35/035D_05397_131013/metadata/035D_05397_131013-poly.txt": poly,
            f"{b}/35/035D_09999_131313/metadata/035D_09999_131313-poly.txt": far,
            f"{b}/35/035D_05397_131013/interferograms/": self._index([p + "/" for p in pairs]),
            # the old address of a pair (no trailing slash) links to the moved files
            f"{b}/35/035D_05397_131013/interferograms/20260720_20260801":
                "<a href='" + f"{L.PUBLIC}/35/035D_05397_131013/interferograms/20260720_20260801/"
                "20260720_20260801.geo.unw.tif'>x</a>",
        }
        self.heads = set()

    @staticmethod
    def _index(names):
        return "<html><pre>" + "".join(f'<a href="{n}">{n}</a>\n' for n in ["../"] + names) + "</pre></html>"

    def get(self, url, timeout=60):
        class R:
            pass
        r = R()
        r.status_code, r.text = (200, self.pages[url]) if url in self.pages else (404, "")
        return r

    def head(self, url, timeout=60, allow_redirects=True):
        class R:
            pass
        r = R()
        r.status_code = 200 if url in self.heads else 404
        return r


@pytest.fixture
def server(monkeypatch):
    srv = FakeServer()
    monkeypatch.setattr(L, "_get", srv.get)
    monkeypatch.setattr(L, "_get_head", srv.head)
    for f in (L._index, L.outline, L.resolve):
        f.cache_clear()
    yield srv
    for f in (L._index, L.outline, L.resolve):
        f.cache_clear()


def test_search_finds_frames_and_pairs(server):
    aoi = AOI.from_bbox(*DEMO_BBOX)
    products, notes = L.search(aoi, date(2026, 7, 1), date(2026, 9, 1), tracks={35: "D"})
    assert {p.frame for p in products} == {"035D_05397_131013"}  # the far frame and the variant are skipped
    assert len(products) == 4 and all(p.flight_direction == "DESCENDING" and p.track == 35 for p in products)
    assert products[0].url.startswith(L.PUBLIC) and products[0].url.endswith(".geo.unw.tif")
    assert products[0].extra["cover"].area == pytest.approx(aoi.geometry.area)
    assert "035D_05397_131013 (5 pairs, covering 100%)" in notes[0].text
    assert L.search(aoi, tracks={35: "D"}, flight_direction="ASCENDING")[0] == []


def test_resolve_uses_the_new_storage_or_the_link_page(server):
    url = L.file_url(35, "035D_05397_131013", date(2026, 7, 8), date(2026, 7, 20), "geo.unw.tif")
    server.heads.add(url)
    assert L.resolve(url) == url
    moved = L.file_url(35, "035D_05397_131013", date(2026, 7, 20), date(2026, 8, 1), "geo.unw.tif")
    assert L.resolve(moved) == moved  # found through the link page at the old address
    gone = L.file_url(35, "035D_05397_131013", date(2026, 8, 1), date(2026, 8, 13), "geo.unw.tif")
    assert L.resolve(gone) is None


def test_pipeline_with_licsar_sentinel1(demo_cfg, tmp_path, monkeypatch):
    """NISAR from the demo against LiCSAR-format Sentinel-1 pairs: read in place, right sign and scale."""
    from dataclasses import replace

    import sarcompare.licsar as LL
    from sarcompare.pipeline import Pipeline
    from sarcompare.products import SENTINEL1, Product

    aoi = AOI.from_bbox(*DEMO_BBOX)
    scene = Scene(aoi)
    start = date(2025, 10, 4)
    products = []
    for k in range(6):
        d1, d2 = start + timedelta(days=48 * k), start + timedelta(days=48 * (k + 1))
        unw, _, _ = licsar_pair(tmp_path, scene, d1, d2, noise=0.002, seed=k)
        products.append(Product(SENTINEL1, "LICSAR", f"035D_05397_131013_{unw.parent.name}", url=None,
                                date1=d1, date2=d2, flight_direction="DESCENDING", track=35,
                                frame="035D_05397_131013", local_path=unw,
                                extra={"licsar": (35, "035D_05397_131013")}))
    monkeypatch.setattr(LL, "search", lambda *a, **k: (products, []))
    cfg = replace(demo_cfg, s1_source="LICSAR", s1_dir=None, name="licsar", output_dir=str(tmp_path / "out"),
                  published_maps=[], published_values=[])
    p = Pipeline(cfg, on_event=lambda e: None)
    p.search()
    assert p.result.s1_products and all(q.product_type == "LICSAR" for q in p.result.s1_products)
    p.load_and_stack()
    c = p.reference_and_compare()
    assert p.result.s1_rate is not None and c.pearson_r > 0.8 and 0.8 < c.slope < 1.25  # sign and scale agree
