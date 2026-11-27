# SPDX-License-Identifier: Apache-2.0
import logging
import os
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from tests.helpers import make_tone, requires_ffmpeg
from wavescribe.audio import SAMPLE_RATE
from wavescribe.sources.stream import StreamSource

pytestmark = requires_ffmpeg


def test_decodes_to_16k_mono_float(tmp_path: Path) -> None:
    tone = make_tone(tmp_path / "tone.wav", 2.0)
    src = StreamSource("t", str(tone), max_reconnects=0)
    chunks = list(src)

    total = sum(len(c.samples) for c in chunks)
    assert abs(total - 2 * SAMPLE_RATE) < SAMPLE_RATE * 0.05
    assert all(c.samples.dtype == np.float32 for c in chunks)
    assert [c.discontinuity for c in chunks] == [True] + [False] * (len(chunks) - 1)
    peak = max(float(np.abs(c.samples).max()) for c in chunks)
    assert 0.05 < peak <= 1.0
    assert src.health.samples_total == total
    assert src.health.reconnects == 0


def test_reconnects_after_eof_and_marks_discontinuity(tmp_path: Path) -> None:
    tone = make_tone(tmp_path / "tone.wav", 1.0)
    src = StreamSource("t", str(tone), max_reconnects=2, backoff_initial=0)
    chunks = list(src)

    assert sum(c.discontinuity for c in chunks) == 3
    assert src.health.reconnects == 2
    assert abs(sum(len(c.samples) for c in chunks) - 3 * SAMPLE_RATE) < SAMPLE_RATE * 0.1


def test_bad_url_gives_up_after_max_reconnects(tmp_path: Path) -> None:
    src = StreamSource("t", str(tmp_path / "missing.mp3"), max_reconnects=2, backoff_initial=0)
    assert list(src) == []
    assert src.health.reconnects == 2


def test_each_failed_try_is_one_warning_that_says_why(tmp_path: Path, caplog) -> None:
    from wavescribe.sources.stream import CHECK

    src = StreamSource("t", str(tmp_path / "missing.mp3"), max_reconnects=2, backoff_initial=0)
    with caplog.at_level("DEBUG", logger="wavescribe.sources.stream"):
        assert list(src) == []
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [r.getMessage() for r in warnings] == ["source_reconnect"] * 2  # ffmpeg's: debug
    assert "No such file or directory" in warnings[0].fields["error"]
    assert "No such file or directory" in src.error and src.error.endswith(CHECK)
    assert "missing.mp3:" not in src.error  # the address isn't said again
    assert any(r.getMessage() == "ffmpeg" for r in caplog.records)


def test_stall_is_detected(tmp_path: Path) -> None:
    fifo = tmp_path / "stalled"
    os.mkfifo(fifo)
    # Hold the FIFO open for writing but never write, so ffmpeg waits for data forever.
    done = threading.Event()

    def hold_open() -> None:
        with open(fifo, "wb"):
            done.wait(10)

    threading.Thread(target=hold_open, daemon=True).start()
    src = StreamSource("t", str(fifo), stall_timeout=0.5, max_reconnects=0)
    start = time.monotonic()
    try:
        assert list(src) == []
        assert time.monotonic() - start < 5
    finally:
        done.set()


def test_close_from_another_thread_stops_iteration(tmp_path: Path) -> None:
    tone = make_tone(tmp_path / "tone.wav", 30.0)
    src = StreamSource("t", str(tone), realtime=True, backoff_initial=0)
    heard = threading.Event()
    closed_at: list[float] = []

    def close_once_heard() -> None:
        heard.wait(30)  # the first chunk, however long ffmpeg takes to start
        closed_at.append(time.monotonic())
        src.close()

    threading.Thread(target=close_once_heard, daemon=True).start()
    chunks = []
    for chunk in src:  # played at its own pace, the file would last 30 s
        chunks.append(chunk)
        heard.set()
    assert chunks and closed_at
    assert time.monotonic() - closed_at[0] < 5
    assert len(chunks) < 10


def test_backoff_stays_finite_after_a_day_of_failures() -> None:
    src = StreamSource("x", "https://example.org/a.mp3", backoff_initial=1.0, backoff_max=60.0)
    assert [src._delay(n) for n in (0, 1, 5)] == [1.0, 2.0, 32.0]
    assert src._delay(2000) == 60.0  # 2.0 ** 2000 would raise OverflowError


def test_web_streams_may_only_use_web_protocols() -> None:
    cmd = StreamSource("x", "https://example.org/a.m3u8")._command()
    assert cmd[cmd.index("-protocol_whitelist") + 1] == "http,https,tcp,tls,crypto"
    assert "-protocol_whitelist" not in StreamSource("x", "/tmp/clip.wav")._command()


