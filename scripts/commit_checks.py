#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Checks that run before every commit, as a git pre-commit hook (`commit_checks.py install`).

1. **The built page is committed and fresh.** Once `frontend/` exists, the build output in
   `src/wavescribe/web/dist/` must be committed with it, built from exactly the sources being
   committed. The build records those sources in `dist/.sources` (`commit_checks.py stamp`).
2. **Licences are recorded.** A new dependency (pyproject.toml, uv.lock, frontend/package.json,
   frontend/package-lock.json) needs a docs/LICENSING.md entry, unless it is a transitive
   package with a tier-A licence. A new or re-pinned model in models.toml needs LICENSING.md
   updated in the same commit.
3. **The README lists every command.** Each top-level `wavescribe` command must be named in
   README.md as `wavescribe <name>`: its line in the list under "What it does". The names are
   read from the sources, where each is registered with `sub.add_parser("<name>", ...)`.

Checks of your own (anything local to your clone) can go in `.git/hooks/pre-commit.local`: the
installed hook runs it after these when it exists and is executable.

Usage:
  commit_checks.py install        Install the pre-commit hook.
  commit_checks.py check          Checks 1-3 on the staged commit (the pre-commit hook).
  commit_checks.py check --base REV
                                  The same on what is checked out, with check 2 on everything
                                  that changed since commit REV (CI: a pull request or a push).
  commit_checks.py stamp          Record the page sources a build used (the last step of the build).

Standard library only, so it runs on any Python 3.11+ without the project's environment.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from email.parser import HeaderParser
from pathlib import Path

FRONTEND = "frontend"
BUILT = "src/wavescribe/web/dist"
SOURCES = f"{BUILT}/.sources"
LICENSING = "docs/LICENSING.md"
MODELS = "src/wavescribe/models.toml"
README = "README.md"
PACKAGE = "src/wavescribe"
# How a top-level command is registered: on the main parser's subcommands, which cli.py and
# every module that adds a command call `sub`. A command's own actions go on another name.
ADD_COMMAND = re.compile(r"""(?<![\w.])sub\.add_parser\(\s*["']([a-z][a-z0-9_-]*)["']""")
BUILD_COMMAND = f"npm --prefix {FRONTEND} run build"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
HOOK_MARKER = "# installed by scripts/commit_checks.py"
RUN = 'exec python3 -I "$(git rev-parse --show-toplevel)/scripts/commit_checks.py"'
# The hook runs the checks, then the clone's own `.git/hooks/pre-commit.local` if there is one.
HOOKS = {
    "pre-commit": f"{RUN.removeprefix('exec ')} check || exit 1\n"
    '[ -x "$0.local" ] && exec "$0.local" "$@"\nexit 0',
}

# Tier A in docs/LICENSING.md: may be imported, linked or bundled without review.
TIER_A = {
    "MIT",
    "MIT-0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "0BSD",
    "ISC",
    "Apache-2.0",
    "BSL-1.0",
    "Zlib",
    "PSF-2.0",
    "CC0-1.0",
    "Unlicense",
    "MPL-2.0",
}
# Free-text licence fields and trove classifiers seen in Python packages.
LICENSE_ALIASES = {
    "mit": "MIT",
    "mit license": "MIT",
    "the mit license": "MIT",
    "bsd": "BSD-3-Clause",
    "bsd license": "BSD-3-Clause",
    "new bsd": "BSD-3-Clause",
    "new bsd license": "BSD-3-Clause",
    "bsd 3-clause": "BSD-3-Clause",
    "bsd-3": "BSD-3-Clause",
    "3-clause bsd": "BSD-3-Clause",
    "bsd 2-clause": "BSD-2-Clause",
    "apache 2.0": "Apache-2.0",
    "apache-2": "Apache-2.0",
    "apache 2": "Apache-2.0",
    "apache license 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "isc license": "ISC",
    "isc license (iscl)": "ISC",
    "python software foundation license": "PSF-2.0",
    "psf": "PSF-2.0",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "mpl 2.0": "MPL-2.0",
    "the unlicense (unlicense)": "Unlicense",
    "zlib/libpng license": "Zlib",
    "public domain": "CC0-1.0",
}


