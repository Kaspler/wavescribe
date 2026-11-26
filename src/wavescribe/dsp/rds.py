# SPDX-License-Identifier: Apache-2.0
"""RDS (Radio Data System) from an FM station's multiplex: the station's name (PS), its
programme identification code (PI), programme type, traffic flags, radiotext and alternative
frequencies.

    multiplex at 240 kHz (WfmDemodulator.mpx)
      ─► mix the 57 kHz subcarrier to 0 Hz, low-pass ±2.4 kHz, decimate to 9.6 kHz
      ─► carrier phase from the squared signal (squaring BPSK removes the data)
      ─► biphase matched filter; bit timing by the best average over each ~250 bits
      ─► differential decoding ─► 26-bit blocks, found by their checkwords ─► groups

Every station that sends RDS names itself, so a scan can label what it finds with no outside
data, and the PI code says which frequencies carry the same programme. Written from the public
description of the system (IEC 62106 / EN 50067). Error bursts of up to two adjacent bits are
corrected from the syndrome; any other block whose checkword fails is dropped. Correction has a
price: a block of pure noise then passes one time in twenty, so nothing is reported from a
single block. The PI and the traffic flags must repeat, the name, programme type and each
alternative frequency are counted, and every piece of radiotext must arrive twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.signal import lfilter

from wavescribe.dsp import IF_RATE
from wavescribe.dsp.wfm import Decimator, lowpass

SUBCARRIER_HZ = 57_000
BIT_RATE = 1187.5
DECIMATE = 25
RATE = IF_RATE // DECIMATE  # 9.6 kHz
SPS = RATE / BIT_RATE  # about 8.08 samples per bit
CHUNK = 2048  # samples (about 250 bits) per timing estimate

POLY = 0x5B9  # x^10 + x^8 + x^7 + x^5 + x^4 + x^3 + 1
OFFSETS = {"A": 0x0FC, "B": 0x198, "C": 0x168, "C'": 0x350, "D": 0x1B4}
SLOT = {"A": 0, "B": 1, "C": 2, "C'": 2, "D": 3}
BY_SYNDROME = {v: k for k, v in OFFSETS.items()}

# The RDS character set (basic set, IEC 62106 annex E): ASCII with a few changes, then
# accented Latin letters from 0x80.
_CHARS = (
    " !\"#¤%&'()*+,-./0123456789:;<=>?@"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]―_‖abcdefghijklmnopqrstuvwxyz{|}¯ "
    "áàéèíìóòúùÑÇŞß¡Ĳâäêëîïôöûüñçşğıĳªα©‰Ğěňőπ€£$←↑→↓º¹²³±İńűµ¿÷°¼½¾§"
    "ÁÀÉÈÍÌÓÒÚÙŘČŠŽÐĿÂÄÊËÎÏÔÖÛÜřčšžđŀÃÅÆŒŷÝÕØÞŊŔĆŚŹŦðãåæœŵýõøþŋŕćśźŧ "
)

PTY_RDS = (
    "None", "News", "Current affairs", "Information", "Sport", "Education", "Drama", "Culture",
    "Science", "Varied", "Pop music", "Rock music", "Easy listening", "Light classical",
    "Serious classical", "Other music", "Weather", "Finance", "Children's programmes",
    "Social affairs", "Religion", "Phone-in", "Travel", "Leisure", "Jazz music", "Country music",
    "National music", "Oldies music", "Folk music", "Documentary", "Alarm test", "Alarm",
)  # fmt: skip
# North America (RBDS) numbers the programme types differently; its names aren't applied yet.


def char(code: int) -> str:
    if code == 0x0D:
        return "\r"  # ends a radiotext early
    return _CHARS[code - 0x20] if 0x20 <= code <= 0xFF else " "


def syndrome(word: int) -> int:
    """The remainder of a 26-bit block divided by the generator: its offset word if intact."""
    for i in range(25, 9, -1):
        if word >> i & 1:
            word ^= POLY << (i - 10)
    return word & 0x3FF


# Error bursts the code corrects reliably, by their syndrome: any one of the 26 bits, or any 2
# adjacent ones. The code could correct 5-bit bursts, but longer corrections also turn more noise
# into false blocks: with these 51 patterns, 52 of the 1024 syndromes pass for each offset word.
_BURSTS = {
    syndrome(burst << pos): burst << pos
    for burst, places in ((0b1, 26), (0b11, 25))
    for pos in range(places)
}
AF_LF_MF_FOLLOWS = 250  # alternative-frequency code: the next one is a long or medium wave channel


def correct(word: int, offset: str) -> int | None:
    """The block with a short error burst fixed, if it is one away from carrying ``offset``."""
    error = _BURSTS.get(syndrome(word) ^ OFFSETS[offset])
    return None if error is None else word ^ error


def encode_block(data: int, offset: str) -> int:
    """A 16-bit word with its checkword (used by the tests to build a signal)."""
    return (data << 10) | (syndrome(data << 10) ^ OFFSETS[offset])


def pty_name(code: int) -> str:
    return PTY_RDS[code & 31]


@dataclass
class RdsInfo:
    """What a station has said about itself so far."""

    pi: int | None = None
    ps: str | None = None  # the most recent complete name (8 characters, trailing spaces cut)
    pty: int | None = None
    tp: bool = False  # carries traffic announcements
    ta: bool = False  # a traffic announcement is on now
    rt: str | None = None  # radiotext: what's on, a headline (up to 64 characters)
    af: set[float] = field(default_factory=set)  # other frequencies with the same programme, MHz
    groups: int = 0
    bad_blocks: int = 0
    corrected: int = 0  # blocks saved by error correction
    names: dict[str, int] = field(default_factory=dict)  # each complete PS and how often
    voted: str | None = None  # PS from per-segment votes (survives a stray bad block)

    @property
    def name(self) -> str | None:
        """The station's name: the complete PS seen most often (at least twice, so a block that
        slipped through with errors can't name it), else the per-segment vote."""
        if self.names:
            best = max(self.names, key=lambda k: self.names[k])
            if self.names[best] >= 2:
                return best
        return self.voted

    def to_dict(self) -> dict[str, object]:
        return {
            "pi": f"{self.pi:04X}" if self.pi is not None else None,
            "ps": self.name,
            "pty": self.pty,
            "pty_name": pty_name(self.pty) if self.pty is not None else None,
            "tp": self.tp,
            "ta": self.ta,
            "rt": self.rt,
            "af": sorted(round(f, 1) for f in self.af),
            "groups": self.groups,
        }


