"""Process-temp directories for workspaces and extracted artifacts."""

from __future__ import annotations

import tempfile
from pathlib import Path

# Under process temp root so chat symlink cleanup (honors TMPDIR) can delete it.
ROOTCOZ_TEMP_DIRNAME = "rootcoz"


def rootcoz_temp_base() -> Path:
    """Return ``{tempdir}/rootcoz`` (``tempfile.gettempdir()`` honors ``TMPDIR``)."""
    return Path(tempfile.gettempdir()) / ROOTCOZ_TEMP_DIRNAME
