# Put the app online with Hugging Face Spaces

> **October 2026:** Hugging Face now requires a PRO subscription to run Docker Spaces, even on the free "CPU
> basic" hardware (the upload is refused with "402 Payment Required"). For a free option, use
> [Streamlit Community Cloud](deploy-streamlit-cloud.md). With PRO, the steps below still work; the deploy
> workflow now runs only when you start it from the Actions tab.

There are two ways. **Option A (automatic, recommended):** GitHub publishes the app for you and updates it
on every change. **Option B (manual):** drag and drop the files.

## Option A: automatic deployment from GitHub

1. Create a free account at <https://huggingface.co/join> and confirm your email.
2. Create a token: Hugging Face → your profile picture → **Settings** → **Access Tokens** → **Create new
   token** → type **Write** → name it e.g. `github-deploy` → **Create** → copy it.
3. Give it to GitHub: open this repository on GitHub → **Settings** → **Secrets and variables** →
   **Actions** → **New repository secret** → Name: `HF_TOKEN`, Secret: paste the token → **Add secret**.
4. Start the first deployment: GitHub → **Actions** tab → **Deploy to Hugging Face Space** → **Run
   workflow**. After this, every push deploys automatically.
5. After about 1 minute the workflow summary shows your link,
   `https://huggingface.co/spaces/<your-hf-username>/opensar-explorer`. The Space then builds for 5–10
   minutes before the app appears.

The Space is created for you as **private**, so only you can see it while you check it. When you are
happy, make it public on Hugging Face: your Space → **Settings** → **Change Space visibility** → **Public**.
To use a different name, add a repository **variable** (not a secret) called
`HF_SPACE` with the value `your-hf-username/your-space-name`.

## Option B: manual upload

1. **Make the upload folder.** Run `python scripts/make_hf_space.py`, or use the provided `hf-space.zip`,
   and unzip it. You get a folder `hf-space/` with 34 files.
2. **Create an account** at <https://huggingface.co/join> and confirm your email.
3. **Create the Space:** <https://huggingface.co/new-space>
   - Space name: e.g. `opensar-explorer`
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
