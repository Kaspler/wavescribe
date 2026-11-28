# Contributing to Wavescribe

Wavescribe is an early alpha and the design is still moving: open an issue before starting large
work. Before changing anything substantial, read
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (how it works, and the decisions already made) and
[docs/LICENSING.md](docs/LICENSING.md) (**check it before adding any dependency or model**).
[docs/RADIO.md](docs/RADIO.md), [docs/MODELS.md](docs/MODELS.md) and [docs/FEED.md](docs/FEED.md)
cover their own areas.

## Getting started

```sh
brew install ffmpeg uv node
uv python install 3.12                      # newer Pythons lack wheels for some ML dependencies
uv sync                                     # includes the dev tools (pytest, ruff)
npm ci --prefix frontend                    # only to change the page
python3 scripts/commit_checks.py install    # the commit checks, as git hooks
uv run pytest -m "not models"               # runs without downloaded models
uv run wavescribe models fetch              # about 8 GB; then `uv run pytest` runs everything
uv run ruff check . && uv run ruff format --check .
```

No radio hardware is needed: internet streams go through the same `AudioSource` interface as
radio, and the radio code is tested on synthetic signals.
A stand-in `rtl_sdr` (`tests/fake_rtl_sdr.py`) plays a made-up sky
(`tests/fake_world.py`: transmitters at fixed frequencies, and a dongle's edges, centre spike and
clock error if asked), and `uv run python tests/page_fixture.py --port 8771` serves the page with
three made-up receivers, from a scratch directory.

## Ground rules

1. **Offline at runtime.** No cloud APIs and no telemetry. The network is used only by explicit
   setup actions that download models and assets.
2. **Licences are checked before use.** Every direct dependency, tool and model gets a
   [docs/LICENSING.md](docs/LICENSING.md) entry first, following its tiers; a transitive package
   needs one when its licence isn't in tier A. Permissive code can be imported; LGPL libraries
   only dynamically linked, unmodified; GPL tools run only as separate processes. We never
   commit, mirror, convert-and-upload or bundle model weights: users download them from the
   rights holder. Every dependency must be installable offline from a cache. Say which licence
   it has in the pull request.
3. **Nothing private or copyrighted in the repo:** no secrets, personal data, model weights or
   recorded broadcast audio. Test fixtures are synthetic (e.g. TTS-generated) or clearly licensed.
4. **Language-agnostic.** Languages are BCP-47 codes in config. The speech and translation
   models, the keyword pack and the spoken-alert voice are chosen per language from config and
   the model registry, so adding a language takes configuration and a model.
5. **Alerts use source-language text.** Translation is for people and never sits on the alert
   path.
6. **Receive only, on the receivers.** No code transmits with a receiver, and nothing decrypts
   protected services. Alerts leave only through outputs the user sets up (a webhook, ntfy, or a
   mesh node the user owns, which sends them).
7. **Runs 24/7, possibly on battery.** Memory and disk stay bounded, it recovers from SDR or
   stream dropouts, and power draw stays modest. Measure, don't guess.
8. **Platform-specific code sits behind interfaces** (MLX, `say`, notifications), with a portable
   fallback where one is reasonable.

## Code map

`src/wavescribe/` has one module per interface implementation, wired together by `pipeline.py`:

- **Sources:** `sources/stream.py` (internet streams through ffmpeg), `sources/sdr.py` (an
  `rtl_sdr` pipe) with our numpy/scipy demodulators: `dsp/wfm.py` (broadcast FM),
  `dsp/channel.py` (NFM and AM with squelch), `dsp/rds.py` (RDS); `sources/watch.py` (a
  watch: every channel of a dongle's window at once, each transmission a catch of its own, with
  `dsp/activity.py` noticing them)
