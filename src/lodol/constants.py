from __future__ import annotations

from lodol.version import __version__

DEFAULT_BASE_URL = "https://api-prod.lodol.com/api/v1"
DEFAULT_MAX_RETRIES = 2
DEFAULT_TIMEOUT = 30.0

# How long ``wait()`` waits before giving up. A run that has not finished in
# five minutes is usually stuck on something the caller needs to know about,
# and a default of "forever" turns that into a hung process instead.
DEFAULT_WAIT_TIMEOUT = 300.0

# Polling is a ladder, not a fixed interval. A workspace's whole Developer API
# budget is 15-50 requests per minute shared across every key, so a fixed
# two-second poll would spend more than all of it on a single run. Starting at
# one second keeps short runs feeling immediate; doubling up to eight seconds
# settles a long run at about eight requests per minute, leaving the rest of
# the budget for the caller's own work.
POLL_INTERVAL_START_SECONDS = 1.0
POLL_INTERVAL_MAX_SECONDS = 8.0
POLL_INTERVAL_MULTIPLIER = 2.0
# Spread concurrent waiters so several runs started together don't poll in
# lockstep and hit the shared limit at the same instant.
POLL_JITTER_RATIO = 0.1

RETRY_BACKOFF_BASE_SECONDS = 0.5
RETRY_BACKOFF_MAX_SECONDS = 8.0
RETRY_BACKOFF_MULTIPLIER = 2.0
# A server may ask for a long wait; honouring it without a ceiling would park a
# synchronous call for that long with nothing to show the caller.
RETRY_AFTER_MAX_SECONDS = 60.0

USER_AGENT = f"lodol-python/{__version__}"
