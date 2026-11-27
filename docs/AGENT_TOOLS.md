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
  writes a short note in your language. Alerts can be missed or wrong, so an agent must never
  present the absence of alerts as an all-clear, nor act on one where a mistake causes harm.
- **Summarise.** It reads the last hour of a few stations and tells you what mattered.
- **Notice trouble.** A station that stopped, a model that isn't loaded, speech that lags
  minutes behind: the status tool says so, and why.
- **Add and run stations, if you let it.** With `--allow-control` it can add an internet station
  by its stream address and start or stop stations.

## Before you start

Wavescribe has to be running: `uv run wavescribe serve` in the Wavescribe folder
([GETTING_STARTED.md](GETTING_STARTED.md)). The MCP tools and `follow` talk to it at
`http://localhost:8765`, and when nothing answers there they say so and tell you to start it.
The `--json` commands read Wavescribe's files and work without it.

### Privacy: the agent reads everything

- **The agent can read everything Wavescribe has heard, including radio that isn't
  broadcast.** `search` and `ask` read all the history kept on this machine, as the page does:
  broadcast radio, and also amateur, aircraft, marine or walkie-talkie traffic for as long as
  you keep it ([RADIO.md](RADIO.md), *What Wavescribe does with each kind of radio*). The
  tools that read the feed (`recent`, `alerts`, `wait_for_alert`, and `follow` on the command
  line) carry only what may leave this machine: by default broadcast radio and internet
  streams, and alerts from other radio without their words.
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
start and stop stations, add `"--allow-control"` at the end of `args`. Its own messages go to
stderr: stdout carries the protocol and nothing else.

| Tool | What it does |
|---|---|
| `stations` | The stations set up: id, name, language, kind (stream, receiver, another Wavescribe), running, connected, last heard, lag. |
| `recent` | What was said, oldest first: from some minutes ago, or after the last utterance it saw (`after_id`), so the agent can follow along without reading anything twice. |
| `search` | Full-text search of everything kept, in the original words and the translations, newest first (60 at most). Each word is found at the start of a word (*flood* finds *floods*); `*word` finds it inside words too. |
| `alerts` | Recent alerts, newest first, with their status and what was said. |
| `wait_for_alert` | Waits up to 300 s for the next announced alert, optionally only from some stations or above a severity. |
| `ask` | A question in any language, answered by Wavescribe's local model with the excerpts it cites. It needs a language model as the translator; otherwise it fails with the reason. |
| `status` | Models loaded, stations running and why not, queue depths, whether `ask` works, version. |
| `start_station`, `stop_station` | Only with `--allow-control`. |
| `add_station` | Only with `--allow-control`: an internet station by its `http://` or `https://` stream address. |

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
| `wavescribe search WORDS --json` | an utterance, oldest first; exits 1 when nothing matches |
| `wavescribe alerts --json` (or `alerts list --json`) | an alert, oldest first |
| `wavescribe models list --json` | a model: id, capability, licence, tier, size, languages, accepted, installed |
| `wavescribe radio heard --json` | a frequency the scans found, as kept in `data/radio/heard.json`, with the receiver that heard it as `device` (its id) in place of the dongle's serial |
| `wavescribe doctor --json` | a check (`"type": "check"`: group, check, status `ok`, `warn`, `fail` or `skip`, summary, fix, details), then one `"type": "summary"`; exits 1 when something is broken |
| `wavescribe service status --json` | one line: the background service, installed, loaded, running, pid, last exit, options and logs; exits 3 when it isn't running |
| `wavescribe follow` | a live utterance or alert, until you stop it |

`search` and `alerts` print utterances and alerts in the shapes of [FEED.md](FEED.md), without
`audio_url` and `cap_url`: a command doesn't know the address the page is reached at. `follow`
prints the feed's events as they come.

```sh
$ uv run wavescribe search água --json | jq -r '.time + "  " + .station.name + "  " + .translations.en'
2026-11-26T20:10:58Z  TSF  Water cut tomorrow between nine and one o'clock.
```

`follow` prints the live feed (`/feed/v1/events`). Like `tail -f`, it runs until you stop it or
the program reading it goes away. A lost connection is tried again, waiting a little longer
each time, and it resumes after the last utterance it printed.

| Flag | |
|---|---|
| `--stations IDS` | only these stations, comma-separated |
| `--langs LANGS` | only these translations, comma-separated |
| `--alerts-only` | alerts, no utterances |
| `--since-id ID`, `--since EPOCH` | first, what was stored after that utterance, or heard since that time (up to 1,000), then live |
| `--url URL` | another Wavescribe (default `http://localhost:8765`) |
| `--token TOKEN` | a feed token, which a Wavescribe on another machine asks for once it has one ([FEED.md](FEED.md), *Who may read*); `WAVESCRIBE_TOKEN` in the environment does the same and doesn't show in the list of running processes |

An alert is sent again each time its status changes: `pending` while it is checked or
translated; `delivered`, `partial`, `failed` or `logged` once it is announced; `covered` when
another alert announced it. To hand each announced alert to another program, as JSON on its
stdin:

```sh
uv run wavescribe follow --alerts-only | jq --unbuffered -c 'select(.status != "pending" and .status != "covered")' | while read -r alert; do printf '%s\n' "$alert" | ./notify; done
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
- **Receivers.** Tuning and scanning a radio receiver have no tools.

## Security notes

- Read tools change nothing. The control tools are offered only when `wavescribe mcp` is
  started with `--allow-control`.
- Requests go straight to Wavescribe and never through a proxy set for the Mac, so they are
  treated as this machine's own.
- Control requests carry the same header the page sends, which a form on another website
  can't send.

## What's next

These tools are a first, experimental step; a fuller API is planned.
