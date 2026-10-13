# Backing results with published evidence

Results become trustworthy when independent sources agree with them. OpenSAR Explorer can compare each run
with three kinds of published evidence, and the report lists every source in its **References** section.

| Evidence | What it is | How to use it in the app |
|---|---|---|
| **GNSS stations** | Precise ground motion measured on the ground, at points | Turn on *Compare with GNSS ground stations* (automatic, worldwide), or upload a CSV of station velocities from a paper |
| **Published velocity maps** | Ground-motion maps released by agencies or with papers | Upload the GeoTIFF/NetCDF and fill in its units, direction (vertical/LOS) and citation |
| **Rates from papers** | Numbers reported in peer-reviewed studies or official reports | Type them into *Compare with rates from published studies*: citation, place, coordinates, rate, period |

## 1. GNSS: the referee
GNSS stations are the standard independent check for InSAR. The app downloads time series from the Nevada
Geodetic Laboratory and fits velocities **over the same period as the satellites**. Report the result as:
*"Agrees with N GNSS stations within X mm/yr (RMSE)."*

## 2. Published velocity maps: where to find them
| Region | Source | Notes for the app |
|---|---|---|
| Europe | European Ground Motion Service (EGMS), Copernicus | Use the *Ortho* vertical product: vertical, mm/yr, positive = up |
| Worldwide (selected frames) | COMET LiCSAR / LiCSBAS portal (University of Leeds) | Usually LOS velocity in mm/yr; enter the incidence angle |
| North America | NASA OPERA DISP-S1 (ASF) | LOS displacement; convert to a velocity first, or use it as the Sentinel-1 source |
| California | California Department of Water Resources (DWR), InSAR subsidence data (TRE ALTAMIRA), on the California Natural Resources Agency open-data portal | Vertical displacement, usually in **feet** over a stated period: choose *ft (total over a period)* and enter the years |
| Any | Supplementary data of papers (journal sites, Zenodo, PANGAEA) | Check units, sign and whether values are vertical or LOS |

## 3. Rates from papers: doing it properly
1. **Take numbers only from the paper itself** (text, tables or figures with stated values), not from news
   articles. Note the page or figure.
2. **Match the place.** Use coordinates the paper gives, or read them carefully from its map. Set a radius
   that matches the area the paper describes.
3. **Match the quantity.** Prefer **vertical** rates. Many papers report LOS rates; the config file accepts
   `component: LOS` with `incidence_deg`.
4. **Record the period.** Rates change over time. In the Central Valley, subsidence speeds up during droughts
   and pauses after wet winters, so a 2015 rate is not expected to equal a 2026 rate. A difference with a
   different period is information, not failure.
5. **Use ranges when the paper gives them** (e.g. "20–35 cm/yr"). In a config file, use `rate_min_mm_yr` and
   `rate_max_mm_yr`.
6. **Cite fully**: authors, year, title, journal, volume and pages or DOI.

### Starting points for the test sites
These are well-known studies to look up. I could not open them from the build environment, so **check every
detail and take all numbers from the papers themselves**:

- **Central Valley, California**
  - Faunt, C. C., Sneed, M., Traum, J., & Brandt, J. T. (2016). Water availability and land subsidence in the
    Central Valley, California, USA. *Hydrogeology Journal*, 24(3), 675–684.
  - Murray, K. D., & Lohman, R. B. (2018). Short-lived pause in Central California subsidence after heavy
    winter precipitation of 2017. *Science Advances*, 4(8).
  - California DWR InSAR subsidence data (TRE ALTAMIRA): the official, regularly updated map.
- **Mexico City**
  - Chaussard, E., Wdowinski, S., Cabral-Cano, E., & Amelung, F. (2014). Land subsidence in central Mexico
    detected by ALOS InSAR time-series. *Remote Sensing of Environment*, 140, 94–106.
  - Cigna, F., & Tapete, D. (2021). Present-day land subsidence rates, surface faulting hazard and risk in
    Mexico City with 2014–2020 Sentinel-1 IW InSAR. *Remote Sensing of Environment*, 253.
- **Tehran**
  - Haghshenas Haghighi, M., & Motagh, M. (2019). Ground surface response to continuous compaction of aquifer
    system in Tehran, Iran: results from a long-term multi-sensor InSAR analysis. *Remote Sensing of
    Environment*, 221, 534–550.
- **Jakarta**
  - Abidin, H. Z., Andreas, H., Gumilar, I., Fukuda, Y., Pohan, Y. E., & Deguchi, T. (2011). Land subsidence of
    Jakarta (Indonesia) and its relation with urban development. *Natural Hazards*, 59, 1753–1771.

## Config file example
```yaml
published_values:
  - citation: "Author, A. et al. (2021). Title. Journal, vol, pages."
    place: "district or landmark"
    lon: -119.55
    lat: 36.05
    radius_km: 2
    rate_mm_yr: -150           # vertical, negative = sinking (the paper's central value)
    rate_min_mm_yr: -200       # optional range from the paper
    rate_max_mm_yr: -100
    period: "2015-2020"
published_maps:
  - path: data/published/dwr_vertical.tif
    label: "DWR InSAR vertical displacement"
    units: ft                  # total displacement over the period
    period_years: 1
    component: vertical
    sign: 1                    # + = up
    citation: "California Department of Water Resources, TRE ALTAMIRA InSAR subsidence data"
```

## How to word findings
- Good: *"NISAR shows up to X cm/yr of subsidence near Y (Oct 2025–Jun 2026), consistent with GNSS (RMSE Z
  mm/yr) and with the rates reported by Author et al. (2021) for 2015–2020."*
- Avoid: *"This predicts…"* or *"This proves…"*. Ground-motion maps show where and how fast the ground is
  moving now. They do not forecast disasters.
