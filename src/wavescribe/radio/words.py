# SPDX-License-Identifier: Apache-2.0
"""How messages name receivers and list what holds them, the way the page words them.

A dongle's USB serial is in no message a user might paste somewhere: messages name a receiver
by its id from the config, and a dongle no receiver uses by a masked serial. The full serial is
under Settings → Receivers, in ``wavescribe doctor`` and in the logs, where a dongle has to be
told apart from another.
"""

from __future__ import annotations

from collections.abc import Sequence


def receiver_name(device_id: str) -> str:
    """A receiver as the page names it: a one-letter id in capitals ("A"), others as they are."""
    return device_id.upper() if len(device_id) == 1 else device_id


def masked_serial(serial: str) -> str:
    """A dongle's serial with all but its last few characters hidden ("…1234")."""
    keep = min(4, len(serial) // 2)
    return "…" + serial[-keep:] if keep else "…"


def joined(names: Sequence[str]) -> str:
    """Names in a sentence: A; A and B; A, B and C."""
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def them(names: Sequence[str]) -> str:
    """The pronoun for what ``names`` lists: "it" for one, else "them"."""
    return "it" if len(names) == 1 else "them"


def sentence(text: str) -> str:
    """A message as the page shows it on its own: a capital letter first and a full stop at the
    end (another program's words, quoted after a colon, stay as they are)."""
    text = text.strip()
    if not text:
        return text
    text = text[0].upper() + text[1:]
    return text if text[-1] in ".?!" else text + "."
