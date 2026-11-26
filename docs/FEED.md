# Wavescribe as a component: the feed, outputs and remote stations

Wavescribe works fully on one machine with nothing else. It can also be part of something
bigger. Another program can read what it hears (the **feed**), it can push what it hears to
other programs (**outputs**: webhooks and live pushes), and one Wavescribe can follow the
stations of others (**remote stations**). That last one lets a central server show and alert
on many places' local radio.

The feed, outputs and remote stations carry text and metadata, never audio: an `audio_url`
points back at the machine that kept the recording. With `serve --lan`, devices on your network
can play that audio on the page, as they can read the text.

## Event format (version 1)

The pull feed and the pushes carry the same JSON objects.

```json
{
  "type": "utterance", "version": 1, "id": 4182,
  "node": {"name": "kitchen-mac", "url": "http://kitchen-mac.local:8765"},
  "station": {"id": "tsf", "name": "TSF", "language": "pt-PT", "privacy": "broadcast"},
  "time": "2026-11-26T14:03:12Z", "t_start": 1795701792.1, "duration": 6.2,
  "language": "pt-PT",
  "text": "A circulação na ponte está cortada nos dois sentidos.",
  "translations": {"en": "Traffic on the bridge is closed in both directions."},
  "flags": [], "rules": ["roads"],
  "audio_url": "http://kitchen-mac.local:8765/api/segments/4182/audio.wav"
}
```

Utterance fields:

- `id`: the segment id on the machine that heard it. It grows in arrival order, and resuming
  uses it.
- `text`: what the speech recogniser wrote, in the station's language.
- `translations`: the languages that machine translates this station into.
- `flags`: notes from the ASR checks. For example, `lang:es` means it sounded like Spanish, and
  `repetition` that a phrase was repeated four or more times inside a longer message.
- `rules`: the alert rules this utterance tripped.
- `audio_url`: appears only when the recording is kept and the machine's address is known.
  The page sets that address under Settings → Connections → *For other programs* (*Public URL*).
- `station.privacy`: the station's privacy class on the machine that heard it (`broadcast`,
  `amateur`, `utility`, `private`, `digital`; see [RADIO.md](RADIO.md)). A machine that takes
  the text in keeps it, quotes it in alerts and passes it on by its own rules for that class.
  The class is the sender's statement: a sender that leaves it out is taken to send broadcast,
  so a program that pushes radio that isn't broadcast must say so. A class the receiver doesn't
  know is handled as `private`. Once a sender states a stricter class, the station is handled
  by it from then on.

```json
{
  "type": "alert", "version": 1, "id": 77, "node": {"name": "kitchen-mac", "url": "…"},
  "rule": "water", "kind": "keyword", "severity": "high", "status": "delivered",
  "time": "2026-11-26T14:05:40Z", "first_at": 1795701740.0, "last_at": 1795701940.2,
  "mentions": 3, "terms": ["corte de água"], "reason": null,
  "station": {"id": "antena1", "name": "Antena 1", "language": "pt-PT", "privacy": "broadcast"},
  "stations": [{"id": "antena1", "name": "Antena 1", "language": "pt-PT", "privacy": "broadcast"}],
  "language": "pt-PT", "text": "…what was said…", "translations": {"en": "…"},
  "context": [{"id": 4180, "time": "…", "station": "antena1", "text": "…", "translations": {}}],
  "segment_id": 4181,
  "audio_url": "…/api/segments/4181/audio.wav", "cap_url": "…/api/alerts/77.cap"
}
```

Alert fields:

- `kind`: `keyword` or `llm` (a rule written in plain language and checked by the LLM).
- `reason`: the LLM's one-line reason, when it was involved.
- `text`: what was announced, in the source language.
- `translations`: only translations checked to be in their target language.
- `context`: the utterances just before it on the same station.
- `cap_url`: the same alert as a CAP 1.2 document.

A batch, used by pushes that batch utterances, is
`{"type": "batch", "version": 1, "node": {…}, "events": [ … ]}`.

## Pull: `/feed/v1`

