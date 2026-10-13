"""Product readers. ``read_product`` dispatches on the product type."""

from __future__ import annotations

from ..aoi import AOI
from ..products import Product
from .base import Interferogram
from .licsar import read_licsar, read_licsar_product
from .nisar import read_nisar_gunw
from .sentinel1 import read_aria_gunw, read_hyp3_insar, read_opera_disp

__all__ = ["Interferogram", "read_product", "read_nisar_gunw", "read_aria_gunw", "read_opera_disp",
           "read_hyp3_insar", "read_licsar"]


def read_product(product: Product, aoi: AOI, *, nisar_polarization=None, nisar_apply_ionosphere=False,
                 nisar_sign=None, s1_sign=None, fileobj=None) -> Interferogram:
    """Read a downloaded product, or one opened remotely (``fileobj``, see sarcompare.remote)."""
    if product.product_type == "LICSAR":  # public GeoTIFFs: read directly, only the window over the area
        return read_licsar_product(product, aoi, sign=s1_sign)
    if product.local_path is None and fileobj is None:
        raise ValueError(f"{product.name} has not been downloaded")
    t = product.product_type
    src = fileobj if fileobj is not None else product.local_path
    name = (product.url or "").split("?")[0].rsplit("/", 1)[-1] if fileobj is not None else None
    if t == "GUNW":
        return read_nisar_gunw(src, aoi, polarization=nisar_polarization,
                               apply_ionosphere=nisar_apply_ionosphere, sign=nisar_sign, name=name)
    if t == "ARIA_GUNW":
        return read_aria_gunw(src, aoi, sign=s1_sign, name=name)
    if t == "DISP-S1":
        return read_opera_disp(src, aoi, sign=s1_sign, name=name)
    if t == "HYP3_INSAR":
        return read_hyp3_insar(src, aoi, sign=s1_sign)
    raise ValueError(f"No reader for product type {t!r}")
