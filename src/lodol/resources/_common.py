from __future__ import annotations

from typing import Any


def object_id(value: Any, model: type, noun: str) -> str:
    """The id of a workflow, run or approval request, given either the id or
    the object itself."""
    if isinstance(value, model):
        ident = str(getattr(value, "id", ""))
    elif isinstance(value, str):
        ident = value.strip()
    else:
        raise TypeError(
            f"Expected a {noun} id or a {model.__name__}, not {type(value).__name__}"
        )
    if not ident:
        raise ValueError(f"The {noun} id is empty")
    return ident
