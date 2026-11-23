"""OIDC implementation shared by every OpenID Connect provider.

Discovery, the authorization URL, the token exchange and the ``end_session``
lookup are the same protocol for Google, OpenShift and Red Hat SSO; only the
issuer and the client credentials differ, so a provider module is a few class
attributes on top of this base.

Two rules keep this safe:

* The **issuer comes from configuration, never from a request** — a constant in
  code for Google, a deployment setting for OpenShift and Red Hat SSO. Nothing in
  the flow lets a caller point discovery elsewhere; issuing identities from an
  attacker's server would require an admin to set that server as the issuer.
* The ID token is verified against the issuer's JWKS — signature pinned to
  RS256, then ``iss``, ``aud``, ``exp``/``nbf``/``iat`` (each required to be a
  finite number) with a small clock skew — before any claim is trusted. A token
  response that carries no ``id_token`` is refused: the userinfo endpoint is
  authenticated by the access token alone (no signature, no nonce), so it cannot
  tell a fresh login from a replayed one. A provider that cannot issue ID tokens
  gets its own module (``openshift.py``) instead of a weaker path here.
* The OAuth ``state`` binds the *redirect* to this browser attempt; the OIDC
  ``nonce`` binds the *token* to it. A token that does not echo the attempt's
  nonce is refused, so a token minted for one login cannot be replayed into
  another.
"""

from __future__ import annotations

import hmac
import math
import time
from typing import Any, ClassVar

import httpx

from rootcoz import get_child_logger
from rootcoz.auth.sso.base import AuthError, ClaimSet, SSOProvider
from rootcoz.auth.sso.http import request_json
from rootcoz.config import Settings, issuer_is_secure
from rootcoz.url_utils import is_safe_navigation_url

logger = get_child_logger(__name__)

#: Scopes every provider is asked for. ``openid`` is what makes it OIDC.
SCOPES = ("openid", "email", "profile")

#: How long discovered metadata and JWKS are reused before a refetch. The cache
#: is process-wide and keyed by issuer: providers are built per request, so an
#: instance-level cache would refetch twice per login for documents that change
#: about never.
METADATA_TTL_SECONDS = 3600

#: issuer → (fetched_at, discovery document)
_METADATA_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

#: jwks_uri → (fetched_at, key-set document)
_JWKS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _reset_discovery_cache() -> None:
    """Clear the process-wide discovery/JWKS cache.

    For tests only: the cache is keyed by issuer and the discovery tests reuse
    ``https://idp.example``, so one test's fake document (and its key set) would
    answer the next one's. Production never resets it — a TTL is what expires a
    document there.
    """
    _METADATA_CACHE.clear()
    _JWKS_CACHE.clear()


#: Tolerance for clock drift between rootcoz and the provider, applied to
#: ``exp``/``nbf``/``iat``: without it a good token can be refused because two
#: machines disagree by a second.
CLOCK_SKEW_SECONDS = 60

#: The only signature algorithm accepted. Pinning it is what makes an ``alg:
#: none`` or HMAC-confusion token a refusal rather than a decision left to the
#: token's own header.
ID_TOKEN_ALGORITHMS = ("RS256",)


def _date_claim(claims: dict[str, Any], name: str) -> float | None:
    """Read a numeric date claim, refusing one no comparison can bound.

    ``bool`` is an ``int``, and ``NaN``/``inf`` compare unequal to every bound,
    so an ``isinstance`` check alone lets ``exp: NaN`` or ``exp: true`` through
    the expiry test unchanged.

    Args:
        claims: The token's claims.
        name: The claim to read (``exp``, ``nbf`` or ``iat``).

    Returns:
        The claim in seconds, or ``None`` when it is absent.

    Raises:
        AuthError: When the claim is present but is not a finite number.
    """
    value = claims.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AuthError("id_token_invalid", f"ID token has a malformed {name}")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        # An integer too large for a float is a date no clock can reach.
        # Preserve the sign: a huge positive exp is the far future (unexpired),
        # a huge negative exp is the far past (expired).
        return math.inf if value > 0 else -math.inf
    if not finite:
        raise AuthError("id_token_invalid", f"ID token has a malformed {name}")
    return float(value)