class Repo:
    """What the next commit will contain: the index. During `git commit -a` git hands its hooks
    a temporary index that already holds every tracked change. ``against`` is the commit it is
    compared with: HEAD for a commit, an earlier one (``--base``) for a range in CI, where the
    index is what was checked out."""

    def __init__(self, root: Path, against: str | None = None) -> None:
        self.root = root
        self.against = against

    def git(self, *args: str, stdin: str | None = None) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            input=stdin,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def read(self, path: str) -> str | None:
        try:
            return self.git("show", f":{path}")
        except subprocess.CalledProcessError:
            return None

    def read_head(self, path: str) -> str | None:
        """``path`` as it was in the commit compared with (HEAD, or ``--base``)."""
        try:
            return self.git("show", f"{self.base()}:{path}")
        except subprocess.CalledProcessError:
            return None

    def has_head(self) -> bool:
        try:
            self.git("rev-parse", "--verify", "-q", "HEAD")
        except subprocess.CalledProcessError:
            return False
        return True

    def changed(self) -> set[str]:
        out = self.git("diff", "--cached", "--name-only", "-z", self.base())
        return {p for p in out.split("\0") if p}

    def base(self) -> str:
        if self.against:
            return self.against
        return "HEAD" if self.has_head() else EMPTY_TREE

    def blobs(self, prefix: str) -> dict[str, str]:
        """Path → blob id of every file under `prefix` that the commit will contain."""
        found = {}
        for entry in self.git("ls-files", "-s", "-z", "--", prefix).split("\0"):
            if entry:
                meta, path = entry.split("\t", 1)
                found[path] = meta.split()[1]
        return found

    def hash_objects(self, paths: list[str]) -> list[str]:
        if not paths:
            return []
        return self.git("hash-object", "--stdin-paths", stdin="\n".join(paths) + "\n").split()


def shown(items: list[str], limit: int = 8) -> str:
    more = f" and {len(items) - limit} more" if len(items) > limit else ""
    return ", ".join(items[:limit]) + more


# ---- the built page ----


def parse_sources(text: str | None) -> dict[str, str] | None:
    if text is None:
        return None
    found = {}
    for line in text.splitlines():
        if line.strip() and not line.startswith("#"):
            blob, path = line.split(" ", 1)
            found[path] = blob
    return found


def stamp(repo: Repo) -> Path:
    """Record the page sources on disk (tracked or not, unless ignored) next to the build."""
    out = repo.git("ls-files", "-co", "--exclude-standard", "-z", "--", FRONTEND)
    paths = sorted(p for p in out.split("\0") if p and (repo.root / p).is_file())
    lines = [f"{blob} {path}" for path, blob in zip(paths, repo.hash_objects(paths), strict=True)]
    target = repo.root / SOURCES
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# The page sources this build was made from (scripts/commit_checks.py stamp).\n"
        + "".join(line + "\n" for line in lines)
    )
    return target


def check_page(repo: Repo) -> list[str]:
    if repo.read(f"{FRONTEND}/package.json") is None:
        return []
    problems = []
    recorded = parse_sources(repo.read(SOURCES))
    current = repo.blobs(FRONTEND)
    if recorded is None:
        problems.append(
            f"{SOURCES} is not in the commit: the page in {FRONTEND}/ was never "
            "built, or its build isn't staged."
        )
    elif recorded != current:
        differ = sorted(
            p for p in recorded.keys() | current.keys() if recorded.get(p) != current.get(p)
        )
        problems.append(
            f"The built page doesn't match the page sources being committed ({shown(differ)})."
        )
    loose = repo.git("ls-files", "-o", "--exclude-standard", "-z", "--", BUILT).split("\0")
    loose += repo.git("diff", "--name-only", "-z", "--", BUILT).split("\0")
    loose = sorted({p for p in loose if p})
    if loose:
        problems.append(f"Build output isn't staged: {shown(loose)}.")
    if problems:
        problems.append(
            f"Fix: run `{BUILD_COMMAND}` (its last step runs "
            f"`scripts/commit_checks.py stamp`), then `git add -A {FRONTEND} {BUILT}`. "
            "The built page is always committed with its sources, so running "
            "Wavescribe never needs Node (CONTRIBUTING.md)."
        )
    return problems


