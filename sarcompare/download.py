"""Authenticated downloads from ASF (NASA Earthdata login)."""

from __future__ import annotations

import logging
import netrc
import os
from pathlib import Path
from typing import Callable

from .products import Product

EARTHDATA_HOST = "urs.earthdata.nasa.gov"

log = logging.getLogger(__name__)


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
    looks_jwt = t.startswith("eyJ") and t.count(".") == 2 and len(t) > 300
    return (f"{len(t)} characters, {'looks like' if looks_jwt else 'does NOT look like'} an Earthdata token "
            "(those are long, about 500-1500 characters, start with 'eyJ' and contain two dots)")


def check_token(session, token: str) -> tuple[bool, str]:
    """Ask Earthdata whether ``token`` is valid for ASF's download application: (ok, reason). Never echoes the token."""
    import requests

    host = getattr(session, "edl_host", EARTHDATA_HOST)
    client = getattr(session, "edl_client_id", "BO_n7nTIlMljdvU6kRRB3g")  # ASF's Earthdata application
    url = f"https://{host}/oauth/tokens/user?client_id={client}"
    try:
        r = session.post(url, data={"token": token}, timeout=30)
    except requests.RequestException as e:
        return False, f"could not reach Earthdata ({type(e).__name__})"
    if 200 <= r.status_code < 300:
        return True, "accepted"
    try:
        body = r.json()
        why = str(body.get("error_description") or body.get("error") or "") if isinstance(body, dict) else ""
    except ValueError:
        why = ""
    why = why.replace(token, "<token>")[:200]
    return False, f"HTTP {r.status_code}" + (f" ({why})" if why else "")


def make_session(token: str | None = None):
    """ASF session authenticated from an explicit token (per user, e.g. typed into the web app), else
    EARTHDATA_TOKEN, EARTHDATA_USERNAME/PASSWORD, or ~/.netrc on the machine running the app.

    A token is always used, even when Earthdata's pre-check refuses it: that check concerns ASF's login
    application and can fail for a token that downloads fine. The first real file access then reports exactly
    what is wrong (see sarcompare.remote.AccessError). The pre-check result is kept in ``session.token_check``.
    """
    import asf_search as asf

    session = asf.ASFSession()
    # NISAR files are served from *.earthdatacloud.nasa.gov; keep the token on its redirects to Earthdata login
    # (it is still dropped on the final redirect to Amazon S3).
    if "earthdatacloud.nasa.gov" not in getattr(session, "auth_domains", ["earthdatacloud.nasa.gov"]):
        session.auth_domains = [*session.auth_domains, "earthdatacloud.nasa.gov"]
    session.token_check = None
    token = clean_token(token or os.environ.get("EARTHDATA_TOKEN"))
    user = os.environ.get("EARTHDATA_USERNAME")
    password = os.environ.get("EARTHDATA_PASSWORD")
    if token:
        session.token_check = check_token(session, token)
        if not session.token_check[0]:
            log.warning("Earthdata's token pre-check failed (%s; token: %s); using the token anyway.",
                        session.token_check[1], describe_token(token))
        session.headers["Authorization"] = f"Bearer {token}"
        return session
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
            _check_not_login_page(target)
        p.local_path = target
    return products


def _check_not_login_page(path: Path) -> None:
    """A refused download can save Earthdata's login page under the product's name; catch that early."""
    with open(path, "rb") as fh:
        head = fh.read(512).lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html")):
        path.unlink()
        from .remote import AUTHORIZE_HELP, AccessError

        raise AccessError("NASA Earthdata sent its login page instead of the file, so the token was not accepted "
                          "for ASF downloads. " + AUTHORIZE_HELP)
