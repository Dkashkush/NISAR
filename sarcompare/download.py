"""Authenticated downloads from ASF (NASA Earthdata login)."""

from __future__ import annotations

import netrc
import os
from pathlib import Path
from typing import Callable

from .products import Product

EARTHDATA_HOST = "urs.earthdata.nasa.gov"


def clean_token(token: str | None) -> str | None:
    """Remove what often sneaks in when a token is copied: spaces, line breaks, quotes, a 'Bearer ' prefix."""
    if not token:
        return None
    t = token.strip().strip('"').strip("'").strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    return "".join(t.split()) or None


def describe_token(token: str | None) -> str:
    """Safe description (never the token itself) to help diagnose a rejected token."""
    t = clean_token(token)
    if not t:
        return "no token"
    looks_jwt = t.startswith("eyJ") and t.count(".") == 2
    return (f"{len(t)} characters, {'looks like' if looks_jwt else 'does NOT look like'} an Earthdata token "
            "(those are long, about 500-1500 characters, start with 'eyJ' and contain two dots)")


def make_session(token: str | None = None):
    """ASF session authenticated from an explicit token (per user, e.g. typed into the web app), else
    EARTHDATA_TOKEN, EARTHDATA_USERNAME/PASSWORD, or ~/.netrc on the machine running the app."""
    import asf_search as asf

    session = asf.ASFSession()
    token = clean_token(token or os.environ.get("EARTHDATA_TOKEN"))
    user = os.environ.get("EARTHDATA_USERNAME")
    password = os.environ.get("EARTHDATA_PASSWORD")
    if token:
        try:
            return session.auth_with_token(token)
        except asf.exceptions.ASFAuthenticationError as e:
            raise RuntimeError(
                "NASA Earthdata rejected the token (" + describe_token(token) + "). Generate a new one at "
                "https://urs.earthdata.nasa.gov (Generate Token), copy the whole token, and save it again.") from e
    if user and password:
        return session.auth_with_creds(user, password)
    try:
        creds = netrc.netrc().authenticators(EARTHDATA_HOST)
    except (FileNotFoundError, netrc.NetrcParseError):
        creds = None
    if creds:
        return session.auth_with_creds(creds[0], creds[2])
    raise RuntimeError(
        "No Earthdata credentials found. Create a free account at https://urs.earthdata.nasa.gov and then "
        "either set EARTHDATA_TOKEN (or EARTHDATA_USERNAME + EARTHDATA_PASSWORD), or add a line to ~/.netrc:\n"
        f"  machine {EARTHDATA_HOST} login <user> password <password>"
    )


def download_products(products: list[Product], dest: str | Path, session=None,
                      progress: Callable[[int, int, Product], None] | None = None) -> list[Product]:
    """Download each product's data file into ``dest/<sensor>/``; existing files are reused."""
    import asf_search as asf

    dest = Path(dest)
    for i, p in enumerate(products):
        if progress:
            progress(i, len(products), p)
        if p.local_path and Path(p.local_path).exists():
            continue
        if not p.url:
            raise ValueError(f"Product {p.name} has no download URL")
        folder = dest / p.sensor.replace(" ", "_")
        folder.mkdir(parents=True, exist_ok=True)
        filename = p.url.rstrip("/").split("/")[-1]
        target = folder / filename
        if not (target.exists() and target.stat().st_size > 0):
            if session is None:
                session = make_session()
            asf.download_url(p.url, path=str(folder), filename=filename, session=session)
        p.local_path = target
    return products
