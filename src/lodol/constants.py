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
# How long a retry waits for an earlier attempt of the same request (same
# Idempotency-Key) that Lodol is still working on: as long as the server
# holds its lock on that key.
IN_FLIGHT_MAX_WAIT = 300.0
# The most an idempotency key can be, as Lodol takes them.
MAX_IDEMPOTENCY_KEY_LENGTH = 256

# Polling while waiting for a run: checks 1s apart at first (a run that was
# just started isn't checked at once), slowing by half again each time to one
# check every 10s. Slower still when the workspace's rate limit asks for it:
# never more than its per-minute allowance, and not at all while it is used up.
INITIAL_POLL_INTERVAL = 1.0
MAX_POLL_INTERVAL = 10.0
POLL_BACKOFF = 1.5
# How long waiting rides out failures (lost connection, rate limits, server
# errors) with no successful check in between, before giving up.
WAIT_ERROR_TOLERANCE = 300.0
# The least time a check made while waiting is given to answer, however close
# the wait's deadline is.
MIN_CHECK_TIMEOUT = 1.0

# The most a list call asks for in one page; the API's own maximum.
MAX_PAGE_SIZE = 100

USER_AGENT = f"lodol-python/{__version__} python/{platform.python_version()}"
