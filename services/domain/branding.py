"""White-label the two static pages the API serves (the admin panel and the developer portal).

The pages are written with the engine name. At serve time every capitalised whole-word "Scotri" in
the text becomes the deployment's display name (config/client.yaml), and a configured logo replaces
the built-in mark and favicon. Lower-case identifiers (`scotri_start`, CSS classes, file names) are
untouched: they are the engine's API, not its branding. Pure: no I/O.
"""
from __future__ import annotations

import html
import re

from domain.client_config import ENGINE_NAME

_BRAND_WORD = re.compile(rf"\b{ENGINE_NAME}\b")
_FAVICON = re.compile(r'<link rel="icon"[^>]*>')
_ADMIN_MARK = re.compile(r'(<span[^>]*id="brand-mark"[^>]*>)<svg.*?</svg>(</span>)', re.DOTALL)
_PORTAL_MARK = re.compile(r'(<a class="brand"[^>]*>\s*)<svg.*?</svg>', re.DOTALL)

LOGO_ROUTE = "/brand/logo"


def brand_html(page: str, display_name: str, logo_url: str | None = None) -> str:
    if display_name and display_name != ENGINE_NAME:
        page = _BRAND_WORD.sub(html.escape(display_name, quote=True), page)
    if logo_url:
        src = html.escape(logo_url, quote=True)
        img = f'<img src="{src}" alt="" style="height:1.5em;width:auto;vertical-align:middle">'
        page = _FAVICON.sub(f'<link rel="icon" href="{src}">', page, count=1)
        page = _ADMIN_MARK.sub(lambda m: m.group(1) + img + m.group(2), page, count=1)
        page = _PORTAL_MARK.sub(lambda m: m.group(1) + img, page, count=1)
    return page
