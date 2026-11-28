# SPDX-License-Identifier: Apache-2.0
"""docs/COMMANDS.md names every command and every option the program takes, and none it
doesn't: a command or an option added, renamed or removed without the page fails here."""

import argparse
import re
from collections.abc import Iterator
from pathlib import Path

from wavescribe.cli import build_parser

DOC = Path("docs/COMMANDS.md")
HEADING = re.compile(r"^#{2,3} (.+)$", re.MULTILINE)


def commands(
    parser: argparse.ArgumentParser, path: str = "wavescribe"
) -> Iterator[tuple[str, argparse.ArgumentParser]]:
    """Every command with its parser: ``wavescribe``, ``wavescribe radio``, ``… radio scan``."""
    yield path, parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from commands(sub, f"{path} {name}")


def options(parser: argparse.ArgumentParser) -> set[str]:
    """What a command takes with a dash, ``--help`` aside."""
    return {
        o
        for a in parser._actions
        if not isinstance(a, argparse._HelpAction)
        for o in a.option_strings
    }


def slug(heading: str) -> str:
    """A heading's anchor, as the Markdown renderer makes it."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def sections(text: str) -> dict[str, str]:
    """{anchor: the text under that heading, up to the next one}. What comes before the first
    heading is under ``wavescribe``."""
    marks = list(HEADING.finditer(text))
    out = {"wavescribe": text[: marks[0].start()]}
    for m, nxt in zip(marks, [*marks[1:], None], strict=True):
        out[slug(m[1])] = text[m.end() : nxt.start() if nxt else len(text)]
    return out


def named(section: str) -> set[str]:
    """The options a section names in `code`: ``--tag TAG …`` names ``--tag``."""
    spans = re.findall(r"`([^`\n]+)`", section)
    return {w.rstrip(",.;:") for span in spans for w in span.split() if w.startswith("-")}


def test_the_command_reference_names_every_command_and_every_option() -> None:
    doc = sections(DOC.read_text(encoding="utf-8"))
    missing = []
    every = list(commands(build_parser()))
    # An option every command takes (--home) is told once, before the first command.
    shared = set.intersection(*(options(p) for _, p in every)) & named(doc["wavescribe"])
    for path, parser in every:
        own = doc.get(slug(path))
        if own is None:
            missing.append(f"no section for `{path}`")
            continue
        # A command that takes another's options says so with a link to that section.
        linked = [doc.get(anchor, "") for anchor in re.findall(r"\]\(#([\w-]+)\)", own)]
        told = named(own).union(shared, *map(named, linked))
        missing += [f"`{path}`: {o}" for o in sorted(options(parser) - told)]
    assert not missing, f"{DOC} leaves out: " + "; ".join(missing)


def test_the_command_reference_describes_nothing_the_program_does_not_take() -> None:
    parsers = dict(commands(build_parser()))
    text = DOC.read_text(encoding="utf-8")
    extra = [h for h in HEADING.findall(text) if "wavescribe" in h and h.strip("`") not in parsers]
    for path in parsers:
        # An option in the first cell of a table row is the command's, or one its commands share.
        taken = set().union(*(options(p) for name, p in parsers.items() if name.startswith(path)))
        rows = re.findall(r"^\| (`-[^|]*) \|", sections(text).get(slug(path), ""), re.MULTILINE)
        extra += [f"`{path}`: {o}" for row in rows for o in sorted(named(row) - taken)]
    assert not extra, f"{DOC} describes what isn't there: " + "; ".join(extra)


def test_the_tripwire_reads_headings_options_and_links() -> None:
    doc = sections(
        "intro `wavescribe --version`\n## `wavescribe a b`\nsee [x](#shared-ones)\n"
        "| `-v`, `--verbose` | x |\n| `--tag TAG …` | x |\n## Shared ones\n`--all`\n"
    )
    assert set(doc) == {"wavescribe", "wavescribe-a-b", "shared-ones"}
    assert named(doc["wavescribe"]) == {"--version"}
    assert named(doc["wavescribe-a-b"]) == {"-v", "--verbose", "--tag"}
