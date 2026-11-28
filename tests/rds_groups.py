# SPDX-License-Identifier: Apache-2.0
"""RDS groups built from scratch, as a station sends them: for the RDS tests and the stand-in
rtl_sdr's stations (tests/fake_world.py)."""

from collections.abc import Callable

from wavescribe.dsp.rds import encode_block

Group = tuple[int, int, int, int]  # the 16-bit words of blocks A to D


def name_groups(pi: int, ps: str, pty: int = 1, af: tuple[int, int] = (0xE1, 52),
                tp: int = 1, ta: int = 1) -> list[Group]:  # fmt: skip
    """The four 0A groups that carry a name, each with a pair of alternative-frequency codes."""
    ps = ps.ljust(8)
    return [
        (pi, (tp << 10) | (pty << 5) | (ta << 4) | (1 << 3) | seg, (af[0] << 8) | af[1],
         (ord(ps[2 * seg]) << 8) | ord(ps[2 * seg + 1]))
        for seg in range(4)
    ]  # fmt: skip


def text_groups(pi: int, rt: str, pty: int = 1, ab: int = 0) -> list[Group]:
    """The 2A groups of a radiotext, four characters each (pad or end ``rt`` as the station
    would)."""
    rt = rt.ljust(-(-len(rt) // 4) * 4)
    return [
        (pi, (2 << 12) | (1 << 10) | (pty << 5) | (ab << 4) | seg,
         (ord(rt[4 * seg]) << 8) | ord(rt[4 * seg + 1]),
         (ord(rt[4 * seg + 2]) << 8) | ord(rt[4 * seg + 3]))
        for seg in range(len(rt) // 4)
    ]  # fmt: skip


def bits_of(sent: list[Group], blocks: Callable[[int, str], int] = encode_block) -> list[int]:
    out = []
    for group in sent:
        for data, offset in zip(group, "ABCD", strict=True):
            block = blocks(data, offset)
            out += [(block >> (25 - i)) & 1 for i in range(26)]
    return out
