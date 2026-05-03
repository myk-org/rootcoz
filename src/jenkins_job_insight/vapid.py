"""VAPID key management for Web Push notifications.

VAPID keys are read from environment variables (VAPID_PUBLIC_KEY,
VAPID_PRIVATE_KEY). When not set, a key pair is auto-generated on
first use and persisted alongside the database (parent of DB_PATH).
Falls back to $XDG_DATA_HOME/jji/ or ~/.local/share/jji/ when
DB_PATH is not set.

The claim email defaults to 'mailto:noreply@jji.local' if
VAPID_CLAIM_EMAIL is not set.
"""

import base64
import json
import os
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from simple_logger.logger import get_logger

logger = get_logger(name=__name__, level=os.environ.get("LOG_LEVEL", "INFO"))

DEFAULT_CLAIM_EMAIL = "mailto:noreply@jji.local"


def _get_data_dir() -> Path:
    """Return the data directory for persistent files.

    Uses the parent directory of DB_PATH (same volume as the database).
    Falls back to $XDG_DATA_HOME/jji/ or ~/.local/share/jji/.
    """
    db_path = os.getenv("DB_PATH", "")
    if db_path:
        return Path(db_path).parent

    return (
        Path(os.environ.get("XDG_DATA_HOME", ""))
        if os.environ.get("XDG_DATA_HOME")
        else Path.home() / ".local" / "share"
    ) / "jji"


def _generate_vapid_keys() -> dict:
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


def _read_key_file_when_ready(
    key_file: Path, retries: int = 20, delay_seconds: float = 0.05
) -> dict:
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


def _get_or_create_vapid_keys() -> dict:
    """Return VAPID keys from file, generating on first use.

    The key file is stored at ``$XDG_DATA_HOME/jji/.vapid_keys.json``
    (defaults to ``~/.local/share/jji/.vapid_keys.json``) and is only
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


def get_vapid_config() -> dict:
    """Return the full VAPID configuration.

    Priority: env vars > auto-generated file.

    Returns dict with ``public_key``, ``private_key``, ``claim_email``.
    Returns empty dict if keys cannot be resolved.
    """
    pub = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
    priv = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    email = os.environ.get("VAPID_CLAIM_EMAIL", "").strip()

    if pub and priv:
        # Env vars take priority
        return {
            "public_key": pub,
            "private_key": priv,
            "claim_email": email or DEFAULT_CLAIM_EMAIL,
        }

    # Auto-generate
    try:
        keys = _get_or_create_vapid_keys()
        return {
            "public_key": keys["public_key"],
            "private_key": keys["private_key"],
            "claim_email": email or DEFAULT_CLAIM_EMAIL,
        }
    except Exception:  # noqa: BLE001 — VAPID resolution must never raise; callers gate on truthy dict
        logger.warning("Failed to resolve VAPID keys", exc_info=True)
        return {}
