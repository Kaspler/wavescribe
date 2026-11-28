# SPDX-License-Identifier: Apache-2.0
"""The home: the folder that holds a Wavescribe's settings (``config/``) and what it keeps
(``data/``, and ``models/`` for the models that aren't in the Hugging Face cache).

Every command runs in a home: ``--home DIR``, else ``$WAVESCRIBE_HOME``, else the folder it was
started in. ``cli.main`` settles it once (``enter``), before anything reads or writes a file:
the paths given on the command line are already absolute (``given_path``, the type of every
option that takes one), the paths in the environment are made absolute against the folder the
command was started in, the home is made when it is missing, and the process changes into it.
From then on every relative path (``config/stations.toml``, ``data/wavescribe.db``) is the
home's, and ``WAVESCRIBE_HOME`` names it for the programs Wavescribe starts.

The examples that come with Wavescribe (``example``) are read from the home's ``config/`` first,
then from the package's copy: ``wavescribe/examples/`` in an installed package, ``config/`` in a
Wavescribe folder.

The commands that run stations hold the home's lock (``hold``), ``data/wavescribe.lock``, while
they run: an exclusive ``flock`` that the system lets go of when the process ends, however it
ends, so a crash leaves nothing to clean up. The file says, as JSON, which process holds it,
the home, and the page's address once there is one. A second such command on the same home
stops at once (``HomeInUse``).
"""

from __future__ import annotations

import errno
import json
import os
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

try:
    import fcntl
except ImportError:  # no flock (Windows): commands run without the lock
    fcntl = None  # type: ignore[assignment]

ENV = "WAVESCRIBE_HOME"
LOCK_PATH = Path("data") / "wavescribe.lock"
EXAMPLES = "examples"  # the package's folder of examples, in an installed package
# Paths in the environment that Wavescribe, or a library it runs, reads: Wavescribe's model
# folder, the Hugging Face cache, certificate files, Python's temporary folder.
PATH_VARS = (
    "WAVESCRIBE_MODELS_DIR", "HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE",
    "HF_ASSETS_CACHE", "HUGGINGFACE_ASSETS_CACHE", "HF_XET_CACHE", "HF_TOKEN_PATH",
    "XDG_CACHE_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "TMPDIR",
)  # fmt: skip


class HomeError(ValueError):
    """The home can't be made or entered."""


@dataclass(frozen=True)
class Home:
    path: Path  # absolute and resolved
    source: str  # "flag" (--home), "env" (WAVESCRIBE_HOME) or "folder" (where it was started)

    @property
    def named(self) -> bool:
        """Whether --home or WAVESCRIBE_HOME named it, rather than the folder it started in."""
        return self.source != "folder"


_current: Home | None = None


def given_path(text: str) -> Path:
    """The type of every command-line option that takes a path: absolute against the folder
    the command was started in, since options are read before the change into the home. ``~``
    is the user's home folder. (A default is not passed through it unless it is a string.)"""
    return Path(os.path.abspath(os.path.expanduser(text)))


def choose(flag: Path | None, environ: MutableMapping[str, str], cwd: Path) -> Home:
    """``--home``, else ``$WAVESCRIBE_HOME`` (relative to ``cwd``), else ``cwd``; not yet
    resolved, as the folder may not exist."""
    if flag is not None:
        return Home(cwd / Path(flag).expanduser(), "flag")
    if environ.get(ENV):
        return Home(cwd / Path(environ[ENV]).expanduser(), "env")
    return Home(cwd, "folder")


def absolute_environment(environ: MutableMapping[str, str], cwd: Path) -> list[str]:
    """Makes the relative paths in ``PATH_VARS`` absolute against ``cwd``; one that starts with
    ``~`` or ``$`` is left to the library that expands it. Returns the names changed."""
    changed = []
    for name in PATH_VARS:
        value = environ.get(name)
        if value and not os.path.isabs(value) and not value.startswith(("~", "$")):
            environ[name] = os.path.abspath(cwd / value)
            changed.append(name)
    return changed


def enter(flag: Path | None) -> Home:
    """Settles the home for this process and changes into it (see the module's docstring)."""
    global _current
    cwd = Path.cwd()
    home = choose(flag, os.environ, cwd)
    absolute_environment(os.environ, cwd)
    try:
        home.path.mkdir(parents=True, exist_ok=True)
        os.chdir(home.path)
    except FileExistsError:
        raise HomeError(f"the home {home.path} isn't a folder") from None
    except OSError as e:
        raise HomeError(f"the home {home.path} can't be used: {e.strerror or e}") from None
    _current = Home(Path.cwd().resolve(), home.source)
    os.environ[ENV] = str(_current.path)
    return _current


