"""VAPID key management for Web Push notifications.

VAPID keys are resolved with the priority **Server Settings DB > env var >
auto-generated key file** — the same single precedence rule every other Server
Settings field uses, so an admin's key edit is not silently ignored by an env
var.  When neither the DB nor the env var provide a private key, a key pair is
auto-generated on first use and persisted alongside the database (parent of
DB_PATH).  Falls back to $XDG_DATA_HOME/rootcoz/ or ~/.local/share/rootcoz/
when DB_PATH is not set.

The public key is taken from the *same* source as the private key and is
always derived from that private key when the configured public key does not
match it, so the served pair is always self-consistent.

The claim email defaults to 'mailto:noreply@rootcoz.local' if neither
VAPID_CLAIM_EMAIL nor the DB setting is set.
"""

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from simple_logger.logger import get_logger

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))

DEFAULT_CLAIM_EMAIL = "mailto:noreply@rootcoz.local"


def _get_data_dir() -> Path:
    """Return the data directory for persistent files.

    Uses the parent directory of DB_PATH (same volume as the database).
    Falls back to $XDG_DATA_HOME/rootcoz/ or ~/.local/share/rootcoz/.
    """
    db_path = os.getenv("DB_PATH", "")
    if db_path:
        return Path(db_path).parent

    return (
        Path(os.environ.get("XDG_DATA_HOME", ""))
        if os.environ.get("XDG_DATA_HOME")
        else Path.home() / ".local" / "share"
    ) / "rootcoz"


def _generate_vapid_keys() -> dict[str, str]:
    """Generate a new VAPID key pair.

    Returns dict with ``public_key`` and ``private_key`` as URL-safe
    base64 strings (unpadded).
    """
    private_key = ec.generate_private_key(ec.SECP256R1())

    # Private key: raw 32-byte scalar, URL-safe base64
    priv_numbers = private_key.private_numbers()
    priv_bytes = priv_numbers.private_value.to_bytes(32, "big")
    priv_b64 = base64.urlsafe_b64encode(priv_bytes).rstrip(b"=").decode()

    # Public key: uncompressed point (65 bytes), URL-safe base64
    pub_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    pub_b64 = base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode()

    return {"public_key": pub_b64, "private_key": priv_b64}


def _setting(name: str, source: str = "") -> tuple[str, str]:
    """Resolve a VAPID setting as ``(value, source)``.

    Server Settings DB overrides env — the one precedence rule used by every
    other setting, resolved through ``get_settings()`` so admin edits in
    Server Settings actually take effect.  ``source`` is ``"settings"`` (DB
    override) or ``"env"``.

    Passing *source* ("env"/"settings") pins the lookup to that single layer,
    so a public key is never taken from a different layer than its private key
    (which would hand the browser a key that does not match the signer).
    """
    # Late import: config.py imports this module at import time.
    from rootcoz.config import get_db_setting, get_settings

    db_value = get_db_setting(name).strip()
    if source == "env":
        return os.environ.get(name.upper(), "").strip(), "env"
    if db_value:
        return db_value, "settings"
    if source == "settings":
        return "", "settings"
    # No DB override for this key: get_settings() yields the env value.
    return str(getattr(get_settings(), name, "") or "").strip(), "env"


def _derive_public_key(private_key_b64: str) -> str:
    """Derive the VAPID public key from a raw base64 private key scalar."""
    padded = private_key_b64 + "=" * (-len(private_key_b64) % 4)
    priv_bytes = base64.urlsafe_b64decode(padded)
    if len(priv_bytes) != 32:
        raise ValueError("VAPID private key must decode to 32 bytes")
    private_key = ec.derive_private_key(
        int.from_bytes(priv_bytes, "big"), ec.SECP256R1()
    )
    pub_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode()


