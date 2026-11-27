# SPDX-License-Identifier: Apache-2.0
"""Every command the program tells someone to run is written as it is typed in this install:
``uv run wavescribe …`` in a Wavescribe folder, ``wavescribe …`` where it is installed."""

import ast
import re
from pathlib import Path

import pytest

from wavescribe import invocation
from wavescribe.invocation import detect, typed

SOURCES = Path("src/wavescribe")
# "wavescribe" followed by a command, as a person would type it; "uv run wavescribe …" is the
# folder's own wording, and "wavescribe:", "wavescribe/0.1" or "wavescribe-12" are no command.
BARE = re.compile(r"(?<![\w./@-])wavescribe +(?=[a-z<{])")
# A message that starts with the program's name and a command's, as an error prefix does:
# "wavescribe token: no token 'x'".
PREFIX = re.compile(r"wavescribe [a-z-]+:")
# Text for --help and --version, which names the program rather than telling anyone what to type.
HELP_ARGS = {"help", "description", "epilog", "usage", "version"}
ALLOWED = {
    # Help text that reaches argparse through a name rather than a help= argument.
    ("token_cli.py", "the running `wavescribe serve` on this machine (default: {})"),
    # pipeline.py's, which still says the bare command.
    ("pipeline.py", "transcribing needs the speech detector (Silero VAD): download it under "
     "Settings → Languages and models (or wavescribe models fetch); listening works without it"),
}  # fmt: skip


def checkout(root: Path, name: str = "wavescribe", lock: bool = True) -> Path:
    """A project folder laid out as a clone: src/wavescribe beside pyproject.toml (and uv.lock)."""
    package = root / "src" / "wavescribe"
    package.mkdir(parents=True)
    (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n')
    if lock:
        (root / "uv.lock").write_text("version = 1\n")
    return package


def on_path(*names: str):
    return lambda name: f"/usr/local/bin/{name}" if name in names else None


def test_a_wavescribe_folder_runs_it_through_uv(tmp_path: Path) -> None:
    package = checkout(tmp_path / "wavescribe-main")
    assert detect(package, on_path("uv")) == "uv run wavescribe"
    # An activated environment puts the script on the PATH; uv run works there all the same.
    assert detect(package, on_path("uv", "wavescribe")) == "uv run wavescribe"
    # uv is missing from a background service's PATH more often than not: still the folder's way.
    assert detect(package, on_path()) == "uv run wavescribe"


def test_an_installed_package_is_typed_by_its_name(tmp_path: Path) -> None:
    site = tmp_path / "lib" / "python3.12" / "site-packages" / "wavescribe"
    site.mkdir(parents=True)
    assert detect(site, on_path("uv", "wavescribe")) == "wavescribe"
    # A folder that isn't Wavescribe's own project, or isn't a uv project.
    assert detect(checkout(tmp_path / "other", name="something"), on_path("uv")) == "wavescribe"
    assert detect(checkout(tmp_path / "pip", lock=False), on_path("uv")) == "wavescribe"
    (tmp_path / "broken").mkdir()
    broken = checkout(tmp_path / "broken" / "x")
    (broken.parents[1] / "pyproject.toml").write_text("[project\n")
    assert detect(broken, on_path("uv")) == "wavescribe"


def test_a_folder_without_uv_whose_script_is_on_the_path_is_typed_by_its_name(
    tmp_path: Path,
) -> None:
    assert detect(checkout(tmp_path / "w"), on_path("wavescribe")) == "wavescribe"


def test_typed_puts_the_command_after_this_installs_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert typed("serve --open") == "uv run wavescribe serve --open"  # the tests run in a clone
    monkeypatch.setattr(invocation, "invocation", lambda: "wavescribe")
    assert typed("models fetch yamnet") == "wavescribe models fetch yamnet"
    assert typed() == "wavescribe"


def docstrings(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def help_texts(tree: ast.AST) -> set[int]:
    return {
        id(sub)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg in HELP_ARGS
        for sub in ast.walk(kw.value)
    }


def strings(tree: ast.AST) -> list[tuple[int, str]]:
    """Every string in the code as it reads, an f-string's fields as {}, with its line."""
    skip = docstrings(tree) | help_texts(tree)
    parts = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
    out = []
    for node in ast.walk(tree):
        if id(node) in skip or id(node) in parts:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            text = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
            out.append((node.lineno, text))
    return out


def test_no_hint_hard_codes_the_bare_command() -> None:
    files = [p for p in SOURCES.rglob("*.py") if "dist" not in p.relative_to(SOURCES).parts]
    assert len(files) > 50
    found = []
    for path in files:
        name = path.relative_to(SOURCES).as_posix()
        for line, text in strings(ast.parse(path.read_text(encoding="utf-8"))):
            if (name, text) in ALLOWED:
                continue
            for m in BARE.finditer(text):
                if text[: m.start()].endswith("uv run "):
                    continue
                if m.start() == 0 and PREFIX.match(text):
                    continue
                found.append(f"{name}:{line}: {text!r}")
    assert not found, "use wavescribe.invocation.typed() for: " + "; ".join(found)


def test_the_tripwire_finds_a_bare_command_and_passes_what_isnt_one() -> None:
    tree = ast.parse(
        '"""Run `wavescribe serve`."""\n'
        'a = "run: wavescribe models fetch x"\n'
        'b = f"or `wavescribe radio scan {rx} fm`"\n'
        'c = ("join " "`wavescribe "\n     "doctor`")\n'
        'd = "wavescribe: failed"\n'
        'e = f"wavescribe token: {why}"\n'
        "f = f\"{typed('serve')}\"\n"
        'g = "as `uv run wavescribe doctor`"\n'
        'h = "wavescribe/0.1, wavescribe-12"\n'
        'p.add_argument("--url", help="the running `wavescribe serve`")\n'
    )
    texts = [t for _, t in strings(tree)]
    hits = [t for t in texts for m in BARE.finditer(t)
            if not t[: m.start()].endswith("uv run ")
            and not (m.start() == 0 and PREFIX.match(t))]  # fmt: skip
    assert hits == [
        "run: wavescribe models fetch x",
        "or `wavescribe radio scan {} fm`",
        "join `wavescribe doctor`",
    ]
