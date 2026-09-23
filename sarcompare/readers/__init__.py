"""Product readers. ``read_product`` dispatches on the product type."""

from __future__ import annotations

from ..aoi import AOI
from ..products import Product
from .base import Interferogram
from .nisar import read_nisar_gunw
from .sentinel1 import read_aria_gunw, read_hyp3_insar, read_opera_disp

__all__ = ["Interferogram", "read_product", "read_nisar_gunw", "read_aria_gunw", "read_opera_disp",
           "read_hyp3_insar"]


def read_product(product: Product, aoi: AOI, *, nisar_polarization=None, nisar_apply_ionosphere=False,
                 nisar_sign=None, s1_sign=None) -> Interferogram:
    if product.local_path is None:
        raise ValueError(f"{product.name} has not been downloaded")
    t = product.product_type
    if t == "GUNW":
        return read_nisar_gunw(product.local_path, aoi, polarization=nisar_polarization,
                               apply_ionosphere=nisar_apply_ionosphere, sign=nisar_sign)
    if t == "ARIA_GUNW":
        return read_aria_gunw(product.local_path, aoi, sign=s1_sign)
    if t == "DISP-S1":
        return read_opera_disp(product.local_path, aoi, sign=s1_sign)
    if t == "HYP3_INSAR":
        return read_hyp3_insar(product.local_path, aoi, sign=s1_sign)
    raise ValueError(f"No reader for product type {t!r}")
