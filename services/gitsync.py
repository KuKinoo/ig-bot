"""Persist state.json + draft images by force-pushing the `data` folder to the `data` branch (GitHub Actions only)."""
from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger(__name__)


def push(message: str) -> None:
    """No-op locally. In GitHub Actions (GITSYNC=1) snapshot ./data to the `data` branch right now."""
    if os.getenv("GITSYNC") != "1":
        return
    logger.info("Pushing data branch: %s", message)
    subprocess.run(["bash", "scripts/data_branch.sh", "push", message], check=True)
