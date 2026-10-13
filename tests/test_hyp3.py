"""Ordering Sentinel-1 interferograms from ASF HyP3 (with a fake HyP3 API) and reading the results."""

import io
import zipfile
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from sarcompare import hyp3 as H
from sarcompare.aoi import AOI
from sarcompare.demo import DEMO_BBOX, Scene

AOI_ = AOI.from_bbox(51.10, 35.45, 51.45, 35.72, name="tehran")


def _slc(platform: str, day: date, n: int = 0) -> str:
    return f"{platform}_IW_SLC__1SDV_{day:%Y%m%d}T025500_{day:%Y%m%d}T025527_06{n:04d}_07A1B2_ABCD"


def _scenes():
    out = []
    first = date(2025, 10, 4)
    for k in range(30):  # Sentinel-1A, ascending track 28, every 12 days, covering the area
        d = first + timedelta(days=12 * k)
        out.append(H._Scene(_slc("S1A", d, k), d, "ASCENDING", 28, "Sentinel-1A", 0.97))
        # a second, partial image on the same date (the next frame): the better one must be used
        out.append(H._Scene(_slc("S1A", d, 100 + k), d, "ASCENDING", 28, "Sentinel-1A", 0.20))
    for k in range(30):  # a descending track that only clips the area
        d = first + timedelta(days=12 * k + 5)
        out.append(H._Scene(_slc("S1A", d, 200 + k), d, "DESCENDING", 108, "Sentinel-1A", 0.15))
    return out


def test_plan_matches_the_nisar_dates():
    nisar_dates = [date(2025, 10, 30) + timedelta(days=12 * k) for k in range(5)]  # 4 NISAR pairs
    pairs, notes = H.plan_pairs(AOI_, target={"ASCENDING": nisar_dates}, max_pairs=6, scenes=_scenes())
    assert len(pairs) == 4
    assert all(p.direction == "ASCENDING" and p.path == 28 and p.cover > 0.9 for p in pairs)
    assert all(p.date2 == q.date1 for p, q in zip(pairs, pairs[1:]))  # gapless chain
    assert all(abs((p.date1 - d).days) <= 6 for p, d in zip(pairs, nisar_dates))  # same dates as NISAR
    assert all(p.span_days == 12 for p in pairs)
    job = pairs[0].job(H.job_name(AOI_))
    assert job["job_type"] == "INSAR_GAMMA" and job["job_parameters"]["include_look_vectors"]
    assert job["job_parameters"]["granules"] == [pairs[0].reference, pairs[0].secondary]
    assert any("matching the NISAR dates" in n.text for n in notes)
    # the descending track covers too little of the area to be used, even when asked for
    pairs, notes = H.plan_pairs(AOI_, directions=["DESCENDING"], max_pairs=4, scenes=_scenes())
    assert not pairs and any("descending" in n.text for n in notes)


def test_plan_without_nisar_spreads_pairs_over_the_period():
    pairs, _ = H.plan_pairs(AOI_, date(2025, 10, 1), date(2026, 9, 30), max_pairs=6, scenes=_scenes())
    assert len(pairs) == 6 and pairs[0].date1 <= date(2025, 10, 16) and pairs[-1].date2 >= date(2026, 8, 1)
    assert all(p.date2 == q.date1 for p, q in zip(pairs, pairs[1:]))


