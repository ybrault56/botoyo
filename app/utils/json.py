"""Thin orjson wrapper with stdlib-like helpers."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

import numpy as np
import orjson

_BASE_OPTIONS = orjson.OPT_NAIVE_UTC | orjson.OPT_SERIALIZE_NUMPY


def _default(value: Any) -> Any:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def dumps(obj: Any, *, indent: int | None = None, sort_keys: bool = False) -> str:
    """Serialize an object to a JSON string."""

    options = _BASE_OPTIONS
    if indent:
        options |= orjson.OPT_INDENT_2
    if sort_keys:
        options |= orjson.OPT_SORT_KEYS
    return orjson.dumps(obj, default=_default, option=options).decode("utf-8")


def loads(data: str | bytes | bytearray | memoryview) -> Any:
    """Deserialize JSON input into Python objects."""

    return orjson.loads(data)

