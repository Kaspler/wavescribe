# SPDX-License-Identifier: Apache-2.0
"""``wavescribe follow``, ``wavescribe digest`` and ``wavescribe mcp`` (experimental;
docs/AGENT_TOOLS.md)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

from wavescribe.agent import DEFAULT_URL, TOKEN_ENV


def _client(args: argparse.Namespace) -> Any:
    from wavescribe.agent.client import Client

    return Client(args.url, args.token or os.environ.get(TOKEN_ENV) or None)


def cmd_follow(args: argparse.Namespace) -> int:
    from wavescribe.agent.client import follow

    query = {"events": "alert"} if args.alerts_only else {}
    for key in ("stations", "langs", "since", "since_id"):
        if getattr(args, key) is not None:
            query[key] = str(getattr(args, key))
    try:
        return follow(_client(args), query, sys.stdout)
    except BrokenPipeError:
        # The program reading the lines has gone (`| head -1`): stop quietly. Python would try
        # to flush stdout once more on the way out and complain, so it goes to /dev/null.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


def cmd_digest(args: argparse.Namespace) -> int:
    from wavescribe.agent.client import Refused, Unreachable
    from wavescribe.digest.cli import body, render
    from wavescribe.events import to_json

    try:
        out = _client(args).digest(body(args))
    except (Unreachable, Refused) as e:
        print(f"wavescribe: {e}", file=sys.stderr)
        return 1
    print(to_json(out) if args.json else render(out))
    speech = out.get("speech") or {"started": True}
    if not speech["started"]:
        print(f"wavescribe: not read aloud: {speech['error']}", file=sys.stderr)
        return 1
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from wavescribe.agent.mcp import run

    # The protocol owns stdout. Anything else that prints, here or in a library, goes to
    # stderr, which MCP clients keep as the server's log.
    protocol, sys.stdout = sys.stdout.buffer, sys.stderr
    logging.basicConfig(
        stream=sys.stderr, level=logging.WARNING, format="wavescribe mcp: %(message)s"
    )
    control = "on" if args.allow_control else "off"
    print(f"wavescribe mcp: tools for {args.url}; control {control}", file=sys.stderr)
    return run(_client(args), sys.stdin.buffer, protocol, allow_control=args.allow_control)


def add_parser(sub: Any) -> None:
    def server(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--url",
            default=DEFAULT_URL,
            help=f"the running `wavescribe serve` (default: {DEFAULT_URL}, this machine's)",
        )
        p.add_argument(
            "--token",
            help=f"a feed token, which another machine needs once tokens exist "
            f"(default: ${TOKEN_ENV})",
        )

    p = sub.add_parser(
        "follow",
        help="live utterances and alerts from a running Wavescribe, one JSON object per line "
        "(experimental)",
        description="Print the live feed of a running Wavescribe (/feed/v1/events), one JSON "
        "object per line, until interrupted; experimental. A lost connection is tried again and "
        "resumes after the last utterance printed. See docs/AGENT_TOOLS.md.",
    )
    server(p)
    p.add_argument("--stations", metavar="IDS", help="only these stations, comma-separated")
    p.add_argument("--langs", metavar="LANGS", help="only these translations, comma-separated")
    p.add_argument("--alerts-only", action="store_true", help="alerts, no utterances")
    p.add_argument(
        "--since-id",
        type=int,
        metavar="ID",
        help="first, the utterances stored after this id (up to 1,000)",
    )
    p.add_argument(
        "--since",
        type=float,
        metavar="EPOCH",
        help="first, the utterances heard since this time, in seconds since 1970 (up to 1,000)",
    )
    p.set_defaults(func=cmd_follow)

    from wavescribe.digest.cli import since

    p = sub.add_parser(
        "digest",
        help="what was said while you weren't listening, from a running Wavescribe",
        description="A few points the running Wavescribe's local model picks out of what some "
        "stations said over the last hour (or --minutes, or --since), each with the lines it "
        "rests on, in your language; --now: what one station is on about. It waits behind "
        "live translation, so it can take a few minutes, and it can miss things or get them "
        "wrong. See docs/GETTING_STARTED.md.",
    )
    server(p)
    p.add_argument("--stations", metavar="IDS", help="only these stations, comma-separated")
    window = p.add_mutually_exclusive_group()
    window.add_argument("--minutes", type=float, help="how far back to look (60; with --now, 5)")
    window.add_argument(
        "--since",
        type=since,
        metavar="TIME",
        help="from this time on: a time of day like 21:30, or seconds since 1970",
    )
    p.add_argument(
        "--language",
        metavar="LANG",
        help="write it in this language, as BCP-47 (default: the stations' translations)",
    )
    p.add_argument(
        "--now",
        action="store_true",
        help="what one station is on about now: the one --stations names, else the station "
        "heard last",
    )
    p.add_argument(
        "--speak",
        action="store_true",
        help="also read its points aloud on the Wavescribe machine, in the alerts' voice for the "
        "language; an alert stops it at once",
    )
    p.add_argument("--json", action="store_true", help="the server's answer, as one line of JSON")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser(
        "mcp",
        help="tools for an AI agent: an MCP server on stdin and stdout (experimental)",
        description="A Model Context Protocol server over stdio for an AI agent, in front of "
        "a running Wavescribe on this machine; experimental. Its tools read stations, what "
        "was said, search, alerts and status, wait for the next alert and ask the local "
        "model. See docs/AGENT_TOOLS.md.",
    )
    server(p)
    p.add_argument(
        "--allow-control",
        action="store_true",
        help="also offer tools that start and stop stations and add an internet station",
    )
    p.set_defaults(func=cmd_mcp)