def _key_document(document: dict[str, Any]) -> dict[str, Any]:
    """Refuse a key-set document that cannot hold keys.

    Args:
        document: The ``jwks_uri`` response.

    Returns:
        The same document, once it is known to carry a key list.

    Raises:
        AuthError: ``jwks_failed`` when ``keys`` is not a list. Checked here so
            a document with no key list never reaches the cache and never raises
            ``KeyError`` out of ``KeySet.import_key_set`` — that would escape the
            refusals the callback handles and answer a 500.
    """
    if not isinstance(document.get("keys"), list):
        raise AuthError("jwks_failed", "key set document has no key list")
    return document


class OIDCProvider(SSOProvider):
    """An OpenID Connect provider, configured from settings.

    Class attributes:
        issuer: The provider's issuer URL when it is fixed in code (Google).
        issuer_setting: Settings field naming the issuer, for providers a
            deployment installs itself (OpenShift, Red Hat SSO). Never taken
            from a request — only from the deployment's own configuration.
        client_id_setting: Settings field holding the client id.
        client_secret_setting: Settings field holding the client secret.
    """

    #: Class default for providers whose issuer is fixed in code; ``__init__``
    #: sets it per instance (OpenShift/Red Hat SSO read it from settings).
    issuer: str = ""
    issuer_setting: ClassVar[str] = ""
    client_id_setting: ClassVar[str] = ""
    client_secret_setting: ClassVar[str] = ""

    #: Whether this provider publishes ``email_verified``. Overridden by the
    #: providers whose IdP does not (OpenShift), so a deployment can still use
    #: SSO_ALLOWED_DOMAINS against them.
    asserts_email_verified: ClassVar[bool] = True

    def __init__(self, client_id: str, client_secret: str, issuer: str) -> None:
        """Store the client credentials and the issuer they belong to.

        Args:
            client_id: OAuth client id registered at the provider.
            client_secret: OAuth client secret for that client.
            issuer: Issuer URL, from code or from the deployment's settings.
        """
        self.issuer = issuer.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret

    def accepted_issuers(self) -> frozenset[str]:
        """The ``iss`` values this provider's ID tokens may carry.

        The discovery document and the JWKS always come from the configured
        issuer; only the ``iss`` *claim* is allowed to differ, and only for a
        provider that documents a second form (Google). Everything else must
        match exactly, so an override here is what widens that check — never a
        looser comparison in :meth:`_verify_id_token`.

        Returns:
            The accepted issuers, normalised the way ``__init__`` stores the
            configured one.
        """
        return frozenset({self.issuer})

    @classmethod
    def from_settings(cls, settings: Settings) -> OIDCProvider | None:
        """Build the provider when both credentials are configured.

        Args:
            settings: Application settings for this request.

        Returns:
            The provider, or ``None`` when either credential is missing.
        """
        client_id = getattr(settings, cls.client_id_setting, "") or ""
        secret = getattr(settings, cls.client_secret_setting, None)
        client_secret = secret.get_secret_value() if secret is not None else ""
        if not client_id or not client_secret:
            return None
        issuer = cls.issuer
        if cls.issuer_setting:
            configured = (getattr(settings, cls.issuer_setting, "") or "").strip()
            # Same rule the settings field enforces: https, or loopback http for
            # a development IdP. A plaintext issuer would put the token exchange
            # and the JWKS on a network anyone can rewrite.
            if not configured or not issuer_is_secure(configured):
                if configured:
                    logger.warning(
                        "SSO provider %s is disabled: issuer %r is not https",
                        cls.name,
                        configured,
                    )
                return None
            issuer = configured
        if not issuer:
            return None
        return cls(client_id=client_id, client_secret=client_secret, issuer=issuer)

    async def metadata(self) -> dict[str, Any]:
        """Fetch (and cache) the provider's discovery document.

        Returns:
            The discovery document.

        Raises:
            AuthError: When discovery fails or is missing a required endpoint.
        """
        now = time.monotonic()
        cached = _METADATA_CACHE.get(self.issuer)
        if cached is not None and now - cached[0] < METADATA_TTL_SECONDS:
            return cached[1]
        document = await request_json(
            "GET",
            f"{self.issuer}/.well-known/openid-configuration",
            reason="discovery_failed",
        )
        for required in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not document.get(required):
                raise AuthError(
                    "discovery_failed", f"discovery document has no {required}"
                )
        # The document comes from *our* issuer's well-known URL, but it is trusted
        # for an hour afterwards: pin the issuer it claims and refuse endpoints
        # that are not https (loopback http is the documented dev exception), so a
        # redirected or tampered document cannot move the login elsewhere. A
        # document that states no issuer pins nothing, so it is refused rather
        # than cached for that hour.
        claimed_issuer = str(document.get("issuer", "")).rstrip("/")
        if not claimed_issuer:
            raise AuthError(
                "discovery_failed",
                "discovery document states no issuer, so it cannot be pinned",
            )
        if claimed_issuer != self.issuer.rstrip("/"):
            raise AuthError(
                "discovery_failed",
                f"discovery document claims issuer {claimed_issuer!r}",
            )
        checked = ["authorization_endpoint", "token_endpoint", "jwks_uri"]
        for endpoint_key in checked:
            endpoint = str(document[endpoint_key])
            if not is_safe_navigation_url(endpoint, allow_loopback_http=True):
                raise AuthError(
                    "discovery_failed",
                    f"{endpoint_key} is not an https URL",
                )
        _METADATA_CACHE[self.issuer] = (now, document)
        return document

    async def authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        code_challenge: str = "",
        nonce: str = "",
    ) -> str:
        """Build the authorization URL.

        Args:
            redirect_uri: Callback URL registered with the provider.
            state: CSRF nonce for this attempt.
            code_challenge: PKCE S256 challenge.
            nonce: OIDC nonce echoed into the ID token.

        Returns:
            The URL to redirect the browser to.

        Raises:
            AuthError: When discovery fails.
        """
        document = await self.metadata()
        query = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(SCOPES),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        endpoint = str(document["authorization_endpoint"])
        separator = "&" if "?" in endpoint else "?"
        return f"{endpoint}{separator}{httpx.QueryParams(query)}"

    async def exchange(
        self,
        *,
        code: str,
        code_verifier: str = "",
        redirect_uri: str,
        nonce: str = "",
    ) -> ClaimSet:
        """Exchange an authorization code for claims.

        Args:
            code: The ``code`` parameter from the callback.
            code_verifier: The PKCE verifier issued with the state.
            redirect_uri: The same callback URL used to start the login.
            nonce: The nonce sent with the authorization request.

        Returns:
            The normalised claims.

        Raises:
            AuthError: When the exchange, verification, or claim parsing fails.
        """
        document = await self.metadata()
        token = await self._post_token(document, code, code_verifier, redirect_uri)
        claims = await self._claims_from_token(document, token, nonce)
        return self._normalise(claims)

    async def _post_token(
        self,
        document: dict[str, Any],
        code: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        """POST the code to the token endpoint.

        Args:
            document: The discovery document.
            code: Authorization code.
            code_verifier: PKCE verifier.
            redirect_uri: Callback URL used for the login.

        Returns:
            The token response.

        Raises:
            AuthError: When the provider rejects the exchange.
        """
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "code_verifier": code_verifier,
        }
        return await request_json(
            "POST",
            str(document["token_endpoint"]),
            reason="exchange_failed",
            data=data,
        )

    async def _claims_from_token(
        self, document: dict[str, Any], token: dict[str, Any], nonce: str
    ) -> dict[str, Any]:
        """Get claims from the ID token, and only from it.

        Every provider rootcoz ships asks for the ``openid`` scope and answers
        with an ``id_token``: Google and Red Hat SSO both do, GitHub and OpenShift
        never reach this code (they are OAuth2 providers with their own module).
        The userinfo endpoint is not a fallback here on purpose — it is
        authenticated by the access token alone, so it has no signature and no
        nonce, and a token replayed from an earlier attempt would name a real
        person and be indistinguishable from a fresh login. A provider that
        cannot issue ID tokens needs its own module (see ``openshift.py``), not a
        weaker path through this one.

        Args:
            document: The discovery document.
            token: The token response.
            nonce: The nonce this attempt sent; the ID token must echo it.

        Returns:
            The claim set.

        Raises:
            AuthError: ``no_id_token`` when the response carries none, or
                ``id_token_invalid`` when it does not verify.
        """
        id_token = token.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise AuthError(
                "no_id_token",
                "token response carried no id_token, so the identity is unverified",
            )
        return await self._verify_id_token(document, id_token, nonce)

    async def _verify_id_token(
        self, document: dict[str, Any], id_token: str, nonce: str
    ) -> dict[str, Any]:
        """Verify an ID token against the issuer's keys.

        Args:
            document: The discovery document.
            id_token: The compact JWS from the token response.
            nonce: The nonce this attempt sent — a token that does not echo it
                belongs to a different attempt (or none) and is refused.

        Returns:
            The verified claims.

        Raises:
            AuthError: When the signature, issuer, audience, expiry, issue time
                or the shape of a date claim is wrong.
        """
        from joserfc import jwt as jose_jwt
        from joserfc.errors import JoseError

        jwks_uri = str(document["jwks_uri"])
        try:
            decoded = jose_jwt.decode(
                id_token,
                await self._jwks_for(jwks_uri),
                algorithms=list(ID_TOKEN_ALGORITHMS),
            )
        except JoseError, ValueError, TypeError:
            # One forced refetch covers a key rotation; if the token still does
            # not verify, it is not ours.
            try:
                decoded = jose_jwt.decode(
                    id_token,
                    await self._jwks_for(jwks_uri, force_refresh=True),
                    algorithms=list(ID_TOKEN_ALGORITHMS),
                )
            except (JoseError, ValueError, TypeError) as exc:
                raise AuthError(
                    "id_token_invalid", f"ID token rejected: {exc}"
                ) from exc
        claims = dict(decoded.claims)
        # joserfc verifies the signature only; the claims are checked here, and
        # each one is a refusal the caller cannot argue with.
        now = time.time()
        expires = _date_claim(claims, "exp")
        if expires is None or expires + CLOCK_SKEW_SECONDS < now:
            raise AuthError("id_token_invalid", "ID token is expired or has no exp")
        not_before = _date_claim(claims, "nbf")
        if not_before is not None and not_before - CLOCK_SKEW_SECONDS > now:
            raise AuthError("id_token_invalid", "ID token is not valid yet")
        issued_at = _date_claim(claims, "iat")
        if issued_at is None:
            raise AuthError("id_token_invalid", "ID token has no iat")
        if issued_at - CLOCK_SKEW_SECONDS > now:
            raise AuthError("id_token_invalid", "ID token was issued in the future")
        issuer = claims.get("iss")
        # A trailing slash is the one spelling difference tolerated on both
        # sides; every other value must be in the provider's accepted set, and a
        # non-string claim is a refusal rather than an unhashable-set TypeError.
        claimed_issuer = issuer.rstrip("/") if isinstance(issuer, str) else None
        if claimed_issuer not in self.accepted_issuers():
            raise AuthError("id_token_invalid", f"unexpected issuer {issuer!r}")
        audience = claims.get("aud")
        if isinstance(audience, str):
            audiences = [audience]
        elif isinstance(audience, list) and all(isinstance(a, str) for a in audience):
            audiences = list(audience)
        else:
            # A numeric, object or mixed `aud` is a malformed claim, and list() on
            # it would raise a TypeError deep in the callback instead of refusing.
            raise AuthError("id_token_invalid", "audience is not a string or list")
        if self._client_id not in audiences:
            raise AuthError("id_token_invalid", "audience does not name this client")
        authorized_party = claims.get("azp")
        if authorized_party is not None and authorized_party != self._client_id:
            # OIDC requires azp to name the client the token was issued to. Check
            # it whenever it is present, not only when ``aud`` holds several
            # values: a token with ``aud=[this client]`` and a foreign ``azp`` is
            # minted for a sibling client of the same issuer, and accepting it
            # would let that client's login be replayed here.
            raise AuthError("id_token_invalid", "token is authorized for another party")
        if len(audiences) > 1 and authorized_party != self._client_id:
            raise AuthError("id_token_invalid", "token is authorized for another party")
        echoed = claims.get("nonce")
        if (
            not isinstance(echoed, str)
            or not nonce
            or not hmac.compare_digest(echoed.encode(), nonce.encode())
        ):
            raise AuthError("id_token_invalid", "ID token does not echo this attempt")
        return claims

    async def _jwks_for(self, jwks_uri: str, *, force_refresh: bool = False) -> Any:
        """Fetch (and cache) the issuer's key set.

        Args:
            jwks_uri: The ``jwks_uri`` from discovery.
            force_refresh: Refetch even when the cache is warm. Used when a
                signature fails to verify: the issuer may have rotated keys, and
                without this every login stays broken until the TTL expires.

        Returns:
            A key set usable by :mod:`joserfc`.

        Raises:
            AuthError: ``jwks_failed`` when the keys cannot be fetched, when the
                document carries no key list, or when its key entries cannot be
                imported (an unknown ``kty``, a malformed key).
        """
        from joserfc.errors import JoseError
        from joserfc.jwk import KeySet

        now = time.monotonic()
        cached = _JWKS_CACHE.get(jwks_uri)
        fetched = (
            force_refresh or cached is None or now - cached[0] >= METADATA_TTL_SECONDS
        )
        if fetched:
            # Validated before it is cached, not after: a document that cannot be
            # imported must cost one login, not every login for the TTL.
            _JWKS_CACHE[jwks_uri] = (
                now,
                _key_document(
                    await request_json("GET", jwks_uri, reason="jwks_failed")
                ),
            )
        # Total: `get` above returned None only in the branch that filled it.
        document = _JWKS_CACHE[jwks_uri][1]
        try:
            return KeySet.import_key_set(document)
        except (JoseError, KeyError, ValueError, TypeError) as exc:
            if fetched:
                # A key list of unimportable entries is still a broken document:
                # drop it so the next attempt refetches instead of repeating it.
                _JWKS_CACHE.pop(jwks_uri, None)
            raise AuthError("jwks_failed", f"key set is unusable: {exc}") from exc

    def _normalise(self, claims: dict[str, Any]) -> ClaimSet:
        """Turn provider claims into the provider-agnostic shape.

        Args:
            claims: Verified claims.

        Returns:
            The normalised claim set.

        Raises:
            AuthError: When the provider did not identify the person.
        """
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise AuthError("no_subject", "claims carried no subject")
        email = claims.get("email")
        email = email if isinstance(email, str) and email else None
        return ClaimSet(
            subject=subject,
            username_hint=username_hint(claims, email),
            email=email,
            claims=claims,
            asserts_email_verified=self.asserts_email_verified,
            email_is_authoritative=self.email_is_authoritative,
        )

    async def end_session_url(self, post_logout_redirect_uri: str) -> str | None:
        """Where to end the provider's session.

        Discovery is fetched here when the cache is cold, because logout is a
        separate request from the login that populated it.

        Args:
            post_logout_redirect_uri: Where the provider should send the user
                back (a value registered at the provider).

        Returns:
            The end-session URL carrying ``post_logout_redirect_uri`` and
            ``client_id`` (Keycloak needs the client to validate the redirect
            when no ``id_token_hint`` is sent), or ``None`` when the provider
            advertises no ``end_session_endpoint``, discovery is unreachable, or
            the discovered endpoint is not a plain https URL (loopback http for
            a dev IdP) — a local logout is then the safe answer.
        """
        try:
            document = await self.metadata()
        except AuthError as exc:
            logger.debug("No end-session URL for %s: %s", self.issuer, exc.reason)
            return None
        endpoint = document.get("end_session_endpoint")
        if not endpoint or not is_safe_navigation_url(
            str(endpoint), allow_loopback_http=True
        ):
            # The endpoint comes from the issuer's discovery document: it is
            # trusted for identity, not for navigation. Anything that is not a
            # plain https URL (loopback http for a dev IdP) is refused.
            return None
        # Keycloak (Red Hat SSO) needs `client_id` to know whose redirect URI it
        # is validating when no `id_token_hint` is sent; other providers ignore
        # the extra parameter.
        query = httpx.QueryParams(
            {
                "post_logout_redirect_uri": post_logout_redirect_uri,
                "client_id": self._client_id,
            }
        )
        # An endpoint may already carry a query; a second "?" would corrupt it.
        separator = "&" if "?" in endpoint else "?"
        return f"{endpoint}{separator}{query}"


def username_hint(claims: dict[str, Any], email: str | None) -> str:
    """Pick the best local-username hint from a claim set.

    Args:
        claims: Verified claims.
        email: Email from the claims, when present.

    Returns:
        ``preferred_username``, else the email local-part, else an empty string.
    """
    for key in ("preferred_username", "nickname"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if email:
        return email.split("@", 1)[0]
    return ""


__all__ = ["SCOPES", "OIDCProvider", "username_hint"]
