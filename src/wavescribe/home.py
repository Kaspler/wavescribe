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
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

ENV = "WAVESCRIBE_HOME"
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
