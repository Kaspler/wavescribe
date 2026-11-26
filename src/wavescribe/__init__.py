# SPDX-License-Identifier: Apache-2.0
"""Wavescribe: an offline, language-agnostic radio listener."""

import os

# No third-party telemetry, ever (docs/LICENSING.md, "Hygiene").
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

__version__ = "0.1.0a1"