# ---- licences ----


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def mentioned(name: str, licensing: str) -> bool:
    text = re.sub(r"[_.]+", "-", licensing.lower())
    pattern = rf"(?<![a-z0-9@/-]){re.escape(norm(name))}(?![a-z0-9-])"
    return re.search(pattern, text) is not None


def requirement_name(spec: str) -> str | None:
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    return match.group(1) if match else None


def python_direct(pyproject: str) -> set[str]:
    data = tomllib.loads(pyproject)
    project = data.get("project", {})
    specs = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        specs += group
    for group in data.get("dependency-groups", {}).values():
        specs += [s for s in group if isinstance(s, str)]
    specs += data.get("build-system", {}).get("requires", [])
    return {n for n in map(requirement_name, specs) if n}


def lock_packages(text: str | None) -> set[str]:
    if not text:
        return set()
    return {p["name"] for p in tomllib.loads(text).get("package", []) if "name" in p}


def npm_direct(package_json: str) -> set[str]:
    data = json.loads(package_json)
    names: set[str] = set()
    for key in ("dependencies", "devDependencies", "optionalDependencies"):
        names |= set(data.get(key, {}))
    return names


def npm_locked(text: str | None) -> dict[str, str | None]:
    """Package name → its licence, from a lockfile v2/v3."""
    if not text:
        return {}
    found = {}
    for path, meta in json.loads(text).get("packages", {}).items():
        if path:
            license_ = meta.get("license")
            found[path.rsplit("node_modules/", 1)[-1]] = (
                license_ if isinstance(license_, str) else None
            )
    return found


def spdx_ok(expression: str | None) -> bool:
    """True when the SPDX expression can be used under tier A (an OR needs one side)."""
    if not expression:
        return False
    tokens = re.findall(r"\(|\)|[A-Za-z0-9.+-]+", expression)
    pos = 0

    def parse_or() -> bool:
        nonlocal pos
        ok = parse_and()
        while pos < len(tokens) and tokens[pos].upper() == "OR":
            pos += 1
            ok = parse_and() or ok
        return ok

    def parse_and() -> bool:
        nonlocal pos
        ok = parse_atom()
        while pos < len(tokens) and tokens[pos].upper() == "AND":
            pos += 1
            ok = parse_atom() and ok
        return ok

    def parse_atom() -> bool:
        nonlocal pos
        if pos >= len(tokens):
            return False
        token = tokens[pos]
        pos += 1
        if token == "(":
            ok = parse_or()
            pos += 1  # ")"
        else:
            ok = token in TIER_A
        if pos < len(tokens) and tokens[pos].upper() == "WITH":
            pos += 2  # the exception keeps the base licence's terms
        return ok

    try:
        return parse_or()
    except IndexError:
        return False


