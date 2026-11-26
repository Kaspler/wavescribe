# SPDX-License-Identifier: Apache-2.0
"""Wavescribe for scripts and AI agents (experimental; docs/AGENT_TOOLS.md).

``wavescribe follow`` prints the running server's live feed as JSON lines, and ``wavescribe
mcp`` offers what the server lets a program on this machine read, and with ``--allow-control``
a few changes, as Model Context Protocol tools. Both talk to ``wavescribe serve`` over the HTTP
routes the page and the feed already use, and decide nothing themselves.
"""

DEFAULT_URL = "http://localhost:8765"  # `wavescribe serve` on this machine, on its default port
TOKEN_ENV = "WAVESCRIBE_TOKEN"  # a feed token, for a server on another machine
