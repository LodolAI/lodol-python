"""Lists you loop over without thinking about pages.

``lodol.runs.list()`` and ``lodol.approvals.list()`` return one of these.
Looping over it fetches a page at a time, only as far as you go, so stopping
early (``break``, or ``limit=``) costs no requests beyond the page you are on.
Each loop starts again from the newest item.
"""

from __future__ import annotations

from typing import AsyncIterator, Awaitable, Callable, Generic, Iterator, TypeVar

from lodol import constants
from lodol.exceptions import APIResponseValidationError

T = TypeVar("T")

# Given the cursor from the previous page (None for the first) and how many
# items to ask for, fetch a page: its items and the next page's cursor, or
# None when there are no more.
FetchPage = Callable[[str | None, int], tuple[list[T], str | None]]
AsyncFetchPage = Callable[[str | None, int], Awaitable[tuple[list[T], str | None]]]


class Paginator(Generic[T]):
    """Every item a list call matches, newest first, fetched as you loop."""

    def __init__(
        self, fetch_page: FetchPage[T], *, limit: int | None, noun: str
    ) -> None:
        self._fetch_page = fetch_page
        self._limit = limit
        self._noun = noun

    def __iter__(self) -> Iterator[T]:
        remaining = self._limit
        cursor: str | None = None
        while remaining is None or remaining > 0:
            size = _page_size(remaining)
            items, next_cursor = self._fetch_page(cursor, size)
            for item in items[:size]:
                yield item
            if remaining is not None:
                remaining -= len(items)
            if next_cursor is None or not items:
                return
            cursor = _advance(cursor, next_cursor)

    def __repr__(self) -> str:
        return _describe(type(self).__name__, self._noun, self._limit)


class AsyncPaginator(Generic[T]):
    """Every item a list call matches, newest first, fetched as you loop
    with ``async for``."""

    def __init__(
        self, fetch_page: AsyncFetchPage[T], *, limit: int | None, noun: str
    ) -> None:
        self._fetch_page = fetch_page
        self._limit = limit
        self._noun = noun

    async def __aiter__(self) -> AsyncIterator[T]:
        remaining = self._limit
        cursor: str | None = None
        while remaining is None or remaining > 0:
            size = _page_size(remaining)
            items, next_cursor = await self._fetch_page(cursor, size)
            for item in items[:size]:
                yield item
            if remaining is not None:
                remaining -= len(items)
            if next_cursor is None or not items:
                return
            cursor = _advance(cursor, next_cursor)

    def __repr__(self) -> str:
        return _describe(type(self).__name__, self._noun, self._limit)


def check_limit(limit: int | None) -> int | None:
    if limit is None:
        return None
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a whole number of 1 or more, or None for all")
    return limit


def _advance(cursor: str | None, next_cursor: str) -> str:
    # A page that points back at itself would be fetched forever.
    if next_cursor == cursor:
        raise APIResponseValidationError(
            f"Lodol returned the page after {cursor!r} again", status_code=200
        )
    return next_cursor


def _page_size(remaining: int | None) -> int:
    if remaining is None:
        return constants.MAX_PAGE_SIZE
    return min(remaining, constants.MAX_PAGE_SIZE)


def _describe(kind: str, noun: str, limit: int | None) -> str:
    amount = "all" if limit is None else f"up to {limit}"
    return f"<{kind}: {amount} {noun}, newest first; loop over it to fetch them>"
