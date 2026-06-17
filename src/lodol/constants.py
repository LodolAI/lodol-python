from __future__ import annotations

from lodol.version import __version__

DEFAULT_BASE_URL = "https://api-prod.lodol.com/api/v1"
DEFAULT_MAX_RETRIES = 2
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_TIMEOUT = 30.0
RETRY_BACKOFF_BASE_SECONDS = 0.5
RETRY_BACKOFF_MAX_SECONDS = 8.0
RETRY_BACKOFF_MULTIPLIER = 2.0
USER_AGENT = f"lodol-python/{__version__}"
