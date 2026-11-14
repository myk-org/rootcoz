"""The one HTTP call the SSO providers make.

Every provider talks to an IdP over HTTPS and needs the same four things: a
timeout, a status check, a JSON decode, and a refusal that names the failing
step. Keeping that in one function is what makes a GitHub failure and an OIDC
failure look the same in the audit trail.

Two shapes are supported because two protocols need them: OIDC endpoints answer
with an object, GitHub's ``/user/emails`` answers with an array.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx

from rootcoz.auth.sso.base import AuthError

#: Ceiling for any call to a provider, so a hung IdP cannot hold a request.
HTTP_TIMEOUT_SECONDS = 10.0


async def request_json(
    method: str,
    url: str,
    *,
    reason: str,
    expect: Literal["object", "array"] = "object",
    **kwargs: Any,
) -> Any:
    """Call a provider once and return its decoded JSON.

    Args:
        method: HTTP method.
        url: Absolute URL.
        reason: Reason code to report when the call fails.
        expect: Required top-level shape — ``"object"`` for the OIDC endpoints,
            ``"array"`` for GitHub's address list.
        **kwargs: Passed to ``httpx`` (``data``, ``headers``, ...).

    Returns:
        The decoded JSON: a ``dict`` for ``expect="object"``, a ``list`` for
        ``expect="array"``.

    Raises:
        AuthError: On a transport error, a non-2xx status, or a body of the wrong
            shape.
    """
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = await client.request(method, url, **kwargs)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AuthError(reason, f"{reason}: {exc}") from exc
    expected = dict if expect == "object" else list
    if not isinstance(payload, expected):
        raise AuthError(reason, f"{reason}: response is not a JSON {expect}")
    return payload
