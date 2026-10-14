# Security policy

## Reporting a problem

If you find a security problem in OpenSAR Explorer (for example a way to see another visitor's files or
credentials), please report it **privately**:

- on GitHub, open the repository's **Security** tab and choose **Report a vulnerability**, or
- contact the author through https://github.com/Dkashkush.

Please do not open a public issue for security problems, and never post tokens or keys anywhere.

## How credentials are handled

- Earthdata tokens and EGMS keys typed into the app are kept in memory for the session only and are never
  written to disk, logs, reports or results (see [PRIVACY.md](PRIVACY.md)).
- The Earthdata token is sent only to NASA Earthdata, ASF and ASF's HyP3 API; public result links (HyP3,
  LiCSAR) are read without it.
- The automated tests on GitHub use the repository secret `EARTHDATA_TOKEN`, which GitHub masks in logs.

## Supported version

Only the latest version on the default branch is maintained.