def python_license(root: Path, name: str) -> str | None:
    """The licence of an installed package, from its metadata in .venv (None if unknown)."""
    for dist in root.glob(".venv/lib/python*/site-packages/*.dist-info"):
        if norm(dist.name.removesuffix(".dist-info").rsplit("-", 1)[0]) != norm(name):
            continue
        meta = HeaderParser().parsestr((dist / "METADATA").read_text(errors="replace"))
        if meta.get("License-Expression"):
            return meta["License-Expression"].strip()
        free = (meta.get("License") or "").strip()
        if free and len(free) < 80:
            alias = LICENSE_ALIASES.get(free.lower(), free)
            if spdx_ok(alias):
                return alias
        for classifier in meta.get_all("Classifier") or []:
            if classifier.startswith("License ::"):
                alias = LICENSE_ALIASES.get(classifier.split("::")[-1].strip().lower())
                if alias:
                    return alias
        return free or None
    return None


def check_licences(repo: Repo) -> list[str]:
    changed = repo.changed()
    licensing = repo.read(LICENSING) or ""
    missing: list[str] = []
    risky: list[str] = []

    if "pyproject.toml" in changed and (text := repo.read("pyproject.toml")):
        missing += [
            f"{n} (pyproject.toml)"
            for n in sorted(python_direct(text))
            if n != "wavescribe" and not mentioned(n, licensing)
        ]
    if "uv.lock" in changed:
        new = lock_packages(repo.read("uv.lock")) - lock_packages(repo.read_head("uv.lock"))
        for name in sorted(new - {"wavescribe"}):
            if mentioned(name, licensing):
                continue
            license_ = python_license(repo.root, name)
            if not spdx_ok(license_):
                risky.append(f"{name} (uv.lock, transitive; licence: {license_ or 'unknown'})")
    package_json = f"{FRONTEND}/package.json"
    if package_json in changed and (text := repo.read(package_json)):
        missing += [
            f"{n} ({package_json})" for n in sorted(npm_direct(text)) if not mentioned(n, licensing)
        ]
    lock = f"{FRONTEND}/package-lock.json"
    if lock in changed:
        now = npm_locked(repo.read(lock))
        before = npm_locked(repo.read_head(lock))
        for name in sorted(set(now) - set(before)):
            if not spdx_ok(now[name]) and not mentioned(name, licensing):
                risky.append(f"{name} ({lock}, transitive; licence: {now[name] or 'unknown'})")

    problems = []
    if missing:
        problems.append(
            "No docs/LICENSING.md entry for these dependencies: " + ", ".join(missing) + ". "
            "Check each licence at its source (the package page and the LICENSE file in its "
            "repository), sort it into the tiers in LICENSING.md's Policy, add a row to 'Audit: "
            "libraries and tools' (page packages: 'Page dependencies (npm)') and stage "
            "docs/LICENSING.md. A tier B or C licence can't be a dependency: remove it and tell "
            "the maintainers."
        )
    if risky:
        problems.append(
            "New transitive packages without a tier-A licence: " + ", ".join(risky) + ". "
            "Check each licence at its source; if it is allowed, record it in LICENSING.md "
            "('Transitive Python dependencies' or 'Page dependencies (npm)') with its verdict, "
            "otherwise find another way and tell the maintainers. For an unknown Python licence, "
            "`uv sync --offline` first so its metadata is installed."
        )
    problems += check_models(repo, changed)
    return problems


def check_models(repo: Repo, changed: set[str]) -> list[str]:
    if MODELS not in changed:
        return []

    def entries(text: str | None) -> dict[str, dict]:
        if not text:
            return {}
        return {m["id"]: m for m in tomllib.loads(text).get("model", []) if "id" in m}

    now, before = entries(repo.read(MODELS)), entries(repo.read_head(MODELS))
    keys = ("source", "revision", "sha256", "upstream", "license")
    touched = sorted(
        i
        for i, m in now.items()
        if i not in before or any(m.get(k) != before[i].get(k) for k in keys)
    )
    problems = []
    incomplete = sorted(
        i for i in touched if not all(now[i].get(k) for k in ("license", "license_url", "tier"))
    )
    if incomplete:
        problems.append(
            "models.toml entries without license, license_url or tier: "
            + ", ".join(incomplete)
            + "."
        )
    if touched and LICENSING not in changed:
        problems.append(
            "Models added or re-pinned in models.toml: " + ", ".join(touched) + ". LICENSING.md "
            "says to re-check a model's licence on every version bump: read it upstream (the "
            "model card and its licence at that revision), then add or update the model's row "
            "in 'Audit: models' with the date checked, and stage docs/LICENSING.md."
        )
    return problems


