# SPDX-License-Identifier: Apache-2.0
"""JSON that comes from outside: a request body, another Wavescribe's feed, a catalogue.

Nesting deep enough to exhaust Python's recursion raises RecursionError from ``json.loads``,
which callers don't expect; here it is refused like any malformed JSON, with ValueError.
"""

from __future__ import annotations

import json
from typing import Any


def loads(raw: str | bytes) -> Any:
    try:
        return json.loads(raw)
    except RecursionError:
        raise ValueError("JSON nested too deeply") from None
