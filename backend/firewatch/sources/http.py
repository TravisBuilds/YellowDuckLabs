"""Shared HTTP client for source adapters.

Public government endpoints are the backbone of this product, so requests are
polite (identifying User-Agent, conservative retries) and failures are
descriptive rather than silent.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

from firewatch.config import settings


class SourceUnavailable(Exception):
    """The source could not be reached or refused us.

    Raised rather than returning empty data, because an empty result and an
    unreachable source mean completely different things operationally.
    """


# Several municipal portals sit behind bot protection that rejects non-browser
# User-Agents outright. We send a browser-shaped UA with our own identifier
# appended so operators can still see who we are in their logs.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def default_headers(browser_shaped: bool = False) -> dict[str, str]:
    if browser_shaped:
        return {
            "User-Agent": f"{_BROWSER_UA} {settings.user_agent}",
            "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-CA,en;q=0.9",
        }
    return {"User-Agent": settings.user_agent, "Accept": "*/*"}


#: Responses that will not improve on retry: we are being refused, not throttled.
_TERMINAL_STATUS = (400, 401, 403, 404, 501)

#: Shared endpoints (Overpass in particular) shed load with these codes when
#: their query slots are saturated. They need a real pause, not a quick retry.
_THROTTLED_STATUS = (406, 429, 503, 504, 509)


def request(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    retries: int = 2,
    timeout: float | None = None,
    browser_shaped: bool = False,
    expect_json: bool = False,
) -> httpx.Response:
    timeout = timeout or settings.http_timeout_seconds
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        wait_seconds = 1.5 * (attempt + 1)
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                response = client.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=default_headers(browser_shaped),
                )
            if response.status_code >= 400:
                if response.status_code in _THROTTLED_STATUS:
                    wait_seconds = _throttle_wait(response, attempt)
                raise SourceUnavailable(
                    f"HTTP {response.status_code} from {response.request.url}: "
                    f"{summarise_body(response)}"
                )
            if expect_json:
                # Some OGC services answer errors with 200 and an XML body.
                ctype = response.headers.get("content-type", "")
                if "json" not in ctype:
                    raise SourceUnavailable(
                        f"Expected JSON from {response.request.url} but got "
                        f"'{ctype}': {summarise_body(response)}"
                    )
            return response
        except SourceUnavailable as exc:
            last_error = exc
            if any(f"HTTP {code}" in str(exc) for code in _TERMINAL_STATUS):
                break
        except httpx.HTTPError as exc:
            last_error = SourceUnavailable(f"{type(exc).__name__} for {url}: {exc}")

        if attempt < retries:
            time.sleep(wait_seconds)

    raise last_error or SourceUnavailable(f"Unknown failure for {url}")


#: The useful part of an OGC or ArcGIS error is a short message buried in
#: markup. These pull it out so the data-health panel reads as an explanation
#: rather than a page of HTML.
_OGC_MESSAGE = re.compile(
    r"<(?:ows:)?ExceptionText>(.*?)</(?:ows:)?ExceptionText>", re.S | re.I
)
_TITLE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")


#: Titles used by bot-protection interstitials, which carry no diagnostic value.
_CHALLENGE_TITLES = ("just a moment", "attention required", "access denied", "checking")


def summarise_body(response: httpx.Response, limit: int = 220) -> str:
    """A readable one-line reason from an error response body."""
    body = response.text or ""

    # A real service error, which is the most informative thing available.
    ogc = _OGC_MESSAGE.search(body)
    if ogc:
        body = ogc.group(1)
    elif "<html" in body[:500].lower():
        title = _TITLE.search(body)
        heading = (title.group(1).strip().lower() if title else "")
        if not heading or any(c in heading for c in _CHALLENGE_TITLES):
            # The markup is a challenge page; say so rather than quoting it.
            body = (
                "the endpoint returned a bot-protection page instead of data, so "
                "it cannot be read by an automated client"
            )
        else:
            body = title.group(1) if title else body

    text_only = " ".join(_TAGS.sub(" ", body).split())
    if len(text_only) > limit:
        text_only = f"{text_only[:limit].rstrip()}…"
    return text_only or f"empty {response.status_code} response"


def _throttle_wait(response: httpx.Response, attempt: int) -> float:
    """How long to wait before retrying a throttled request."""
    header = response.headers.get("retry-after", "").strip()
    if header.isdigit():
        return min(float(header), 120.0)
    return min(15.0 * (attempt + 1), 120.0)


def get_json(url: str, params: dict[str, Any] | None = None, **kwargs) -> dict:
    return request("GET", url, params=params, expect_json=True, **kwargs).json()


def get_bytes(url: str, params: dict[str, Any] | None = None, **kwargs) -> bytes:
    return request("GET", url, params=params, **kwargs).content


def get_text(url: str, params: dict[str, Any] | None = None, **kwargs) -> str:
    return request("GET", url, params=params, **kwargs).text


def post_form(url: str, data: dict[str, Any], **kwargs) -> httpx.Response:
    return request("POST", url, data=data, **kwargs)