| Request | What it returns |
|---|---|
| `GET /feed/v1/events` | Server-Sent Events: `utterance` and `alert` by default. Filters: `?events=utterance,alert,status`, `?stations=a,b`, `?langs=en`. |
| `GET /feed/v1/utterances` | What was said: `?since=<epoch>&until=<epoch>` or `?since_id=<id>`, `?stations=`, `?langs=`, `?limit=` (500 by default, up to 10,000; counts only what this machine may give), `?format=json\|ndjson\|csv`. |
| `GET /feed/v1/alerts` | Recent alerts (`?since=<epoch>`). |
| `GET /feed/v1/stations` | Stations: language, translation languages, running, connected, last heard, lag. |
| `GET /feed/v1/node` | This machine: name, URL, version, feed version, translation languages. |
| `GET /healthz` | `{"ok": true}` once models are loaded (503 before). It needs no token. |
| `GET /metrics` | Prometheus text: stations running and connected, utterances, queues, drops, lag, outputs, memory footprint, free disk, power draw. |

**Resuming.** Each `utterance` event carries `id:` with its segment id. EventSource sends the
last one back as `Last-Event-ID` when it reconnects, and the feed replays what was stored since
(up to 1,000 utterances), then goes live. After a restart, a client that kept no id can ask for
`?since=<epoch>`. Pass `?events=status` to get a status event every 10 s.

```sh
curl -N http://kitchen-mac.local:8765/feed/v1/events?stations=tsf
curl "http://kitchen-mac.local:8765/feed/v1/utterances?since=$(date -v-1H +%s)&format=csv" > hour.csv
```

In CSV, a cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading
apostrophe, so a spreadsheet doesn't run it as a formula. A parameter that should be a number
and isn't gets a 400.

## Who may read

- **From the machine itself:** reading needs no token. Pushing to `/feed/v1/ingest` always needs
  a `push` token, as JSON (`Content-Type: application/json`): otherwise any web page open on the
  machine could post events and set off alerts.
- **By the machine's name only:** every request must name the machine in its `Host` header
  (`localhost`, `127.0.0.1`, `[::1]`, a `.localhost` name, the machine's own name or an
  address); from other machines a `.local` name and the public URL's host count too. Anything
  else gets 421, so a website whose name is made to resolve to 127.0.0.1
  (DNS rebinding) can't reach the page's controls.
- **From other machines:** only when Wavescribe was started with `serve --lan`, which binds to
  the network. Until the first feed token is made, the LAN reads the feed as it reads the page.
  Once one exists, every feed request from another machine needs a token, sent as
  `Authorization: Bearer <token>` or `?token=<token>` (browsers' EventSource can't set
  headers). Prefer the header: a token in a URL ends up in logs, here and on any proxy.
- **Tokens** are made on the page (*Settings → Connections → For other programs → Feed
  tokens*) with a scope:
  - `read`: the feed;
  - `push`: `/feed/v1/ingest`.

  A token is shown once and only its SHA-256 hash is kept, in `config/ui.toml` (mode 0600).
- **The page's controls (`/api/manage/…`: Settings, Find, tuning) never work from another machine**, including requests
  that come through a proxy (see below).
- **Over the internet:** put Wavescribe behind a reverse proxy with TLS, and start it with
  `serve --behind-proxy`: a proxy on the same machine would otherwise make every visitor look
  like this machine. With that option the feed needs a token even before the first one is
  made. Set the proxy's public name as the *public URL* (page, *Outputs*) first: requests that
  name the machine any other way get 421. Tokens protect `/feed/v1` only; the page's own read-only endpoints are as open as
  `--lan` makes them. On both, radio whose class keeps its text on this machine (by default
  everything but broadcast and streams) is invisible to other machines, and its alerts reach them without words.

## Push: outputs

An output is a URL plus filters, set on the page (*Settings → Connections*) and saved in `config/ui.toml`:

