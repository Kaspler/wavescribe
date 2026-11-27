# SPDX-License-Identifier: Apache-2.0
"""A stand-in for ``rtl_sdr``: writes synthetic 8-bit IQ of FM stations to stdout.

Environment:
    FAKE_RTL_STATIONS  "offset_hz:tone_hz,..." relative to the tuned centre (default "300000:1000")
    FAKE_RTL_SPAWNS    a file that gets one line per start (to count processes)
    FAKE_RTL_MAX_BYTES exit after writing this many bytes (to test reconnects)
    FAKE_RTL_MISSING   if set, behave like a dongle that isn't plugged in
    FAKE_RTL_BUSY      if set, behave like a dongle another program holds
    FAKE_RTL_AUDIO     float32 16 kHz mono file: broadcast it once on the first station, then exit
    FAKE_RTL_THEN      what every start after the first does (needs FAKE_RTL_SPAWNS): "missing"
                       or "busy" as above, "stream" to write without FAKE_RTL_MAX_BYTES' limit
    FAKE_RTL_BURSTS    "offset_hz:period_s:on_s": a walkie-talkie keyed for the last on_s of every
                       period_s (narrowband FM, a 1 kHz tone at 2 kHz deviation), from each start
"""

import argparse
import os
import sys

import numpy as np

BLOCK_S = 0.256  # offsets and tones are multiples of 1/BLOCK_S Hz, so the block loops seamlessly


def broadcast(audio16k: np.ndarray, offset: float, rate: int) -> bytes:
    """What a transmitter does: pre-emphasis, then FM at up to 75 kHz deviation, as 8-bit IQ."""
    from scipy.signal import lfilter, resample_poly

    x = np.concatenate([np.zeros(16_000), audio16k, np.zeros(32_000)])
    a = np.exp(-1 / (16_000 * 50e-6))
    x = lfilter([1, -a], [1 - a], x)  # inverse of the receiver's 50 µs de-emphasis
    x = resample_poly(x / np.max(np.abs(x)) * 0.9, rate // 16_000, 1)
    t = np.arange(len(x)) / rate
    phase = 2 * np.pi * offset * t + 2 * np.pi * 75e3 * np.cumsum(x) / rate
    rng = np.random.default_rng(0)
    iq_c = 0.2 * np.exp(1j * phase) + 0.01 * (
        rng.normal(size=len(x)) + 1j * rng.normal(size=len(x))
    )
    iq = np.empty(2 * len(x))
    iq[0::2], iq[1::2] = iq_c.real, iq_c.imag
    return np.clip(np.round(iq * 128 + 127.4), 0, 255).astype(np.uint8).tobytes()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("-d")
    p.add_argument("-f", type=float)
    p.add_argument("-s", type=int, default=2_048_000)
    p.add_argument("-g", type=float)
    p.add_argument("-p", type=int)
    p.add_argument("-n", type=int)
    p.add_argument("out")
    args = p.parse_args()
    then = ""
    if spawns := os.environ.get("FAKE_RTL_SPAWNS"):
        with open(spawns, "a") as f:
            f.write(" ".join(sys.argv[1:]) + "\n")
        with open(spawns) as f:
            then = os.environ.get("FAKE_RTL_THEN", "") if len(f.readlines()) > 1 else ""
    if os.environ.get("FAKE_RTL_MISSING") or then == "missing":
        print("No matching devices found.", file=sys.stderr)
        return 1
    if os.environ.get("FAKE_RTL_BUSY") or then == "busy":
        print("usb_claim_interface error -6\nFailed to open rtlsdr device #0.", file=sys.stderr)
        return 1
    print(f"Found 1 device(s):\n  0:  RTLSDRBlog, Blog V4L, SN: {args.d}", file=sys.stderr)

    if audio := os.environ.get("FAKE_RTL_AUDIO"):
        offset = float(os.environ.get("FAKE_RTL_STATIONS", "300000:1000").split(":")[0])
        sys.stdout.buffer.write(broadcast(np.fromfile(audio, dtype=np.float32), offset, args.s))
        return 0

    n = round(args.s * BLOCK_S)
    t = np.arange(n) / args.s
    x = np.zeros(n, dtype=np.complex128)
    for spec in os.environ.get("FAKE_RTL_STATIONS", "300000:1000").split(","):
        offset, tone = (float(v) for v in spec.split(":"))
        x += 0.2 * np.exp(
            1j * (2 * np.pi * offset * t + 50e3 / tone * np.sin(2 * np.pi * tone * t))
        )
    rng = np.random.default_rng(0)
    x += 0.01 * (rng.normal(size=n) + 1j * rng.normal(size=n))
    blocks = [u8(x)]
    if bursts := os.environ.get("FAKE_RTL_BURSTS"):
        offset, period, on = (float(v) for v in bursts.split(":"))
        keyed = x + 0.1 * np.exp(1j * (2 * np.pi * offset * t + 2.0 * np.sin(2 * np.pi * 1e3 * t)))
        quiet = round((period - on) / BLOCK_S)  # first, so a receiver learns the quiet channel
        blocks = blocks * quiet + [u8(keyed)] * round(on / BLOCK_S)

    limit = int(os.environ.get("FAKE_RTL_MAX_BYTES", "0")) or (2 * args.n if args.n else None)
    if then == "stream":
        limit = None
    written, k = 0, 0
    out = sys.stdout.buffer
    try:
        while limit is None or written < limit:
            block = blocks[k % len(blocks)]
            data = block if limit is None else block[: limit - written]
            out.write(data)
            out.flush()
            written, k = written + len(data), k + 1
    except BrokenPipeError:
        pass
    return 0


def u8(x: np.ndarray) -> bytes:
    """Complex samples as rtl_sdr writes them: I and Q interleaved, 8 bits each, offset 127.4."""
    iq = np.empty(2 * len(x))
    iq[0::2], iq[1::2] = x.real, x.imag
    return np.clip(np.round(iq * 128 + 127.4), 0, 255).astype(np.uint8).tobytes()


if __name__ == "__main__":
    sys.exit(main())