def test_a_file_is_played_once(tmp_path: Path) -> None:
    # The end of a file is not a dropped connection: playing it again would transcribe
    # everything twice and raise its alerts again.
    tone = make_tone(tmp_path / "tone.wav", 1.0)
    src = StreamSource("t", str(tone), backoff_initial=0)
    chunks = list(src)
    assert abs(sum(len(c.samples) for c in chunks) - SAMPLE_RATE) < SAMPLE_RATE * 0.05
    assert src.health.reconnects == 0


def reconnect_delays(caplog: pytest.LogCaptureFixture) -> list[float]:
    return [r.fields["delay_s"] for r in caplog.records if r.getMessage() == "source_reconnect"]


def test_a_moment_of_audio_does_not_reset_the_backoff(tmp_path: Path, caplog) -> None:
    # A server that sends half a second and closes would otherwise be tried every second.
    tone = make_tone(tmp_path / "tone.wav", 0.5)
    src = StreamSource("t", str(tone), max_reconnects=3, backoff_initial=0.01)
    with caplog.at_level("WARNING", logger="wavescribe.sources.stream"):
        assert len(list(src)) == 4
    assert reconnect_delays(caplog) == [0.01, 0.02, 0.04]


def test_a_sustained_run_of_audio_resets_the_backoff(tmp_path: Path, caplog) -> None:
    tone = make_tone(tmp_path / "tone.wav", 31.0, rate=16_000, channels=1)
    src = StreamSource("t", str(tone), max_reconnects=2, backoff_initial=0.01)
    with caplog.at_level("WARNING", logger="wavescribe.sources.stream"):
        list(src)
    assert reconnect_delays(caplog) == [0.01, 0.01]


# Pacing: a live stream leaves the source at the pace it was spoken (sources/stream.py _Pacer).
CHUNK_S = 0.512


def simulate(arrivals: list[tuple[float, int]], until: float, step: float = 0.004):
    """Feed (time, whole chunks) arrivals to a pacer on a simulated clock: the pacer, the
    release times and the most audio held after each step from 35 s on."""
    from wavescribe.sources.stream import _Pacer

    pacer, released, held_late = _Pacer(CHUNK_S), [], 0.0
    pending, i, t = sorted(arrivals), 0, 0.0
    while t <= until:
        while i < len(pending) and pending[i][0] <= t:
            for _ in range(pending[i][1]):
                pacer.push(b"x", t)
            i += 1
        released += [t] * len(pacer.pop(t))
        if t >= 35:
            held_late = max(held_late, pacer.held_s)
        t = round(t + step, 6)
    pushed = sum(n for at, n in arrivals if at <= until)
    assert len(released) + len(pacer.queue) == pushed  # nothing dropped
    return pacer, released, held_late


def most_in_a_second(times: list[float], start: float, stop: float) -> int:
    window = [t for t in times if start <= t <= stop]
    return max((sum(1 for u in window if t <= u < t + 1.0) for t in window), default=0)


JITTER = [0.3, -0.2, 0.5, 0.0, -0.4, 0.2, 0.4, -0.3]


def test_whole_segments_leave_at_the_pace_they_were_spoken() -> None:
    # As measured on a BBC stream: three 6.4 s segments within a second or so of connecting,
    # then one segment every 6 to 7 s.
    seg = 12  # chunks (6.1 s)
    arrivals = [(0.0, seg), (0.6, seg), (1.2, seg)] + [
        (k * seg * CHUNK_S + JITTER[k % len(JITTER)], seg) for k in range(1, 30)
    ]
    pacer, out, held = simulate(arrivals, 180)
    # Real time before the first window ends, then once the excess of the connect burst is let
    # go, real time again: no bursts, never dry, and the delay stays small.
    assert abs(sum(1 for t in out if t < 29) - 29 / CHUNK_S) <= 2
    assert most_in_a_second(out, 0, 29) <= 2
    assert 8 < pacer.let_go_s < 13
    assert most_in_a_second(out, 29, 35) > 10  # let go in the first window, not a later one
    assert abs(sum(1 for t in out if 35 <= t < 175) - 140 / CHUNK_S) <= 2
    assert most_in_a_second(out, 35, 175) <= 2
    assert pacer.late_s == 0
    assert held <= 6 + seg * CHUNK_S