def _read_key_file_when_ready(
    key_file: Path, retries: int = 20, delay_seconds: float = 0.05
) -> dict[str, Any]:
    """Read VAPID keys from file, retrying briefly for a racing writer.

    Another process may have created the file but not yet written the
    keys.  We retry with short sleeps before treating the file as corrupt.

    Raises ``RuntimeError`` if the file remains empty/corrupt after all
    retries.
    """
    for attempt in range(retries):
        try:
            keys = json.loads(key_file.read_text(encoding="utf-8"))
            if keys.get("public_key") and keys.get("private_key"):
                return keys
            raise RuntimeError("missing keys")
        except (json.JSONDecodeError, RuntimeError, OSError) as err:
            if attempt == retries - 1:
                raise RuntimeError(
                    f"VAPID key file {key_file} is empty or corrupt"
                ) from err
            time.sleep(delay_seconds)
    raise RuntimeError(
        f"VAPID key file {key_file} is empty or corrupt"
    )  # pragma: no cover


def _ensure_private_key_file(key_file: Path) -> None:
    """Tighten permissions to 0600 if the file is group/world-readable."""
    if key_file.stat().st_mode & 0o077:
        key_file.chmod(0o600)


def _get_or_create_vapid_keys() -> dict[str, Any]:
    """Return VAPID keys from file, generating on first use.

    The key file is stored at ``$XDG_DATA_HOME/rootcoz/.vapid_keys.json``
    (defaults to ``~/.local/share/rootcoz/.vapid_keys.json``) and is only
    readable by the owning user (mode 0600).

    Returns dict with ``public_key`` and ``private_key``.
    """
    data_dir = _get_data_dir()
    key_file = data_dir / ".vapid_keys.json"

    if key_file.exists():
        _ensure_private_key_file(key_file)
        try:
            return _read_key_file_when_ready(key_file)
        except RuntimeError:
            logger.warning("VAPID key file %s is corrupt, regenerating", key_file)
            key_file.unlink(missing_ok=True)

    # Generate new keys
    keys = _generate_vapid_keys()
    data_dir.mkdir(parents=True, exist_ok=True)

    # Use O_CREAT|O_EXCL for atomic exclusive creation to avoid TOCTOU races.
    try:
        fd = os.open(str(key_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(keys, f)
        logger.info("Generated VAPID keys at %s", key_file)
    except FileExistsError:
        # Another process created the file — read theirs
        _ensure_private_key_file(key_file)
        return _read_key_file_when_ready(key_file)

    return keys


def get_vapid_config() -> dict[str, Any]:
    """Return the full VAPID configuration.

    Priority: Server Settings DB > env vars > auto-generated key file.  The
    public key is read from the same source as the private key and is derived
    from that private key whenever the configured public key is absent or does
    not match it, so the pair the browser gets always signs-verify.

    Returns dict with ``public_key``, ``private_key``, ``claim_email``.
    Returns empty dict if keys cannot be resolved.
    """
    priv, priv_source = _setting("vapid_private_key")
    email = _setting("vapid_claim_email")[0] or DEFAULT_CLAIM_EMAIL

    if priv:
        # Private key resolved — always publish the public key that matches it.
        try:
            derived = _derive_public_key(priv)
        except (ValueError, TypeError) as exc:
            logger.warning("Configured VAPID private key is unusable: %s", exc)
            return {}
        pub, _ = _setting("vapid_public_key", source=priv_source)
        if pub and pub != derived:
            logger.warning(
                "Configured VAPID public key does not match the %s private key; "
                "using the derived public key",
                priv_source,
            )
        return {
            "public_key": derived,
            "private_key": priv,
            "claim_email": email,
        }

    # Auto-generate
    try:
        keys = _get_or_create_vapid_keys()
        return {
            "public_key": keys["public_key"],
            "private_key": keys["private_key"],
            "claim_email": email,
        }
    except Exception:  # VAPID resolution must never raise; callers gate on truthy dict
        logger.warning("Failed to resolve VAPID keys", exc_info=True)
        return {}
