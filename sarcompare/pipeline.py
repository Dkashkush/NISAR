"""End-to-end workflow: search → download → load → stack → reference → compare → report.

Each stage reports progress and interpretation notes through ``on_event`` so a CLI or web UI can
show results as they come in.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import plots
from .compare import Comparison, compare
from .config import Config
from .download import download_products
from .interpret import (comparison_notes, gnss_notes, literature_notes, missing_sensor_notes, pair_notes,
                        published_notes, stack_notes)
from .notes import INFO, WARN, Note
from .processing import (RateMap, Reference, aoi_mask, apply_reference, choose_reference, combine_directions,
                         common_grid, merge_frames, reference_notes, stack_rates, to_vertical)
from .products import NISAR, SENTINEL1, Product
from .readers import read_product
from .search import date_range, overlap_notes, scan_local, search_nisar, search_sentinel1, select_directions


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
    east: dict = field(default_factory=dict)  # sensor -> east-west RateMap when both flight directions were combined
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

    @property
    def rates(self) -> list[RateMap]:
        """The rate maps that exist, NISAR first. Where one satellite has no data, the run uses the other alone."""
        return [rm for rm in (self.nisar_rate, self.s1_rate) if rm is not None]

    @property
    def single(self) -> bool:
        return len(self.rates) == 1


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
        # then interferograms already ordered from HyP3 for this area (needs the Earthdata token), then LiCSAR
        return (["ARIA_S1_GUNW"] + (["OPERA_DISP_S1"] if north_america else [])
                + (["HYP3"] if self._token() else []) + ["LICSAR"])

    def _token(self) -> str | None:
        return self.cfg.earthdata_token or os.environ.get("EARTHDATA_TOKEN") or None

    def _hyp3(self):
        from .hyp3 import HyP3

        if getattr(self, "_hyp3_client", None) is None:
            self._hyp3_client = HyP3(self._token() or "")
        return self._hyp3_client

    def _hyp3_search(self) -> list[Product]:
        """Finished HyP3 interferograms for this area; their status goes into the search notes."""
        from .hyp3 import find_products

        products, counts, notes = find_products(self._hyp3(), self.aoi)
        self._hyp3_counts = counts
        self.emit("search", f"Checked ASF HyP3 for Sentinel-1 interferograms ordered for this area "
                            f"({len(products)} ready)", notes)
        return products

    def hyp3_plan(self):
        """Sentinel-1 pairs to order from HyP3, matching the NISAR pairs' dates and flight directions."""
        from .hyp3 import plan_pairs

        cfg = self.cfg
        combine = cfg.combine_directions and cfg.project_to_vertical
        target: dict = {}
        for prod in self.result.nisar_products:
            key = (prod.flight_direction or "") if combine else ""
            target.setdefault(key, set()).update(d for d in (prod.date1, prod.date2) if d)
        target = {k: sorted(v) for k, v in target.items()}
        directions = None
        if cfg.s1_flight_direction:
            directions = [cfg.s1_flight_direction]
        elif not combine and self.result.nisar_products:
            directions = sorted({prod.flight_direction for prod in self.result.nisar_products
                                 if prod.flight_direction})[:1] or None
        return plan_pairs(self.aoi, cfg.start, cfg.end, target=target, directions=directions,
                          max_pairs=cfg.max_pairs, min_span=min(cfg.min_span_days, 6), max_span=cfg.max_span_days)

    def hyp3_order(self, pairs, validate_only: bool = False):
        from .hyp3 import order

        return order(self._hyp3(), self.aoi, pairs, validate_only=validate_only)

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
        combine = cfg.combine_directions and cfg.project_to_vertical
        if cfg.s1_source == "LOCAL":
            s1_all = scan_local(cfg.s1_dir, SENTINEL1)
            s1, n2 = select_directions(s1_all, cfg.max_pairs, cfg.min_span_days, s1_span, cfg.s1_flight_direction,
                                       target=nisar_dates, combine=combine)
        else:
            # Try each source in turn and keep the first that yields usable pairs (not merely any products).
            s1_all, s1, n2 = [], [], []
            names = {"ARIA_S1_GUNW": "ARIA interferograms", "OPERA_DISP_S1": "OPERA DISP-S1",
                     "HYP3": "interferograms ordered from ASF HyP3", "LICSAR": "LiCSAR (COMET) interferograms"}
            for source in self._s1_sources():
                if source == "HYP3":
                    try:
                        found = self._hyp3_search()
                    except Exception as e:  # HyP3 unreachable or token refused: Sentinel-1 is then left out
                        notes.append(Note("search", WARN, f"Could not check ASF HyP3 for ordered interferograms "
                                                          f"({type(e).__name__}: {e})."))
                        continue
                elif source == "LICSAR":
                    from .licsar import search as licsar_search

                    try:
                        found, lnotes = licsar_search(self.aoi, cfg.start, cfg.end, cfg.s1_flight_direction)
                    except Exception as e:  # LiCSAR unreachable: Sentinel-1 is then left out
                        notes.append(Note("search", WARN, f"Could not check LiCSAR (COMET) for interferograms "
                                                          f"({type(e).__name__}: {e})."))
                        continue
                    notes += lnotes
                else:
                    found = search_sentinel1(self.aoi, source, cfg.start, cfg.end, cfg.s1_flight_direction)
                span = max(cfg.max_span_days, 730) if source == "OPERA_DISP_S1" else cfg.max_span_days
                sel, sel_notes = select_directions(found, cfg.max_pairs, cfg.min_span_days, span,
                                                   cfg.s1_flight_direction, target=nisar_dates, combine=combine)
                s1_all, s1, n2, s1_span = found, sel, sel_notes, span
                if sel:
                    if cfg.s1_source == "AUTO":
                        notes.append(Note("search", INFO, f"Sentinel-1 source chosen automatically: {names[source]}."))
                    break
                if cfg.s1_source == "AUTO":
                    notes.append(Note("search", INFO, f"{names[source]}: {len(found)} product(s) found, none usable "
                                                      "for this area and period; trying the next source."))
        notes.insert(0, Note("search", INFO, f"Found {len(nisar_all)} NISAR and {len(s1_all)} Sentinel-1 products."))
        nisar, n1 = select_directions(nisar_all, cfg.max_pairs, cfg.min_span_days, cfg.max_span_days,
                                      cfg.nisar_flight_direction, target=date_range(s1), combine=combine)
        if nisar and s1:
            s1, n2 = select_directions(s1_all, cfg.max_pairs, cfg.min_span_days, s1_span, cfg.s1_flight_direction,
                                       target=date_range(nisar), combine=combine)
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
        licsar = [p for p in todo if p.product_type == "LICSAR"]  # always read in place (only the area's window)
        todo = [p for p in todo if p not in licsar]
        if licsar:
            self.emit("download", f"Reading {len(licsar)} LiCSAR interferograms directly from COMET's public "
                                  "archive, only the part over your area")
        if self.cfg.stream_remote:
            remote = [p for p in todo if is_streamable(p.url) or _public(p)]
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

        from .hyp3 import anonymous_session

        public = [p for p in todo if _public(p)]
        if public:  # HyP3 results are public links: never send the Earthdata token there
            download_products(public, self.cfg.data_dir, session=anonymous_session(), progress=prog)
        rest = [p for p in todo if not _public(p)]
        if rest:
            download_products(rest, self.cfg.data_dir, session=self._session(), progress=prog)
        self.emit("download", "Download complete", progress=1.0)

    def _read(self, p: Product):
        """Read one product: locally if downloaded, otherwise remotely (falling back to a full download)."""
        cfg = self.cfg
        kw = dict(nisar_polarization=cfg.nisar_polarization, nisar_apply_ionosphere=cfg.nisar_apply_ionosphere,
                  nisar_sign=cfg.nisar_sign, s1_sign=cfg.s1_sign)
        if p.local_path is not None or p.product_type == "LICSAR":  # LiCSAR: public files, read in place
            return read_product(p, self.aoi, **kw)
        from .hyp3 import anonymous_session
        from .remote import AccessError, UnavailableError, open_remote

        session = anonymous_session() if _public(p) else self._session()  # the token stays with ASF/Earthdata
        try:
            with open_remote(p.url, session) as fobj:
                return read_product(p, self.aoi, fileobj=fobj, **kw)
        except (ValueError, AccessError, UnavailableError):
            raise  # no overlap with the area, login refused, or file unavailable: downloading would not help
        except Exception as e:  # network / server problems: fall back to downloading the file
            self.emit("load", f"Remote read failed for {p.name}; downloading it instead",
                      [Note("load", INFO, f"Remote read of {p.name} failed ({type(e).__name__}: {e}); "
                                          "downloading the whole file instead.")])
            download_products([p], cfg.data_dir, session=session)
            return read_product(p, self.aoi, **kw)

    def load_and_stack(self) -> None:
        cfg = self.cfg
        template = common_grid(self.aoi, cfg.resolution_m)
        self.result.inside = aoi_mask(self.aoi, template)
        rates = {}
        for sensor, products in ((NISAR, self.result.nisar_products), (SENTINEL1, self.result.s1_products)):
            if not products:
                continue  # explained below; the other satellite is used alone
            groups: dict = {}  # flight direction -> frame -> interferograms
            tracks: dict = {}  # flight direction -> track numbers
            for k, p in enumerate(products):
                try:
                    ifg = self._read(p)
                except (ValueError, KeyError, OSError, RuntimeError) as e:  # bad subset, missing layer, bad file
                    self.emit("load", f"Skipping {p.name}", [Note("load", WARN, f"Could not use {p.name}: {e}")])
                    continue
                direction = (p.flight_direction or ifg.flight_direction or "").upper() or None
                groups.setdefault(direction, {}).setdefault(p.frame, []).append(ifg)
                tracks.setdefault(direction, set()).add(p.track)
                self.emit("load", f"Loaded {ifg.sensor} pair {ifg.date1} → {ifg.date2}",
                          pair_notes(ifg, cfg.coherence_threshold), progress=(k + 1) / len(products))
            if not groups:
                self.emit("load", f"No {sensor} pair could be used",
                          [Note("load", WARN, f"None of the {len(products)} selected {sensor} products could be "
                                              "read over this area (see the messages above).")])
                continue
            los: dict = {}
            join_notes: list[Note] = []
            for direction, frames in groups.items():
                # frames are processed separately (each has its own zero): stack each, then join them
                stacks = [stack_rates(ifgs, template, cfg.coherence_threshold) for ifgs in frames.values()]
                rm = stacks[0]
                if len(stacks) > 1:
                    rm, notes = merge_frames(stacks)
                    join_notes += notes
                rm.direction = direction
                track_txt = ", ".join(str(t) for t in sorted(t for t in tracks[direction] if t is not None))
                rm.geometry = f"{(direction or 'unknown direction').lower()} track {track_txt or '?'}"
                los[direction] = rm
            east = None
            if cfg.project_to_vertical and {"ASCENDING", "DESCENDING"} <= set(los):
                rm, east, combine_notes = combine_directions(los["ASCENDING"], los["DESCENDING"],
                                                             self.result.inside)
                join_notes += combine_notes
            else:
                rm = next(iter(los.values()))
                if cfg.project_to_vertical:
                    rm = to_vertical(rm)
            load_notes = [n for n in rm.notes] + join_notes  # reader notes, e.g. ionosphere / incidence
            rates[sensor] = rm
            self.result.east[sensor] = east
            self.emit("stack", f"{sensor}: mean {rm.component} rate from {rm.n_pairs} pairs",
                      _dedupe(load_notes) + stack_notes(rm))
        if not rates:
            raise RuntimeError("Neither NISAR nor Sentinel-1 has usable image pairs for this area and period; see "
                               "the search notes above. Try a larger area, a longer period or other dates.")
        self.result.nisar_rate, self.result.s1_rate = rates.get(NISAR), rates.get(SENTINEL1)
        missing = [k for k in (NISAR, SENTINEL1) if k not in rates]
        if missing:
            present = next(iter(rates))
            self.emit("stack", f"Continuing with {present} alone (no usable {missing[0]} data here)",
                      missing_sensor_notes(present, missing[0], self.aoi.center,
                                           getattr(self, "_hyp3_counts", None)))

    def _gnss_stations(self):
        """GNSS stations (stations, skipped, source), fetched once and shared by the reference and validation."""
        if getattr(self, "_gnss_cache", None) is None:
            from . import validate as V

            cfg, r = self.cfg, self.result
            if str(cfg.gnss).upper() == "NGL":
                rates = r.rates
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
        """Make the area-referenced maps match GNSS (absolute rates). All maps or none are tied."""
        from .validate import fit_gnss_correction

        maps = self.result.rates
        try:
            stations, _, source = self._gnss_stations()
        except Exception as e:  # network or file problems: keep the area reference
            return [Note("reference", WARN, f"Could not fetch GNSS to make the rates absolute "
                                            f"({type(e).__name__}: {e}); zero stays the area median.")]
        fits = {rm.sensor: fit_gnss_correction(rm, stations, self.cfg.gnss_correction) for rm in maps}
        if any(f is None for f in fits.values()):
            whose = "from both satellites" if len(maps) > 1 else f"from {maps[0].sensor}"
            return [Note("reference", INFO, f"No GNSS station has valid data {whose}, so the rates stay "
                                            "relative (zero = the area median).")]
        for rm in maps:
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
        maps = self.result.rates
        a, b = maps[0], (maps[1] if len(maps) > 1 else None)  # b is None when only one satellite has data
        ref = choose_reference(a, b, self.cfg.reference_lonlat, self.result.inside)
        for rm in maps:
            apply_reference(rm, ref)
        notes = reference_notes(ref, a, b)
        if ref.mode == "area" and self.cfg.gnss and self.cfg.tie_to_gnss:
            r0 = float("nan")
            if b is not None:
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
        self.emit("reference", "Put both rate maps on a common reference" if b is not None
                  else f"Set the zero of the {a.sensor} rate map", notes)
        c = compare(a, b, self.result.inside)
        self.result.comparison = c
        self.emit("compare", "Compared NISAR and Sentinel-1" if b is not None
                  else f"Measured ground motion with {a.sensor} alone", comparison_notes(c, a, b, ref.mode))
        return c

    def validate(self) -> None:
        """Compare both rate maps with independent published data (GNSS and/or published velocity maps)."""
        from . import validate as V

        cfg, r = self.cfg, self.result
        rates = r.rates
        if cfg.gnss:
            notes: list[Note] = []
            try:
                stations, skipped, source = self._gnss_stations()
                r.gnss = V.validate_gnss(stations, rates, source, skipped, corrections=r.gnss_corrections)
                notes = gnss_notes(r.gnss, rates, tied=r.reference is not None and r.reference.mode == "gnss")
            except Exception as e:  # network or file problems must not lose the whole comparison
                notes = [Note("validate", WARN, f"GNSS validation failed: {type(e).__name__}: {e}")]
            self.emit("validate", "Compared with GNSS", notes)
        self._egms_download()
        for raw in cfg.published_maps:
            spec = {**V.egms_defaults(raw.get("path", "")), **raw}  # EGMS tiles are recognised by name
            targets = rates
            if V.published_component(spec) == "east-west":
                targets = [v for v in r.east.values() if v is not None]
                if not targets:
                    self.emit("validate", f"Skipped published map {spec.get('label') or spec.get('path')}",
                              [Note("validate", INFO, f"'{spec.get('label') or spec.get('path')}' shows east-west "
                                                      "motion. It can only be compared when both orbit directions "
                                                      "were combined, which needs ascending and descending passes "
                                                      "over the area.")])
                    continue
            try:
                pv = V.validate_published(spec, targets, r.reference, r.inside)
                r.published.append(pv)
                notes = published_notes(pv, targets)
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

    def _egms_download(self) -> None:
        """With an EGMS key and an area in Europe, fetch the EGMS Ortho maps as an independent check."""
        from . import egms
        from . import validate as V

        cfg, r = self.cfg, self.result
        key = cfg.egms_token or os.environ.get("EGMS_TOKEN")  # the environment: on your own computer only
        if not key or not egms.in_europe(self.aoi):
            return
        if any(V.egms_defaults(m.get("path", "")) for m in cfg.published_maps):
            return  # EGMS maps given already
        comps = ("U", "E") if any(v is not None for v in r.east.values()) else ("U",)
        try:
            maps, notes = egms.download(self.aoi, key, Path(cfg.data_dir) / "egms", comps)
        except Exception as e:  # key refused, no tile, network: the other checks still run
            self.emit("validate", "EGMS maps could not be downloaded",
                      [Note("validate", WARN, f"EGMS: {e}")])
            return
        cfg.published_maps = list(cfg.published_maps) + [{"path": str(p)} for p in maps.values()]
        self.emit("validate", "Downloaded EGMS Ortho maps for the area", notes)

    def make_figures(self) -> None:
        r = self.result
        maps, c = r.rates, r.comparison
        used = {rm.sensor for rm in maps}
        pairs = {NISAR: r.nisar_products, SENTINEL1: r.s1_products}
        builders = {
            "timeline": lambda th: plots.timeline(
                {k: [(i.date1, i.date2) for i in v] for k, v in pairs.items() if k in used}, th),
            "coherence_maps": lambda th: plots.coherence_maps(maps, th),
            "coherence_hist": lambda th: plots.coherence_hist(maps, self.cfg.coherence_threshold, r.inside, th),
            "rate_maps": lambda th: plots.rate_maps(maps, c, r.reference, th,
                                                    r.gnss.stations if r.gnss else None),
        }
        if len(maps) > 1:
            builders["scatter"] = lambda th: plots.scatter(maps[0], maps[1], c, th)
        east = {k: v for k, v in r.east.items() if v is not None}
        if east:
            builders["east_west"] = lambda th: plots.east_west_maps(east, th)
        if r.gnss and r.gnss.stations:
            builders["gnss"] = lambda th: plots.gnss_scatter(r.gnss, [rm.sensor for rm in maps], th)
        for k, pv in enumerate(r.published):
            builders[f"published_{k}"] = lambda th, pv=pv: plots.published_panels(pv, list(pv.stats),
                                                                                  r.reference, th)
        for name, build in builders.items():
            r.figures[name] = {th: plots.to_png(build(th)) for th in ("light", "dark")}
        self.emit("figures", f"Rendered {len(builders)} figures")

    def write_outputs(self) -> Path:
        from .report import build_report

        out = self.cfg.run_dir
        out.mkdir(parents=True, exist_ok=True)
        r = self.result
        comp = r.rates[0].component
        short = {NISAR: "nisar", SENTINEL1: "s1"}
        layers = [(f"{short[rm.sensor]}_{comp}_rate_m_per_yr", rm.rate) for rm in r.rates]
        if r.comparison.difference is not None:
            layers.append(("nisar_minus_s1_m_per_yr", r.comparison.difference))
        layers += [(f"{short[rm.sensor]}_coherence", rm.coherence) for rm in r.rates]
        layers += [(f"{short[k]}_east_west_rate_m_per_yr", v.rate) for k, v in r.east.items() if v is not None]
        for key, da in layers:
            path = out / f"{key}.tif"
            da.rio.to_raster(path, driver="GTiff", compress="deflate")
            r.outputs[key] = str(path)
        fig_dir = out / "figures"
        fig_dir.mkdir(exist_ok=True)
        for name, themes in r.figures.items():
            (fig_dir / f"{name}.png").write_bytes(themes["light"])
        metrics = {
            "config": self.cfg.to_dict(),
            "nisar": _rate_summary(r.nisar_rate), "sentinel1": _rate_summary(r.s1_rate),  # None = no data
            "east_west": {k: _rate_summary(v) for k, v in r.east.items() if v is not None},
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
        if (self.cfg.gnss or self.cfg.published_maps or self.cfg.published_values or self.cfg.egms_token
                or os.environ.get("EGMS_TOKEN")):
            self.validate()
        self.make_figures()
        self.write_outputs()
        return self.result


def _public(p: Product) -> bool:
    """Products on public links (HyP3 results), read without the Earthdata token."""
    return p.product_type == "HYP3_INSAR" and bool(p.url) and p.local_path is None


def _dedupe(notes: list[Note]) -> list[Note]:
    seen, out = set(), []
    for n in notes:
        if n.text not in seen:
            seen.add(n.text)
            out.append(n)
    return out


def _rate_summary(rm: RateMap | None) -> dict | None:
    if rm is None:
        return None
    return {"component": rm.component, "n_pairs": rm.n_pairs, "total_days": rm.total_days,
            "date_start": str(rm.date_start), "date_end": str(rm.date_end), "incidence_deg": rm.incidence_deg,
            "wavelength_m": rm.wavelength, "noise_rate_mm_yr": rm.noise_rate * 1000, "pairs_qc": rm.pair_qc,
            "geometry": rm.geometry, "two_direction_fraction": rm.two_direction_fraction}


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
