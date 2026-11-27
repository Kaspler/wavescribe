# SPDX-License-Identifier: Apache-2.0
"""scripts/commit_checks.py: the built page and licence checks on every commit."""

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "commit_checks.py"
spec = importlib.util.spec_from_file_location("commit_checks", SCRIPT)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

LICENSING = "# Licensing\n\n| numpy | BSD-3-Clause | ✅ |\n| react | MIT | ✅ |\n"


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def write(root, path, text):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "core.excludesFile", "/dev/null")  # no global ignores
    write(tmp_path, "docs/LICENSING.md", LICENSING)
    write(tmp_path, "pyproject.toml", '[project]\nname = "x"\ndependencies = ["numpy>=2"]\n')
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "init")
    return cc.Repo(tmp_path)


def stage(repo, files):
    for path, text in files.items():
        write(repo.root, path, text)
    git(repo.root, "add", "-A")


def test_no_frontend_means_no_page_check(repo):
    assert cc.check_page(repo) == []


def test_page_must_be_built_from_the_committed_sources(repo):
    stage(repo, {"frontend/package.json": "{}", "frontend/src/main.tsx": "a"})
    assert "never built" in cc.check_page(repo)[0]

    cc.stamp(repo)
    write(repo.root, "src/wavescribe/web/dist/index.html", "<html>")
    assert any("isn't staged" in p for p in cc.check_page(repo))
    git(repo.root, "add", "-A")
    assert cc.check_page(repo) == []

    stage(repo, {"frontend/src/main.tsx": "b"})
    problems = cc.check_page(repo)
    assert "frontend/src/main.tsx" in problems[0]
    assert "npm --prefix frontend run build" in problems[-1]


def test_page_built_from_unstaged_sources_is_caught(repo):
    stage(repo, {"frontend/package.json": "{}"})
    write(repo.root, "frontend/src/new.tsx", "not staged")
    cc.stamp(repo)
    git(repo.root, "add", "src")
    assert "frontend/src/new.tsx" in cc.check_page(repo)[0]


def test_new_python_dependency_needs_an_entry(repo):
    stage(repo, {"pyproject.toml": '[project]\ndependencies = ["numpy", "left_pad>=1"]\n'})
    problems = cc.check_licences(repo)
    assert "left_pad (pyproject.toml)" in problems[0]
    stage(repo, {"docs/LICENSING.md": LICENSING + "| left-pad | MIT | ✅ |\n"})
    assert cc.check_licences(repo) == []


def test_transitive_python_packages_pass_only_with_a_tier_a_licence(repo):
    site = repo.root / ".venv/lib/python3.12/site-packages"
    write(repo.root, ".gitignore", ".venv/\n")
    write(site, "okpkg-1.0.dist-info/METADATA", "Name: okpkg\nLicense-Expression: MIT\n")
    write(
        site,
        "gplpkg-2.0.dist-info/METADATA",
        "Name: gplpkg\nClassifier: License :: OSI Approved :: GNU General Public License v3\n",
    )
    lock = "".join(f'[[package]]\nname = "{n}"\n\n' for n in ("okpkg", "gplpkg", "gone"))
    stage(repo, {"uv.lock": lock})
    problems = cc.check_licences(repo)
    assert len(problems) == 1
    assert "gplpkg" in problems[0] and "okpkg" not in problems[0]
    assert "gone (uv.lock, transitive; licence: unknown)" in problems[0]


def test_npm_dependencies(repo):
    package = {"dependencies": {"react": "^19"}, "devDependencies": {"vite": "^7"}}
    lock = {
        "packages": {
            "": {},
            "node_modules/react": {"license": "MIT"},
            "node_modules/vite": {"license": "MIT"},
            "node_modules/odd": {"license": "GPL-3.0-only"},
            "node_modules/dual": {"license": "(MIT OR GPL-3.0)"},
        }
    }
    stage(
        repo,
        {
            "frontend/package.json": json.dumps(package),
            "frontend/package-lock.json": json.dumps(lock),
        },
    )
    problems = cc.check_licences(repo)
    assert "vite (frontend/package.json)" in problems[0]
    assert "react" not in problems[0]
    assert "odd (frontend/package-lock.json" in problems[1]
    assert "dual" not in problems[1]


def test_models_need_licensing_updated_in_the_same_commit(repo):
    model = '[[model]]\nid = "m"\nlicense = "MIT"\nlicense_url = "u"\ntier = "default"\n'
    stage(repo, {"src/wavescribe/models.toml": model})
    assert "Models added or re-pinned" in cc.check_licences(repo)[0]
    stage(repo, {"docs/LICENSING.md": LICENSING + "| m | MIT | ✅ | checked |\n"})
    assert cc.check_licences(repo) == []
    git(repo.root, "commit", "-qm", "model")
    stage(repo, {"src/wavescribe/models.toml": model + 'revision = "abc"\n'})
    assert "m" in cc.check_licences(repo)[0]


def test_ci_checks_the_licences_of_everything_since_a_base_commit(repo):
    base = git(repo.root, "rev-parse", "HEAD").stdout.strip()
    stage(repo, {"pyproject.toml": '[project]\ndependencies = ["numpy", "left_pad>=1"]\n'})
    git(repo.root, "commit", "-qm", "a dependency, no entry")  # e.g. with the hook skipped
    stage(repo, {"README.md": "later\n"})
    git(repo.root, "commit", "-qm", "something else")
    assert cc.check_licences(repo) == []  # what CI sees without a base: a clean checkout
    assert "left_pad (pyproject.toml)" in cc.check_licences(cc.Repo(repo.root, base))[0]

    def check(*args):
        return subprocess.run(["python3", "-I", str(SCRIPT), "check", *args], cwd=repo.root,
                              capture_output=True, text=True)  # fmt: skip

    assert check().returncode == 0
    found = check("--base", base)
    assert found.returncode == 1 and "left_pad (pyproject.toml)" in found.stderr
    assert check("--base", "HEAD").returncode == 0
    assert "no such commit" in check("--base", "0" * 40).stderr
    assert check("--base").returncode == 1  # the revision is missing