def current() -> Home:
    """The home this process runs in: the one ``enter`` settled, else the current folder."""
    return _current or Home(Path.cwd().resolve(), "folder")


# -- the examples that come with Wavescribe ------------------------------------------------


def packaged(name: str) -> Path | None:
    """An example as Wavescribe ships it (``name`` relative to ``config/``, such as
    ``stations.example.toml`` or ``keywords``): ``wavescribe/examples/<name>`` in an installed
    package, ``config/<name>`` in the Wavescribe folder the package runs from. None when
    neither has it."""
    from wavescribe.invocation import is_checkout

    package = Path(__file__).resolve().parent
    places = [package / EXAMPLES / name]
    if is_checkout(package):
        places.append(package.parent.parent / "config" / name)
    return next((p for p in places if p.exists()), None)


def example(name: str) -> Path | None:
    """The home's ``config/<name>``, else the copy that comes with Wavescribe, else None."""
    own = Path("config") / name
    return own if own.exists() else packaged(name)


# -- the lock ------------------------------------------------------------------------------


class HomeInUse(RuntimeError):
    """Another long-running command holds the home's lock. ``holder`` is what its lock file
    says (pid, command, home, url), or {} when it says nothing yet."""

    def __init__(self, holder: dict[str, Any], *, installing: bool = False) -> None:
        self.holder = holder
        super().__init__(in_use(holder, installing=installing))


def in_use(holder: dict[str, Any], *, installing: bool = False) -> str:
    """What a long-running command says when ``holder`` has the home."""
    pid, url, command = holder.get("pid"), holder.get("url"), holder.get("command")
    said = []
    if isinstance(pid, int):
        said.append(f"pid {pid}")
    if isinstance(url, str) and url:
        said.append(f"at {url}")
    elif isinstance(command, str) and command and said:
        said.append(command)
    who = f" ({', '.join(said)})" if said else ""
    if installing:
        then = "stop it first, or install with another --home"
    elif isinstance(url, str) and url:
        then = "use its page, stop it first, or give this one another --home"
    else:
        then = "stop it first, or give this one another --home"
    return f"Wavescribe is already running with this home{who}; {then}"


def _read(fd: int) -> dict[str, Any]:
    try:
        data = json.loads(os.pread(fd, 65536, 0) or b"{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class Lock:
    """The home's lock, held: ``note`` adds to what its file says, ``release`` lets it go."""

    def __init__(self, fd: int, path: Path, info: dict[str, Any]) -> None:
        self.fd: int | None = fd
        self.path = path
        self.info = info
        self._write()

    def _write(self) -> None:
        if self.fd is None:
            return
        data = (json.dumps(self.info, ensure_ascii=False) + "\n").encode()
        os.ftruncate(self.fd, 0)
        os.pwrite(self.fd, data, 0)

    def note(self, **fields: Any) -> None:
        self.info.update(fields)
        self._write()

    def release(self) -> None:
        global _held
        if self.fd is None:
            return
        try:
            os.ftruncate(self.fd, 0)  # nobody holds it now: say nothing about anyone
        finally:
            os.close(self.fd)  # closing lets go of the flock
            self.fd = None
            if _held is self:
                _held = None


_held: Lock | None = None


def hold(command: str) -> Lock:
    """Takes the home's lock for ``command`` (``serve``, ``listen``…), or raises HomeInUse."""
    global _held
    path = LOCK_PATH.absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)  # not inherited by the programs it starts
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            holder = _read(fd)
            os.close(fd)
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise HomeInUse(holder) from None
            raise
    info = {"pid": os.getpid(), "command": command, "home": str(current().path)}
    _held = Lock(fd, path, info)
    return _held


def note_page(url: str) -> None:
    """The page's address, written into the lock file of the command that holds it."""
    if _held is not None:
        _held.note(url=url)


def running() -> dict[str, Any] | None:
    """What the lock file of the command that holds the home says ({} when nothing yet), or
    None when no command holds it. It doesn't keep the lock."""
    if fcntl is None:
        return None
    try:
        fd = os.open(LOCK_PATH, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return _read(fd)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


F = TypeVar("F", bound=Callable[..., Any])


def holds(command: str) -> Callable[[F], F]:
    """Marks a command's function as one that runs stations: ``cli.main`` holds the home's
    lock while it runs."""

    def mark(func: F) -> F:
        func.holds_home = command  # type: ignore[attr-defined]
        return func

    return mark