# ---- the README's list ----


def commands_in(source: str) -> list[str]:
    """The top-level `wavescribe` commands a module's source registers."""
    return ADD_COMMAND.findall(source)


def unlisted(commands: list[str], readme: str) -> list[str]:
    """The commands ``readme`` doesn't name as `wavescribe <name>`."""
    return sorted(
        c
        for c in set(commands)
        if not re.search(rf"(?<![\w-])wavescribe\s+{re.escape(c)}(?![\w-])", readme)
    )


def check_readme(repo: Repo) -> list[str]:
    try:
        found = repo.git("grep", "--cached", "-lzF", "sub.add_parser(", "--", f"{PACKAGE}/*.py")
    except subprocess.CalledProcessError as e:
        if e.returncode != 1:
            raise
        return []  # nothing registers a command
    commands = [c for p in found.split("\0") if p for c in commands_in(repo.read(p) or "")]
    missing = unlisted(commands, repo.read(README) or "")
    if not missing:
        return []
    return [
        f"{README} doesn't name these commands: "
        + ", ".join(f"`wavescribe {c}`" for c in missing)
        + ". Every `wavescribe` command has a line in the README's list (What it does): add "
        "one for each, saying in plain words what a person can do with it, with the command "
        "written as `wavescribe <name>` and a link to its how-to in docs/, and stage "
        f"{README} (CONTRIBUTING.md, Conventions)."
    ]


def install(repo: Repo) -> list[str]:
    hooks = repo.root / repo.git("rev-parse", "--git-path", "hooks").strip()
    hooks.mkdir(parents=True, exist_ok=True)
    notes = []
    for name, command in HOOKS.items():
        target = hooks / name
        if target.exists() and HOOK_MARKER not in target.read_text(errors="replace"):
            notes.append(f"kept your own {target}: add `{command}` to it yourself")
            continue
        target.write_text(f"#!/bin/sh\n{HOOK_MARKER}\n{command}\n")
        target.chmod(0o755)
        notes.append(f"installed {target}")
    return notes


# ---- entry points ----


def run_checks(repo: Repo) -> list[str]:
    return check_page(repo) + check_licences(repo) + check_readme(repo)


def report(problems: list[str]) -> str:
    lines = ["Commit blocked by scripts/commit_checks.py. Fix these, then commit again:"]
    lines += [f"{n}. {p}" for n, p in enumerate(problems, 1)]
    return "\n".join(lines)


def repo_at(path: str | Path) -> Repo | None:
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError):
        return None
    return Repo(Path(top))


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "check"
    base = argv[3] if len(argv) == 4 and argv[2] == "--base" and mode == "check" else None
    if len(argv) > 2 and base is None:
        print(__doc__, file=sys.stderr)
        return 1
    repo = repo_at(Path.cwd()) or repo_at(Path(__file__).resolve().parent)
    if repo is None:
        print("not inside a git repository", file=sys.stderr)
        return 1
    if base is not None:
        try:
            repo.against = repo.git("rev-parse", "--verify", "-q", f"{base}^{{commit}}").strip()
        except subprocess.CalledProcessError:
            print(f"--base {base}: no such commit here (fetch it first)", file=sys.stderr)
            return 1
    if mode == "install":
        print("\n".join(install(repo)))
        return 0
    if mode == "stamp":
        print(f"wrote {stamp(repo).relative_to(repo.root)}")
        return 0
    if mode == "check":
        problems = run_checks(repo)
    else:
        print(__doc__, file=sys.stderr)
        return 1
    if problems:
        print(report(problems), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
