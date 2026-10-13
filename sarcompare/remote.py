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

STREAMABLE_SUFFIXES = (".h5", ".nc", ".he5", ".hdf5")


def is_streamable(url: str | None) -> bool:
    return bool(url) and url.split("?")[0].lower().endswith(STREAMABLE_SUFFIXES)


def probe(url: str, session, timeout: float = 60) -> tuple[str, int]:
    """Follow redirects with the authenticated session; return (final URL, file size in bytes)."""
    r = session.get(url, headers={"Range": "bytes=0-0"}, stream=True, allow_redirects=True, timeout=timeout)
    try:
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} when opening {url.split('?')[0]}")
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
