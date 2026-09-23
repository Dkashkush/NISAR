"""End-to-end workflow: search → download → load → stack → reference → compare → report.

Each stage reports progress and interpretation notes through ``on_event`` so a CLI or web UI can
show results as they come in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import plots
from .compare import Comparison, compare
from .config import Config
from .download import download_products
from .interpret import comparison_notes, gnss_notes, pair_notes, published_notes, stack_notes
from .notes import INFO, WARN, Note
from .processing import (RateMap, Reference, aoi_mask, apply_reference, choose_reference, common_grid,
                         reference_notes, stack_rates, to_vertical)
from .products import NISAR, SENTINEL1, Product
from .readers import read_product
from .search import overlap_notes, scan_local, search_nisar, search_sentinel1, select_pairs


@dataclass
class Event:
    stage: str
    message: str
    notes: list[Note] = field(default_factory=list)
    progress: float | None = None  # 0..1 within the stage


@dataclass
class RunResult:
    config: Config
    nisar_products: list[Product] = field(default_factory=list)
    s1_products: list[Product] = field(default_factory=list)
    nisar_rate: RateMap | None = None
    s1_rate: RateMap | None = None
    reference: Reference | None = None
    comparison: Comparison | None = None
    inside: np.ndarray | None = None
    notes: list[Note] = field(default_factory=list)
    figures: dict[str, dict[str, bytes]] = field(default_factory=dict)  # name -> {theme: png}
    outputs: dict[str, str] = field(default_factory=dict)
    gnss: object | None = None  # validate.GnssValidation
    published: list = field(default_factory=list)  # [validate.PublishedValidation]


def _print_event(ev: Event) -> None:
    print(f"\n== {ev.stage.upper()}: {ev.message}")
    for n in ev.notes:
        print("  " + str(n).split("] ", 1)[-1])


class Pipeline:
    def __init__(self, config: Config, on_event: Callable[[Event], None] | None = None):
        self.cfg = config
        self.aoi = config.get_aoi()
        self.on_event = on_event or _print_event
        self.result = RunResult(config=config)

    def emit(self, stage: str, message: str, notes: list[Note] | None = None, progress=None):
        notes = notes or []
        self.result.notes.extend(notes)
        self.on_event(Event(stage, message, notes, progress))

    # -- stages -------------------------------------------------------------

    def search(self) -> tuple[list[Product], list[Product]]:
        cfg = self.cfg
        area = self.aoi.area_km2()
        self.emit("search", f"Searching for products over {area:,.0f} km² ({cfg.start or 'any'} → {cfg.end or 'any'})")
        if cfg.nisar_source == "LOCAL":
            nisar_all = scan_local(cfg.nisar_dir, NISAR)
        else:
            nisar_all = search_nisar(self.aoi, cfg.start, cfg.end, cfg.nisar_flight_direction)
        if cfg.s1_source == "LOCAL":
            s1_all = scan_local(cfg.s1_dir, SENTINEL1)
        else:
            s1_all = search_sentinel1(self.aoi, cfg.s1_source, cfg.start, cfg.end, cfg.s1_flight_direction)
        notes = [Note("search", INFO, f"Found {len(nisar_all)} NISAR and {len(s1_all)} Sentinel-1 products.")]
        s1_max_span = max(cfg.max_span_days, 730) if cfg.s1_source == "OPERA_DISP_S1" else cfg.max_span_days
        nisar, n1 = select_pairs(nisar_all, cfg.max_pairs, cfg.min_span_days, cfg.max_span_days,
                                 cfg.nisar_flight_direction)
        s1, n2 = select_pairs(s1_all, cfg.max_pairs, cfg.min_span_days, s1_max_span, cfg.s1_flight_direction)
        notes += n1 + n2 + overlap_notes(nisar, s1, cfg.start)
        self.result.nisar_products, self.result.s1_products = nisar, s1
        self.emit("search", f"Selected {len(nisar)} NISAR and {len(s1)} Sentinel-1 pairs", notes)
        return nisar, s1

    def download(self) -> None:
        todo = [p for p in self.result.nisar_products + self.result.s1_products if p.local_path is None]
        if not todo:
            self.emit("download", "All selected products are available locally")
            return
        size = sum(p.size_mb or 0 for p in todo)
        self.emit("download", f"Downloading {len(todo)} products (~{size / 1000:.1f} GB) to {self.cfg.data_dir}",
                  [Note("download", INFO, "Files already present are reused, so re-running is cheap.")])

        def prog(i, n, p):
            self.on_event(Event("download", f"{i + 1}/{n}: {p.name}", progress=i / n))

        download_products(todo, self.cfg.data_dir, progress=prog)
        self.emit("download", "Download complete", progress=1.0)

    def load_and_stack(self) -> None:
        cfg = self.cfg
        template = common_grid(self.aoi, cfg.resolution_m)
        self.result.inside = aoi_mask(self.aoi, template)
        rates = {}
        for sensor, products in ((NISAR, self.result.nisar_products), (SENTINEL1, self.result.s1_products)):
            if not products:
                raise RuntimeError(f"No {sensor} products to compare; see the search notes above.")
            ifgs = []
            for k, p in enumerate(products):
                try:
                    ifg = read_product(p, self.aoi, nisar_polarization=cfg.nisar_polarization,
                                       nisar_apply_ionosphere=cfg.nisar_apply_ionosphere,
                                       nisar_sign=cfg.nisar_sign, s1_sign=cfg.s1_sign)
                except (ValueError, KeyError, OSError) as e:  # bad subset, missing layer, corrupt file
                    self.emit("load", f"Skipping {p.name}", [Note("load", WARN, f"Could not use {p.name}: {e}")])
                    continue
                ifgs.append(ifg)
                self.emit("load", f"Loaded {ifg.sensor} pair {ifg.date1} → {ifg.date2}",
                          pair_notes(ifg, cfg.coherence_threshold), progress=(k + 1) / len(products))
            if not ifgs:
                raise RuntimeError(f"None of the {sensor} products could be read over this AOI.")
            rm = stack_rates(ifgs, template, cfg.coherence_threshold)
            load_notes = [n for n in rm.notes]  # reader notes, e.g. ionosphere / incidence assumptions
            if cfg.project_to_vertical:
                rm = to_vertical(rm)
            rates[sensor] = rm
            self.emit("stack", f"{sensor}: mean {rm.component} rate from {rm.n_pairs} pairs",
                      _dedupe(load_notes) + stack_notes(rm))
        self.result.nisar_rate, self.result.s1_rate = rates[NISAR], rates[SENTINEL1]

    def reference_and_compare(self) -> Comparison:
        a, b = self.result.nisar_rate, self.result.s1_rate
        ref = choose_reference(a, b, self.cfg.reference_lonlat, self.result.inside)
        apply_reference(a, ref)
        apply_reference(b, ref)
        self.result.reference = ref
        self.emit("reference", "Put both rate maps on a common reference", reference_notes(ref, a, b))
        c = compare(a, b, self.result.inside)
        self.result.comparison = c
        self.emit("compare", "Compared NISAR and Sentinel-1", comparison_notes(c, a, b))
        return c

    def validate(self) -> None:
        """Compare both rate maps with independent published data (GNSS and/or published velocity maps)."""
        from . import validate as V

        cfg, r = self.cfg, self.result
        rates = [r.nisar_rate, r.s1_rate]
        if cfg.gnss:
            notes: list[Note] = []
            try:
                if str(cfg.gnss).upper() == "NGL":
                    start = min(rm.date_start for rm in rates)
                    end = max(rm.date_end for rm in rates)
                    self.emit("validate", "Fetching GNSS stations from the Nevada Geodetic Laboratory")
                    stations, skipped = V.ngl_stations(
                        self.aoi, start, end, cfg.gnss_dir, cfg.gnss_max_stations,
                        progress=lambda i, n, site: self.on_event(Event("validate", f"GNSS {i + 1}/{n}: {site}",
                                                                        progress=i / max(n, 1))))
                    source = "Nevada Geodetic Laboratory, IGS20"
                else:
                    stations, skipped, source = V.csv_stations(cfg.gnss), [], f"table {Path(cfg.gnss).name}"
                r.gnss = V.validate_gnss(stations, rates, source, skipped)
                notes = gnss_notes(r.gnss, rates)
            except Exception as e:  # network or file problems must not lose the whole comparison
                notes = [Note("validate", WARN, f"GNSS validation failed: {type(e).__name__}: {e}")]
            self.emit("validate", "Compared with GNSS", notes)
        for spec in cfg.published_maps:
            try:
                pv = V.validate_published(spec, rates, r.reference, r.inside)
                r.published.append(pv)
                notes = published_notes(pv, rates)
            except Exception as e:
                notes = [Note("validate", WARN, f"Could not use published map {spec.get('path')}: {e}")]
            self.emit("validate", f"Compared with published map {spec.get('label') or spec.get('path')}", notes)

    def make_figures(self) -> None:
        r = self.result
        a, b, c = r.nisar_rate, r.s1_rate, r.comparison
        builders = {
            "timeline": lambda th: plots.timeline(
                {NISAR: [(i.date1, i.date2) for i in r.nisar_products],
                 SENTINEL1: [(i.date1, i.date2) for i in r.s1_products]}, th),
            "coherence_maps": lambda th: plots.coherence_maps(a, b, th),
            "coherence_hist": lambda th: plots.coherence_hist(a, b, self.cfg.coherence_threshold, r.inside, th),
            "rate_maps": lambda th: plots.rate_maps(a, b, c, r.reference, th,
                                                    r.gnss.stations if r.gnss else None),
            "scatter": lambda th: plots.scatter(a, b, c, th),
        }
        if r.gnss and r.gnss.stations:
            builders["gnss"] = lambda th: plots.gnss_scatter(r.gnss, [NISAR, SENTINEL1], th)
        for k, pv in enumerate(r.published):
            builders[f"published_{k}"] = lambda th, pv=pv: plots.published_panels(pv, [NISAR, SENTINEL1],
                                                                                  r.reference, th)
        for name, build in builders.items():
            r.figures[name] = {th: plots.to_png(build(th)) for th in ("light", "dark")}
        self.emit("figures", f"Rendered {len(builders)} figures")

    def write_outputs(self) -> Path:
        from .report import build_report

        out = self.cfg.run_dir
        out.mkdir(parents=True, exist_ok=True)
        r = self.result
        comp = r.nisar_rate.component
        for key, da in ((f"nisar_{comp}_rate_m_per_yr", r.nisar_rate.rate),
                        (f"s1_{comp}_rate_m_per_yr", r.s1_rate.rate),
                        ("nisar_minus_s1_m_per_yr", r.comparison.difference),
                        ("nisar_coherence", r.nisar_rate.coherence), ("s1_coherence", r.s1_rate.coherence)):
            path = out / f"{key}.tif"
            da.rio.to_raster(path, driver="GTiff", compress="deflate")
            r.outputs[key] = str(path)
        fig_dir = out / "figures"
        fig_dir.mkdir(exist_ok=True)
        for name, themes in r.figures.items():
            (fig_dir / f"{name}.png").write_bytes(themes["light"])
        metrics = {
            "config": self.cfg.to_dict(),
            "nisar": _rate_summary(r.nisar_rate), "sentinel1": _rate_summary(r.s1_rate),
            "reference": {"mode": r.reference.mode, "lon": r.reference.lon, "lat": r.reference.lat},
            "comparison": r.comparison.to_dict(),
            "pairs": [p.to_row() for p in r.nisar_products + r.s1_products],
            "notes": [n.to_dict() for n in r.notes],
            "validation": _validation_summary(r),
        }
        (out / "metrics.json").write_text(json.dumps(metrics, indent=2, default=str))
        r.outputs["metrics"] = str(out / "metrics.json")
        report = out / "report.html"
        report.write_text(build_report(r), encoding="utf-8")
        r.outputs["report"] = str(report)
        self.emit("report", f"Wrote report and GeoTIFFs to {out}",
                  [Note("report", INFO, "The GeoTIFFs open directly in QGIS/ArcGIS for overlay with geology "
                                        "maps, DEMs and optical imagery.")])
        return report

    def run(self, download: bool = True) -> RunResult:
        self.search()
        if download:
            self.download()
        self.load_and_stack()
        self.reference_and_compare()
        if self.cfg.gnss or self.cfg.published_maps:
            self.validate()
        self.make_figures()
        self.write_outputs()
        return self.result


def _dedupe(notes: list[Note]) -> list[Note]:
    seen, out = set(), []
    for n in notes:
        if n.text not in seen:
            seen.add(n.text)
            out.append(n)
    return out


def _rate_summary(rm: RateMap) -> dict:
    return {"component": rm.component, "n_pairs": rm.n_pairs, "total_days": rm.total_days,
            "date_start": str(rm.date_start), "date_end": str(rm.date_end), "incidence_deg": rm.incidence_deg,
            "wavelength_m": rm.wavelength, "noise_rate_mm_yr": rm.noise_rate * 1000}


def _validation_summary(r: RunResult) -> dict:
    from dataclasses import asdict

    out: dict = {}
    if r.gnss:
        out["gnss"] = {"source": r.gnss.source, "stats": {k: asdict(v) for k, v in r.gnss.stats.items()},
                       "stations": [asdict(s) for s in r.gnss.stations], "skipped": r.gnss.skipped}
    out["published_maps"] = [{"label": p.label, "path": p.path, "reference_mode": p.reference_mode,
                              "stats": {k: asdict(v) for k, v in p.stats.items()}} for p in r.published]
    return out