class FakeResponse:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeHyP3:
    """Just enough of the HyP3 API: /user, /costs, /jobs (GET by name, POST)."""

    def __init__(self, credits=500.0, spec=""):
        self.headers, self.jobs, self.credits, self.spec, self.posts = {}, [], credits, spec, []

    def get(self, url, params=None, timeout=None):
        path = url.split("hyp3-api.asf.alaska.edu", 1)[-1]
        if path == "/user":
            names = sorted({j["name"] for j in self.jobs})
            return FakeResponse(200, {"user_id": "someone", "remaining_credits": self.credits, "job_names": names})
        if path == "/costs":
            return FakeResponse(200, {"INSAR_GAMMA": {"cost_parameter": "looks",
                                                      "cost_table": {"10x2": 15.0, "20x4": 10.0}}})
        if path == "/jobs":
            return FakeResponse(200, {"jobs": [j for j in self.jobs if j["name"] == (params or {}).get("name")]})
        if path.startswith("/openapi"):
            return FakeResponse(200 if self.spec else 404, text=self.spec)
        return FakeResponse(404, {"detail": "not found"})

    def post(self, url, json=None, timeout=None):
        self.posts.append(json)
        out = []
        for k, job in enumerate(json["jobs"]):
            out.append({**job, "job_id": f"id{len(self.jobs) + k}", "status_code": "PENDING",
                        "request_time": "2026-10-13T12:00:00Z"})
        if not json.get("validate_only"):
            self.jobs += out
            self.credits -= 10.0 * len(out)
        return FakeResponse(200, {"jobs": out})


def _client(fake):
    return H.HyP3("eyJ" + "x" * 400 + ".a.b", session=fake)


def test_order_then_find_the_finished_results(monkeypatch):
    fake = FakeHyP3()
    client = _client(fake)
    assert fake.headers["Authorization"].startswith("Bearer eyJ")  # the token goes only to the HyP3 API session
    pairs, _ = H.plan_pairs(AOI_, target={"ASCENDING": [date(2025, 10, 28) + timedelta(days=12 * k)
                                                        for k in range(4)]}, scenes=_scenes())
    res = H.order(client, AOI_, pairs)
    assert len(res.submitted) == 3 and res.cost == 30.0 and res.remaining == 500.0
    assert all(j["name"] == H.job_name(AOI_) for j in fake.jobs)
    # ordering the same pairs again sends nothing
    again = H.order(client, AOI_, pairs)
    assert again.submitted == [] and again.skipped == 3 and len(fake.posts) == 1

    # still processing: nothing to read yet, and the status says so
    products, counts, notes = H.find_products(client, AOI.from_bbox(51.2, 35.5, 51.3, 35.6))  # a redrawn box
    assert products == [] and counts == {"PENDING": 3}
    assert "Come back later" in notes[0].text

    # finished (one already deleted by HyP3): the others become Sentinel-1 products on public links
    future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    for k, j in enumerate(fake.jobs):
        j.update(status_code="SUCCEEDED", expiration_time=past if k == 0 else future,
                 files=[{"url": f"https://d3gm2hf49xd6jj.cloudfront.net/{j['job_id']}/S1AA_x{k}.zip",
                         "filename": f"S1AA_x{k}.zip", "size": 250_000_000}])
    monkeypatch.setattr(H, "_lookup_geometry", lambda names: {n: ("ASCENDING", 28) for n in names})
    products, counts, _ = H.find_products(client, AOI_)
    assert counts == {"EXPIRED": 1, "SUCCEEDED": 2}
    assert len(products) == 2 and all(p.product_type == "HYP3_INSAR" and p.flight_direction == "ASCENDING"
                                      and p.track == 28 and p.date1 < p.date2 for p in products)
    assert products[0].date1 == pairs[1].date1

    # an area elsewhere does not pick these up
    assert H.find_products(client, AOI.from_bbox(10, 10, 10.2, 10.2))[0] == []


def test_order_checks_credits_and_check_only_needs_the_spec():
    fake = FakeHyP3(credits=15.0)
    client = _client(fake)
    pairs, _ = H.plan_pairs(AOI_, target={"ASCENDING": [date(2025, 10, 28) + timedelta(days=12 * k)
                                                        for k in range(4)]}, scenes=_scenes())
    with pytest.raises(H.HyP3Error, match="credits"):
        H.order(client, AOI_, pairs)
    assert fake.posts == []
    fake.credits = 500.0
    with pytest.raises(H.HyP3Error, match="nothing was sent"):  # no check-only mode described: never post
        H.order(client, AOI_, pairs, validate_only=True)
    assert fake.posts == []
    fake.spec = '{"properties": {"validate_only": {"type": "boolean"}}}'
    res = H.order(client, AOI_, pairs, validate_only=True)
    assert fake.posts[-1]["validate_only"] is True and fake.jobs == [] and len(res.submitted) == 3


