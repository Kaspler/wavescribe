# SPDX-License-Identifier: Apache-2.0
"""``wavescribe token``: make, list and revoke feed tokens from the Wavescribe machine.

The page makes them too (Settings → Connections), but behind a proxy its controls are off.
This command talks to the running Wavescribe on this machine with the control key
(``wavescribe.control``), so it works there as well. It never edits the settings file itself:
the running server rewrites that file whole on each change and would drop what it doesn't know,
and only the server can end the streams of a token that is revoked.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from wavescribe.agent import DEFAULT_URL

DATA_DIR = Path("data")
SCOPES = ("read", "push")


def _client(args: argparse.Namespace) -> tuple[Any, dict[str, str]] | str:
    """A client for the server at ``--url`` and the control key's header, or why not."""
    from wavescribe.agent.client import Client
    from wavescribe.control import KEY_HEADER, is_local_url, load_key

    if not is_local_url(args.url):
        return (f"{args.url} isn't this machine: tokens are made on the Wavescribe machine "
                "itself, with --url pointing at its own address (localhost)")  # fmt: skip
    key = load_key(DATA_DIR)
    if key is None:
        return (f"no control key in {DATA_DIR}/: run this in the folder Wavescribe runs in "
                "(where `wavescribe serve` was started), with Wavescribe running")  # fmt: skip
    return Client(args.url), {KEY_HEADER: key}


def _call(args: argparse.Namespace, method: str, path: str, body: Any = None) -> Any:
    import json

    from wavescribe.agent.client import Refused, Unreachable

    made = _client(args)
    if isinstance(made, str):
        raise SystemExit(f"wavescribe token: {made}")
    client, headers = made
    try:
        with client.open(method, "/api/manage" + path, body=body, control=True,
                         headers=headers) as resp:  # fmt: skip
            return json.loads(resp.read())
    except Unreachable as e:
        raise SystemExit(f"wavescribe token: {e}") from None
    except Refused as e:
        why = e.message
        if e.status == 403:
            why += (": the control key didn't match. Run this in the folder that Wavescribe "
                    "runs in, against the Wavescribe started there")  # fmt: skip
        raise SystemExit(f"wavescribe token: {why}") from None


def cmd_make(args: argparse.Namespace) -> int:
    made = _call(args, "POST", "/tokens", {"label": args.label, "scopes": args.scope or ["read"]})
    print(made["token"])
    print(f"# {made['id']}: {made['label']} ({', '.join(made['scopes'])}). Shown only now: "
          f"keep it somewhere safe. {made.get('note', '')}".rstrip(), file=sys.stderr)  # fmt: skip
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    import time

    tokens = _call(args, "GET", "/outputs").get("tokens") or []
    if not tokens:
        print("# no feed tokens: the feed is open to the LAN under --lan until the first is made",
              file=sys.stderr)  # fmt: skip
    for t in tokens:
        made = time.strftime("%Y-%m-%d %H:%M", time.localtime(t.get("created") or 0))
        print(f"{t['id']}  {', '.join(t.get('scopes', []))}  {made}  {t.get('label', '')}")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    known = {t["id"]: t for t in _call(args, "GET", "/outputs").get("tokens") or []}
    if args.id not in known:
        raise SystemExit(f"wavescribe token: no token {args.id!r} (`wavescribe token list` "
                         "shows them)")  # fmt: skip
    _call(args, "DELETE", f"/tokens/{args.id}")
    print(f"# revoked {args.id} ({known[args.id].get('label', '')}): streams using it have "
          "ended", file=sys.stderr)  # fmt: skip
    return 0


def add_parser(sub: Any) -> None:
    p = sub.add_parser(
        "token",
        help="make, list and revoke feed tokens (on the Wavescribe machine)",
        description="Make, list and revoke the feed tokens other machines and programs read "
        "with. The page does the same under Settings → Connections; behind a proxy its controls "
        "are off, and this is the way. Run it on the Wavescribe machine, in the folder "
        "Wavescribe runs in, while it runs. See docs/FEED.md.",
    )
    p.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"the running `wavescribe serve` on this machine (default: {DEFAULT_URL})",
    )
    actions = p.add_subparsers(dest="action", required=True, metavar="ACTION")
    make = actions.add_parser("make", help="make a token and print it once")
    make.add_argument("--label", default="", help="what it is for, shown in the list")
    make.add_argument(
        "--scope",
        action="append",
        choices=SCOPES,
        help="read: read the feed and the page; push: send events here (default: read; "
        "give it twice for both)",
    )
    make.set_defaults(func=cmd_make)
    actions.add_parser("list", help="the tokens made, without their secrets").set_defaults(
        func=cmd_list
    )
    revoke = actions.add_parser("revoke", help="revoke a token now: its streams end")
    revoke.add_argument("id", help="the token's id, as `list` shows it")
    revoke.set_defaults(func=cmd_revoke)
