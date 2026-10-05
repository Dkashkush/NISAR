# Put the app online with Hugging Face Spaces (free)

1. **Make the upload folder.** Run `python scripts/make_hf_space.py`, or use the provided `hf-space.zip`,
   and unzip it. You get a folder `hf-space/` with 34 files.
2. **Create an account** at <https://huggingface.co/join> and confirm your email.
3. **Create the Space:** <https://huggingface.co/new-space>
   - Space name: e.g. `nisar-vs-sentinel1`
   - License: e.g. MIT
   - SDK: **Docker**, then template **Blank**
   - Hardware: **CPU basic (free)**
   - Visibility: **Public** (for a portfolio link) or Private
   - Click **Create Space**.
4. **Upload the files.** In the Space, open **Files** → **+ Contribute** / **Add file** → **Upload files**.
   Drag in **everything inside** `hf-space/`, not the folder itself, including the `sarcompare`, `docs`,
   `examples` and `.streamlit` folders. Allow it to **replace README.md**: the new one carries the Space
   settings. Click **Commit changes to main**.
5. **Wait for the build.** The Space shows **Building**; click **Logs** to watch. The first build takes
   about 5–10 minutes. When it says **Running**, the app appears on the Space page.
6. **Share the link:** `https://huggingface.co/spaces/<your-username>/<space-name>`

Notes
- **Hidden folders:** if the upload skips `.streamlit/` or `.dockerignore`, the app still works; they only
  set the upload limit and slim the build.
- **Updating:** run the script again and upload the changed files. The Space rebuilds automatically.
- **Sleeping:** a free Space sleeps after a period without visitors and wakes up on the next visit, which
  takes a minute.
- **Keep your token out:** never add your own Earthdata token as a Space secret. Visitors type their own.
- **If the build fails:** open **Logs**, copy the last 30 lines, and use them to find the problem.
