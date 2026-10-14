# Protecting OpenSAR Explorer: what is in place and what only you can do

Copyright © 2026 Deepa Kumari. All rights reserved. This page is practical guidance, not legal advice; for
commercial plans or a dispute, ask a lawyer or your institution's intellectual-property office.

## Already in place (in this repository)

| What | Where | Why it helps |
|---|---|---|
| Copyright notice, all rights reserved | [LICENSE](../LICENSE), app footer, every report, every source file's first line, README | Ownership is stated wherever the code or its output goes |
| Terms of use | [TERMS.md](../TERMS.md), linked in the app footer | Results are screening estimates (limits your liability); no warranty; acceptable use; what users may do with their results |
| Privacy notice | [PRIVACY.md](../PRIVACY.md), linked in the app footer | Says exactly what happens to tokens, uploads and place searches |
| Security policy | [SECURITY.md](../SECURITY.md) | Problems are reported privately, not in public |
| Third-party notices | [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) | Shows you respect the data providers' and libraries' licences, which protects you from claims |
| Citation file | [CITATION.cff](../CITATION.cff) | GitHub's "Cite this repository" button; others credit you correctly |
| Provenance in results | GeoTIFF tags, figure metadata, `metrics.json` | Every result file records "OpenSAR Explorer \<version\> (\<commit\>), © 2026 Deepa Kumari", so copies can be traced |
| Version in the app | footer, e.g. "Version 0.1.0 (fb05f85)" | Shows which dated version produced a result |
| Registration deposit builder | `python scripts/copyright_deposit.py` | Makes the source-code PDF that copyright offices ask for |
| No secrets in the repository | checked October 2026 | Your Earthdata token lives only in GitHub's encrypted secrets |

## Do these yourself (about 30 minutes)

### 1. Secure your GitHub account and repository
1. **Two-factor authentication:** GitHub → your photo → Settings → Password and authentication → Enable
   two-factor authentication. Whoever controls the account controls the code.
2. **Protect the main branch:** repository → Settings → Rules → Rulesets → New branch ruleset → target the
   default branch → tick "Restrict deletions" and "Block force pushes" → Create. History (your evidence of
   authorship) can then not be erased.
3. **Private vulnerability reporting, secret scanning and push protection:** repository → Settings →
   Security (Code security) → enable all three (free for public repositories).
4. **Earthdata token:** keep it only in Settings → Secrets and variables → Actions. Tokens expire after
   60 days; replace the secret then. Never put your own token in the Streamlit app's secrets.
5. **Hide the code if you prefer:** Settings → General → Danger zone → Change visibility → Private. The
   website keeps working only if Streamlit is allowed to read the private repository (check right after); the
   GitHub "fork" right that public repositories give other users then disappears.

### 2. Make a dated record of authorship
1. **GitHub release:** repository → Releases → Draft a new release → tag `v0.1.0` → Publish. This creates a
   dated, downloadable snapshot of this version.
2. **Your own copy:** download the release ZIP and the deposit PDF (step 3) and keep them somewhere dated, for
   example emailed to yourself or in cloud storage.
3. **Optional DOI:** connect the repository to Zenodo (zenodo.org → GitHub). Each GitHub release then gets a
   permanent, citable DOI with your name. Zenodo lets you choose restricted access if you do not want the code
   downloadable there.

### 3. Register the copyright (optional, strongest proof)
Copyright exists without registration, but a registration certificate is strong evidence in a dispute, and in
some countries (e.g. the USA) it is needed before suing.
1. Build the deposit: `python scripts/copyright_deposit.py` (whole source) or `--identifying` (first and last
   25 pages, the usual US requirement). The PDF lands in `dist/`.
2. File with your copyright office as a computer program (a "literary work"):
   - India: Copyright Office, https://copyright.gov.in (online filing; source-code copy and a fee).
   - USA: https://www.copyright.gov (eCO online system).
   - Elsewhere: your national office. Check each office's current forms, fees and deposit rules first.
3. **AI assistance must be described honestly.** Parts of the code were written with an AI assistant (Claude).
   Offices such as the US Copyright Office require AI-generated material to be disclosed and protect the human
   authorship: describe your own contributions (the concept, scientific design and choices, selection of
   methods and data, validation against GNSS and published studies, testing, and the arrangement and editing of
   the work). Ask the office or a lawyer how to word this.

### 4. The name "OpenSAR Explorer"
Copyright does not cover names. Before registering it as a trademark, search for similar names: for example ASF
already runs a service called "OpenSARLab". You may write "OpenSAR Explorer™" without registration to signal
that you use it as a name, but only a registered trademark (®) gives strong protection.

### 5. Check who owns it
If you built OpenSAR Explorer as part of a job, a fellowship or a degree, your contract may give your employer or
institution rights to it. Check before registering or selling.

### 6. If someone copies it
- Copies on GitHub: send a takedown notice through https://github.com/contact/dmca.
- Copies elsewhere: most hosts have a similar copyright-complaint form. The LICENSE, the dated release, the
  provenance tags and (if you have it) the registration certificate are your evidence.
- Before acting against a copy, check that it really copies your code or text, not only the idea: ideas and
  methods (e.g. comparing two satellites) are not protected by copyright.