def _hyp3_zip(scene: Scene, d1: date, d2: date) -> tuple[bytes, str]:
    """A HyP3 INSAR_GAMMA-style zip: <name>/<name>_unw_phase.tif, _corr.tif, _lv_theta.tif."""
    import rasterio
    from rasterio.transform import from_origin

    from sarcompare.products import WAVELENGTH_S1_C

    name = f"S1AA_{d1:%Y%m%d}T025500_{d2:%Y%m%d}T025500_VVP012_INT80_G_ueF_1A2B"
    res = 80.0
    xs = np.arange(scene.w - 2000, scene.e + 2000, res)
    ys = np.arange(scene.n + 2000, scene.s - 2000, -res)
    X, Y = np.meshgrid(xs, ys)
    inc = np.radians(34.0)
    years = (d2 - d1).days / 365.25
    los = scene.vertical_rate(X, Y) * years * np.cos(inc)  # toward the satellite = positive
    layers = {"unw_phase": -los * 4 * np.pi / WAVELENGTH_S1_C,  # HyP3: positive phase = away from the satellite
              "corr": np.full(X.shape, 0.8), "lv_theta": np.full(X.shape, np.pi / 2 - inc)}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for suffix, arr in layers.items():
            mem = io.BytesIO()
            with rasterio.open(mem, "w", driver="GTiff", width=arr.shape[1], height=arr.shape[0], count=1,
                               dtype="float32", crs=scene.crs, transform=from_origin(xs[0] - res / 2, ys[0] + res / 2,
                                                                                    res, res)) as dst:
                dst.write(arr.astype("float32"), 1)
            z.writestr(f"{name}/{name}_{suffix}.tif", mem.getvalue())
        z.writestr(f"{name}/{name}_dem.tif", b"\0" * 1000)  # a layer the reader must not need
    return buf.getvalue(), name


def test_hyp3_zip_read_remotely_or_locally(tmp_path):
    from sarcompare.readers.sentinel1 import read_hyp3_insar

    aoi = AOI.from_bbox(*DEMO_BBOX)
    scene = Scene(aoi)
    d1, d2 = date(2025, 11, 9), date(2025, 12, 3)
    data, name = _hyp3_zip(scene, d1, d2)

    ifg = read_hyp3_insar(io.BytesIO(data), aoi)  # a file object, as open_remote gives for a remote zip
    (tmp_path / f"{name}.zip").write_bytes(data)
    local = read_hyp3_insar(tmp_path / f"{name}.zip", aoi)
    assert (ifg.date1, ifg.date2, ifg.name) == (d1, d2, name)
    assert abs(ifg.incidence_deg - 34.0) < 0.01
    np.testing.assert_allclose(ifg.los.values, local.los.values, equal_nan=True)
    X, Y = np.meshgrid(ifg.los.x.values, ifg.los.y.values)
    truth = scene.vertical_rate(X, Y) * (d2 - d1).days / 365.25 * np.cos(np.radians(34.0))
    assert np.nanmin(ifg.los.values) < 0  # subsidence comes out negative (toward-positive convention)
    np.testing.assert_allclose(ifg.los.values, truth, atol=1e-5)