class RdsDecoder:
    """Feed it a station's multiplex (``WfmDemodulator.mpx``) in chunks of any size; ``info``
    fills in as groups arrive."""

    def __init__(self, rate: int = IF_RATE) -> None:
        if rate != IF_RATE:
            raise ValueError(f"RDS decoding expects the multiplex at {IF_RATE} Hz")
        # 57 kHz is 19/80 of 240 kHz: the mixer is an 80-sample table.
        k = np.arange(80)
        self._lo = np.exp(-2j * np.pi * SUBCARRIER_HZ * k / IF_RATE).astype(np.complex64)
        self._n = 0
        self._down = Decimator(lowpass(IF_RATE, 2_400, RATE - 2_400), DECIMATE, np.complex64)
        self._smooth_a = float(np.exp(-1.0 / (RATE * 0.04)))  # 40 ms for the carrier phase
        self._z2 = np.zeros(1, dtype=np.complex128)
        self._angle = 0.0  # the last unwrapped angle of the squared signal
        half = round(SPS / 2)
        self._mf = (np.r_[-np.ones(half), np.ones(half)], np.zeros(2 * half - 1))  # time-reversed
        self._y = np.zeros(0, dtype=np.float32)
        self._t = 0.0  # where the next bit is sampled, in self._y's samples
        self._last_symbol = 0
        self._reg = 0  # the last 26 bits
        self._bits = 0
        self._sync_end: int | None = None  # bit count at which the next block ends
        self._slot = 0
        self._recent: list[bool] = []  # last blocks' checks, while in sync
        self._seen: list[tuple[int, int]] = []  # intact blocks seen while hunting: (bits, slot)
        self._group: list[int | None] = [None, None, None, None]
        self._ps = [" "] * 8
        self._ps_seen = 0
        self._ps_votes: list[dict[str, int]] = [{} for _ in range(4)]
        self._pty_votes: dict[int, int] = {}
        self._rt_ab: int | None = None
        self._rt_ab_new: int | None = None  # a changed A/B flag, until it is seen twice
        self._rt_reset()
        self._pi_candidate: int | None = None
        self._tp_last: bool | None = None  # the flags of the group before: they count when equal
        self._ta_last: bool | None = None
        self._af_seen: dict[int, int] = {}  # alternative-frequency codes and how often
        self._af_skip = False  # the next code is an LF/MF channel, not an FM frequency
        self.info = RdsInfo()

    @property
    def synced(self) -> bool:
        return self._sync_end is not None

    def __call__(self, mpx: np.ndarray) -> None:
        if len(mpx) == 0:
            return
        idx = (self._n + np.arange(len(mpx))) % 80
        self._n = (self._n + len(mpx)) % 80
        bb = self._down(mpx.astype(np.float32) * self._lo[idx])
        if len(bb) == 0:
            return
        # Carrier phase: the angle of the smoothed square, unwrapped, halved.
        a = self._smooth_a
        z2, self._z2 = lfilter([1 - a], [1, -a], bb.astype(np.complex128) ** 2, zi=self._z2)
        ang = np.unwrap(np.concatenate([[self._angle], np.angle(z2)]))[1:]
        # Keep the unwrapped angle small; a multiple of 4π leaves the halved phase unchanged.
        ang -= 4 * np.pi * np.floor(ang[-1] / (4 * np.pi))
        self._angle = float(ang[-1])
        r = np.real(bb * np.exp(-0.5j * ang)).astype(np.float32)
        taps, zi = self._mf
        y, zi = lfilter(taps, [1.0], r, zi=zi)
        self._mf = (taps, zi)
        self._y = np.concatenate([self._y, y.astype(np.float32)])
        while len(self._y) - self._t >= CHUNK + SPS:
            self._take_chunk()

    def _take_chunk(self) -> None:
        y = self._y
        n_bits = int(CHUNK // SPS)
        k = np.arange(n_bits) * SPS
        best, best_d = -1.0, 0.0
        for d in np.linspace(-SPS / 2, SPS / 2, 16, endpoint=False):
            pos = self._t + d + k
            if pos[0] < 0:
                continue
            e = float(np.mean(np.abs(np.interp(pos, np.arange(len(y)), y))))
            if e > best:
                best, best_d = e, d
        pos = self._t + best_d + k
        samples = np.interp(pos, np.arange(len(y)), y)
        self._t = float(pos[-1] + SPS)
        drop = max(0, int(self._t) - 4)
        self._y = y[drop:]
        self._t -= drop
        symbols = (samples > 0).astype(np.int8)
        for s in symbols:
            self._bit(int(s) ^ self._last_symbol)
            self._last_symbol = int(s)

    def _bit(self, b: int) -> None:
        self._reg = ((self._reg << 1) | b) & 0x3FFFFFF
        self._bits += 1
        if self._sync_end is None:
            kind = BY_SYNDROME.get(syndrome(self._reg))
            if kind is None:
                return
            slot = SLOT[kind]
            # Locked when an earlier block sits a whole number of blocks back, in sequence.
            for prev_bits, prev_slot in self._seen:
                gap = self._bits - prev_bits
                if gap % 26 == 0 and 0 < gap <= 26 * 6 and (prev_slot + gap // 26) % 4 == slot:
                    self._sync_end = self._bits + 26
                    self._slot = slot
                    self._recent = []
                    self._group = [None, None, None, None]
                    self._block(slot, kind)
                    return
            self._seen.append((self._bits, slot))
            del self._seen[:-12]
            return
        if self._bits < self._sync_end:
            return
        expected = (self._slot + 1) % 4
        kind = BY_SYNDROME.get(syndrome(self._reg))
        ok = kind is not None and SLOT[kind] == expected
        if not ok:
            for k in ("C", "C'") if expected == 2 else ("ABCD"[expected],):
                fixed = correct(self._reg, k)
                if fixed is not None:
                    self._reg, kind, ok = fixed, k, True
                    self.info.corrected += 1
                    break
        self._slot = expected
        self._sync_end = self._bits + 26
        self._recent.append(ok)
        del self._recent[:-50]
        if ok:
            self._block(expected, kind)  # type: ignore[arg-type]
        else:
            self.info.bad_blocks += 1
            self._group[expected] = None
            if expected == 3:
                self._finish_group()
        if len(self._recent) >= 20 and self._recent.count(False) > 0.6 * len(self._recent):
            self._sync_end = None  # lost: hunt again
            self._seen = []

    def _block(self, slot: int, kind: str) -> None:
        if slot == 0:
            self._group = [None, None, None, None]
        self._group[slot] = self._reg >> 10
        if kind == "C'":
            self._group[2] = None  # version B: block C repeats the PI
            self._pi(self._reg >> 10)
        if slot == 3:
            self._finish_group()

    def _pi(self, pi: int) -> None:
        # Two matching PIs in a row before trusting one: a broken block can still pass.
        if pi == self._pi_candidate or pi == self.info.pi:
            self.info.pi = pi
        self._pi_candidate = pi

    def _finish_group(self) -> None:
        a, b, c, d = self._group
        self._group = [None, None, None, None]
        if a is not None:
            self._pi(a)
        if b is None:
            return
        info = self.info
        info.groups += 1
        gtype, version_b = b >> 12, bool(b >> 11 & 1)
        tp = bool(b >> 10 & 1)
        if tp == self._tp_last:
            info.tp = tp
        self._tp_last = tp
        # The programme type, once seen twice and the most frequent: a block that error
        # correction repaired wrongly mustn't name a news station "Alarm test".
        votes = self._pty_votes
        pty = b >> 5 & 31
        votes[pty] = votes.get(pty, 0) + 1
        if votes[pty] > 30:
            for k in list(votes):
                votes[k] //= 2
        best = max(votes, key=lambda k: votes[k])
        if votes[best] >= 2:
            info.pty = best
        if gtype == 0:
            ta = bool(b >> 4 & 1)
            if ta == self._ta_last:
                info.ta = ta
            self._ta_last = ta
            seg = b & 3
            if d is not None:
                # A name has no end mark: a carriage return in one is a space, like any control.
                pair = (char(d >> 8) + char(d & 0xFF)).replace("\r", " ")
                self._ps[2 * seg : 2 * seg + 2] = list(pair)
                self._ps_seen |= 1 << seg
                votes = self._ps_votes[seg]
                votes[pair] = votes.get(pair, 0) + 1
                if votes[pair] > 30:  # let a changed name take over in time
                    for k in list(votes):
                        votes[k] //= 2
                tops = [max(v.items(), key=lambda kv: kv[1]) if v else ("", 0)
                        for v in self._ps_votes]  # fmt: skip
                if all(n >= 2 for _, n in tops):
                    info.voted = "".join(t for t, _ in tops).strip() or None
                if seg == 3 and self._ps_seen == 0b1111:
                    name = "".join(self._ps).strip()
                    if name:
                        info.ps = name
                        info.names[name] = info.names.get(name, 0) + 1
                        if len(info.names) > 32:  # scrolling text: keep the frequent ones
                            for k in sorted(info.names, key=info.names.get)[:16]:  # type: ignore[arg-type]
                                del info.names[k]
                    self._ps_seen = 0
            if not version_b:
                self._alternatives(c)
        elif gtype == 2:
            ab, seg = b >> 4 & 1, b & 15
            if ab != self._rt_ab:
                # A new text, once the flag is seen twice: one bad block mustn't wipe it.
                if self._rt_ab is None or self._rt_ab_new == ab:
                    self._rt_reset()
                    self._rt_ab, self._rt_ab_new = ab, None
                else:
                    self._rt_ab_new = ab
                    return
            self._rt_ab_new = None
            if seg <= self._rt_at:  # segments are sent in order: the text has started over
                self._rt_ends = (self._rt_ends[1], self._rt_at)
            self._rt_at = seg
            words = [d] if version_b else [c, d]
            if all(w is not None for w in words):
                text = "".join(char(w >> 8) + char(w & 0xFF) for w in words)  # type: ignore[operator]
                # A segment counts once it has come twice running, and the one held gives way
                # to another on the same terms: some stations change their text without
                # flipping the A/B flag, and a single one may be a block that error correction
                # got wrong.
                if text in (self._rt_seg[seg], self._rt_new[seg]):
                    self._rt_seg[seg], self._rt_new[seg] = text, None
                else:
                    self._rt_new[seg] = text
                rt = self._radiotext()
                if rt:
                    info.rt = rt

    def _alternatives(self, block: int | None) -> None:
        """Alternative frequencies from block C of a 0A group, two codes: 1 to 204 are 87.6 to
        107.9 MHz, and each counts once it has been seen twice. The code after AF_LF_MF_FOLLOWS
        is a long or medium wave channel numbered the same way, so it is passed over, as it is
        when the block it was in is lost."""
        if block is None:
            self._af_skip = False
            return
        for code in (block >> 8, block & 0xFF):
            if self._af_skip:
                self._af_skip = False
            elif code == AF_LF_MF_FOLLOWS:
                self._af_skip = True
            elif 1 <= code <= 204:
                self._af_seen[code] = self._af_seen.get(code, 0) + 1
                if self._af_seen[code] == 2:
                    self.info.af.add(round(87.5 + code * 0.1, 1))

    def _rt_reset(self) -> None:
        self._rt_seg: list[str | None] = [None] * 16  # the text, 4 (2A) or 2 (2B) characters each
        self._rt_new: list[str | None] = [None] * 16  # what each segment carried last, if not that
        self._rt_at = -1  # the segment sent last
        # The last segment of the two passes through the text before this one: where they agree,
        # the text ends there. Until then it has all 16 segments.
        self._rt_ends = (15, 15)

    def _radiotext(self) -> str | None:
        """The text, once every segment of it is held: up to a carriage return, or else up to
        where the last two passes through it both ended (a short text needn't end in a carriage
        return, and what an earlier, longer one left beyond its end isn't part of it). None
        while a segment is in doubt, so a text that is being replaced segment by segment is
        never shown half old, half new."""
        first, second = self._rt_ends
        last = first if first == second else 15
        parts = []
        for i in range(last + 1):
            seg = self._rt_seg[i]
            if seg is None or self._rt_new[i] is not None:
                return None
            end = seg.find("\r")
            if end >= 0:
                parts.append(seg[:end])
                break
            parts.append(seg)
        return "".join(parts).strip() or None
