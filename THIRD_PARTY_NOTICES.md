# Third-party notices

OpenSAR Explorer is Copyright © 2026 Deepa Kumari, all rights reserved (see [LICENSE](LICENSE)). It relies on the
data and software below, which are **not** covered by that copyright and remain under their owners' terms.

## Data

| Data | Owner and terms | Credit used in reports |
|---|---|---|
| NISAR L2 GUNW interferograms | NASA/ISRO; distributed by the ASF DAAC; free and open | "NISAR data: NASA/ISRO, distributed by the ASF DAAC" |
| Sentinel-1 data | European Union, Copernicus programme (ESA); free, full and open | "Contains modified Copernicus Sentinel data" |
| ARIA S1 GUNW and OPERA DISP-S1 | NASA JPL; distributed by the ASF DAAC; free and open | "Sentinel-1 interferometric products (ARIA, OPERA (NASA JPL) or HyP3 (ASF)) distributed by the ASF DAAC" |
| ASF HyP3 on-demand interferograms | Processed by ASF's HyP3 service from Copernicus Sentinel-1 data | as above |
| LiCSAR interferograms | COMET (UK Centre for the Observation and Modelling of Earthquakes, Volcanoes and Tectonics); see the provider's licence | Lazecký, M., et al. (2020), *Remote Sensing*, 12(15), 2430, https://doi.org/10.3390/rs12152430 |
| EGMS Ortho maps | European Union, Copernicus Land Monitoring Service; free, full and open | "European Ground Motion Service (EGMS), Copernicus Land Monitoring Service" |
| GNSS time series | Nevada Geodetic Laboratory | Blewitt, G., Hammond, W. C., & Kreemer, C. (2018), *Eos*, 99 |
| Place search | OpenStreetMap Nominatim (data © OpenStreetMap contributors, ODbL); used under its usage policy (one request per search, identified by name) | "© OpenStreetMap contributors" |
| Map backgrounds | OpenStreetMap (ODbL) and Esri World Imagery (Esri and its data providers, Esri terms of use) | shown on every map |
| Published values (examples) | The cited papers; short factual values quoted with full citations | full citation in each report |

## Software

These packages are installed alongside OpenSAR Explorer (they are not part of this repository) and are used
under their own open-source licences, as stated in their package metadata (October 2026):

| Package | Licence |
|---|---|
| numpy | BSD-3-Clause (and bundled 0BSD, MIT, Zlib, CC0-1.0 parts) |
| scipy | BSD |
| xarray | Apache-2.0 |
| h5py | BSD-3-Clause |
| netCDF4 | MIT |
| rasterio | BSD |
| rioxarray | Apache |
| pyproj | MIT |
| shapely | BSD-3-Clause |
| matplotlib | Matplotlib license (PSF-based) |
| PyYAML | MIT |
| asf_search | BSD |
| fsspec | BSD-3-Clause |
| aiohttp | Apache-2.0 and MIT |
| requests | Apache-2.0 |
| streamlit | Apache-2.0 |
| folium, branca | MIT |
| streamlit-folium | see its project page (not stated in the installed package) |

Product reading follows the published conventions of MintPy (Yunjun et al., 2019) and opera-utils.
