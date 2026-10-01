"""Shared rootcoz issue attribution: one footer, one server-owned provenance.

Every rootcoz-created issue ends with exactly one attribution footer naming
the AI provider/model that generated the content, or stating that no model
did.  The footer is built from *preview-time* provenance, not from whatever
the server is configured with when the issue is finally created.

Because the preview body round-trips through the browser, provenance travels
inside it as an HMAC-signed token (server secret = the Fernet key secret).
The create step re-verifies the signature, so a client can neither change the
credited model nor forge one, and any attribution line it adds anywhere in the
body is stripped before the single server-owned footer is appended.
"""

import hashlib
import hmac
import json
import re
from dataclasses import dataclass

from rootcoz.encryption import get_hmac_secret

# Attribution footer for issues submitted as raw feedback (no model wrote them).
NO_AI_ATTRIBUTION = (
    "\n\n---\n*No AI model generated this issue — submitted as raw feedback "
    "via [rootcoz](https://github.com/myk-org/rootcoz)*"
)

# Shared "Generated using AI with [rootcoz](...)" footer text (optionally
# followed by " (provider / model)").  The bare form is bug_creation's
# legacy footer; the model-specific form comes from a resolved AI pair.
AI_ATTRIBUTION_PREFIX = (
    "Generated using AI with [rootcoz](https://github.com/myk-org/rootcoz)"
)

# Matches a rootcoz attribution line wherever it appears in a body (not only a
# trailing one), so a client-supplied fake footer cannot survive creation.
ATTRIBUTION_RE = re.compile(
    r"\n*---\n\*(?:Generated using AI|No AI model generated)[^\n]*\*"
)

_PROVENANCE_PREFIX = "<!--rootcoz-ai:"
_PROVENANCE_RE = re.compile(r"<!--rootcoz-ai:[^\n]*-->")


@dataclass(frozen=True, slots=True)
class AiProvenance:
    """Preview-time AI provenance for an issue body.

    ``ai_used`` is False when no model generated the content (fallback
    template or hand-written), regardless of the current AI configuration.
    """

    ai_used: bool
    provider: str = ""
    model: str = ""


def _pair(provenance: AiProvenance) -> tuple[str, str]:
    """Return the creditable (provider, model) pair — empty when AI was unused."""
    if not provenance.ai_used:
        return "", ""
    return provenance.provider.strip(), provenance.model.strip()


def _sign(payload: str) -> str:
    """Return the HMAC-SHA256 hex digest of *payload* (server secret)."""
    return hmac.new(
        get_hmac_secret().encode(), payload.encode(), hashlib.sha256
    ).hexdigest()


def _encode(provenance: AiProvenance) -> str:
    """Render *provenance* as a signed, body-invisible token."""
    ai_used, provider, model = (
        provenance.ai_used,
        *_pair(provenance),
    )
    payload = json.dumps([ai_used, provider, model], separators=(",", ":"))
    return f"{_PROVENANCE_PREFIX}{_sign(payload)}:{payload}-->"


def read_provenance(body: str) -> AiProvenance | None:
    """Return the server-signed provenance carried by *body*, or ``None``.

    Unsigned or tampered tokens are ignored: the caller must fall back to a
    safe (no-AI) attribution rather than trust client-supplied text.
    """
    for match in _PROVENANCE_RE.finditer(body):
        token = match.group(0)[len(_PROVENANCE_PREFIX) : -len("-->")]
        signature, _, payload = token.partition(":")
        if not hmac.compare_digest(signature, _sign(payload)):
            continue
        try:
            parts = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(parts, list) or len(parts) != 3:
            continue
        ai_used, provider, model = parts
        return AiProvenance(bool(ai_used), str(provider), str(model))
    return None


def build_ai_attribution(provenance: AiProvenance) -> str:
    """Return the footer for *provenance* (the AI pair, or the no-AI footer)."""
    provider, model = _pair(provenance)
    if provider and model:
        return f"\n\n---\n*{AI_ATTRIBUTION_PREFIX} ({provider} / {model})*"
    return NO_AI_ATTRIBUTION


def apply_ai_attribution(body: str, provenance: AiProvenance) -> str:
    """Strip every attribution line from *body* and append exactly one footer.

    The result carries one visible footer plus a signed provenance token, so
    the create step re-verifies the model instead of re-resolving it.
    """
    stripped = _PROVENANCE_RE.sub("", ATTRIBUTION_RE.sub("", body)).rstrip()
    return stripped + build_ai_attribution(provenance) + "\n" + _encode(provenance)
