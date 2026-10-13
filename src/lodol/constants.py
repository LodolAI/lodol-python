from __future__ import annotations

import platform

from lodol.version import __version__

DEFAULT_BASE_URL = "https://api-prod.lodol.com/api/v1"
API_PATH = "/api/v1"

# Seconds a single request may take before it is abandoned (and, when safe,
# retried).
DEFAULT_TIMEOUT = 30.0
# Seconds to wait for a connection to open, when that is shorter: an address
# that can't be reached fails, and is retried, quickly.
CONNECT_TIMEOUT = 10.0
# Retries after the first attempt. Only requests that can't run twice by
# accident are retried: reads, and writes that carry an Idempotency-Key.
DEFAULT_MAX_RETRIES = 2

# Backoff between retries, before jitter: 0.5s, 1s, 2s, ... up to 8s.
INITIAL_RETRY_DELAY = 0.5
MAX_RETRY_DELAY = 8.0
# The longest Retry-After the client waits out by itself. The API's request
# rate limit resets within a minute; anything longer (a month's runs used up,
# say) is reported at once rather than slept through.
MAX_RETRY_AFTER = 60.0

# Polling while waiting for a run: the first check after 1s, then slowing by
# half again each time to one check every 10s, so a long run spends little of
# the workspace's per-minute request allowance.
INITIAL_POLL_INTERVAL = 1.0
MAX_POLL_INTERVAL = 10.0
POLL_BACKOFF = 1.5
# How long waiting rides out failures (lost connection, rate limits, server
# errors) with no successful check in between, before giving up.
WAIT_ERROR_TOLERANCE = 300.0

# The most a list call asks for in one page; the API's own maximum.
MAX_PAGE_SIZE = 100

USER_AGENT = f"lodol-python/{__version__} python/{platform.python_version()}"