| Field | Meaning |
|---|---|
| `events` | `alert`, `utterance`, or both |
| `rules`, `min_severity`, `stations` | which alerts go out (all rules, medium and up, all stations, by default) |
| `languages` | translations to include (all, by default) |
| `format` | `json` (the events above), `cap` (CAP 1.2 XML, alerts only), `text` (a readable line or two, for chat bots and ntfy) |
| `batch_s` | utterances: 0 sends each one; more sends one batch per window. Alerts always go at once |
| `headers` | extra headers, e.g. `Authorization` for the receiving end |
| `secret` | signs each request, see below |
| `context` | how many utterances before an alert's to include (2) |

**Delivery:**

- Alerts get six tries over about two minutes on network errors, 408, 425, 429 and 5xx.
  Utterances get three.
- Each output has its own queue (2,000 events; the oldest utterances go first when it is
  full), so a dead endpoint never slows anything else.
- Every request is logged, with its status, attempts and time. The page shows the log; it is
  also in the store's `deliveries` table.
- The page's *Test* button sends one sample event.

**Signatures.** With a secret, each request carries `X-Wavescribe-Timestamp` (Unix seconds)
and `X-Wavescribe-Signature: sha256=<hex>`, the HMAC-SHA256 of `"<timestamp>.<body>"` keyed
with the secret. To verify:

```python
import hashlib, hmac

expected = "sha256=" + hmac.new(secret, ts.encode() + b"." + body, hashlib.sha256).hexdigest()
ok = hmac.compare_digest(expected, request.headers["X-Wavescribe-Signature"])
```

Reject timestamps older than a few minutes, to stop replays.

## Many places, one central server

Each place runs Wavescribe on its own machine with its own antenna; one machine collects the
text. There are two ways to connect them.

**The central server pulls.** Use this when it can reach the places, on a LAN or a VPN. Add
a *remote station* on the central server, on the page under *Find → On the internet → Other
ways to add → From another Wavescribe*, or in its `stations.toml`:

```toml
[[station]]
id = "north-tsf"
name = "TSF (north node)"
language = "pt-PT"
translate_to = ["en", "es"]
remote = "http://north-node.local:8765"
remote_station = "tsf"
remote_token = "wst_…"      # if that node has feed tokens
```

- The central server subscribes to the node's feed and catches up on the last hour the first
  time.
- After that it resumes after the last utterance it stored, going back a week at most.
- On a lost connection it reconnects, backing off from 2 s to 60 s.

**The places push.** Use this when they sit behind NAT. On the central server, make a token
with the `push` scope. On each place, add an output:

- URL: `http://central:8765/feed/v1/ingest`
- events: `utterance` (the central server runs its own rules; pushed alerts are ignored)
- batch: 5 s
- header: `Authorization: Bearer <that token>`

Each of a place's stations appears on the central server the first time it sends something, as
`<place>-<station>`, with `remote = "push"`. A push only writes into stations that pushes made:
an event whose id would land on any other station here is refused. One place can make 25
stations and all places together 100. The answer is `{"accepted": n, "rejected": m}` with status
200; the log says why events were refused. An event is refused whole if a value is out of
bounds: `t_start` older than 7 days or more than 10 minutes ahead of this machine's clock,
`duration` outside 0–600 s, text over 4,000 characters, a language that isn't a BCP-47 tag. A
`t_start` up to 10 minutes ahead is taken as a clock that runs fast and stored as the time the
event arrived.

**Either way:**

- The central server stores, searches and answers questions over the text like its own, by the
  privacy class each station's events carry: radio that isn't broadcast is kept, quoted and
  passed on by the central server's own rules for that class.
- It runs **its own** rules on the source-language text. Translation stays off the alert path.
- It translates only into languages the text didn't arrive with, so the place can translate
  into the local language and the central server into its own.
- No audio crosses. Each event's `audio_url` points back at the place that kept it.

## Using Wavescribe from another program

- **Dashboards:** read `/feed/v1/events` (or poll `/feed/v1/utterances?since_id=`).
- **Automations:** point an output at your webhook endpoint (Home Assistant, Node-RED, n8n, a
  script). It receives the alert JSON above.
- **Monitoring:** scrape `/metrics`; health checks use `/healthz`.
- **Archiving:** fetch `/feed/v1/utterances?format=ndjson` by time range.
