# SPDX-License-Identifier: Apache-2.0
"""Which program holds a receiver, read from macOS's USB registry without opening anything.

A program that opens an RTL-SDR dongle through libusb (rtl_sdr, rtl_power, SDR++, another
Wavescribe) gets the device, or claims its interface, for itself. macOS records that in the
IORegistry, which ``ioreg`` reads without touching the device:

- ``"UsbExclusiveOwner" = "pid 4321, rtl_sdr"`` on the device or on one of its interfaces;
- an interface user client (``AppleUSBHostInterfaceUserClient``) whose
  ``"IOUserClientCreator" = "pid 4321, rtl_sdr"``: the process streaming from it.

A device user client alone is not a hold: browsers make one for every USB device they enumerate,
and opening a dongle only to read its name doesn't stop anyone else from using it. ``lsof`` can't
help here: libusb on macOS goes through IOKit, not through a file.

A holder whose parent is this process is one of Wavescribe's own rtl tools; one whose parent's
command line names wavescribe is another Wavescribe. Each is told apart with ``ps``.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from wavescribe.doctor import RTL_USB_IDS

Run = Callable[[Sequence[str]], str | None]

# Each device, its interfaces and their user clients: device, interface, client, one spare.
IOREG = ["ioreg", "-r", "-c", "IOUSBHostDevice", "-l", "-w", "0", "-d", "4"]

_NODE = re.compile(r"^(?P<indent>[\s|]*)\+-o (?P<name>.+?)\s+<class (?P<cls>\w+)")
_PROP = re.compile(r'^[\s|]*"([^"]+)" = (.*)$')
_OWNER = re.compile(r'^"pid (\d+), (.+)"$')


@dataclass(frozen=True)
class Holder:
    pid: int
    program: str  # the process name macOS records: "rtl_sdr", "sdrpp"


@dataclass
class _Node:
    depth: int
    name: str
    cls: str
    props: dict[str, str]
    children: list[_Node]


def _tree(text: str) -> list[_Node]:
    """Every node of an ``ioreg -l`` listing, each with its children."""
    nodes: list[_Node] = []
    stack: list[_Node] = []
    for line in text.splitlines():
        if m := _NODE.match(line):
            node = _Node(len(m["indent"]), m["name"], m["cls"], {}, [])
            while stack and stack[-1].depth >= node.depth:
                stack.pop()
            if stack:
                stack[-1].children.append(node)
            stack.append(node)
            nodes.append(node)
        elif stack and (p := _PROP.match(line)):
            stack[-1].props.setdefault(p[1], p[2].strip())
    return nodes


def _unquote(value: str) -> str:
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else value


def _is_rtl(node: _Node) -> bool:
    try:
        ids = int(node.props.get("idVendor", "")), int(node.props.get("idProduct", ""))
    except ValueError:
        return False
    name = _unquote(node.props.get("USB Product Name") or node.name)
    return ids in RTL_USB_IDS or bool(re.search(r"RTL28|RTL-?SDR", name, re.I))


def _holders_under(node: _Node) -> list[Holder]:
    out: list[Holder] = []

    def visit(n: _Node, interface_client: bool) -> None:
        owners = [n.props.get("UsbExclusiveOwner", "")]
        if interface_client:
            owners.append(n.props.get("IOUserClientCreator", ""))
        for owner in owners:
            if m := _OWNER.match(owner):  # kernel drivers own hubs by name, with no pid
                holder = Holder(int(m[1]), m[2])
                if holder not in out:
                    out.append(holder)
        for child in n.children:
            visit(child, "InterfaceUserClient" in child.cls)

    visit(node, False)
    return out


def parse_holders(text: str) -> dict[str, list[Holder]]:
    """RTL-SDR dongles in an ``ioreg -l`` listing of USB devices, by serial: the processes that
    hold each one. A device listed twice (under its hub too) counts once."""
    seen: dict[str, list[Holder]] = {}
    for node in _tree(text):
        if node.cls != "IOUSBHostDevice" or not _is_rtl(node):
            continue
        serial = _unquote(node.props.get("USB Serial Number")
                          or node.props.get("kUSBSerialNumberString") or "")  # fmt: skip
        held = seen.setdefault(serial, [])
        held += [h for h in _holders_under(node) if h not in held]
    return seen


def held_elsewhere(run: Run | None = None, *, own_pid: int | None = None) -> dict[str, Any]:
    """Dongles another program holds, by serial: what holds each and why that matters. Empty
    when none does, and wherever it can't be told (not macOS, ioreg missing)."""
    if run is None:
        if sys.platform != "darwin":
            return {}
        from wavescribe.system import run_command as run
    own_pid = os.getpid() if own_pid is None else own_pid
    text = run(IOREG)
    if not text:
        return {}
    out: dict[str, Any] = {}
    for serial, holders in parse_holders(text).items():
        for h in holders:
            if h.pid == own_pid:
                continue
            ppid, parent = _parent(run, h.pid)
            if ppid == own_pid:
                continue  # one of this Wavescribe's own rtl tools
            other = "wavescribe" in parent.lower()
            who = "another Wavescribe" if other else h.program
            out[serial] = {
                "pid": h.pid, "program": h.program, "other_wavescribe": other,
                "reason": (f"another Wavescribe is using it ({h.program}, pid {h.pid})" if other
                           else f"{who} (pid {h.pid}) is using it; stop it to use this "
                           "receiver here"),
            }  # fmt: skip
            break
    return out


def _parent(run: Run, pid: int) -> tuple[int | None, str]:
    """A process's parent pid and the parent's command line, from ``ps``."""
    out = (run(["ps", "-o", "ppid=", "-p", str(pid)]) or "").strip()
    if not out.isdigit():
        return None, ""
    ppid = int(out)
    return ppid, (run(["ps", "-o", "command=", "-p", str(ppid)]) or "").strip()
