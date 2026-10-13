# Real-data test results

These are the results of running OpenSAR Explorer on real NISAR and Sentinel-1 products with the
**Real-data test** workflow (`.github/workflows/real-data-test.yml`), in October 2026. Each run searches ASF,
reads only the part of each file that covers the area (about 20 GB of products per run is never
downloaded), compares the two satellites, and checks both against GNSS stations from the Nevada Geodetic
Laboratory. GNSS rates are measured over each satellite's own dates.

All rates are vertical, in mm/yr; negative means sinking.

## Mexico City: the strong-signal check (passed)

Mexico City sinks by about 25-30 cm/yr in the old lake bed, far above the noise, so it tests the sign and
the size of the measurements.

| | NISAR | Sentinel-1 (ARIA) | Sentinel-1 (OPERA) |
|---|---|---|---|
| Data | 9 consecutive pairs, Oct 30 2025 → Feb 27 2026, ascending track 19 | 9 consecutive pairs, Oct 31 2025 → Feb 22 2026, ascending track 78 | 1 file, Nov 28 2025 → Feb 26 2026, descending track 143 |
| Station MMX1 (lake bed): GNSS over the same dates | −270 ± 6 | −255 ± 7 | −292 ± 10 |
| Station MMX1: InSAR | **−278** | **−231** | **−264** |
| Agreement with NISAR (all pixels) | | r = 0.73 | r = 0.87 |
| Fastest 5% of the main subsiding area | −263 | | −280 |
| Rate noise (estimated) | 18 | 31 | n/a (single file) |

What this shows:

- **Signs and units are right for all three product types.** NISAR, ARIA and OPERA all show the
  subsidence bowl as sinking, at the rate GNSS measures, within about 10-30 mm/yr.
- The vertical projection works across viewing directions: OPERA (descending) agrees with NISAR
  (ascending) at r = 0.87.
- The remaining NISAR − Sentinel-1 differences are mostly a smooth tilt across the city (about 120 mm/yr
  end to end; 43 mm/yr RMS after removing it), the typical signature of atmosphere on individual dates.
- Only 2 NGL stations have current data inside the area, so the maps are tied to GNSS with a single shift.
  More stations (or a longer period) would let the app remove that tilt.

### Mexico City against published studies

Five values taken from two peer-reviewed papers (page numbers in `examples/mexico_city.yaml`), compared
with the NISAR and Sentinel-1 (ARIA) run above. **7 of 10 comparisons agree.** All rates are vertical, in mm/yr.

| Study (period) | Place | Published | NISAR | Sentinel-1 | Verdict |
|---|---|---|---|---|---|
| Cigna & Tapete 2021 (Oct 2017 – Oct 2019) | GNSS station MMX1, airport | −294 | −245 | −229 | both consistent |
| Cigna & Tapete 2021 (2017–2019) | Central Nezahualcóyotl, peaks | −350 to −387 | −278 | −261 | both lower |
| Cigna & Tapete 2021 (2017–2019) | Metropolitan Cathedral | −70 to −88 | −51 | −115 | both consistent |
| Cigna & Tapete 2021 (Oct 2017 – Oct 2019) | TNGF, volcanic rock (stable) | +0.05 | +60 | −11 | NISAR differs, Sentinel-1 consistent |
| Chaussard et al. 2014 (2007–2011) | Eastern Mexico City, maximum | −300 | −278 | −261 | both consistent |

Reading the three differences:
- **Nezahualcóyotl peaks:** both satellites agree with each other (−278, −261) and are about 25% below the
  2017–2019 peaks. Likely reasons: the district centre used is approximate (the paper gives no coordinates),
  this run's pixels are 90 m (finer maps show sharper peaks), and the period differs by 6-8 years. They match
  the 2007–2011 maximum of about −300.
