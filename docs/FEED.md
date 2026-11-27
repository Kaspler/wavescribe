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

- `id`: the segment id on the machine that heard it. It grows in the order lines are
  transcribed, which isn't always the order they finish (see *Resuming*).
- `text`: what the speech recogniser wrote, in the station's language.
- `translations`: the languages that machine translates this station into.
- `flags`: notes from the ASR checks. For example, `lang:es` means it sounded like Spanish,
  `repetition` that a phrase was repeated four or more times inside a longer message, and
  `rechecked` that Whisper listened again to a line heard in another language: without `lang:`
  beside it, Whisper heard the station's language and the text is its transcript.
- `rules`: the alert rules this utterance tripped.
- `audio_url`: appears only when the recording is kept and the machine's address is known.
  The page sets that address under Settings → Connections → *For other programs* (*Public URL*).
- `station.privacy`: the station's privacy class on the machine that heard it (`broadcast`,
  `amateur`, `utility`, `private`, `digital`; see [RADIO.md](RADIO.md)). A machine that takes
  the text in keeps it, quotes it in alerts and passes it on by its own rules for that class.
  The class is the sender's statement: a sender that leaves it out is taken to send broadcast,
  so a program that pushes radio that isn't broadcast must say so. A class the receiver doesn't
  know is handled as `private`. Once a sender states a stricter class, the station is handled
  by it from then on. On the receiving machine, `privacy` on a remote station in
  `stations.toml` (pulled or `remote = "push"`) sets the least strict class its text gets:
  what the sender says can make it stricter, never looser.

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
  "audio_url": "…/api/segments/4181/audio.wav", "cap_url": "…/api/alerts/77.cap",
  "change": "new"
}
```

Alert fields:

- `change` (live events and pushes; not in `/feed/v1/alerts`): what this event is about. An
  alert is one incident: a story repeated on the air is one alert whose `mentions` grow, and it
  ends once nobody mentioned it for the cooldown (15 minutes by default); the next mention is a
  new alert with a new `id`.
  - `new`: the alert was announced for the first time.
  - `escalated`: announced again because it got more serious (`severity` rose).
  - `reminder`: still being mentioned, at most once per `remind_minutes` (an hour by default).
  - `ended`: a station alert is over (the station's audio is back).
  - `update`: nothing was announced: another mention folded in, a translation added, a
    delivery outcome recorded, or an alert opened while it is still being checked (`pending`).

  `/feed/v1/events` sends every change, so a program that reacts to alerts should act on
  `change != "update"` (or only on `new`) and treat `update` as fresh numbers for an alert it
  has seen. Pushes to outputs only ever carry `new`, `escalated`, `reminder` and `ended`.

- `kind`: `keyword` or `llm` (a rule written in plain language and checked by the LLM), or a
  station alert: `silence` (a station gone silent) or `traffic` (an FM station's RDS traffic
  announcement). A station alert's `text` is Wavescribe's own wording, in English (`language`
  `en`), with no words heard and no `terms`; when a silent station's audio is back, a second
  event for the same alert carries "Audio is back on …" as its `text` and `reason`.
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
| `GET /feed/v1/utterances` | What was said, in the order it was stored (by `id`): `?since=<epoch>&until=<epoch>` or `?since_id=<id>` (a segment id, or a live stream's event id), `?stations=`, `?langs=`, `?limit=` (500 by default, up to 10,000; counts only what this machine may give), `?format=json\|ndjson\|csv`. A page cut at the limit goes on with `?since_id=<its last id>`, with nothing skipped. A line still being translated is there without its translations. |
| `GET /feed/v1/alerts` | Recent alerts (`?since=<epoch>`). |
| `GET /feed/v1/stations` | Stations: language, translation languages, running, connected, last heard, lag. |
| `GET /feed/v1/node` | This machine: name, URL, version, feed version, translation languages. |
| `GET /healthz` | `{"ok": true}` once models are loaded (503 before). It needs no token. |
| `GET /metrics` | Prometheus text: stations running and connected, utterances, queues, drops, lag, outputs, memory footprint, free disk, power draw. |

**Resuming.** Utterances finish out of order: one whose translation waited behind a busier
station, or that Whisper re-checked, finishes after later ones. So each `utterance` event's
`id:` is a resume point rather than a segment id (the event's own segment id is its `id`
field): `<low>` or `<low>,<id>,<id>…`. Every utterance up to segment id `low` that the stream
may send has been sent, and so have the ids after it; `low` stays just below the oldest line
still in flight on the stations you follow. Treat it as text and send it back as it came.

EventSource sends the last one back as `Last-Event-ID` when it reconnects (a client that opens a
new stream passes it as `?since_id=`). The feed then replays the lines that finished since (up
to 1,000), sends those still being translated when they finish, never unfinished, and goes on
live: each line once.

A resume can still send a few lines again: the id lists at most 64 lines sent past `low` (more
finish while an old one waits only on a very busy machine), and a replay cut at 1,000 sends the
rest on the next resume. **Skip an utterance whose `id` you already have** (with the same
text); `wavescribe follow` and remote stations do. Alerts carry no `id:` and aren't replayed:
ask `/feed/v1/alerts?since=` for those raised meanwhile. After a restart, a client that kept no
id can ask for `?since=<epoch>`. Pass `?events=status` to get a status event every 10 s.

```sh
curl -N http://kitchen-mac.local:8765/feed/v1/events?stations=tsf
curl "http://kitchen-mac.local:8765/feed/v1/utterances?since=$(date -v-1H +%s)&format=csv" > hour.csv
```

In CSV, a cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading
apostrophe, so a spreadsheet doesn't run it as a formula. A parameter that should be a number
and isn't gets a 400.

## Who can read what

`serve` prints one line when it starts saying which of these holds.

| Who | The page's data: what was said, alerts, audio, live events | The feed: `/feed/v1`, `/metrics` | Asking questions (`/api/ask`) | Controls (`/api/manage/…`, start and stop) |
|---|---|---|---|---|
| This machine | yes | yes, no token | yes | yes |
| The LAN (`serve --lan`) | yes, no token | no token until the first feed token is made; then a `read` token | only with `serve --allow-remote-ask` | never |
| Through a proxy (`serve --behind-proxy`, or a request carrying a proxy's header) | a `read` token, unless `serve --public-read` | always a `read` token | only with `--allow-remote-ask`, and a `read` token unless `--public-read` | never |

- **Everyone:** the page itself (its HTML, scripts and icons) and `/healthz` need nothing.
  Pushing to `/feed/v1/ingest` always needs a `push` token, even from this machine, as JSON
  (`Content-Type: application/json`): otherwise any web page open on the machine could post
  events and set off alerts.
- **The page's data** is what the page reads: `/api/state`, `/api/events`, `/api/history`,
  `/api/search`, `/api/alerts`, `/api/alerts/<id>.cap`, `/api/segments/<id>/audio.wav` (the
  feed's `audio_url`) and `…/audio.pcm`, and `/audio/<station>.pcm`. Without a token where one is
  needed, the answer is 401 with `{"error": …, "token_required": true}` and
  `WWW-Authenticate: Bearer`.
- **On the LAN** there is no login: anyone on your network reads the page, what it heard and its
  recordings, as they could listen to the radio in the room. Use `--lan` only on a network you
  trust. Feed tokens guard the feed there, not the page.
- **Asking** runs this machine's LLM, which is then busy for everything else, so another machine
  may ask only when `serve --allow-remote-ask` lets it. Otherwise the page there shows Ask as
  unavailable (`ask` in `/api/state` says why) and `POST /api/ask` is 403.
- **Through a proxy.** A request carrying `Forwarded`, `X-Forwarded-For`, `X-Real-IP` or `Via`
  came through a proxy, so it is another machine's even when it connects from this one: it is
  read-only, counts against the limits for other machines, and needs a token as in the table.
  The address in those headers is never trusted or used for anything else. A proxy that adds
  none of them makes every visitor look like this machine, controls included: start Wavescribe
  with `serve --behind-proxy` whenever a proxy or tunnel forwards to it.
- **Over the internet:** put Wavescribe behind a reverse proxy with TLS and start it with
  `serve --behind-proxy`. Set the proxy's public name as the *Public URL* first (*Settings →
  Connections → For other programs*): requests that name the machine any other way get 421.
  `--public-read` lets anyone who reaches the proxy read the page without a token; the feed
  still needs one.
- **What leaves this machine:** to every other machine, radio whose class keeps its text here
  (by default everything but broadcast and streams) is invisible, and its alerts reach them
  without words.
- **By the machine's name only:** every request must name the machine in its `Host` header
  (`localhost`, `127.0.0.1`, `[::1]`, a `.localhost` name, the machine's own name or an
  address); from other machines a `.local` name and the public URL's host count too. Anything
  else gets 421, so a website whose name is made to resolve to 127.0.0.1 (DNS rebinding) can't
  reach the page's controls.
- **Tokens** are made on the page (*Settings → Connections → For other programs → Feed
  tokens*) with a scope:
  - `read`: the feed, and the page's data through a proxy;
  - `push`: `/feed/v1/ingest`.

  Send one as `Authorization: Bearer <token>`. A token is shown once and only its SHA-256 hash
  is kept, in `config/ui.toml` (mode 0600).
- **Where a header can't be set** (a browser's EventSource), trade a `read` token for a stream
  ticket and put that in the URL: `POST /feed/v1/ticket` with the token in the
  `Authorization` header (never in the URL) returns `{"ticket": "wsk_…", "expires_in": 60}`,
  then `GET /feed/v1/events?ticket=wsk_…` (or `/api/events?ticket=…`) opens the stream. A
  ticket works once, within 60 s, and only for reading, so one found in a log is spent. An
  EventSource reconnects with the URL it was given, which no longer works: close it, get a new
  ticket and open a new one with `?since_id=<the last event id>` (EventSource keeps it as
  `lastEventId`). `?token=<token>` still works but is logged as a warning (at most once an
  hour): a token in a URL ends up in logs, here, in browsers and on any proxy.

  ```sh
  T=$(curl -s -X POST -H "Authorization: Bearer $TOKEN" "http://kitchen-mac.local:8765/feed/v1/ticket" | jq -r .ticket)
  curl -N "http://kitchen-mac.local:8765/feed/v1/events?ticket=$T"
  ```

## Push: outputs

Alerts can be missed, late or wrong: don't connect them to anything where a missed or a false
alert causes harm.

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

- An alert goes out when it is announced (`change`: `new`), again only when it gets more
  serious (`escalated`), is still being mentioned an hour later (`reminder`) or, for a station
  alert, is over (`ended`); never for each mention. In `cap` format the first is a CAP `Alert`
  and the others are an `Update` with an identifier of their own, all in the same `incidents`.
  In `text` format the first line says "(more serious now)", "(still being mentioned)" or
  "(over)".
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
privacy = "amateur"         # optional: handle its text at least this strictly
```

- The central server subscribes to the node's feed and catches up on the last hour the first
  time.
- After that it resumes after the last utterance it stored, going back a week at most. A node
  whose clock runs fast sends the last few minutes again, since their times were stored here
  as they arrived; what it sends again is recognised by its words and length and skipped.
- On a lost connection it reconnects, backing off from 2 s to 60 s.

**The places push.** Use this when they sit behind NAT. On the central server, make a token
with the `push` scope. On each place, add an output:

- URL: `http://central:8765/feed/v1/ingest`
- events: `utterance` (the central server runs its own rules; pushed alerts are ignored)
- batch: 5 s
- header: `Authorization: Bearer <that token>`

Each of a place's stations appears on the central server the first time it sends something, as
`<place>-<station>`, with `remote = "push"`. An id longer than 40 characters keeps the first 16
of the place's name and gets a short hash of both names, so two long names never share an id. A push only writes into stations that pushes made:
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