CLI = """
def build_parser():
    sub = parser.add_subparsers(dest="command")
    models = sub.add_parser("models", help="list and download models")
    models_sub = models.add_subparsers(dest="models_command")
    models_sub.add_parser("fetch", help="download models")
    alerts = sub.add_parser(
        "alerts",
        help="recent alerts",
    )
    add_agent_parser(sub)
"""
AGENT_CLI = """
def add_parser(sub):
    p = sub.add_parser("follow", help="the live feed")
    actions = p.add_subparsers(dest="action")
    actions.add_parser("once")
"""


def test_every_command_needs_its_line_in_the_readme(repo):
    assert cc.check_readme(repo) == []  # no command line here
    readme = "- Models, with `wavescribe models fetch`.\n- Alerts: `wavescribe\n  alerts`.\n"
    stage(
        repo,
        {
            "src/wavescribe/cli.py": CLI,
            "src/wavescribe/agent/cli.py": AGENT_CLI,
            "README.md": readme,
        },
    )
    problems = cc.check_readme(repo)
    assert len(problems) == 1 and "`wavescribe follow`" in problems[0]
    # Named ones pass, and a command's own actions (fetch, once) aren't asked for.
    for name in ("models", "alerts", "fetch", "once"):
        assert f"`wavescribe {name}`" not in problems[0]
    assert "What it does" in problems[0] and "docs/" in problems[0]
    assert cc.run_checks(repo) == problems

    # Its name alone, or as the start of another word, isn't its line.
    stage(repo, {"README.md": readme + "`follow`, `wavescribe followers`, Wavescribe follow\n"})
    assert "`wavescribe follow`" in cc.check_readme(repo)[0]
    stage(repo, {"README.md": readme + "- The live feed: `wavescribe follow`.\n"})
    assert cc.check_readme(repo) == []

    # A commit that adds a command without its line is blocked, with what to add.
    git(repo.root, "commit", "-qm", "commands")
    token = 'def add_parser(sub):\n    sub.add_parser("token")\n'
    stage(repo, {"src/wavescribe/token_cli.py": token})

    def check():
        return subprocess.run(["python3", "-I", str(SCRIPT), "check"], cwd=repo.root,
                              capture_output=True, text=True)  # fmt: skip

    blocked = check()
    assert blocked.returncode == 1 and "`wavescribe token`" in blocked.stderr
    stage(repo, {"README.md": (repo.root / "README.md").read_text() + "`wavescribe token`\n"})
    assert check().returncode == 0


def test_the_commands_read_from_the_sources_are_the_real_ones_and_the_readme_names_them():
    """The check reads the sources instead of importing them: this holds it to the parser."""
    import argparse

    from wavescribe.cli import build_parser

    root = SCRIPT.parent.parent
    commands = next(a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    found = [
        name
        for path in sorted((root / "src/wavescribe").rglob("*.py"))
        for name in cc.commands_in(path.read_text(encoding="utf-8"))
    ]
    assert sorted(found) == sorted(commands.choices)
    assert cc.unlisted(found, (root / "README.md").read_text(encoding="utf-8")) == []


def test_spdx_expressions():
    assert cc.spdx_ok("MIT")
    assert cc.spdx_ok("(MIT OR GPL-3.0-or-later)")
    assert cc.spdx_ok("Apache-2.0 WITH LLVM-exception")
    assert not cc.spdx_ok("MIT AND GPL-2.0-only")
    assert not cc.spdx_ok("LGPL-2.1-or-later")
    assert not cc.spdx_ok(None)


def test_installed_hook_blocks_a_stale_page_and_runs_local_checks(repo):
    (repo.root / "scripts").mkdir()
    shutil.copy(SCRIPT, repo.root / "scripts/commit_checks.py")
    (repo.root / ".git/info").mkdir(exist_ok=True)
    (repo.root / ".git/info/exclude").write_text("scripts/\n")
    assert all(n.startswith("installed") for n in cc.install(repo))

    def commit(*args):
        return subprocess.run(
            ["git", "commit", "-q", *args], cwd=repo.root, capture_output=True, text=True
        )

    # `git commit -a`: the hook sees the temporary index with every tracked change.
    stage(repo, {"frontend/package.json": "{}", "frontend/a.ts": "1"})
    cc.stamp(repo)
    git(repo.root, "add", "-A")
    assert commit("-m", "page").returncode == 0
    write(repo.root, "frontend/a.ts", "2")
    assert "frontend/a.ts" in commit("-am", "edit").stderr
    cc.stamp(repo)
    assert commit("-am", "edit").returncode == 0

    # A clone's own checks, in .git/hooks/pre-commit.local, run after these and can block.
    local = repo.root / ".git/hooks/pre-commit.local"
    local.write_text("#!/bin/sh\necho 'local check says no' >&2\nexit 1\n")
    local.chmod(0o755)
    stage(repo, {"b.md": "x\n"})
    blocked = commit("-m", "b")
    assert blocked.returncode != 0 and "local check says no" in blocked.stderr
    local.unlink()
    assert commit("-m", "b").returncode == 0
