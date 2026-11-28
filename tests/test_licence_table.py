"""scripts/licence_table.py: what each locked package is needed for, and where it is installed."""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "licence_table.py"
spec = importlib.util.spec_from_file_location("licence_table", SCRIPT)
lt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lt)

MAC = "platform_machine == 'arm64' and sys_platform == 'darwin'"
LOCK = f"""
[[package]]
name = "app"
version = "0.1"
source = {{ editable = "." }}
dependencies = [
    {{ name = "everywhere" }},
    {{ name = "mac-only", marker = "{MAC}" }},
]
[package.dev-dependencies]
dev = [{{ name = "tester" }}]

[[package]]
name = "everywhere"
version = "1.0"
dependencies = [{{ name = "shared" }}, {{ name = "windows", marker = "sys_platform == 'win32'" }}]

[[package]]
name = "mac-only"
version = "1.0"
dependencies = [
    {{ name = "shared", marker = "sys_platform == 'darwin'" }},
    {{ name = "mac-deep", marker = "sys_platform == 'darwin'" }},
    {{ name = "either", marker = "sys_platform == 'darwin' or sys_platform == 'linux'" }},
]

[[package]]
name = "shared"
version = "1.0"

[[package]]
name = "windows"
version = "1.0"

[[package]]
name = "mac-deep"
version = "1.0"

[[package]]
name = "either"
version = "1.0"

[[package]]
name = "tester"
version = "1.0"
dependencies = [{{ name = "everywhere" }}, {{ name = "only-tests" }}]

[[package]]
name = "only-tests"
version = "1.0"
"""


def test_each_package_is_needed_for_running_or_development_and_says_where_it_installs():
    packages, need, only = lt.locked(tomllib.loads(LOCK))
    assert [p["name"] for p in packages] == [
        "either", "everywhere", "mac-deep", "mac-only", "only-tests", "shared", "tester", "windows",
    ]  # fmt: skip
    assert need == {
        "everywhere": "running", "mac-only": "running", "shared": "running", "windows": "running",
        "mac-deep": "running", "either": "running", "tester": "development",
        "only-tests": "development",
    }  # fmt: skip
    # shared is also reached without a condition; a marker met twice on a path counts once
    assert "shared" not in only and "everywhere" not in only
    assert only["mac-only"] == MAC
    assert only["mac-deep"] == MAC
    assert only["windows"] == "sys_platform == 'win32'"
    assert only["either"] == (f"(sys_platform == 'darwin' or sys_platform == 'linux') and {MAC}")
