# Privacy notice

This notice explains what the OpenSAR Explorer web app (https://opensar-explorer.streamlit.app) does with your
information. The app has no user accounts, adds no advertising and no analytics of its own, and does not sell
or share personal information.

## What the app handles

| Information | What happens to it |
|---|---|
| **Your NASA Earthdata token** | Kept in memory for your browser session only. Sent only to NASA Earthdata and the Alaska Satellite Facility (ASF), and to ASF's HyP3 service if you order Sentinel-1 interferograms. Never written to disk, logs, reports or result files. |
| **Your EGMS key** (optional) | Kept in memory for your session only. Sent only to the European Ground Motion Service to download its maps. Never stored. |
| **The area, dates and settings you choose** | Sent to the data services needed to find data: NASA/ASF search, the Nevada Geodetic Laboratory (GNSS), COMET LiCSAR, EGMS and ASF HyP3. |
| **Place names you type in the search box** | Sent to OpenStreetMap's Nominatim service to find the place. |
| **Map backgrounds** | Your browser loads map tiles directly from OpenStreetMap and Esri, which receive your IP address under their own policies. |
| **Files you upload, and your results** | Saved on the app's server in a folder unique to your browser session, not visible to other visitors, and deleted when the server restarts (often within days). Download what you want to keep. Public satellite data downloaded for one visitor may be reused for others; it contains no personal information. |
| **Sentinel-1 orders on ASF HyP3** | HyP3 jobs are publicly visible: the job name (your area's coordinates), the images used and your Earthdata user name. |

## Hosting

The app runs on Streamlit Community Cloud (Snowflake Inc.), which operates the server and may keep technical
logs (for example IP addresses and request times) under its own privacy policy. Streamlit's own usage
statistics are switched off in the app's settings.

## Your choices

You can use the demo without any token. You can delete or regenerate your Earthdata token at any time in your
Earthdata profile; doing so makes any token you typed here useless.

## Contact

Questions about privacy: contact the author through https://github.com/Dkashkush.

*Last updated: 14 October 2026.*
