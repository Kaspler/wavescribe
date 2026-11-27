# SPDX-License-Identifier: Apache-2.0
"""Wavescribe: an offline, language-agnostic radio listener."""

import os

# No third-party telemetry, ever (docs/LICENSING.md, "Hygiene"). Set before any library loads:
# onnxruntime reads its switch when it starts, and its own disable call comes too late.
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["ORT_DISABLE_TELEMETRY"] = "1"

__version__ = "0.1.0a1"
