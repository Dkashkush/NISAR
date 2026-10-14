# Copyright (c) 2026 Deepa Kumari. All rights reserved. See LICENSE.
"""Read only the needed parts of remote HDF5/NetCDF products, without downloading whole files.

A NISAR GUNW file is about 2-3 GB, but a typical study area needs only a few tens of MB of it. HDF5 files can
be read with HTTP range requests (the same approach as opera-utils' ``open_h5``): the file is opened as a
file-like object that fetches blocks on demand.

ASF download links redirect (with your Earthdata token) to a short-lived signed address on Amazon S3. That
address is resolved first with the authenticated session, then read anonymously; the file size comes from the
first request, because signed S3 addresses usually refuse the HEAD request fsspec would otherwise send.
"""

from __future__ import annotations

from contextlib import contextmanager
from urllib.parse import urlparse

STREAMABLE_SUFFIXES = (".h5", ".nc", ".he5", ".hdf5")


def is_streamable(url: str | None) -> bool:
    return bool(url) and url.split("?")[0].lower().endswith(STREAMABLE_SUFFIXES)


class AccessError(Exception):
    """The server refused the file (login, permission). Downloading it in full would fail the same way."""


class UnavailableError(RuntimeError):
    """The login worked but the storage server refused this one file (often: not, or no longer, published).
    Downloading would fail the same way; the file is skipped."""


AUTHORIZE_HELP = (
    "Fix: sign in at https://urs.earthdata.nasa.gov, open Applications > Authorized Apps, and make sure "
    "'Alaska Satellite Facility Data Access' is listed (if not: Approve More Applications, search 'Alaska "
    "Satellite Facility', Authorize). Signing in once at https://search.asf.alaska.edu does the same. "
    "If the token is older than 60 days, generate a new one and save it again.")


def _host(url: str) -> str:
    return urlparse(url).hostname or "?"


def hops(r) -> str:
    """Redirect chain as host names and status codes only (signed addresses are not shown)."""
    steps = [*getattr(r, "history", []), r]
    return " -> ".join(f"{_host(x.url)} ({x.status_code})" for x in steps)


def access_problem(r) -> str | None:
    """Plain-language reason when a response is a refusal or a login page instead of the file."""
    host = _host(r.url)
    html = "text/html" in r.headers.get("Content-Type", "").lower()
    if host.endswith("urs.earthdata.nasa.gov") or (html and r.status_code in (200, 206)):
        return ("NASA Earthdata answered with its login page instead of the file, so the token was not "
                f"accepted for ASF downloads [{hops(r)}]. " + AUTHORIZE_HELP)
    if r.status_code == 401:
        return f"the token was refused (HTTP 401) [{hops(r)}]. It may be expired or revoked. " + AUTHORIZE_HELP
    hosts = [_host(x.url) for x in [*getattr(r, "history", []), r]]
    if r.status_code in (403, 404) and len(set(hosts)) > 1 and not any("earthdata" in h for h in hosts[-1:]):
        return None  # redirected past the login to storage: this file is unavailable, not the account (see probe)
    if r.status_code == 403:
        return (f"access refused (HTTP 403) [{hops(r)}]. The account may still need to approve ASF's application "
                "or a data-use agreement; download one file by hand at https://search.asf.alaska.edu and "
                "accept any prompts. " + AUTHORIZE_HELP)
    return None


def probe(url: str, session, timeout: float = 60) -> tuple[str, int]:
    """Follow redirects with the authenticated session; return (final URL, file size in bytes)."""
    r = session.get(url, headers={"Range": "bytes=0-0"}, stream=True, allow_redirects=True, timeout=timeout)
    try:
        problem = access_problem(r)
        if problem:
            raise AccessError(problem)
        if r.status_code in (403, 404) and r.history:
            raise UnavailableError(f"the storage server refused this file (HTTP {r.status_code}) [{hops(r)}] after the "
                                   "login succeeded; it may not be published (yet) or may have been withdrawn")
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} when opening {url.split('?')[0]} [{hops(r)}]")
        if r.status_code != 206:
            raise RuntimeError("the server does not support partial reads (HTTP range requests)")
        content_range = r.headers.get("Content-Range", "")
        if "/" not in content_range or content_range.endswith("/*"):
            raise RuntimeError("the server did not report the file size")
        return r.url, int(content_range.rsplit("/", 1)[1])
    finally:
        r.close()


@contextmanager
def open_remote(url: str, session, block_size: int = 8 * 1024 * 1024):
    """Yield a seekable file-like object for ``url`` that downloads only the blocks that are read."""
    import fsspec

    final, size = probe(url, session)
    headers = {}
    if final == url and getattr(session, "headers", None) and "Authorization" in session.headers:
        headers["Authorization"] = session.headers["Authorization"]  # no redirect: same host still needs auth
    # encoded=True keeps signed URLs byte-for-byte (re-quoting would break the signature)
    fs = fsspec.filesystem("https", encoded=True, skip_instance_cache=True, client_kwargs={"headers": headers})
    f = fs.open(final, mode="rb", block_size=block_size, cache_type="blockcache", size=size)
    try:
        yield f
    finally:
        f.close()
