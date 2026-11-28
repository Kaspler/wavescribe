# SPDX-License-Identifier: Apache-2.0
"""A stand-in for the ``meshtastic`` command-line program, for tests: it takes the options its
documentation lists for sending a text (``--port``/``--host``/``--ble`` to reach the node,
``--dest``, ``--sendtext``, ``--ch-index``, ``--ack``), prints what such a run prints, and ends
as told. No radio, no device, no network.

Environment:
    FAKE_MESH_ANSWER  ack (the default): the node acknowledges it; nak: no acknowledgement came
                      after the node's tries; implicit: a relay was heard, no acknowledgement;
                      silent: wait until stopped; nodevice: no node attached, exit 1; plain: exit 0
                      without a word about an acknowledgement
    FAKE_MESH_LOG     a file that gets one JSON line per run: the arguments, as parsed
    FAKE_MESH_DELAY   seconds to take before answering
"""

import argparse
import json
import os
import sys
import time


def main() -> int:
    p = argparse.ArgumentParser(prog="meshtastic")
    p.add_argument("--port", "--serial", "-s")
    p.add_argument("--host")
    p.add_argument("--ble", "-b")
    p.add_argument("--dest")
    p.add_argument("--sendtext")
    p.add_argument("--ch-index", type=int)
    p.add_argument("--ack", action="store_true")
    args = p.parse_args()
    answer = os.environ.get("FAKE_MESH_ANSWER", "ack")
    if log := os.environ.get("FAKE_MESH_LOG"):
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({**vars(args), "argv": sys.argv[1:]}, ensure_ascii=False) + "\n")
    if answer == "nodevice":
        print("No Meshtastic device found on a serial port or the network", file=sys.stderr)
        return 1
    print("Connected to radio")
    if args.sendtext is None:
        return 0
    where = args.dest or "^all"
    print(f"Sending text message {args.sendtext} to {where} on channelIndex:{args.ch_index or 0}")
    if not args.ack:
        return 0
    print("Waiting for an acknowledgment from remote node (this could take a while)", flush=True)
    time.sleep(float(os.environ.get("FAKE_MESH_DELAY", "0")))
    if answer == "silent":
        while True:
            time.sleep(1)
    if answer == "nak":
        print("Received a NAK, error reason: MAX_RETRANSMIT")
    elif answer == "implicit":
        print("Received an implicit ACK. Packet will likely arrive, but cannot be guaranteed.")
    elif answer == "ack":
        print("Received an ACK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
