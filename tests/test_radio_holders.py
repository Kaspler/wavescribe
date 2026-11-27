# SPDX-License-Identifier: Apache-2.0
"""Which program holds a dongle, from a synthetic USB registry listing shaped like
``ioreg -r -c IOUSBHostDevice -l -w 0`` on macOS 27 (made-up serials and pids). Nothing here
runs ioreg, ps or an rtl tool."""

from wavescribe.radio.holders import IOREG, Holder, held_elsewhere, parse_holders

OWN_PID = 1000


def device(name: str, serial: str, *, vendor: int = 3034, product: int = 10296,
           owner: str | None = None, location: str = "00110000") -> list[str]:  # fmt: skip
    props = [f'"idProduct" = {product}', f'"idVendor" = {vendor}',
             f'"USB Product Name" = "{name}"', f'"USB Serial Number" = "{serial}"']  # fmt: skip
    if owner:
        props.append(f'"UsbExclusiveOwner" = "{owner}"')
    return [f"+-o {name}@{location}  <class IOUSBHostDevice, id 0x100000a01, registered, "
            "matched, active, busy 0 (10 ms), retain 20>", "  | {",
            *(f"  |   {p}" for p in props), "  | }", "  | "]  # fmt: skip


def client(name: str, pid: int, cls: str = "AppleUSBHostDeviceUserClient", depth: int = 1):
    node, pad = "  " + "| " * (depth - 1), "  " + "| " * depth  # "+-o" two columns a level
    return [f"{node}+-o {name}  <class {cls}, id 0x100000b02, !registered, !matched, active, "
            "busy 0, retain 7>", f"{pad}  {{",
            f'{pad}    "IOUserClientCreator" = "pid {pid}, {name}"', f"{pad}  }}"]  # fmt: skip


def interface(n: int, owner: str | None = None) -> list[str]:
    props = [f'"bInterfaceNumber" = {n}'] + ([f'"UsbExclusiveOwner" = "{owner}"'] if owner else [])
    return [f"  +-o Bulk-In, Interface@{n}  <class IOUSBHostInterface, id 0x100000c0{n}, "
            "registered, matched, active, busy 0 (8 ms), retain 6>", "  | | {",
            *(f"  | |   {p}" for p in props), "  | | }"]  # fmt: skip


LISTING = "\n".join([
    # Ours: an rtl_sdr this Wavescribe started streams from it (an interface user client).
    *device("Blog V4L", "00001234"), *client("Google Chrome", 3101),
    *interface(0), *client("rtl_sdr", 2001, "AppleUSBHostInterfaceUserClient", depth=2),
    *interface(1),
    # A program opened the whole device for itself.
    *device("RTL2838UHIDIR", "00002345", owner="pid 3001, sdrpp", location="00120000"),
    *client("Arc", 3102), *interface(0), *interface(1),
    # Another Wavescribe's scan claimed its interface.
    *device("Blog V4L", "00003456", location="00130000"),
    *interface(0, owner="pid 4001, rtl_power"), *interface(1),
    # Only browsers looking: device user clients don't hold anything.
    *device("Blog V4L", "00004567", location="00140000"),
    *client("Google Chrome", 3101), *client("Arc", 3102), *interface(0), *interface(1),
    # A hub its kernel driver owns, listing a dongle again with its subtree cut off.
    *device("USB2.0 Hub", "", vendor=5296, product=1154, owner="AppleUSB20Hub",
            location="00100000"),
    "  +-o AppleUSB20HubPort@00120000  <class AppleUSB20HubPort, id 0x100000d01, registered>",
    "    +-o RTL2838UHIDIR@00120000  <class IOUSBHostDevice, id 0x100000a02, registered>",
    '      "idProduct" = 10296', '      "idVendor" = 3034',
    '      "USB Serial Number" = "00002345"',
    # Not a radio: its holder is none of our business.
    *device("Security Key", "K1", vendor=4176, product=1031, location="00150000"),
    *interface(0), *client("helper", 5001, "AppleUSBHostInterfaceUserClient", depth=2),
])  # fmt: skip

PS = {  # ps -o ppid= -p <pid>, and the parent's command line
    "2001": str(OWN_PID), "3001": "1", "4001": "4000",
    "1": "/sbin/launchd", "4000": "/Users/someone/wavescribe/.venv/bin/python -m wavescribe serve",
}  # fmt: skip


def fake_run(calls: list[list[str]]):
    def run(args):
        calls.append(list(args))
        if list(args) == IOREG:
            return LISTING
        assert args[0] == "ps", f"only ioreg and ps are run, not {args[0]}"
        return PS.get(args[-1], "") + "\n"

    return run


def test_holders_come_from_exclusive_owners_and_interface_clients_only() -> None:
    assert parse_holders(LISTING) == {
        "00001234": [Holder(2001, "rtl_sdr")],
        "00002345": [Holder(3001, "sdrpp")],  # listed twice: counted once
        "00003456": [Holder(4001, "rtl_power")],
        "00004567": [],  # browsers enumerate every device; that holds nothing
    }


def test_another_program_or_wavescribe_is_named_and_our_own_tools_are_not() -> None:
    calls: list[list[str]] = []
    held = held_elsewhere(fake_run(calls), own_pid=OWN_PID)
    assert held == {
        "00002345": {"pid": 3001, "program": "sdrpp", "other_wavescribe": False,
                     "wavescribe_pid": None, "serve": False,
                     "reason": "sdrpp (pid 3001) is using it; stop it to use this receiver here"},
        # The Wavescribe that started the rtl tool is the process to name: here, a serve.
        "00003456": {"pid": 4001, "program": "rtl_power", "other_wavescribe": True,
                     "wavescribe_pid": 4000, "serve": True,
                     "reason": "another Wavescribe is using it (rtl_power, pid 4001)"},
    }  # fmt: skip
    assert calls[0] == IOREG and {c[0] for c in calls} == {"ioreg", "ps"}


def test_another_wavescribe_command_is_told_from_serve() -> None:
    def run(args):
        if list(args) == IOREG:
            return "\n".join([*device("Blog V4L", "00003456", location="00130000"),
                              *interface(0, owner="pid 4001, rtl_sdr")])  # fmt: skip
        return {"4001": "4000", "4000": "/x/.venv/bin/python /x/.venv/bin/wavescribe radio "
                "watch a 2m"}[args[-1]] + "\n"  # fmt: skip

    held = held_elsewhere(run, own_pid=OWN_PID)["00003456"]
    assert held["wavescribe_pid"] == 4000 and held["serve"] is False


def test_nothing_is_said_when_the_registry_cant_be_read() -> None:
    assert held_elsewhere(lambda _args: None, own_pid=OWN_PID) == {}
