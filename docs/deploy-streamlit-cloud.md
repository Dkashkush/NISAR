# Put the app online with Streamlit Community Cloud (free)

Streamlit Community Cloud runs Streamlit apps straight from a GitHub repository, for free. Since October 2026
Hugging Face charges (PRO subscription) to run apps like this one, so this is the free option. Everything it
needs is already in the repository: `app.py`, `requirements.txt`, `packages.txt` and `.streamlit/config.toml`.

The screens on share.streamlit.io change from time to time; if a button has a slightly different name, look
for the closest match.

## Steps (about 15 minutes, most of it waiting)

1. Open <https://share.streamlit.io> and choose **Continue with GitHub**. Allow Streamlit to see your
   repositories when GitHub asks.
2. Click **Create app** (top right), then choose to deploy an app from GitHub.
3. Fill in:
   - **Repository:** `Dkashkush/opensar-explorer`
   - **Branch:** `claude/clever-ptolemy-0wvgd1` (the repository's default branch; use `main` if you rename it)
   - **Main file path:** `app.py`
   - **App URL:** choose a short name, e.g. `opensar-explorer` (gives `opensar-explorer.streamlit.app`)
4. Open **Advanced settings**:
   - **Python version:** 3.11
   - **Secrets:** paste these three lines:
     ```toml
     SARCOMPARE_ONLINE = "1"
     SARCOMPARE_MAX_AOI_KM2 = "1000"
     SARCOMPARE_MAX_PAIRS = "6"
     ```
     The first switches on hosted mode: each visitor's Earthdata token is used only for their session and
     never stored, files are kept per visitor, and "Use files I already have" is hidden. The other two keep
     jobs small enough for the free server's memory (about 1-2.7 GB).
   - **Never put your own Earthdata token here.** Visitors type their own.
5. Click **Deploy**. The first build installs the scientific libraries and takes about 5-10 minutes. The app
   then opens at its link.
6. Test it: open the link in a private browser window, run **Try the demo**, then a small real area (for
   example Mexico City with 6 pairs) with your token.

## Good to know

- **Updates are automatic:** every push to the branch redeploys the app.
- **It sleeps** after some days without visitors. The next visitor clicks "wake up" and waits a minute.
- **Memory is limited.** If a large job makes the app restart, lower `SARCOMPARE_MAX_AOI_KM2` or
  `SARCOMPARE_MAX_PAIRS` in the app's **Settings → Secrets**. Big studies are better run on your own computer
  or with the Real-data test workflow on GitHub.
- **Logs:** if the build or the app fails, open **Manage app** (bottom right of the app) to see the log.
- **`packages.txt` must list only package names**, one per line: Streamlit passes every word in it to the
  system installer, comments included. It holds `libexpat1`, which rasterio needs.
