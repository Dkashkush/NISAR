"""End-to-end workflow: search → download → load → stack → reference → compare → report.

Each stage reports progress and interpretation notes through ``on_event`` so a CLI or web UI can
show results as they come in.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import plots
from .compare import Comparison, compare
from .config import Config
from .download import download_products
from .interpret import comparison_notes, gnss_notes, literature_notes, pair_notes, published_notes, stack_notes
from .notes import INFO, WARN, Note
from .processing import (RateMap, Reference, aoi_mask, apply_reference, choose_reference, common_grid,
                         merge_frames, reference_notes, stack_rates, to_vertical)
from .products import NISAR, SENTINEL1, Product
from .readers import read_product
from .search import date_range, overlap_notes, scan_local, search_nisar, search_sentinel1, select_pairs


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
    gnss_corrections: dict = field(default_factory=dict)  # sensor -> validate.GnssCorrection (when tied to GNSS)
    comparison: Comparison | None = None
    inside: np.ndarray | None = None
    notes: list[Note] = field(default_factory=list)
    figures: dict[str, dict[str, bytes]] = field(default_factory=dict)  # name -> {theme: png}
    outputs: dict[str, str] = field(default_factory=dict)
    gnss: object | None = None  # validate.GnssValidation
    published: list = field(default_factory=list)  # [validate.PublishedValidation]
    literature: list = field(default_factory=list)  # [validate.LiteratureCheck]


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

    def _s1_sources(self) -> list[str]:
        if self.cfg.s1_source != "AUTO":
            return [self.cfg.s1_source]
        lon, lat = self.aoi.center
        north_america = -170 <= lon <= -50 and 14 <= lat <= 75  # OPERA DISP-S1 coverage
        return ["ARIA_S1_GUNW"] + (["OPERA_DISP_S1"] if north_america else [])

    def search(self) -> tuple[list[Product], list[Product]]:
        cfg = self.cfg
        area = self.aoi.area_km2()
        self.emit("search", f"Searching for products over {area:,.0f} km² ({cfg.start or 'any'} → {cfg.end or 'any'})")
        if cfg.nisar_source == "LOCAL":
            nisar_all = scan_local(cfg.nisar_dir, NISAR)
        else:
            nisar_all = search_nisar(self.aoi, cfg.start, cfg.end, cfg.nisar_flight_direction)
        notes: list[Note] = []
        # Seasonal motion is large (e.g. groundwater pumping), so each satellite's pairs are chosen to overlap the
        # other's time window: Sentinel-1 against all NISAR dates, NISAR against the chosen Sentinel-1 window,
        # then Sentinel-1 once more against the chosen NISAR window.
        nisar_dates = date_range(nisar_all)
        s1_span = cfg.max_span_days
        if cfg.s1_source == "LOCAL":
            s1_all = scan_local(cfg.s1_dir, SENTINEL1)
            s1, n2 = select_pairs(s1_all, cfg.max_pairs, cfg.min_span_days, s1_span, cfg.s1_flight_direction,
                                  target=nisar_dates)
        else:
            # Try each source in turn and keep the first that yields usable pairs (not merely any products).
            s1_all, s1, n2 = [], [], []
            names = {"ARIA_S1_GUNW": "ARIA interferograms", "OPERA_DISP_S1": "OPERA DISP-S1"}
            for source in self._s1_sources():
                found = search_sentinel1(self.aoi, source, cfg.start, cfg.end, cfg.s1_flight_direction)
                span = max(cfg.max_span_days, 730) if source == "OPERA_DISP_S1" else cfg.max_span_days
                sel, sel_notes = select_pairs(found, cfg.max_pairs, cfg.min_span_days, span, cfg.s1_flight_direction,
                                              target=nisar_dates)
                s1_all, s1, n2, s1_span = found, sel, sel_notes, span
                if sel:
                    if cfg.s1_source == "AUTO":
                        notes.append(Note("search", INFO, f"Sentinel-1 source chosen automatically: {names[source]}."))
                    break
                if cfg.s1_source == "AUTO":
                    notes.append(Note("search", INFO, f"{names[source]}: {len(found)} product(s) found, none usable "
                                                      "for this area and period; trying the next source."))
        notes.insert(0, Note("search", INFO, f"Found {len(nisar_all)} NISAR and {len(s1_all)} Sentinel-1 products."))
        nisar, n1 = select_pairs(nisar_all, cfg.max_pairs, cfg.min_span_days, cfg.max_span_days,
                                 cfg.nisar_flight_direction, target=date_range(s1))
        if nisar and s1:
            s1, n2 = select_pairs(s1_all, cfg.max_pairs, cfg.min_span_days, s1_span, cfg.s1_flight_direction,
                                  target=date_range(nisar))
        notes += n1 + n2 + overlap_notes(nisar, s1, cfg.start)
        self.result.nisar_products, self.result.s1_products = nisar, s1
        self.emit("search", f"Selected {len(nisar)} NISAR and {len(s1)} Sentinel-1 pairs", notes)
        return nisar, s1

    def _session(self):
        """Authenticated ASF session, created once when first needed."""
        if getattr(self, "_asf_session", None) is None:
            from .download import make_session
            self._asf_session = make_session(self.cfg.earthdata_token)
            check = getattr(self._asf_session, "token_check", None)
            if check and not check[0]:
                self.emit("download", "Earthdata's token pre-check failed; trying the token on the data anyway",
                          [Note("download", WARN, f"Earthdata's token pre-check answered {check[1]}. Some valid "
                                                  "tokens fail this check, so the files are tried anyway; if they "
                                                  "are refused, the next message says how to fix it.")])
        return self._asf_session

    def download(self) -> None:
        from .remote import is_streamable

        todo = [p for p in self.result.nisar_products + self.result.s1_products if p.local_path is None]
        if self.cfg.stream_remote:
            remote = [p for p in todo if is_streamable(p.url)]
            todo = [p for p in todo if p not in remote]
            if remote:
                full = sum(p.size_mb or 0 for p in remote) / 1000
                self.emit("download", f"Reading {len(remote)} products directly from ASF, only the part over your "
                                      f"area (instead of downloading ~{full:.1f} GB)",
                          [Note("download", INFO, "Only the parts of each file that cover your area are transferred. "
                                                  "If a remote read fails, that file is downloaded in full instead.")])
                self._session()  # check the login now, before any reading
        if not todo:
            if not self.cfg.stream_remote:
                self.emit("download", "All selected products are available locally")
            return
        size = sum(p.size_mb or 0 for p in todo)
        self.emit("download", f"Downloading {len(todo)} products (~{size / 1000:.1f} GB) to {self.cfg.data_dir}",
                  [Note("download", INFO, "Files already present are reused, so re-running is cheap.")])

        def prog(i, n, p):
            self.on_event(Event("download", f"{i + 1}/{n}: {p.name}", progress=i / n))

        download_products(todo, self.cfg.data_dir, session=self._session(), progress=prog)
        self.emit("download", "Download complete", progress=1.0)

    def _read(self, p: Product):
        """Read one product: locally if downloaded, otherwise remotely (falling back to a full download)."""
        cfg = self.cfg
        kw = dict(nisar_polarization=cfg.nisar_polarization, nisar_apply_ionosphere=cfg.nisar_apply_ionosphere,
                  nisar_sign=cfg.nisar_sign, s1_sign=cfg.s1_sign)
        if p.local_path is not None:
            return read_product(p, self.aoi, **kw)
        from .remote import AccessError, UnavailableError, open_remote

        try:
            with open_remote(p.url, self._session()) as fobj:
                return read_product(p, self.aoi, fileobj=fobj, **kw)
        except (ValueError, AccessError, UnavailableError):
            raise  # no overlap with the area, login refused, or file unavailable: downloading would not help
        except Exception as e:  # network / server problems: fall back to downloading the file
            self.emit("load", f"Remote read failed for {p.name}; downloading it instead",
                      [Note("load", INFO, f"Remote read of {p.name} failed ({type(e).__name__}: {e}); "
                                          "downloading the whole file instead.")])
            download_products([p], cfg.data_dir, session=self._session())
            return read_product(p, self.aoi, **kw)

    def load_and_stack(self) -> None:
        cfg = self.cfg
        template = common_grid(self.aoi, cfg.resolution_m)
        self.result.inside = aoi_mask(self.aoi, template)
        rates = {}
        for sensor, products in ((NISAR, self.result.nisar_products), (SENTINEL1, self.result.s1_products)):
            if not products:
                raise RuntimeError(f"No {sensor} products to compare; see the search notes above.")
            by_frame: dict = {}
            for k, p in enumerate(products):
                try:
                    ifg = self._read(p)
                except (ValueError, KeyError, OSError, RuntimeError) as e:  # bad subset, missing layer, bad file
                    self.emit("load", f"Skipping {p.name}", [Note("load", WARN, f"Could not use {p.name}: {e}")])
                    continue
                by_frame.setdefault(p.frame, []).append(ifg)
                self.emit("load", f"Loaded {ifg.sensor} pair {ifg.date1} → {ifg.date2}",
                          pair_notes(ifg, cfg.coherence_threshold), progress=(k + 1) / len(products))
            if not by_frame:
                raise RuntimeError(f"None of the {sensor} products could be read over this AOI.")
            # frames are processed separately (each has its own zero): stack each, then join them
            stacks = [stack_rates(ifgs, template, cfg.coherence_threshold) for ifgs in by_frame.values()]
            join_notes: list[Note] = []
            rm = stacks[0]
            if len(stacks) > 1:
                rm, join_notes = merge_frames(stacks)
            load_notes = [n for n in rm.notes] + join_notes  # reader notes, e.g. ionosphere / incidence
            if cfg.project_to_vertical:
                rm = to_vertical(rm)
            rates[sensor] = rm
            self.emit("stack", f"{sensor}: mean {rm.component} rate from {rm.n_pairs} pairs",
                      _dedupe(load_notes) + stack_notes(rm))
        self.result.nisar_rate, self.result.s1_rate = rates[NISAR], rates[SENTINEL1]

    def _gnss_stations(self):
        """GNSS stations (stations, skipped, source), fetched once and shared by the reference and validation."""
        if getattr(self, "_gnss_cache", None) is None:
            from . import validate as V

            cfg, r = self.cfg, self.result
            if str(cfg.gnss).upper() == "NGL":
                rates = [r.nisar_rate, r.s1_rate]
                start, end = min(rm.date_start for rm in rates), max(rm.date_end for rm in rates)
                self.emit("validate", "Fetching GNSS stations from the Nevada Geodetic Laboratory")
                stations, skipped = V.ngl_stations(
                    self.aoi, start, end, cfg.gnss_dir, cfg.gnss_max_stations,
                    progress=lambda i, n, site: self.on_event(Event("validate", f"GNSS {i + 1}/{n}: {site}",
                                                                    progress=i / max(n, 1))),
                    windows={rm.sensor: (rm.date_start, rm.date_end) for rm in rates})
                self._gnss_cache = (stations, skipped, "Nevada Geodetic Laboratory, IGS20")
            else:
                self._gnss_cache = (V.csv_stations(cfg.gnss), [], f"table {Path(cfg.gnss).name}")
        return self._gnss_cache

    def _tie_to_gnss(self, ref: Reference) -> list[Note]:
        """Make both area-referenced maps match GNSS (absolute rates). Both or neither are tied."""
        from .validate import fit_gnss_correction

        a, b = self.result.nisar_rate, self.result.s1_rate
        try:
            stations, _, source = self._gnss_stations()
        except Exception as e:  # network or file problems: keep the area reference
            return [Note("reference", WARN, f"Could not fetch GNSS to make the rates absolute "
                                            f"({type(e).__name__}: {e}); zero stays the area median.")]
        fits = {rm.sensor: fit_gnss_correction(rm, stations, self.cfg.gnss_correction) for rm in (a, b)}
        if any(f is None for f in fits.values()):
            return [Note("reference", INFO, "No GNSS station has valid data from both satellites, so the rates stay "
                                            "relative (zero = the area median).")]
        for rm in (a, b):
            rm.rate = rm.rate - fits[rm.sensor][0]
        self.result.gnss_corrections = {k: f[1] for k, f in fits.items()}
        ref.mode = "gnss"
        notes = []
        for sensor, (_, c) in fits.items():
            many = f"{c.n} station{'s' if c.n > 1 else ''}"
            if c.kind == "plane":
                text = (f"{sensor} is tied to GNSS ({source}) with a plane through {many}: shift {c.shift_mm_yr:+.0f} "
                        f"mm/yr at the centre, tilt {c.tilt_ew_mm_yr:+.0f} mm/yr west→east and {c.tilt_ns_mm_yr:+.0f} "
                        "mm/yr south→north removed. Over short periods most InSAR error is such a smooth tilt "
                        "(atmosphere of the first and last dates); the GNSS check below predicts each station "
                        "without using it.")
                notes.append(Note("reference", INFO, text))
            else:
                why = {"sign": "a plane was not used because InSAR and GNSS look opposite in sign at the stations, "
                               "and a plane could hide such an error",
                       "spread": "the stations are too bunched together to fix a tilt",
                       "mode": "gnss_correction is set to offset",
                       "few": "with 5 or more stations spread over the area they would be removed too"}[c.why_offset]
                text = (f"{sensor} is tied to GNSS ({source}) with one shift of {c.shift_mm_yr:+.0f} mm/yr from {many}, "
                        f"so rates are absolute (zero = no vertical motion). Large-scale tilts from the atmosphere "
                        f"remain; {why}.")
                if c.n < 2:
                    text += (" With a single station the whole map rests on that one point; its own local motion "
                             "or noise shifts every value.")
                notes.append(Note("reference", INFO if c.n >= 2 else WARN, text))
        return notes

    def reference_and_compare(self) -> Comparison:
        a, b = self.result.nisar_rate, self.result.s1_rate
        ref = choose_reference(a, b, self.cfg.reference_lonlat, self.result.inside)
        apply_reference(a, ref)
        apply_reference(b, ref)
        notes = reference_notes(ref, a, b)
        if ref.mode == "area" and self.cfg.gnss and self.cfg.tie_to_gnss:
            ra, rb = a.rate.values, b.rate.values
            joint = np.isfinite(ra) & np.isfinite(rb) & self.result.inside
            r0 = float(np.corrcoef(ra[joint], rb[joint])[0, 1]) if joint.sum() > 10 else float("nan")
            if math.isfinite(r0) and r0 < -0.3:  # a GNSS fit could partly hide a sign error: resolve that first
                notes.append(Note("reference", WARN, "The two maps are anti-correlated, which usually means a sign "
                                                     "setting is wrong, so they are not tied to GNSS until that is "
                                                     "resolved."))
            else:
                notes += self._tie_to_gnss(ref)
        self.result.reference = ref
        self.emit("reference", "Put both rate maps on a common reference", notes)
        c = compare(a, b, self.result.inside)
        self.result.comparison = c
        self.emit("compare", "Compared NISAR and Sentinel-1", comparison_notes(c, a, b, ref.mode))
        return c

    def validate(self) -> None:
        """Compare both rate maps with independent published data (GNSS and/or published velocity maps)."""
        from . import validate as V

        cfg, r = self.cfg, self.result
        rates = [r.nisar_rate, r.s1_rate]
        if cfg.gnss:
            notes: list[Note] = []
            try:
                stations, skipped, source = self._gnss_stations()
                r.gnss = V.validate_gnss(stations, rates, source, skipped, corrections=r.gnss_corrections)
                notes = gnss_notes(r.gnss, rates, tied=r.reference is not None and r.reference.mode == "gnss")
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
        if cfg.published_values:
            try:
                r.literature = V.validate_literature(cfg.published_values, rates, r.gnss,
                                                     tied=r.reference is not None and r.reference.mode == "gnss")
                notes = literature_notes(r.literature, rates)
            except Exception as e:
                notes = [Note("validate", WARN, f"Could not compare with published values: {type(e).__name__}: {e}")]
            self.emit("validate", f"Compared with {len(cfg.published_values)} published value(s) from studies", notes)

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
        if self.cfg.gnss or self.cfg.published_maps or self.cfg.published_values:
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
            "wavelength_m": rm.wavelength, "noise_rate_mm_yr": rm.noise_rate * 1000, "pairs_qc": rm.pair_qc}


def _validation_summary(r: RunResult) -> dict:
    from dataclasses import asdict

    out: dict = {}
    if r.gnss:
        out["gnss"] = {"source": r.gnss.source, "stats": {k: asdict(v) for k, v in r.gnss.stats.items()},
                       "stations": [asdict(s) for s in r.gnss.stations], "skipped": r.gnss.skipped}
    out["published_maps"] = [{"label": p.label, "path": p.path, "reference_mode": p.reference_mode,
                              "stats": {k: asdict(v) for k, v in p.stats.items()}} for p in r.published]
    out["published_values"] = [asdict(c) for c in r.literature]
    return out