def test_a_burst_on_connect_does_not_stay_as_delay() -> None:
    # As measured on Icecast servers: 10 s at once on connect, then about a second at a time.
    arrivals = [(0.0, 20)] + [(k * 1.024 + JITTER[k % 8] / 2, 2) for k in range(1, 115)]
    pacer, out, held = simulate(arrivals, 120)
    assert pacer.let_go_s > 5
    assert held <= 4
    assert most_in_a_second(out, 35, 115) <= 3


def test_audio_that_comes_late_moves_the_schedule_and_its_excess_is_let_go() -> None:
    # A 10 s stall, then the missed audio at once, as a segmented stream catches up.
    steady = [(k * 1.024, 2) for k in range(0, 40)]
    after = [(50.0, 20)] + [(50 + k * 1.024, 2) for k in range(1, 70)]
    pacer, out, _ = simulate(steady + after, 120)
    assert pacer.late_s > 8
    assert most_in_a_second(out, 50, 60) <= 2  # no rush when the audio comes back
    assert pacer.let_go_s > 5  # and the 10 s it added go once a window shows they're spare
    assert max(t for t in out) > 115 and pacer.held_s <= 4


def test_a_pacer_never_holds_more_than_a_minute() -> None:
    from wavescribe.sources.stream import PACE_MAX_S, _Pacer

    pacer = _Pacer(CHUNK_S)
    for _ in range(300):  # 154 s at once: a file served over http
        pacer.push(b"x", 0.0)
    pacer.pop(0.0)
    assert pacer.held_s <= PACE_MAX_S


def test_a_web_stream_is_paced_and_nothing_is_dropped(tmp_path: Path) -> None:
    # A stand-in for ffmpeg that writes 2 s of audio at once, as one HLS segment arrives.
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\nhead -c 64000 /dev/zero\nsleep 2.2\n")
    fake.chmod(0o755)
    src = StreamSource("t", "https://example.invalid/live.m3u8", ffmpeg=str(fake),
                       max_reconnects=0)  # fmt: skip
    times, sizes = [], []
    for chunk in src:
        times.append(time.monotonic())
        sizes.append(len(chunk.samples))
    assert sum(sizes) == 2 * SAMPLE_RATE
    # Each chunk leaves no earlier than the audio before it took to say (a chunk of leeway).
    before = np.cumsum([0, *sizes[:-1]]) / SAMPLE_RATE
    assert all(t - times[0] >= b - CHUNK_S - 0.05 for t, b in zip(times, before, strict=True))
    assert times[-1] - times[0] > 1.0


# What ffmpeg and ffprobe wrote for a host that doesn't exist and a port nobody listens on.
DEAD_HOST = (
    "[tcp @ 0x7b8ec40080] Failed to resolve hostname dead.invalid: nodename nor servname "
    "provided, or not known\n[in#0 @ 0x7b8f03c000] Error opening input: Input/output error\n"
    "Error opening input file http://dead.invalid/live.mp3.\n"
    "Error opening input files: Input/output error\n"
)
REFUSED_PROBE = (
    "[tcp @ 0x7653080080] Connection to tcp://127.0.0.1:9 failed: Connection refused\n"
    "http://127.0.0.1:9/x.mp3: Connection refused\n"
)


@pytest.mark.parametrize(
    ("stderr", "url", "said"),
    [
        (DEAD_HOST, "http://dead.invalid/live.mp3", "Failed to resolve hostname dead.invalid: "
         "nodename nor servname provided, or not known; check the address, the network, and "
         "whether the stream only plays in a web browser"),
        (REFUSED_PROBE, "http://127.0.0.1:9/x.mp3", "Connection refused; check the address, the "
         "network, and whether the stream only plays in a web browser"),
        (b"http://x.test/: Input/output error\n", "http://x.test/", "Input/output error; check "
         "the address, the network, and whether the stream only plays in a web browser"),
        ("https://x.test/page: Invalid data found when processing input\n", "https://x.test/page",
         "Invalid data found when processing input; the address answers, but not with audio: "
         "it may be a web page, not the stream itself"),
        ("", "http://x.test/", ""),
    ],
)  # fmt: skip
def test_a_stream_that_will_not_open_says_why_and_what_to_check(stderr, url, said) -> None:
    from wavescribe.sources.stream import ffmpeg_error

    assert ffmpeg_error(stderr, url) == said


def test_a_stream_that_played_and_dropped_gives_only_ffmpegs_reason() -> None:
    from wavescribe.sources.stream import ffmpeg_error

    said = ffmpeg_error(["[http @ 0x1] Stream ends prematurely at 1234, should be 99999"],
                        opened=True)  # fmt: skip
    assert said == "Stream ends prematurely at 1234, should be 99999"
