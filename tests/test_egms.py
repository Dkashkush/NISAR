# Copyright (c) 2026 Deepa Kumari. All rights reserved. See LICENSE.
"""Automatic EGMS Ortho download (Europe), against a fake EGMS server."""

import io
import zipfile

import numpy as np
import pytest

from sarcompare import egms as G
from sarcompare.aoi import AOI

VENICE = AOI.from_bbox(12.31, 45.38, 12.42, 45.48)  # inside one 100 km tile


def _tile_zip(name: str, value: float) -> bytes:
    import rasterio
    from rasterio.transform import from_origin

    col, row = int(name.split("_")[2][1:3]), int(name.split("_")[2][4:6])
    mem = io.BytesIO()
    with rasterio.open(mem, "w", driver="GTiff", width=100, height=100, count=1, dtype="float32", crs="EPSG:3035",
                       transform=from_origin(col * 100_000, (row + 1) * 100_000, 1000, 1000)) as dst:
        dst.write(np.full((1, 100, 100), value, "float32"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{name}.tiff", mem.getvalue())
    return buf.getvalue()


class Fake:
    def __init__(self, have: dict[str, float], key="k123"):
        self.have, self.key, self.calls = have, key, []

    def __call__(self, url, params):
        self.calls.append(url.rsplit("/", 1)[-1])

        class R:
            pass
        r = R()
        name = url.rsplit("/", 1)[-1][:-4]
        if params.get("id") != self.key:
            r.status_code, r.content = 401, b"You need a valid download token to download data."
        elif name in self.have:
            r.status_code, r.content = 200, _tile_zip(name, self.have[name])
        else:
            r.status_code, r.content = 404, b""
        return r


def test_tiles_on_the_european_grid():
    assert G.tiles(VENICE) == ["E45N24"]
    assert G.in_europe(VENICE) and not G.in_europe(AOI.from_bbox(51.1, 35.45, 51.45, 35.72))  # Tehran
    wide = AOI.from_bbox(12.31, 45.30, 13.3, 45.6)  # crosses a 100 km tile edge (northing 2,500 km)
    assert G.tiles(wide) == ["E45N24", "E45N25"]


def test_download_newest_release_and_refused_key(tmp_path):
    fake = Fake({"EGMS_L3_E45N24_100km_U_2019_2023_1": -3.0, "EGMS_L3_E45N24_100km_E_2019_2023_1": 1.0})
    maps, notes = G.download(VENICE, " k123 ", tmp_path, ("U", "E"), fetch=fake)
    assert set(maps) == {"U", "E"} and maps["U"].name.startswith("EGMS_L3_E45N24_100km_U_2019_2023_1")
    assert fake.calls[0].startswith("EGMS_L3_E45N24_100km_U_2020_2024_1")  # tried the newest release first
    assert "2019-2023" in notes[0].text
    from sarcompare.validate import egms_defaults

    assert egms_defaults(maps["U"])["component"] == "vertical"
    assert egms_defaults(maps["E"])["component"] == "east-west"
    fake.calls.clear()
    G.download(VENICE, "k123", tmp_path, ("U",), fetch=fake)
    assert not [c for c in fake.calls if "2019_2023" in c]  # reused from disk
    with pytest.raises(G.EGMSError, match="did not accept"):
        G.download(VENICE, "wrong", tmp_path / "other", fetch=fake)
    with pytest.raises(G.EGMSError, match="No EGMS Ortho tile"):
        G.download(AOI.from_bbox(-20.0, 30.0, -19.9, 30.1), "k123", tmp_path / "sea", fetch=fake)


def test_two_tiles_are_joined(tmp_path):
    import rasterio

    wide = AOI.from_bbox(12.31, 45.30, 13.3, 45.6)
    fake = Fake({"EGMS_L3_E45N24_100km_U_2019_2023_1": -2.0, "EGMS_L3_E45N25_100km_U_2019_2023_1": -4.0})
    maps, _ = G.download(wide, "k123", tmp_path, fetch=fake)
    with rasterio.open(maps["U"]) as src:
        assert (src.width, src.height) == (100, 200) and set(np.unique(src.read(1))) == {-2.0, -4.0}
    assert "E45N24+E45N25" in maps["U"].name


def test_pipeline_downloads_egms_for_europe(tmp_path, monkeypatch):
    from sarcompare.config import Config
    from sarcompare.pipeline import Pipeline

    got = {}

    def fake_download(aoi, key, folder, comps):
        got["key"], got["comps"] = key, comps
        return {"U": tmp_path / "EGMS_L3_E45N24_100km_U_2019_2023_1.tif"}, []

    monkeypatch.setattr(G, "download", fake_download)
    cfg = Config(aoi=list(VENICE.bounds), egms_token="k123", data_dir=str(tmp_path))
    assert "egms_token" not in cfg.to_dict() and "k123" not in repr(cfg)  # the key is never written out
    p = Pipeline(cfg, on_event=lambda e: None)
    p._egms_download()
    assert got == {"key": "k123", "comps": ("U",)}
    assert cfg.published_maps[-1]["path"].endswith("EGMS_L3_E45N24_100km_U_2019_2023_1.tif")
    p._egms_download()  # EGMS maps already present: not downloaded twice
    assert len(cfg.published_maps) == 1
