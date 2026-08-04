"""Utility functions for JUnit XML AI analysis enrichment.

These functions handle server communication and XML enrichment.
They are not tied to pytest and can be used independently.

"""

import logging
import os
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

logger = logging.getLogger("rootcoz")


def is_dry_run(config: Any) -> bool:
    """Check if pytest was invoked in dry-run mode (--collectonly or --setupplan)."""
    return config.option.setupplan or config.option.collectonly


def setup_ai_analysis(session: Any) -> None:
    """Configure AI analysis for test failure reporting.

    Loads .env, validates ROOTCOZ_SERVER, and sets defaults for AI provider/model.
    Disables analysis if ROOTCOZ_SERVER is missing or if pytest was invoked
    with --collectonly or --setupplan.

    Args:
        session: The pytest session containing config options.
    """
    if is_dry_run(session.config):
        session.config.option.analyze_with_ai = False
        return

    load_dotenv()

    logger.info("Setting up AI-powered test failure analysis")

    if not os.environ.get("ROOTCOZ_SERVER"):
        logger.warning(
            "ROOTCOZ_SERVER is not set. Analyze with AI features will be disabled."
        )
        session.config.option.analyze_with_ai = False
    else:
        if not os.environ.get("ROOTCOZ_AI_PROVIDER"):
            logger.warning(
                "ROOTCOZ_AI_PROVIDER is not set. Set it explicitly (e.g., 'claude', 'gemini', 'cursor')."
            )
            session.config.option.analyze_with_ai = False
            return

        if not os.environ.get("ROOTCOZ_AI_MODEL"):
            logger.warning(
                "ROOTCOZ_AI_MODEL is not set. Set it explicitly to the desired model name."
            )
            session.config.option.analyze_with_ai = False


def enrich_junit_xml(session: Any) -> None:
    """Read JUnit XML, send to server for analysis, write enriched XML back.

    Reads the JUnit XML that pytest generated, POSTs the raw content to the
    RootCoz server's /analyze endpoint, and writes the enriched XML
    (with analysis results) back to the same file.

    Args:
        session: The pytest session containing config options.
    """
    xml_path_raw = getattr(session.config.option, "xmlpath", None)
    if not xml_path_raw:
        logger.warning(
            "xunit file not found; pass --junitxml. Skipping AI analysis enrichment"
        )
        return

    xml_path = Path(xml_path_raw)
    if not xml_path.exists():
        logger.warning(
            "xunit file not found under %s. Skipping AI analysis enrichment",
            xml_path_raw,
        )
        return

    ai_provider = os.environ.get("ROOTCOZ_AI_PROVIDER", "")
    ai_model = os.environ.get("ROOTCOZ_AI_MODEL", "")
    if not ai_provider or not ai_model:
        logger.warning(
            "ROOTCOZ_AI_PROVIDER and ROOTCOZ_AI_MODEL must be set, skipping AI analysis enrichment"
        )
        return

    server_url = os.environ.get("ROOTCOZ_SERVER", "")
    raw_xml = xml_path.read_text()

    try:
        timeout_value = int(os.environ.get("ROOTCOZ_TIMEOUT", "600"))
    except ValueError:
        logger.warning("Invalid ROOTCOZ_TIMEOUT value, using default 600 seconds")
        timeout_value = 600

    try:
        response = requests.post(
            f"{server_url.rstrip('/')}/analyze",
            json={
                "type": "file",
                "raw_xml": raw_xml,
                "ai_provider": ai_provider,
                "ai_model": ai_model,
            },
            timeout=60,
        )
        response.raise_for_status()
        submit_data = response.json()

        job_id = submit_data.get("job_id")
        if not job_id:
            logger.warning("No job_id in analyze response; skipping enrichment")
            return

        # Poll for completion
        poll_interval = 5
        start = time.monotonic()
        result = None
        while time.monotonic() - start < timeout_value:
            time.sleep(poll_interval)
            poll_response = requests.get(
                f"{server_url.rstrip('/')}/results/{job_id}",
                timeout=30,
            )
            if poll_response.status_code == 200:
                poll_data = poll_response.json()
                status = poll_data.get("status")
                if status == "completed":
                    result = poll_data.get("result", poll_data)
                    break
                elif status in ("failed", "aborted"):
                    logger.warning("Analysis %s for %s", status, xml_path)
                    result = poll_data.get("result", poll_data)
                    break

        if result is None:
            logger.warning("Analysis timed out for %s", xml_path)
            return
    except Exception:
        logger.exception("Failed to enrich JUnit XML, original preserved.")
        return

    if enriched_xml := result.get("enriched_xml"):
        xml_path.write_text(enriched_xml)
        logger.info("JUnit XML enriched with AI analysis: %s", xml_path)
    else:
        logger.info("No enriched XML returned (no failures or analysis failed)")