def test_pipeline_reads_hyp3_results_without_the_token(monkeypatch, tmp_path):
    """HyP3 result links are public: they are read with a session that carries no Earthdata token."""
    from contextlib import contextmanager

    import sarcompare.remote as R
    from sarcompare.config import Config
    from sarcompare.pipeline import Pipeline
    from sarcompare.products import SENTINEL1, Product

    aoi = AOI.from_bbox(*DEMO_BBOX)
    data, name = _hyp3_zip(Scene(aoi), date(2025, 11, 9), date(2025, 12, 3))
    seen = {}

    @contextmanager
    def fake_open_remote(url, session, **kw):
        seen["auth"] = session.headers.get("Authorization")
        yield io.BytesIO(data)

    monkeypatch.setattr(R, "open_remote", fake_open_remote)
    cfg = Config(name="t", aoi=list(DEMO_BBOX), earthdata_token="eyJ" + "x" * 400 + ".a.b",
                 output_dir=str(tmp_path))
    p = Pipeline(cfg, on_event=lambda e: None)
    prod = Product(SENTINEL1, "HYP3_INSAR", name, url=f"https://d3gm2hf49xd6jj.cloudfront.net/x/{name}.zip",
                   date1=date(2025, 11, 9), date2=date(2025, 12, 3))
    ifg = p._read(prod)
    assert seen["auth"] is None and ifg.name == name


def test_auto_source_uses_hyp3_orders_when_nothing_is_ready_made(monkeypatch, tmp_path):
    import sarcompare.pipeline as P
    from sarcompare.config import Config
    from sarcompare.products import NISAR, SENTINEL1, Product

    def nisar(k, d):
        return Product(NISAR, "GUNW", f"N{k}", url=f"https://x/N{k}.h5", date1=d, date2=d + timedelta(days=12),
                       flight_direction="ASCENDING", track=19)

    start = date(2025, 11, 9)
    monkeypatch.setattr(P, "search_nisar", lambda *a, **k: [nisar(i, start + timedelta(days=12 * i))
                                                            for i in range(3)])
    monkeypatch.setattr(P, "search_sentinel1", lambda *a, **k: [])  # no ARIA here
    hyp3 = [Product(SENTINEL1, "HYP3_INSAR", f"S1AA_{i}", url=f"https://d/x{i}.zip",
                    date1=start + timedelta(days=12 * i + 1), date2=start + timedelta(days=12 * i + 13),
                    flight_direction="ASCENDING", track=28) for i in range(3)]
    monkeypatch.setattr(P.Pipeline, "_hyp3_search", lambda self: hyp3)
    cfg = Config(name="t", aoi=[51.10, 35.45, 51.45, 35.72], output_dir=str(tmp_path))
    monkeypatch.delenv("EARTHDATA_TOKEN", raising=False)
    assert "HYP3" not in P.Pipeline(cfg, on_event=lambda e: None)._s1_sources()  # no token: not checked
    cfg.earthdata_token = "eyJ" + "x" * 400 + ".a.b"
    p = P.Pipeline(cfg, on_event=lambda e: None)
    assert p._s1_sources()[-1] == "HYP3"
    nisar_sel, s1_sel = p.search()
    assert len(nisar_sel) == 3 and len(s1_sel) == 3 and all(q.product_type == "HYP3_INSAR" for q in s1_sel)
    assert any("ASF HyP3" in n.text for n in p.result.notes)


def test_access_and_direction_fallback():
    with pytest.raises(H.HyP3Error, match="apply once"):
        H.access_problem({"application_status": "NOT_STARTED"})
    H.access_problem({"application_status": "APPROVED"})
    H.access_problem({})  # older API without the field: no complaint
    # NISAR looks from the descending side, but Sentinel-1 covers the area only from the ascending side
    nisar = [date(2025, 10, 28) + timedelta(days=12 * k) for k in range(3)]
    pairs, notes = H.plan_pairs(AOI_, target={"DESCENDING": nisar}, scenes=_scenes())
    assert len(pairs) == 2 and all(p.direction == "ASCENDING" for p in pairs)
    assert any("instead" in n.text for n in notes)