- **Stable volcanic rock (TNGF):** NISAR reads +60 where the ground is stable; Sentinel-1 reads −11. This is the
  smooth tilt left in the NISAR map (the west of the city reads +15 to +50 in NISAR). With only 2 GNSS
  stations, the tilt cannot be removed; a weather-model (troposphere) correction or a longer period would.

Papers:
- Cigna, F., & Tapete, D. (2021). Present-day land subsidence rates, surface faulting hazard and risk in Mexico
  City with 2014–2020 Sentinel-1 IW InSAR. *Remote Sensing of Environment*, 253, 112161.
  https://doi.org/10.1016/j.rse.2020.112161
- Chaussard, E., Wdowinski, S., Cabral-Cano, E., & Amelung, F. (2014). Land subsidence in central Mexico
  detected by ALOS InSAR time-series. *Remote Sensing of Environment*, 140, 94–106.
  https://doi.org/10.1016/j.rse.2013.08.038

## Central Valley (Tulare Lake basin): a hard case, reported honestly

Winter 2025-26 (Nov 24 → Mar 24), about 5,200 km², 9 GNSS stations.

- **Little real motion in this window.** GNSS over the same dates shows −5 to −44 mm/yr (the strongest:
  TLBN, −44 ± 7; its long-term rate is −113 mm/yr, so winter is much slower than the yearly average).
- **Large atmospheric and ionospheric errors.** NISAR pairs with strong ionosphere (up to 104 mm, 1σ, on
  Nov 24) were corrected well, which also confirms the correction's sign: the spread of that pair fell from
  107 mm to 10 mm. But over 4 months the smooth tilts that remain from the first and last dates are still
  of the order of 100 mm/yr across the area.
- With the GNSS plane tie, NISAR's leave-one-out error against the 9 stations is **51 mm/yr**. That
  matches its predicted noise (54 mm/yr): the error budget is understood, but the winter signal is too
  small for this period length.
- The Sentinel-1 product here (a single OPERA file covering only 29% of the area) disagrees with both GNSS
  (+118 mm/yr at HANF, where GNSS shows −41) and NISAR. The Mexico City test rules out a sign error, so this
  is an unreliable file for this window. The app flags it (RMSE 92 mm/yr, weak agreement).

**Conclusion:** for slow or seasonal motion, a few months of data are not enough. Use a year or more, so
the atmosphere of individual dates averages out and the seasonal cycle does not dominate.

## What changed in the app because of these tests

| Found in a real run | Fix |
|---|---|
| Separately unwrapped pieces made fake ±500-1000 mm/yr spots | NISAR pieces are lined up by whole cycles; C-band keeps the largest piece |
| Sentinel-1 pairs from 2025 compared with NISAR pairs from 2026 | Each satellite's dates are chosen to overlap the other's |
| OPERA files were chained the wrong way | Path search through reference-date changes, as in opera-utils |
| Rate noise overstated (165 mm/yr) | Atmosphere counted once per gapless chain, pixel noise once per pair |
| "Uplift" reported where the area just sank less than its median | Maps tied to GNSS (absolute); wording depends on the reference |
| GNSS compared over the wrong period | GNSS measured over each satellite's own dates |
| Smooth tilt dominates short periods | GNSS plane tie with leave-one-out checking (with ≥ 5 stations) |
| A track that only clips the area was chosen | Geometry and frames chosen by footprint coverage |
| A single unavailable file stopped the run | That file is skipped; a refused login still stops the run |
| Single-pixel outliers reported as peaks | Peak = fastest 5% of the area |

## Still to do

- Fit the rate as a trend through all dates (time-series velocity), not only end minus start. That averages
  the atmosphere of every date and should cut the noise substantially.
- Read incidence angles from OPERA's DISP-S1-STATIC product (38° is assumed now; about 5% scale error).
- Re-run both sites over a full year once OPERA products for mid-2026 are published.
- Add published rates for the Central Valley (e.g. from the California DWR InSAR data or the papers in
  [validation-guide.md](validation-guide.md)), taken from the sources themselves.
