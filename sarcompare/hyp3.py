"""Order Sentinel-1 interferograms from ASF HyP3, for places without ready-made ones.

Sentinel-1 images almost all land every 12 days, but ready-made interferograms (ARIA, OPERA) exist only for some
regions. ASF's HyP3 service (https://hyp3-docs.asf.alaska.edu) makes them on request from the raw images
(INSAR_GAMMA jobs), free with an Earthdata login and a monthly allowance of processing credits.

1. ``plan_pairs`` finds the Sentinel-1 images (SLC) over the area and chooses a gapless chain of pairs whose dates
   match the NISAR pairs (same track and satellite, so the pairs add up without gaps).
2. ``order`` submits them as INSAR_GAMMA jobs named after the area (``job_name``), so they can be found again.
3. Later (processing usually takes under an hour, more when HyP3 is busy; results are kept for 14 days),
   ``find_products`` turns the finished jobs into products that the pipeline reads like any other Sentinel-1
   product: remotely, only the layers it needs.

The Earthdata token is sent only to the HyP3 API, never to the storage the results are downloaded from (those
links are public). As ASF notes, all HyP3 jobs are publicly visible (job name, images, user name).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from .aoi import AOI
from .notes import GOOD, INFO, WARN, Note
from .products import SENTINEL1, Product

API_URL = "https://hyp3-api.asf.alaska.edu"
JOB_TYPE = "INSAR_GAMMA"
LOOKS = "20x4"  # 80 m pixels: the app's grid is 90 m by default; "10x2" (40 m) costs more credits
DEFAULT_COST = 10.0  # credits per INSAR_GAMMA 20x4 job when the price list cannot be read
MAX_JOBS = 48  # never order more than this in one go
NAME_PREFIX = "opensar"
KEEP_DAYS = 14  # HyP3 deletes results this many days after processing

_SLC_DATE = re.compile(r"_(\d{8})T\d{6}_")


class HyP3Error(RuntimeError):
    """HyP3 refused a request; the message says why in plain language."""


# ---------------------------------------------------------------------------
# API client (plain HTTPS; the same calls as ASF's hyp3_sdk)
# ---------------------------------------------------------------------------


class HyP3:
    """Minimal HyP3 API client authenticated with an Earthdata token (a Bearer header, as in hyp3_sdk)."""

    def __init__(self, token: str, api_url: str = API_URL, session=None):
        import requests

        from .download import clean_token

        token = clean_token(token)
        if not token:
            raise HyP3Error("An Earthdata token is needed to order or find Sentinel-1 interferograms on HyP3.")
        self.url = api_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}", "User-Agent": "OpenSAR-Explorer"})

    def _check(self, r):
        if r.status_code == 401:
            raise HyP3Error("HyP3 did not accept the Earthdata token (HTTP 401). It may be expired; generate a new "
                            "one at https://urs.earthdata.nasa.gov. The account may also need to be enabled "
                            "for HyP3 once: sign in at https://search.asf.alaska.edu and open the On Demand queue.")
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", "")
            except ValueError:
                detail = ""
            raise HyP3Error(f"HyP3 answered HTTP {r.status_code}" + (f": {detail}" if detail else "."))
        return r.json()

    def get(self, path: str, **params) -> dict:
        r = self.session.get(f"{self.url}{path}", params={k: v for k, v in params.items() if v is not None},
                             timeout=60)
        return self._check(r)

    def user(self) -> dict:
        """Account information: ``remaining_credits`` (None = no limit) and ``job_names``."""
        return self.get("/user")

    def cost(self, job_type: str = JOB_TYPE, looks: str = LOOKS) -> float:
        """Credits per job, from HyP3's price list (``DEFAULT_COST`` if it cannot be read)."""
        try:
            entry = self.get("/costs").get(job_type, {})
        except Exception:  # price list unavailable: assume the documented default
            return DEFAULT_COST
        if "cost" in entry:
            return float(entry["cost"])
        table = entry.get("cost_table") or {}
        return float(table.get(looks, DEFAULT_COST)) if isinstance(table, dict) else DEFAULT_COST

    def jobs(self, name: str | None = None, job_type: str | None = JOB_TYPE) -> list[dict]:
        out: list[dict] = []
        body = self.get("/jobs", name=name, job_type=job_type)
        out += body.get("jobs", [])
        while body.get("next") and str(body["next"]).startswith(self.url + "/"):  # the token stays on the API host
            body = self._check(self.session.get(body["next"], timeout=60))
            out += body.get("jobs", [])
        return out

    def supports_validate_only(self) -> bool:
        """True when HyP3's published API description offers 'validate_only' (check an order without submitting).
        Checked first because a server that ignored an unknown field would submit the jobs for real."""
        for path in ("/openapi.json", "/openapi.yaml"):
            try:
                r = self.session.get(f"{self.url}{path}", timeout=60)
            except Exception:
                continue
            if r.status_code == 200 and "validate_only" in r.text:
                return True
        return False

    def submit(self, jobs: list[dict], validate_only: bool = False) -> list[dict]:
        payload: dict = {"jobs": jobs}
        if validate_only:
            if not self.supports_validate_only():
                raise HyP3Error("HyP3 does not describe a check-only mode, so nothing was sent (a check could "
                                "otherwise be processed as a real order).")
            payload["validate_only"] = True
        r = self.session.post(f"{self.url}/jobs", json=payload, timeout=120)
        return self._check(r).get("jobs", [])


