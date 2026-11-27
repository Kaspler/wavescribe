# Give your AI agent a radio

**Experimental.** The commands, tools and fields on this page may change in any release while
they settle. The JSON they carry is the feed's own (version 1), described in
[FEED.md](FEED.md).

Wavescribe writes down what the radio says. An AI agent can read it too: it can look things up
in what was said, ask questions of it, wait for an alert and act on it, and tell you about it in
your language, in your chat or wherever the agent lives. Wavescribe itself still runs on your
Mac; where the agent's model runs is up to you (see *Privacy* below). These tools are a thin
layer over the Wavescribe that is already running: they load no models and decide nothing
themselves.

## What your agent can do with it

- **Answer questions about the radio.** "Did they say anything about the water this morning?"
  The agent searches everything kept, or asks Wavescribe's own local model, and quotes the
  moments it found, with the station and the time.
- **Keep watch.** The agent waits for the next alert from your rules (a wildfire, a road
  closure, a strike) and does something with it: tells you, checks it against other sources,
  writes a short note in your language. Alerts can be missed or wrong, and so can a digest's
  points, so an agent must never present the absence of alerts, or a digest without points, as
  an all-clear, nor act on one where a mistake causes harm.
- **Summarise.** It reads the last hour of a few stations and tells you what mattered, or has
  Wavescribe's own local model write a digest, with the moments each point rests on. Digests
  are experimental: a point can still run two stories together, add a detail or cite the wrong
  moment, so the agent should check a point against its sources before passing it on.
- **Notice trouble.** A station that stopped, a model that isn't loaded, speech that lags
  minutes behind: the status tool says so, and why.
- **Add and run stations, if you let it.** With `--allow-control` it can add an internet station
  by its stream address and start or stop stations.

## Before you start

Wavescribe has to be running: `uv run wavescribe serve` in the Wavescribe folder
([GETTING_STARTED.md](GETTING_STARTED.md)). The MCP tools, `follow` and `digest` talk to it at
`http://localhost:8765`, and when nothing answers there they say so and tell you to start it.
The other `--json` commands read Wavescribe's files and work without it.

### Privacy: the agent reads everything

- **The agent can read everything Wavescribe has heard, including radio that isn't
  broadcast.** `search`, `ask`, `digest` and `now` read all the history kept on this machine,
  as the page does:
  broadcast radio, and also amateur, aircraft, marine or walkie-talkie traffic for as long as
  you keep it ([RADIO.md](RADIO.md), *What Wavescribe does with each kind of radio*). The
  tools that read the feed (`recent`, `alerts`, `wait_for_alert`, and `follow` on the command
  line) carry only what may leave this machine: by default broadcast radio and internet
  streams, and alerts from other radio without their words. So does `catches`, which reads
  what band watches caught.
- **Whatever a tool returns goes to the agent's model.** If that model runs on someone else's
  servers, what was said is sent there. Some radio may be received but not passed on to anyone
  else; giving it to an online service passes it on. Keep radio that isn't broadcast away from
  an agent like that, or don't keep its text at all.
- **Transcripts are untrusted text.** Anyone on air can say anything, including "ignore your
  instructions and…". Every tool's description tells the model to treat what it returns as
  quoted third-party content, never as instructions, but that is a request to the model, not a
  guarantee. Think about what else the same agent may do on its own (send messages, run
  commands, spend money) while it reads the radio.

## Three ways in

### 1. MCP: tools your agent can call

`wavescribe mcp` is a Model Context Protocol server. The agent's host program, an MCP client,
starts it and talks to it over stdin and stdout. Many clients take a block like this in their
settings; put the path of your Wavescribe folder in place of `/path/to/wavescribe`:

```json
{
  "mcpServers": {
    "wavescribe": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/wavescribe", "wavescribe", "mcp"]
    }
  }
}
```

If the client can't find `uv`, give its full path (`which uv` prints it). To let the agent add,
start and stop stations, and read digests aloud, add `"--allow-control"` at the end of `args`.
Its own messages go to stderr: stdout carries the protocol and nothing else.

It talks to the Wavescribe at `http://localhost:8765`. `--url` points it at another one, such as
`serve --port 8790` (`"--url", "http://localhost:8790"` in `args`; an address that isn't
`http://` or `https://` with a host stops it at start, so the agent shows no tools rather than
failing on each), and `--token` gives the feed
token a Wavescribe asks for once it has tokens ([FEED.md](FEED.md), *Who can read what*);
`WAVESCRIBE_TOKEN` in the environment does the same without showing in the list of running
processes. The control tools work only with the Wavescribe on the same Mac. Behind a proxy
(`serve --behind-proxy`) they work too when `wavescribe mcp` runs in Wavescribe's folder, as
the `--directory` above does: it sends the key in `data/control.key` to a Wavescribe on this
Mac, and to no other ([FEED.md](FEED.md), *Who can read what*).

