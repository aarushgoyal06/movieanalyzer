"""Polite, cached access to the-numbers.com plus parsers for its number formats."""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

import requests

BASE_URL = "https://www.the-numbers.com"
CACHE_DIR = Path(__file__).resolve().parent / "data" / "raw"
REQUEST_DELAY_S = 2.0
USER_AGENT = (
    "Mozilla/5.0 (compatible; box-office-runs/0.1; "
    "personal research project on weekly box office decay)"
)
# The server closes idle keep-alive connections, so the first retry after a
# dropped connection goes out immediately; HTTP errors always back off.
NETWORK_RETRY_WAITS_S = (0, 10, 30, 90)
HTTP_RETRY_WAITS_S = (10, 30, 90)

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
_last_request_at = 0.0


class NotFound(Exception):
    pass


def cache_path_for(path: str) -> Path:
    path = path.split("#")[0]
    slug = re.sub(r"[^A-Za-z0-9._()-]+", "_", path.strip("/")) or "index"
    digest = hashlib.sha1(path.encode("utf-8")).hexdigest()[:8]
    return CACHE_DIR / f"{slug[:150]}-{digest}.html"


def _wait_for_slot() -> None:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < REQUEST_DELAY_S:
        time.sleep(REQUEST_DELAY_S - elapsed)
    _last_request_at = time.monotonic()


def fetch(path: str, max_age_s: float | None = None) -> str:
    """Return the HTML for a site path such as "/movie/Fall-Guy-The-(2024)".

    Pages are cached on disk; a cached page is reused unless it is older than
    `max_age_s`. Missing pages raise NotFound and are remembered as missing.
    """
    cache_file = cache_path_for(path)
    missing_marker = cache_file.with_suffix(".404")
    for candidate in (cache_file, missing_marker):
        if candidate.exists():
            age = time.time() - candidate.stat().st_mtime
            if max_age_s is None or age < max_age_s:
                if candidate is missing_marker:
                    raise NotFound(path)
                return cache_file.read_text(encoding="utf-8")

    url = BASE_URL + path
    network_waits = list(NETWORK_RETRY_WAITS_S)
    http_waits = list(HTTP_RETRY_WAITS_S)
    while True:
        _wait_for_slot()
        try:
            response = _session.get(url, timeout=30)
        except requests.RequestException as exc:
            if not network_waits:
                raise
            retry_wait = network_waits.pop(0)
            if retry_wait:
                print(f"  network error on {path} ({exc}); retrying in {retry_wait}s")
                time.sleep(retry_wait)
            continue

        if response.status_code == 404:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            missing_marker.write_text(url, encoding="utf-8")
            raise NotFound(path)
        if response.status_code in (429, 500, 502, 503, 504) and http_waits:
            retry_wait = http_waits.pop(0)
            print(f"  HTTP {response.status_code} on {path}; retrying in {retry_wait}s")
            time.sleep(retry_wait)
            continue
        response.raise_for_status()

        response.encoding = response.encoding or "utf-8"
        html = response.text
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(html, encoding="utf-8")
        missing_marker.unlink(missing_ok=True)
        return html


def clean_text(value: str | None) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def parse_money(value: str | None) -> float | None:
    """'$1,234,567' -> 1234567.0; blanks, dashes and 'n/a' -> None."""
    match = re.search(r"-?\$\s*([\d,]+(?:\.\d+)?)", clean_text(value))
    if not match:
        return None
    amount = float(match.group(1).replace(",", ""))
    return -amount if clean_text(value).startswith("-") else amount


def parse_percent(value: str | None) -> float | None:
    """'-45%' -> -0.45, '+59%' -> 0.59; blanks -> None."""
    match = re.search(r"([+-]?[\d,]+(?:\.\d+)?)\s*%", clean_text(value))
    if not match:
        return None
    return float(match.group(1).replace(",", "")) / 100.0


def parse_int(value: str | None) -> int | None:
    """'4,002' -> 4002; blanks and dashes -> None."""
    match = re.search(r"-?[\d,]+", clean_text(value))
    if not match or not re.search(r"\d", match.group(0)):
        return None
    return int(match.group(0).replace(",", ""))
