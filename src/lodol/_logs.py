from __future__ import annotations

import logging
import os

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO}


def setup_logging() -> None:
    """Log to stderr when ``LODOL_LOG`` is ``info`` (retries, and what runs
    being waited on are doing) or ``debug`` (every request too).

    Requests are logged by method, path and status only: never a header, so
    never the API key, and never a body, which may hold a run's inputs.
    """
    logger = logging.getLogger("lodol")
    logger.addHandler(logging.NullHandler())
    level = _LEVELS.get(os.environ.get("LODOL_LOG", "").strip().lower())
    if level is None:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s lodol %(levelname)s %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(level)
