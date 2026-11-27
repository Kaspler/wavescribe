# SPDX-License-Identifier: Apache-2.0
"""Translation with an instruction-tuned LLM through mlx-lm (MIT). Mac-only backend.

Slower than MADLAD and it shares the GPU with ASR, but it copes with recognition errors and
context, and it is the route for secondary languages (docs/ARCHITECTURE.md). Construct and call
from one thread, like every MLX backend.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from wavescribe.languages import language_name
from wavescribe.translate.checks import UNTRANSLATABLE

# The last sentence gives the model a reply for text it can't translate, so that it needn't
# explain in prose, which would be stored as what the station said (translate/checks.py).
SYSTEM_PROMPT = (
    "You translate radio transcripts. The input comes from automatic speech recognition and may "
    "contain recognition errors: translate the most plausible meaning. Keep names, places and "
    "numbers. Never add, omit or summarise information. Reply with the translation only: no "
    "notes, no explanations, no apology, nothing in brackets that the text doesn't have. If you "
    "cannot translate the text (it is too garbled, too short to mean anything, or not in that "
    f"language), reply with exactly {UNTRANSLATABLE} and nothing else."
)


def messages(text: str, source: str, target: str) -> list[dict[str, str]]:
    """The chat that asks for ``text`` in ``target``. Languages go by their CLDR names: a bare
    code is ambiguous to a model ("uk")."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Translate from {language_name(source)} ({source}) into "
            f"{language_name(target)} ({target}):\n\n{text}",
        },
    ]


class LlmTranslator:
    def __init__(self, model_id: str, model_path: Path, *, max_tokens: int = 512) -> None:
        from mlx_lm import load

        self.model_id = model_id
        self._max_tokens = max_tokens
        self._model, self._tokenizer = load(str(model_path))
        self.last_stats: dict[str, float] = {}
        # How the last completion ended: "length" when it was cut at max_tokens, "stop" when
        # the model ended it; None when the backend doesn't say.
        self.last_finish_reason: str | None = None

    def _prompt(self, text: str, source: str, target: str) -> str:
        return self._tokenizer.apply_chat_template(
            messages(text, source, target),
            add_generation_prompt=True,
            tokenize=False,
            enable_thinking=False,
        )

    def translate(self, text: str, source: str, target: str) -> str:
        from mlx_lm import generate
        from mlx_lm.sample_utils import make_sampler

        out = generate(
            self._model,
            self._tokenizer,
            self._prompt(text, source, target),
            max_tokens=self._max_tokens,
            sampler=make_sampler(temp=0.0),
        )
        return out.strip()

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 512,
        on_text: Callable[[str], None] | None = None,
    ) -> str:
        """A chat completion (greedy, no thinking), with each piece of text passed to ``on_text``
        as it's generated. ``last_stats`` gets the prompt and generation speeds, and
        ``last_finish_reason`` whether the reply was cut."""
        from mlx_lm import stream_generate
        from mlx_lm.sample_utils import make_sampler

        prompt = self._tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=False, enable_thinking=False
        )
        parts: list[str] = []
        r = None
        self.last_finish_reason = None
        for r in stream_generate(
            self._model,
            self._tokenizer,
            prompt,
            max_tokens=max_tokens,
            sampler=make_sampler(temp=0.0),
        ):
            if r.text:
                parts.append(r.text)
                if on_text is not None:
                    on_text(r.text)
        if r is not None:
            self.last_stats = {
                "prompt_tokens": r.prompt_tokens,
                "prompt_tps": round(r.prompt_tps),
                "generation_tokens": r.generation_tokens,
                "generation_tps": round(r.generation_tps, 1),
            }
            self.last_finish_reason = getattr(r, "finish_reason", None)
        return "".join(parts).strip()