- **Radio:** `radio/` (band plans in `bands.toml` and `bandplan.py` with the privacy classes,
  `scan.py` sweeps with `rtl_power`, `catalog.py` what was heard here, `service.py` the page's
  scans and heard stations, `live.py` the receiver tuned by hand under Listen, `stills.py` the
  dial's pictures cut from past scans, `watch.py` the watch as a station and what it keeps,
  `holders.py` which other program holds a dongle,
  from macOS's USB registry, `words.py` how messages name receivers, `cli.py` `wavescribe
  radio` and `watch_cli.py` its `watch`, `catches` and `note`); see
  [docs/RADIO.md](docs/RADIO.md)
- **Gate and segmenter:** `segment/vad.py` (Silero VAD), `gate/yamnet.py` (drops music)
- **Speech recognition:** `asr/parakeet.py`, `asr/mlx_audio.py`, `asr/checks.py` (the
  hallucination filter), `asr/recheck.py` (Whisper's second opinion on a line heard in another
  language) and `asr/language.py` (language `auto`)
- **Translation:** `translate/llm.py`, `translate/madlad.py`
- **Rules and alerts:** `rules/` (keyword and LLM rules), `alerts/` (notifications, spoken
  alerts, CAP 1.2 in `cap.py`, `watch.py` for a silent station and traffic announcements,
  `replay.py` behind `alerts replay`)
- **Storage, search and questions:** `store.py` (SQLite with FTS5), `search.py` (how a query
  matches), `ask.py`
- **Digests:** `digest/` (the points the local model picks out of what was said;
  `schedule.py` the `[digest]` schedule, `speech.py` reading one aloud without holding an alert
  back, `cli.py` how a terminal shows one)
- **Outputs and federation:** `outputs.py` (webhooks and pushes), `events.py` (the shared JSON
  shape), `remote.py` (follows another Wavescribe's feed)
- **Other programs and agents:** `agent/` (`client.py` a running Wavescribe from another
  program's side and `wavescribe follow`, `mcp.py` the MCP server, `cli.py` the `follow`,
  `digest` and `mcp` commands), `token_cli.py` (`wavescribe token`) with `control.py` (the key
  a command on this machine reaches the page's controls with); see
  [docs/AGENT_TOOLS.md](docs/AGENT_TOOLS.md)
- **The web page:** `frontend/` holds the page's sources (`src/reading/` the reading view,
  `src/manage/` Listen, Find and Settings, `src/components/` and `src/styles/` the shared
  components and the design tokens in `src/styles/tokens.css`), built into `web/dist/`. `web/`
  holds the server (`server.py`, `feed.py` for `/feed/v1`, `api.py` for the page's controls);
  `app.py` runs a session and applies page changes, which `settings.py` keeps in
  `config/ui.toml`
- **The home:** `home.py` (the folder with `config/` and `data/`: `--home`, `WAVESCRIBE_HOME`
  or the folder a command starts in, which `cli.main` changes into first; the examples that
  come with Wavescribe). A command-line option that takes a
  path uses `home.given_path` as its type, so it counts from the folder it was typed in
- **Models and system:** `models.toml` (the model registry, with pinned revisions and licences)
  read by `registry.py`; `catalog/` (station and model catalogues), `admin.py`, `downloads.py`,
  `system.py` (the resource monitor), `doctor.py` (`wavescribe doctor`, the offline self-check),
  `service.py` (`wavescribe service`, the background service on macOS) and `bakeoff.py` (speech
  models compared on recorded clips)
- **The commands:** `cli.py` builds every command; [docs/COMMANDS.md](docs/COMMANDS.md) lists
  them with their options, and `tests/test_docs_commands.py` fails when a command or an option
  is missing from it. A message that tells someone what to run builds the command with
  `invocation.typed()`: `uv run wavescribe …` in a Wavescribe folder, `wavescribe …` where it
  is installed as a package; `tests/test_invocation.py` fails on a hint that spells it out

## Conventions

- `src/` layout, `pyproject.toml` managed by uv, CLI entry point `wavescribe`.
- `ruff` for lint and format, `pytest`, type hints on public functions. Prefer small modules.
- Config is TOML. `config/*.example.toml` is tracked; your real `config/*.toml` is gitignored.
- Runtime data goes in `data/` (gitignored); models go in `models/` or the tool's own cache.
- Structured logging, with the timing of every pipeline stage: that's how the latency and
  capacity targets are proven.
- Small commits with descriptive messages. When you change an interface or a decision, update
  the matching doc in the same commit. Decisions live in ARCHITECTURE.md;
  revisit one with evidence, and record the change there.
- A change that adds something a person can do adds its line to the README's list
  ([What it does](README.md#what-it-does)) and its how-to to the docs.
- **The page** is React + TypeScript + Vite. Sources live in `frontend/`; `npm --prefix frontend
  run build` writes `src/wavescribe/web/dist/`. The built page is always committed with its
  sources, so running Wavescribe never needs Node. The build works offline from
  `package-lock.json` and bundles every asset (no CDN, no web fonts). `npm --prefix frontend
  test` runs the page's own tests with Node's built-in test runner, so they need no test
  packages (after `npm ci --prefix frontend`, for the modules they import).

## Commit checks

`scripts/commit_checks.py` runs as a `pre-commit` hook once you've run
`python3 scripts/commit_checks.py install`. It blocks a commit when:

- the built page doesn't match the page sources being committed;
- a new dependency has no LICENSING.md entry, or a model is added or re-pinned without
  LICENSING.md updated in the same commit;
- a `wavescribe` command isn't named in README.md, written as `wavescribe <name>`: every
  command has its line in the README's list. It is a tripwire for commands only; what the page
  can do has no such check, so add its line by hand.

Checks of your own can go in `.git/hooks/pre-commit.local`: the hook runs it afterwards when it
exists and is executable. Do what a blocked commit says and commit again; don't bypass the hook.

Every pull request runs the same checks on GitHub: ruff, the tests that need no models, the
page's own tests, a fresh build of the page compared with the committed one, the README's
commands, and the licence check over every commit it brings
(`commit_checks.py check --base <rev>`).

## License

By contributing you agree that your contributions are licensed under [Apache-2.0](LICENSE).