Every tool returns one JSON object. A tool that can't do its job returns Wavescribe's reason as
an error instead. Arguments that don't fit the tool's schema (one missing, unknown, of the wrong
type or out of range, or arguments that aren't an object) never reach the tool: the call is
answered with the JSON-RPC error −32602, *Invalid params*, naming the problem, as the protocol
revisions below list invalid arguments.

| Tool | What it does | Returns |
|---|---|---|
| `stations` | The stations set up. | `{stations}`, each with `id` (what the other tools take), `name`, `language`, `translate_to`, `kind` (`stream`; `sdr`, a radio receiver; `remote`, another Wavescribe), `running`, `connected` (null while it isn't running), `last_heard` (seconds since 1970; null before anything was heard) and `lag`: null until the station has had something written down since Wavescribe started, then an object for the last ten minutes, with `n` (lines written down) and seconds from the end of speech to its text, `text_p50` (the median) and `text_p95` (95 in 100 were faster), and to its translations, `translated_p50` and `translated_p95`; each null when there was nothing to measure. |
| `recent` | What was said, oldest first: from `minutes` ago (60), or after the last utterance it saw (`after_id`), so the agent can follow along without reading anything twice. | `{utterances, next_after_id, more}`: `limit` utterances at most (100, up to 500); `more` is true when the limit cut them short; `next_after_id` is the `after_id` for the next call (the one given, or null, when nothing came). |
| `search` | Full-text search of everything kept, in the original words and the translations. Each word is found at the start of a word (*flood* finds *floods*); `*word` finds it inside words too. Words in double quotes are a phrase, found in that order with only spaces or punctuation between: `"boil water"` finds *boil water notice*, not *water to boil*. | `{utterances}`, newest first: 20, or `limit` up to 60. |
| `alerts` | Recent alerts, newest first, with their status and what was said; `minutes` keeps those mentioned in the last that many minutes. | `{alerts}`: 10, or `limit` up to 30. |
| `wait_for_alert` | Waits for the next announced alert, `timeout_s` seconds (60, up to 300), optionally only from some `stations` or from a `min_severity` up. Further mentions of an alert already announced don't count. | `{alert}`, or `{alert: null, waited_s}` when none came. The alert's `change` says why it came: `new`, `escalated` (more serious now), `reminder` (still being mentioned, at most once per `remind_minutes`, an hour by default) or `ended`; never `update`. |
| `catches` | What your band watches caught, newest first: each transmission that broke the silence on a channel, with the words heard in it. `hours` back (24; 0 for everything kept), optionally only around `mhz` (with `width_khz` either side) or from some `stations`. Only what may leave this machine: by default broadcast radio, so catches on amateur, walkie-talkie or other radio are left out until you let that kind of radio out ([RADIO.md](RADIO.md)). Your names, notes and tags for frequencies stay on the page. | `{catches}`: 50, or `limit` up to 200, each as `wavescribe radio catches --json` gives it without the notes: `id`, `station` (the watch), `freq_hz`, `mhz`, `t_start` and `t_end` (seconds since 1970), `duration`, `mode`, `band`, `label`, `snr_db`, `peak_db`, `voice` (true when someone spoke, false when nobody did, null when not checked), `reason` (`carrier` when it stayed on for minutes), `privacy` and `utterances` (`id`, `t`, `language`, `text`). |
| `ask` | A question in any language, answered by Wavescribe's local model with the excerpts it cites. It takes from a few seconds to about a minute, and needs a language model as the translator; otherwise it fails with the reason. | `{answer, sources, plan}`: `sources` are the cited utterances, each with the `n` the answer cites it by; `plan` is where it looked (`stations`, `since`, `until`, `terms`, and whether it read an `overview`, `widened` the search or `sampled`). |
| `digest` | Experimental. A few points Wavescribe's local model picked out of what `stations` said over the last `minutes` (60, at most a day), in `language` (by default the one the stations are translated into). Without `stations`, those heard in that time; a watch only when named. It waits behind live translation, so it takes from half a minute to a few minutes. With `--allow-control`, `speak` also reads its points aloud on the Wavescribe machine. | `{text, points, sources, alerts, window, counts, outcome, unread, notice, language}`: `text` is the digest as plain lines; `points` each `{text, stations, sources}`, `sources` being the `n` of the utterances it rests on (three at most); `sources` those utterances, each with its `n`; `alerts` those announced in the window, each once, with `announced_at` (seconds since 1970); `window` is `since` and `until` (seconds since 1970) and `minutes`; `counts` has `said` (lines heard in the window), `read` (those the model read; fewer when it read a sample), `skipped` (songs, idents and repeats left out), `calls` (to the model), `uncited` (points left out for citing nothing it read, or nothing that shares a word with them) and `stations` (lines heard on each); `outcome` is `points`, `nothing_said`, `not_read`, `nothing_stood_out` or `unsourced` (no point cited what the model read, or anything that backs it, so nothing is shown), and `text` then says which; `unread` the stations it couldn't read, and why; `notice` who wrote it from how much, and when Wavescribe started if that was within the window (said in `text` when there is no `notice`). With `speak`, also `speech`: `{started: true}`, or `{started: false, error}`. |
| `now` | What one `station` (by default the one heard last, not a watch) is on about: one or two sentences on its last few `minutes` (5, at most 60), in a few seconds. `language` and `speak` as for `digest`. | As `digest`. |
| `status` | What Wavescribe is doing, and why a station isn't. | `{node, stations, status, ask, mt_model, noncommercial, store}`: `node` is this machine as `/feed/v1/node` gives it ([FEED.md](FEED.md): `name`, `url`, `id`, `version`, `feed_version`, `stations` (how many), `translation_languages`, `store`, `ask`); `stations` every station set up, with `available` and `reason` (why it can't run); `status` the running part (`ready`, `models` (`asr`, `mt`, `loading`, `errors`), `asr_queue` and `mt_queue`, `asr_busy` and `mt_busy` (the share of the last minute each was working), `dropped` and `music_skipped_s` since the start, `rechecks`, and each running station's `connected`, `reconnects`, `last_data_age`, `in_speech`, `music`, `quiet_s`, `level`, `stopping`, when they apply `error`, `retry_in_s`, `signal_db`, `clipped`, `rds`, `squelch_open` and `warning`, and `behind_s` (`text` and `translation`: seconds its oldest speech has waited for text and its oldest line for translation; null when nothing waits) and `dropped` (`asr`, `mt`, `rules`: speech never written down, lines left untranslated and LLM rule checks that couldn't run, over the last 10 minutes), `lag` (each station's, as `stations` gives it), and `load`: `state` `ok`, `behind` or `overloaded`, with a `reason` sentence, null when ok); `ask` is null when ask works, else why not; `mt_model` the translation model; `noncommercial` the restricted models in use; `store` whether history is kept. |
| `start_station`, `stop_station` | Only with `--allow-control`. A start right after a stop waits for the stop to finish, up to 10 s, or fails saying so. Either fails for an id no station has. | `{ok: true}`. |
| `add_station` | Only with `--allow-control`: an internet station by its `http://` or `https://` stream address. A name another station already has (ignoring case) is refused; so is renaming a station to one on the page. | The new station as the page's station list has it: `id` (what the other tools take), `name`, `language`, `kind`, `running`, `available` and `reason` (why it can't run; null when it can), among other fields that are the page's own and may change. |

Utterances and alerts come back in the feed's shapes. To see the protocol at work, send it a
few lines by hand:

```sh
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"sh","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search","arguments":{"query":"água"}}}' \
  | uv run wavescribe mcp
```

```
wavescribe mcp: tools for http://localhost:8765; control off
{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"wavescribe","version":"0.1.0a1"},…}}
{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{\"utterances\":[{\"type\":\"utterance\",\"version\":1,\"id\":2,…,\"station\":{\"id\":\"tsf\",\"name\":\"TSF\",…},…,\"text\":\"Corte de \u00e1gua amanh\u00e3 entre as nove e as treze horas.\",…}]}"}],"isError":false}}
```

Each message is one line of ASCII; letters outside it travel as `\u` escapes, and the client
reads them back as they were.

It speaks the protocol revisions 2024-11-05, 2025-03-26 and 2025-06-18, and offers tools only.

### 2. The command line: `--json` and `follow`

For scripts, and agents that run shell commands. With `--json`, these commands print one JSON
object per line and nothing else on stdout; messages go to stderr, and a failure exits
non-zero:

| Command | Each line |
|---|---|
| `wavescribe search WORDS --json` | an utterance, oldest first; exits 1 when nothing matches. A phrase keeps its double quotes inside the shell's: `wavescribe search '"boil water"' --json` |
| `wavescribe alerts --json` (or `alerts list --json`) | an alert, oldest first |
| `wavescribe models list --json` | a model: `id`, `capability` (`asr`, `translate`, `vad` or `gate`), `license`, `license_url`, `tier` (`default`, `opt-in`, `restricted` or `byo`), `size_gb` (null when unknown), `languages` (BCP-47 tags, or `*` for any), `accepted` (its licence, where it needs accepting) and `installed` |
| `wavescribe radio heard --json` | a frequency the scans found, as kept in `data/radio/heard.json`: `freq_hz`, `mhz`, `band`, `label`, `mode`, `privacy`, `level_db`, `snr_db`, `first_heard`, `last_heard`, `times`, `seen`, `passes`, and when known `name`, `user_name`, `rds` and `missing` (not heard on the last scan of its range); with `device`, the id of the receiver that heard it, in place of the dongle's serial |
| `wavescribe radio catches --json` | a transmission a watch caught and kept, newest first: `id`, `station`, `freq_hz`, `mhz`, `t_start`, `t_end`, `duration`, `mode`, `band`, `label`, `snr_db`, `peak_db`, `voice` (null when not checked), `reason`, `privacy`, `utterances` (`id`, `t`, `language`, `text`), and the `name`, `note` and `tags` noted for its frequency |
| `wavescribe doctor --json` | a check (`type` `check`: `group`, `check`, `status` `ok`, `warn`, `fail` or `skip`, `summary`, `fix` (null when there is nothing to do), `details`), then one with `type` `summary`: `status`, `problems`, `warnings`, `version`, `folder`, `options` and `from_service` (whether those are the background service's options); exits 1 when something is broken |
| `wavescribe service status --json` | one line: `label`, `plist`, `installed`, `loaded`, `running`, `pid`, `state`, `runs`, `last_exit`, `last_signal`, `workdir`, `serve_args`, `keep_awake`, `log`, `log_bytes`, `full_log`; exits 3 when it isn't running |
| `wavescribe follow` | a live utterance, with `resume` (where to go on after it; see *Stopping and going on*), or alert, until you stop it |
| `wavescribe digest --json` | one line: what the `digest` tool returns (with `--now`, the `now` tool); it asks the running Wavescribe, like `follow` |

`search` and `alerts` print utterances and alerts in the shapes of [FEED.md](FEED.md), without
`audio_url` and `cap_url`: a command doesn't know the address the page is reached at, so
`node.url` is null, and `node.id` is there once Wavescribe has started in that folder. They read
this machine's store, so they carry everything kept, as the `search` tool does; an alert's
`context` holds up to two utterances stored just before its first mention, on its station. Each
`station` is named as `serve` names it: from the stations file, `--config FILE` as `serve`
takes it (by default the one `serve` reads), with the page's changes. A station that file
doesn't have, because none is found in the current folder or the station has gone from it, is
named as Wavescribe last started it, with the `privacy` class its newest line was heard under;
one the store has no name for, by its id, with `language` null and no `privacy`. A `--config`
file that can't be read fails, saying why. `digest --json` asks the running Wavescribe, so its
utterances and alerts carry those links when that Wavescribe knows its own address. Its
`--now`, `--minutes`, `--since`, `--stations`, `--language` and `--speak` are as in
`wavescribe digest --help`. `follow` prints the feed's events as they come, each utterance with
one field more, `resume`.

```sh
$ uv run wavescribe search água --json | jq -r '.time + "  " + .station.name + "  " + .translations.en'
2026-11-26T20:10:58Z  TSF  Water cut tomorrow between nine and one o'clock.
```

`follow` prints the live feed (`/feed/v1/events`). Like `tail -f`, it runs until you stop it or
the program reading it goes away. A lost connection is tried again, waiting a little longer
each time, and it resumes where the feed left off: lines that finished meanwhile come then,
including one still being translated when the connection dropped, and each utterance is
printed once.

| Flag | |
|---|---|
| `--stations IDS` | only these stations, comma-separated |
| `--langs LANGS` | only these translations, comma-separated |
| `--alerts-only` | alerts, no utterances |
| `--since-id ID`, `--since EPOCH` | first, what finished after that resume point, or was heard since that time (up to 1,000), then live. `ID` is a `resume` as `follow` gives it (`195,197`), or an utterance's `id`, which skips a line that was still being translated then |
| `--resume FILE` | keep the resume point in `FILE`, and start from it: see *Stopping and going on* |
| `--url URL` | another Wavescribe (default `http://localhost:8765`): an `http://` or `https://` address with a host; anything else is refused at once with exit 2, by `follow`, `digest` and `mcp` alike |
| `--token TOKEN` | a feed token, which a Wavescribe on another machine asks for once it has one ([FEED.md](FEED.md), *Who can read what*); `WAVESCRIBE_TOKEN` in the environment does the same and doesn't show in the list of running processes |

**Stopping and going on.** Lines don't always finish in order: one still being translated
can come after later ones. So the `id` of the last line printed is no place to go on from:
`--since-id 197` takes everything up to 197 as printed, and a line 196 that finished after it
never comes. The feed's resume point is the place ([FEED.md](FEED.md), *Resuming*), and `follow`
gives it three ways:

- **On each utterance**, as `resume`: the point to go on from after that line, as text
  (`"195,197"`; null when the feed gives none, as a Wavescribe that keeps no history doesn't).
  A program that reads the lines keeps the `resume` of the last one it has handled, and starts
  `follow` again with `--since-id` and that value.
- **In a file**, with `--resume FILE`: written after each line printed, and replaced whole, so
  it never holds part of one, even when `follow` is killed. `follow` starts from it the next
  time; `--since-id` and `--since` count only while the file holds none, so the same command
  line serves the first start and every restart. A file that holds something else is refused,
  as is one that can't be written, before anything is printed.
- **When you stop it** (Ctrl-C, `kill`, or a refusal such as a revoked token), on stderr:

  ```
  wavescribe: run follow again with --since-id 195,197 to go on after the last line printed
  ```

Going on from a resume point prints every line that finished meanwhile, and those still being
translated when they finish. A line can come twice, such as the last one before `follow` was
killed: skip an utterance whose `id` you already have. Give the same `--stations`: the point
counts only the stations followed. The file says what `follow` printed, not what the program
reading it handled: one that can stop with lines it hadn't read yet should keep `resume`
itself. Alerts have no resume point and aren't printed again; `wavescribe alerts --json` lists
those raised meanwhile.

```sh
uv run wavescribe follow --resume follow.state --since "$(date -v-1H +%s)" >> radio.jsonl
```

`follow` prints every alert event the feed sends, not only announcements: an alert comes when
it opens, again at each further mention (with `mentions` grown), translation or delivery
outcome, and when it is announced. `change` says what each one is: `new` when it is announced,
`escalated` when it is announced again because it got more serious, `reminder` when it is still
being mentioned (at most once per `remind_minutes`, an hour by default), `ended` when a station
alert is over, and `update` for everything else. Its `status` is `pending` while it is checked
or translated; `delivered`, `partial`, `failed` or `logged` once it is announced; `covered` when
another alert announced it. The MCP tool `wait_for_alert` returns only announcements (`new`,
`escalated`, `reminder` and `ended`), never an `update` nor an alert `pending` or `covered`. To
hand each announced alert to another program the same way, as JSON on its stdin:

```sh
uv run wavescribe follow --alerts-only | jq --unbuffered -c 'select(.change != "update" and .status != "covered")' | while read -r alert; do printf '%s\n' "$alert" | ./notify; done
```

Here `./notify` stands for your program. With one that prints a line per alert:

```
high water on Antena 1: Previsão de chuva forte para a tarde.
```

With nothing running, `follow` says so on one line and exits 1:

```
wavescribe: no Wavescribe answers at http://localhost:8765 (Connection refused); start it with `wavescribe serve`
```

### 3. HTTP: the feed and webhooks

Anything that speaks HTTP can read the feed, and Wavescribe can call your webhook on each
alert. Both are in [FEED.md](FEED.md). `follow` and the MCP tools use the same routes.

## What an agent can't do, and why

- **Decisions that are yours.** No tool, in any mode, confirms that a kind of radio may be
  received, accepts a model's licence, deletes history, makes tokens or changes what is kept.
  They have legal or lasting consequences, so they stay on the page with you. When Wavescribe
  refuses the agent something, such as starting a station that is switched off on the page or
  needs a model that isn't downloaded, the agent gets Wavescribe's reason and can tell you.
- **Control from elsewhere.** The control tools use the page's control routes, which work
  only from this machine. Point `wavescribe mcp` at the Wavescribe on the same Mac; `follow
  --url` can read another machine's feed.
- **Audio.** The tools carry text and metadata only.
- **Receivers.** Tuning and scanning a radio receiver, and starting a band watch, have no
  tools; `catches` only reads what a watch you started caught.

## Security notes

- Read tools change nothing. The control tools are offered only when `wavescribe mcp` is
  started with `--allow-control`.
- Requests go straight to Wavescribe and never through a proxy set for the Mac, so they are
  treated as this machine's own. Behind a proxy that takes the control key, which is sent to
  `localhost`, `127.0.0.1` or `::1` only.
- Control requests carry the same header the page sends, which a form on another website
  can't send.

## What's next

These tools are a first, experimental step; a fuller API is planned.
