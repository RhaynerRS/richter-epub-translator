from __future__ import annotations

import re
import zipfile
from math import ceil
from pathlib import Path

import tiktoken

from .config import settings

_TAG_RE = re.compile(r"<[^>]+>")
_HTML_EXTENSIONS = (".xhtml", ".html", ".htm")


def estimate_billable_tokens(epub_path: Path) -> int:
    """Rough token count for pricing purposes only. Reads the epub read-only
    (doesn't reuse epub_translator.epub.zip.Zip, which also requires a write
    path and has no read-only mode) and strips tags with a simple regex — a
    deliberate approximation that doesn't need to match the translator's
    internal segmentation."""
    encoding = tiktoken.get_encoding(settings.token_encoding)
    total_tokens = 0
    with zipfile.ZipFile(epub_path, "r") as archive:
        for name in archive.namelist():
            if not name.lower().endswith(_HTML_EXTENSIONS):
                continue
            raw = archive.read(name).decode("utf-8", errors="ignore")
            text = _TAG_RE.sub(" ", raw)
            total_tokens += len(encoding.encode(text))
    return total_tokens


def estimate_price_cents(tokens: int) -> int:
    return max(ceil(tokens / 1000 * settings.price_per_1k_tokens_cents), settings.min_charge_cents)