# ---------------------------------------------------------------------------
# Naming: jobs for an area are found again from the area itself
# ---------------------------------------------------------------------------


def job_name(aoi: AOI) -> str:
    """'opensar W,S,E,N' (2 decimals). HyP3 lists a user's job names, so later runs over an overlapping area
    find these jobs even if the box was redrawn slightly."""
    w, s, e, n = aoi.bounds
    return f"{NAME_PREFIX} {w:.2f},{s:.2f},{e:.2f},{n:.2f}"


def _name_bounds(name: str) -> tuple[float, float, float, float] | None:
    if not name or not name.startswith(NAME_PREFIX + " "):
        return None
    try:
        w, s, e, n = (float(v) for v in name.split(" ", 1)[1].split(","))
    except ValueError:
        return None
    return w, s, e, n


def matching_names(names: list[str], aoi: AOI) -> list[str]:
    """This app's job names whose area overlaps ``aoi`` (each result covers a whole ~250 km Sentinel-1 scene)."""
    w, s, e, n = aoi.bounds
    out = []
    for name in names or []:
        b = _name_bounds(name)
        if b and b[0] < e and b[2] > w and b[1] < n and b[3] > s:
            out.append(name)
    return out


# ---------------------------------------------------------------------------
# Planning pairs from Sentinel-1 SLC images
# ---------------------------------------------------------------------------


@dataclass
class PlannedPair:
    reference: str  # SLC scene name of the earlier image
    secondary: str
    date1: date
    date2: date
    direction: str
    path: int
    platform: str
    cover: float  # fraction of the area inside both images

    @property
    def span_days(self) -> int:
        return (self.date2 - self.date1).days

    def job(self, name: str) -> dict:
        return {"job_type": JOB_TYPE, "name": name,
                "job_parameters": {"granules": [self.reference, self.secondary], "looks": LOOKS,
                                   "include_look_vectors": True, "apply_water_mask": True}}


@dataclass
class _Scene:
    name: str
    day: date
    direction: str
    path: int
    platform: str
    cover: float
    footprint: object = field(default=None, repr=False)


def slc_date(name: str) -> date | None:
    m = _SLC_DATE.search(name or "")
    return datetime.strptime(m.group(1), "%Y%m%d").date() if m else None


def search_slc(aoi: AOI, start=None, end=None, flight_direction=None, max_results=3000) -> list[_Scene]:
    """Sentinel-1 IW SLC images with VV polarisation over the area (the input HyP3 needs)."""
    from shapely.geometry import shape

    from .search import _asf, _window

    asf = _asf()
    kw = dict(platform=asf.PLATFORM.SENTINEL1, processingLevel="SLC", beamMode="IW", intersectsWith=aoi.wkt,
              maxResults=max_results, **_window(start, end))
    if flight_direction:
        kw["flightDirection"] = flight_direction
    out = []
    for r in asf.search(**kw):
        p = r.properties
        name = p.get("sceneName") or p.get("fileID") or ""
        day = slc_date(name)
        pol = str(p.get("polarization") or "")
        if not day or "VV" not in pol.upper():
            continue  # HyP3 InSAR needs VV (or VV+VH)
        try:
            fp = shape(r.geometry)
            cover = float(fp.intersection(aoi.geometry).area / aoi.geometry.area)
        except (ValueError, TypeError, AttributeError, ZeroDivisionError):
            fp, cover = None, 0.0
        out.append(_Scene(name, day, (p.get("flightDirection") or "").upper(), int(p.get("pathNumber") or 0),
                          str(p.get("platform") or name[:3]), cover, fp))
    return out


