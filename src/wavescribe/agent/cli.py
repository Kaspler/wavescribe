# SPDX-License-Identifier: Apache-2.0
"""``wavescribe follow``, ``wavescribe digest`` and ``wavescribe mcp`` (experimental;
docs/AGENT_TOOLS.md)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import urllib.parse
from typing import Any

from wavescribe.agent import DEFAULT_URL, TOKEN_ENV
from wavescribe.home import given_path


def _client(args: argparse.Namespace) -> Any:
    from wavescribe.agent.client import Client

    return Client(args.url, args.token or os.environ.get(TOKEN_ENV) or None)


def server_url(text: str) -> str:
    """``--url``: a Wavescribe's address, refused at once (argparse says why and exits 2)
    unless it is http:// or https:// with a host. Anything else would hang (ftp://) or fail on
    every request."""
    try:
        parts = urllib.parse.urlsplit(text)
        _ = parts.port  # ValueError for one that isn't a port
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{text!r} isn't a web address ({e})") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise argparse.ArgumentTypeError(
            f"{text!r} isn't an http:// or https:// address with a host, like {DEFAULT_URL}"
        )
    if any(c.isspace() or not c.isprintable() for c in text):
        raise argparse.ArgumentTypeError(f"{text!r} has a space or a control character in it")
    if parts.username is not None or parts.password is not None:
        raise argparse.ArgumentTypeError(
            f"{text!r} has a name or password in it; give a feed token with --token instead"
        )
    if parts.query or parts.fragment or "?" in text or "#" in text:
        raise argparse.ArgumentTypeError(
            f"{text!r} has a ?query or #fragment; give the address Wavescribe answers at"
        )
    return text


def since_id(text: str) -> str:
    """``--since-id``: a resume point as ``follow`` gives it (``195,197``) or a segment id."""
    from wavescribe.events import resume_point

    if resume_point(text) is None:
        raise argparse.ArgumentTypeError(
            f"{text!r} isn't a resume point (like 195,197) nor an utterance id"
        )
    return text


def cmd_follow(args: argparse.Namespace) -> int:
    from wavescribe.agent.client import ResumeFile, follow

    query = {"events": "alert"} if args.alerts_only else {}
    for key in ("stations", "langs", "since"):
        if getattr(args, key) is not None:
            query[key] = str(getattr(args, key))
    start, keep = args.since_id, None
    if args.resume is not None:
        kept = ResumeFile(args.resume)
        try:
            start = kept.start(start)
        except (OSError, ValueError) as e:
            print(f"wavescribe: {e}", file=sys.stderr)
            return 2
        keep = kept.write
    try:
        return follow(_client(args), query, sys.stdout, since_id=start, keep=keep)
    except BrokenPipeError:
        # The program reading the lines has gone (`| head -1`): stop quietly. Python would try
        # to flush stdout once more on the way out and complain, so it goes to /dev/null.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


def cmd_digest(args: argparse.Namespace) -> int:
    from wavescribe.agent.client import Refused, Unreachable
    from wavescribe.agent.mcp import Tools
    from wavescribe.digest.cli import body, render
    from wavescribe.events import to_json

    client = _client(args)
    try:
        out = client.digest(body(args))
        dated = args.until is not None  # a window that ended earlier: its day, not "today"
        shown = to_json(Tools(client).digested(out)) if args.json else render(out, dated=dated)
    except (Unreachable, Refused) as e:
        print(f"wavescribe: {e}", file=sys.stderr)
        return 1
    print(shown)
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
            type=server_url,
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
        "resumes after the last utterance printed; each utterance's `resume`, or --resume FILE, "
        "goes on from there after a restart. See docs/AGENT_TOOLS.md.",
    )
    server(p)
    p.add_argument("--stations", metavar="IDS", help="only these stations, comma-separated")
    p.add_argument("--langs", metavar="LANGS", help="only these translations, comma-separated")
    p.add_argument("--alerts-only", action="store_true", help="alerts, no utterances")
    p.add_argument(
        "--since-id",
        type=since_id,
        metavar="ID",
        help="first, the utterances after this point (up to 1,000): the `resume` of the last "
        "line handled, or what follow says when stopped, like 195,197. An utterance's plain id "
        "can miss a line that was still being translated then",
    )
    p.add_argument(
        "--since",
        type=float,
        metavar="EPOCH",
        help="first, the utterances heard since this time, in seconds since 1970 (up to 1,000)",
    )
    p.add_argument(
        "--resume",
        type=given_path,
        metavar="FILE",
        help="keep the resume point in FILE after each line printed, and start from it: a "
        "restart, even after a kill, loses no line (one may come twice). --since-id and --since "
        "count only while FILE holds none",
    )
    p.set_defaults(func=cmd_follow)

    from wavescribe.digest.cli import since

    p = sub.add_parser(
        "digest",
        help="what was said while you weren't listening, from a running Wavescribe (experimental)",
        description="Experimental. A few points the running Wavescribe's local model picks out "
        "of what some "
        "stations said over the last hour (or --minutes, or --since; --until for a time that "
        "ended earlier), each with the lines it rests on, in your language; --now: what one "
        "station is on about. It waits behind live translation, so it can take a few minutes, "
        "and it can miss things or get them wrong. See docs/GETTING_STARTED.md.",
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
        "--until",
        type=since,
        metavar="TIME",
        help="up to this time rather than now (not with --now): a time of day like 15:00 "
        "(yesterday's when it is still to come), or seconds since 1970; --minutes then counts "
        "back from it",
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
        "was said, search, alerts, what band watches caught (catches) and status, wait for "
        "the next alert, ask the local model, and have it sum up what was said (digest) or "
        "what a station is on about (now). See docs/AGENT_TOOLS.md.",
    )
    server(p)
    p.add_argument(
        "--allow-control",
        action="store_true",
        help="also offer tools that start and stop stations and add an internet station, and "
        "let digest and now read their answer aloud on the Wavescribe machine (speak)",
    )
    p.set_defaults(func=cmd_mcp)
