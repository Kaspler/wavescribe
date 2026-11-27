# SPDX-License-Identifier: Apache-2.0
"""ASR bake-off: the same utterances through several models, scored for speed and accuracy.

Workflow (all files live under ``data/``, which is gitignored: broadcast audio is copyrighted):

1. ``wavescribe record antena1 --minutes 15`` saves a 16 kHz WAV clip.
2. ``wavescribe bakeoff data/bakeoff/*.wav --models a,b,c`` cuts each clip into utterances with
   the live VAD settings, so every model sees identical input, and transcribes them with each
   model in turn. It writes per-utterance WAVs (for a human to listen to), one hypothesis file
   per model, and ``<clip>.ref-template.txt`` prefilled from the first model.
3. A speaker of the language corrects the template line by line while listening to the
   utterance WAVs, and saves it as ``<clip>.ref.txt``. Rerunning the bake-off then reports WER
   against it; without a reference only speed and model agreement are shown.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import unicodedata
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from wavescribe.asr import load_transcriber
from wavescribe.audio import SAMPLE_RATE, AudioChunk
from wavescribe.invocation import typed
from wavescribe.registry import ModelRegistry
from wavescribe.segment.vad import SileroVad, VadSegmenter


@dataclass(frozen=True)
class Segment:
    index: int
    start: float  # seconds from the start of the clip
    end: float


@dataclass
class ModelRun:
    model: str
    texts: list[str]
    seconds: float  # total processing time
    load_seconds: float
    times: list[float] | None = None  # per utterance, to spot pathological cases

    def save(self, path: Path, signature: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"signature": signature, **self.__dict__}
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path, signature: str) -> ModelRun | None:
        """A saved run, if it was made on exactly the same utterances."""
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.pop("signature", None) != signature:
            return None
        return cls(**data)


# -- audio files ------------------------------------------------------------------------------


def write_wav(path: Path, samples: np.ndarray) -> None:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, SAMPLE_RATE):
            raise ValueError(f"{path}: expected 16 kHz mono 16-bit PCM (use `{typed('record')}`)")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


# -- segmentation -----------------------------------------------------------------------------


def segment_clip(audio: np.ndarray, vad_path: Path) -> list[Segment]:
    """Cut a clip exactly as the live pipeline would, with a sample-accurate clock."""
    seg = VadSegmenter("clip", SileroVad(vad_path))
    step = SAMPLE_RATE // 2
    utts = []
    for i in range(0, len(audio), step):
        part = audio[i : i + step]
        utts += seg.feed(AudioChunk(part, t_end=(i + len(part)) / SAMPLE_RATE))
    utts += seg.flush()
    return [Segment(n, round(u.t_start, 3), round(u.t_end, 3)) for n, u in enumerate(utts)]


def cut(audio: np.ndarray, s: Segment) -> np.ndarray:
    return audio[round(s.start * SAMPLE_RATE) : round(s.end * SAMPLE_RATE)]


# -- scoring ----------------------------------------------------------------------------------


def normalise(text: str) -> list[str]:
    """Words for WER: NFKC, case-folded, punctuation removed. Accents are kept on purpose."""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = "".join(" " if unicodedata.category(c).startswith("P") else c for c in text)
    return text.split()


def edit_distance(ref: list[str], hyp: list[str]) -> int:
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1]


def wer(refs: list[str], hyps: list[str]) -> float:
    """Corpus WER: total edits over total reference words."""
    edits = words = 0
    for r, h in zip(refs, hyps, strict=True):
        rw = normalise(r)
        edits += edit_distance(rw, normalise(h))
        words += len(rw)
    return edits / words if words else float("nan")


# -- reference files --------------------------------------------------------------------------


def ref_line(s: Segment, text: str) -> str:
    return f"[{s.index:04d} {_hms(s.start)}-{_hms(s.end)}] {text}"


def read_ref(path: Path, n: int) -> list[str] | None:
    if not path.exists():
        return None
    texts: dict[int, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("[") and "]" in line:
            head, text = line[1:].split("]", 1)
            texts[int(head.split()[0])] = text.strip()
    if len(texts) != n:
        raise ValueError(f"{path}: {len(texts)} lines for {n} utterances")
    return [texts[i] for i in range(n)]


def _hms(seconds: float) -> str:
    m, s = divmod(seconds, 60)
    return f"{int(m):02d}:{s:04.1f}"


# -- running ----------------------------------------------------------------------------------


def run_model(
    registry: ModelRegistry, model_id: str, clips: list[tuple[np.ndarray, list[Segment]]], lang: str
) -> ModelRun:
    entry = registry.get(model_id)
    path = registry.local_path(entry)
    if path is None:
        raise ValueError(f"{model_id} is not installed; run: {typed('models fetch ' + model_id)}")
    t0 = time.perf_counter()
    transcriber = load_transcriber(entry, path)
    load_s = time.perf_counter() - t0
    texts: list[str] = []
    times: list[float] = []
    total = sum(len(segs) for _, segs in clips)
    for audio, segments in clips:
        for s in segments:
            t0 = time.perf_counter()
            texts.append(transcriber.transcribe(cut(audio, s), lang).text)
            times.append(round(time.perf_counter() - t0, 3))
            if len(times) % 25 == 0 or len(times) == total:
                print(
                    f"  {model_id}: {len(times)}/{total} utterances, {sum(times):.0f} s busy",
                    file=sys.stderr,
                    flush=True,
                )
    del transcriber
    _free_gpu_memory()
    return ModelRun(model_id, texts, sum(times), load_s, times)


def _free_gpu_memory() -> None:
    try:
        import gc

        import mlx.core as mx

        gc.collect()
        mx.clear_cache()
    except ImportError:
        pass


def bakeoff(
    clip_paths: list[Path], model_ids: list[str], language: str, *, registry: ModelRegistry
) -> str:
    """Run every model over every clip; write hypotheses and return a Markdown report."""
    vad_path = registry.resolve("vad")[1]
    clips: list[tuple[np.ndarray, list[Segment]]] = []
    for p in clip_paths:
        audio = read_wav(p)
        segments = segment_clip(audio, vad_path)
        clips.append((audio, segments))
        out_dir = p.with_suffix("")
        for s in segments:
            write_wav(out_dir / f"{s.index:04d}.wav", cut(audio, s))

    # Each model's result is saved as soon as it finishes and reused on a rerun, so one slow or
    # crashing model never costs the others' results.
    layout = [
        [p.name, [(s.start, s.end) for s in segs]]
        for p, (_, segs) in zip(clip_paths, clips, strict=True)
    ]
    signature = hashlib.sha256(json.dumps(layout).encode()).hexdigest()[:16]
    runs_dir = clip_paths[0].parent / "runs"
    runs = []
    for m in model_ids:
        cache = runs_dir / f"{m}.json"
        run = ModelRun.load(cache, signature)
        if run is None:
            print(f"{m}: transcribing", file=sys.stderr, flush=True)
            run = run_model(registry, m, clips, language)
            run.save(cache, signature)
        else:
            print(f"{m}: reusing {cache}", file=sys.stderr, flush=True)
        runs.append(run)

    refs: list[str] | None = []
    offset = 0
    for p, (_, segments) in zip(clip_paths, clips, strict=True):
        n = len(segments)
        for run in runs:
            hyp = p.with_suffix(f".{run.model}.txt")
            texts = run.texts[offset : offset + n]
            hyp.write_text(
                "\n".join(ref_line(s, t) for s, t in zip(segments, texts, strict=True)) + "\n",
                encoding="utf-8",
            )
        ref_path = p.with_suffix(".ref.txt")
        clip_refs = read_ref(ref_path, n)
        if clip_refs is None:
            template = p.with_suffix(".ref-template.txt")
            texts = runs[0].texts[offset : offset + n]
            template.write_text(
                "\n".join(ref_line(s, t) for s, t in zip(segments, texts, strict=True)) + "\n",
                encoding="utf-8",
            )
            refs = None
        elif refs is not None:
            refs += clip_refs
        offset += n

    speech_s = sum(s.end - s.start for _, segs in clips for s in segs)
    return report(runs, refs, speech_s, sum(len(a) for a, _ in clips) / SAMPLE_RATE, offset)


def report(
    runs: list[ModelRun], refs: list[str] | None, speech_s: float, clip_s: float, n_utts: int
) -> str:
    lines = [
        f"{len(runs)} models, {n_utts} utterances, {speech_s / 60:.1f} min of speech "
        f"from {clip_s / 60:.1f} min of audio.",
        "",
        "| Model | WER | RTF | Speed | Slowest utterance (s) | Load (s) |",
        "|---|---|---|---|---|---|",
    ]
    for r in runs:
        w = f"{wer(refs, r.texts):.1%}" if refs is not None else "—"
        rtf = r.seconds / speech_s
        slowest = f"{max(r.times):.1f}" if r.times else "—"
        lines.append(
            f"| {r.model} | {w} | {rtf:.3f} | {1 / rtf:.0f}x real time | {slowest} "
            f"| {r.load_seconds:.1f} |"
        )
    if refs is None:
        lines += ["", "No reference transcript yet: WER not computed (see `*.ref-template.txt`)."]
    if len(runs) > 1:
        lines += ["", "Agreement (WER of the column model against the row model):", ""]
        lines.append("| | " + " | ".join(r.model for r in runs) + " |")
        lines.append("|---" * (len(runs) + 1) + "|")
        for a in runs:
            cells = [("—" if a is b else f"{wer(a.texts, b.texts):.1%}") for b in runs]
            lines.append(f"| {a.model} | " + " | ".join(cells) + " |")
    return "\n".join(lines)