def _nodes(target: list[date], avail: list[date], n_pairs: int, start=None, end=None) -> list[date]:
    """Acquisition dates for a chain of ``n_pairs`` pairs: as close as possible to the ``target`` dates (the
    NISAR chain), else evenly spread over the period."""
    if not avail:
        return []
    if target:
        want = sorted(set(target))
    else:
        lo = start if start and start > avail[0] else avail[0]
        hi = end if end and end < avail[-1] else avail[-1]
        k = n_pairs + 1
        want = [lo + (hi - lo) * i / (k - 1) for i in range(k)] if k > 1 else [hi]
    if len(want) > n_pairs + 1:  # subsample the target dates evenly, keeping the first and last
        idx = sorted({round(i * (len(want) - 1) / n_pairs) for i in range(n_pairs + 1)})
        want = [want[i] for i in idx]
    picked: list[date] = []
    for d in want:
        best = min(avail, key=lambda a: (abs((a - d).days), a))
        if not picked or best > picked[-1]:
            picked.append(best)
    return picked


def plan_pairs(aoi: AOI, start=None, end=None, *, target: dict[str, list[date]] | None = None,
               directions: list[str] | None = None, max_pairs: int = 6, min_span: int = 6,
               max_span: int = 120, min_cover: float = 0.5,
               scenes: list[_Scene] | None = None) -> tuple[list[PlannedPair], list[Note]]:
    """Choose Sentinel-1 pairs to order.

    ``target``: flight direction -> the NISAR chain's dates in that direction ("" = any direction), so that the
    Sentinel-1 pairs cover the same period. ``directions``: which flight directions to plan (default: the best
    covered one, plus the other if it also covers the area and ``target`` asks for both).
    """
    notes: list[Note] = []
    scenes = scenes if scenes is not None else search_slc(aoi, start, end)
    if not scenes:
        return [], [Note("search", WARN, "No Sentinel-1 images (SLC, VV) were found over this area and period, so "
                                         "nothing can be ordered. Sentinel-1 does not image some remote areas "
                                         "and oceans regularly.")]
    groups: dict[tuple, dict[date, _Scene]] = {}
    for sc in scenes:  # one image per date in each (direction, track, satellite): the one covering most of the area
        g = groups.setdefault((sc.direction, sc.path, sc.platform), {})
        if sc.day not in g or sc.cover > g[sc.day].cover:
            g[sc.day] = sc

    def score(key):
        days = groups[key]
        covers = sorted(s.cover for s in days.values())
        return (covers[len(covers) // 2], len(days))

    found = sorted(groups, key=lambda k: (k[0], k[1], k[2]))
    notes.append(Note("search", INFO, f"Sentinel-1 images over the area: {len(scenes)} on "
                                      + "; ".join(f"{k[0].lower()} track {k[1]} ({k[2]}, {len(groups[k])} dates "
                                                  f"{min(groups[k])} → {max(groups[k])}, covering "
                                                  f"{score(k)[0]:.0%})" for k in found[:8])
                                      + ("; …" if len(found) > 8 else "") + "."))
    target = target or {}
    if directions:
        wanted = [d.upper() for d in directions]
    else:  # the directions NISAR used, else the best-covered one
        ranked = sorted({k[0] for k in groups}, key=lambda d: max(score(k) for k in groups if k[0] == d),
                        reverse=True)
        wanted = [d for d in ranked if d in target] or ranked[:1]
    plan: list[PlannedPair] = []
    all_dates = sorted({d for v in target.values() for d in v})
    if not directions:  # if no wanted direction covers the area, fall back to the best one, on all NISAR dates
        usable = {k[0] for k in groups if score(k)[0] >= min_cover}
        if usable and not usable & set(wanted):
            best = max(usable, key=lambda d: max(score(k) for k in groups if k[0] == d))
            notes.append(Note("search", INFO, f"Sentinel-1 does not cover the area well from the "
                                              f"{'/'.join(w.lower() for w in wanted)} direction; using its "
                                              f"{best.lower()} passes on the NISAR dates instead."))
            wanted, target = [best], {best: all_dates}
    for direction in wanted:
        keys = [k for k in groups if k[0] == direction and score(k)[0] >= min_cover]
        if not keys:
            notes.append(Note("search", INFO, f"No {direction.lower()} Sentinel-1 track covers at least "
                                              f"{min_cover:.0%} of the area."))
            continue
        want = target.get(direction) or target.get("", []) or all_dates

        def fit(k, want=want):
            """Target dates with an image of this group within 6 days (satellites hand over: e.g. Sentinel-1D
            took over Sentinel-1A's tracks in 2026, so the group with most images may end before NISAR starts)."""
            days_k = sorted(groups[k])
            if not want:
                return sum(1 for d in days_k if (not start or d >= start) and (not end or d <= end))
            return sum(1 for d in want if min(abs((a - d).days) for a in days_k) <= 6)

        key = max(keys, key=lambda k: (fit(k), score(k)))
        days = groups[key]
        avail = sorted(days)
        chain = _nodes(want, avail, max_pairs, start, end)
        pairs = []
        for d1, d2 in zip(chain, chain[1:]):
            if not (min_span <= (d2 - d1).days <= max_span):
                continue
            a, b = days[d1], days[d2]
            pairs.append(PlannedPair(a.name, b.name, d1, d2, direction, key[1], key[2], min(a.cover, b.cover)))
        pairs = pairs[-max_pairs:]
        if not pairs:
            when = f" the NISAR dates ({min(want)} → {max(want)})" if want else " the period"
            notes.append(Note("search", WARN, f"Sentinel-1 {direction.lower()} track {key[1]} ({key[2]}) has images "
                                              f"from {avail[0]} to {avail[-1]}, but none that form pairs of "
                                              f"{min_span}-{max_span} days on{when}. Choose a period when both "
                                              "satellites have images."))
        if pairs:
            gaps = sum(1 for p, q in zip(pairs, pairs[1:]) if p.date2 != q.date1)
            notes.append(Note("search", GOOD if not gaps else INFO,
                              f"Sentinel-1 {direction.lower()} track {key[1]} ({key[2]}): {len(pairs)} pair(s) from "
                              f"{pairs[0].date1} to {pairs[-1].date2}"
                              + (" matching the NISAR dates" if target else "")
                              + (f"; {gaps} gap(s) where an image is missing" if gaps else ", without gaps")
                              + f"; the images cover {min(p.cover for p in pairs):.0%} of the area or more."))
        plan += pairs
    return plan[:MAX_JOBS], notes


# ---------------------------------------------------------------------------
# Ordering and finding results
# ---------------------------------------------------------------------------


def _alive(job: dict, now: datetime | None = None) -> bool:
    """Job that is processing, or finished with results not yet deleted."""
    status = job.get("status_code")
    if status in ("PENDING", "RUNNING"):
        return True
    if status != "SUCCEEDED":
        return False
    exp = job.get("expiration_time")
    if not exp:
        return True
    try:
        t = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
    except ValueError:
        return True
    return t > (now or datetime.now(timezone.utc))


def _granules(job: dict) -> tuple[str, ...]:
    return tuple((job.get("job_parameters") or {}).get("granules") or ())


def existing_jobs(client: HyP3, aoi: AOI) -> list[dict]:
    """This app's INSAR_GAMMA jobs for areas overlapping ``aoi``."""
    names = client.user().get("job_names")
    names = matching_names(names, aoi) if names is not None else [job_name(aoi)]
    jobs: list[dict] = []
    for name in names:
        jobs += client.jobs(name=name)
    return jobs


REQUEST_ACCESS = "https://hyp3-docs.asf.alaska.edu/using/requesting_access/"


def access_problem(info: dict) -> None:
    """New HyP3 users apply once for access (free); raise a plain explanation if this account has not."""
    status = str(info.get("application_status") or "APPROVED").upper()
    if status == "APPROVED":
        return
    why = {"NOT_STARTED": "has not applied for HyP3 access yet",
           "PENDING": "has applied for HyP3 access, and the application is still being reviewed",
           "REJECTED": "was not approved for HyP3 access"}.get(status, f"has HyP3 access status {status}")
    raise HyP3Error(f"This Earthdata account {why}. Access is free: apply once at {REQUEST_ACCESS} (or in ASF "
                    "Vertex, On Demand), then order again.")


@dataclass
class OrderResult:
    submitted: list[dict]
    skipped: int  # pairs already ordered (processing or finished)
    cost: float
    remaining: float | None
    notes: list[Note]


def order(client: HyP3, aoi: AOI, pairs: list[PlannedPair], validate_only: bool = False) -> OrderResult:
    """Submit the planned pairs that are not already ordered; checks the credits first."""
    info = client.user()
    access_problem(info)
    have = {_granules(j) for j in existing_jobs(client, aoi) if _alive(j)}
    todo = [p for p in pairs if (p.reference, p.secondary) not in have]
    skipped = len(pairs) - len(todo)
    remaining = info.get("remaining_credits")
    cost = client.cost() * len(todo)
    notes: list[Note] = []
    if not pairs:
        notes.append(Note("search", INFO, "No Sentinel-1 pairs were planned, so nothing was sent."))
        return OrderResult([], 0, 0.0, remaining, notes)
    if not todo:
        notes.append(Note("search", INFO, "All these Sentinel-1 pairs are already ordered; nothing new was sent."))
        return OrderResult([], skipped, 0.0, remaining, notes)
    if remaining is not None and cost > float(remaining):
        raise HyP3Error(f"These {len(todo)} pairs need {cost:.0f} HyP3 credits but the account has {remaining:.0f} "
                        "left this month. Order fewer pairs (lower 'Image pairs per satellite') or wait for the "
                        "monthly allowance to renew.")
    jobs = client.submit([p.job(job_name(aoi)) for p in todo], validate_only=validate_only)
    what = "checked (not submitted)" if validate_only else "ordered"
    notes.append(Note("search", GOOD, f"{len(todo)} Sentinel-1 interferogram(s) {what} from ASF HyP3 for "
                                      f"{cost:.0f} credits" + (f" ({remaining:.0f} were left)" if remaining is not None
                                                                else "") + "."
                                      + ("" if validate_only else
                                         " Processing usually takes under an hour (longer when HyP3 is busy). Run "
                                         "the same area again later: finished interferograms are found "
                                         f"automatically. HyP3 keeps them for {KEEP_DAYS} days.")))
    return OrderResult(jobs, skipped, cost, remaining, notes)


def status_counts(jobs: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for j in jobs:
        key = j.get("status_code", "?")
        if key == "SUCCEEDED" and not _alive(j):
            key = "EXPIRED"
        out[key] = out.get(key, 0) + 1
    return out


def _lookup_geometry(names: list[str]) -> dict[str, tuple[str | None, int | None]]:
    """Flight direction and track of SLC images (not in their names), from ASF's catalogue."""
    if not names:
        return {}
    try:
        from .search import _asf

        results = _asf().granule_search(sorted(set(names)))
    except Exception:  # catalogue unavailable: products are then grouped without direction
        return {}
    out = {}
    for r in results:
        p = r.properties
        out[p.get("sceneName") or p.get("fileID")] = ((p.get("flightDirection") or "").upper() or None,
                                                       p.get("pathNumber"))
    return out


def to_products(jobs: list[dict]) -> list[Product]:
    """Finished, not yet deleted jobs as Sentinel-1 products (read remotely from their public links)."""
    done = [j for j in jobs if j.get("status_code") == "SUCCEEDED" and _alive(j) and j.get("files")]
    geo = _lookup_geometry([_granules(j)[0] for j in done if _granules(j)])
    out, seen = [], set()
    for j in done:
        f = j["files"][0]
        url, filename = f.get("url"), f.get("filename") or str(f.get("url", "")).rsplit("/", 1)[-1]
        g = _granules(j)
        d = sorted(x for x in (slc_date(n) for n in g) if x)
        if not url or len(d) < 2 or g in seen:
            continue
        seen.add(g)
        direction, track = geo.get(g[0], (None, None)) if g else (None, None)
        out.append(Product(sensor=SENTINEL1, product_type="HYP3_INSAR", name=filename.rsplit(".", 1)[0], url=url,
                           date1=d[0], date2=d[-1], flight_direction=direction, track=track,
                           size_mb=(f.get("size") or 0) / 1e6 or None, extra={"hyp3_job": j.get("job_id")}))
    return out


def find_products(client: HyP3, aoi: AOI) -> tuple[list[Product], dict[str, int], list[Note]]:
    """Finished HyP3 interferograms for this area, the status of all its jobs, and notes explaining them."""
    jobs = existing_jobs(client, aoi)
    counts = status_counts(jobs)
    products = to_products(jobs)
    notes: list[Note] = []
    if jobs:
        text = ", ".join(f"{v} {k.lower()}" for k, v in sorted(counts.items()))
        busy = counts.get("PENDING", 0) + counts.get("RUNNING", 0)
        notes.append(Note("search", INFO, f"ASF HyP3 jobs ordered for this area: {text}."
                                          + (" Come back later to use the ones still processing." if busy else "")
                                          + (" Expired results were deleted by HyP3; order them again if needed."
                                             if counts.get("EXPIRED") else "")
                                          + (" Failed jobs usually mean the two images do not overlap enough."
                                             if counts.get("FAILED") else "")))
    return products, counts, notes


def anonymous_session():
    """Session without the Earthdata token, for HyP3's public result links (the token stays with ASF)."""
    import requests

    s = requests.Session()
    s.headers["User-Agent"] = "OpenSAR-Explorer"
    return s
