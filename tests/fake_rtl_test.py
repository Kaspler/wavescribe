# SPDX-License-Identifier: Apache-2.0
"""A stand-in for ``rtl_test`` asked for no dongle in particular: lists the dongles named in
FAKE_RTL_SERIALS (comma separated) the way rtl_test does, opens none, and says none matched."""

import os
import sys


def main() -> int:
    serials = [s for s in os.environ.get("FAKE_RTL_SERIALS", "").split(",") if s]
    if not serials:
        print("No supported devices found.", file=sys.stderr)
        return 1
    print(f"Found {len(serials)} device(s):", file=sys.stderr)
    for i, serial in enumerate(serials):
        print(f"  {i}:  RTLSDRBlog, Blog V4, SN: {serial}", file=sys.stderr)
    print("\nNo matching devices found.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
