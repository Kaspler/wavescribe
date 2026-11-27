# Security

## Reporting a problem

Please report security problems privately, not in a public issue: use **Report a vulnerability**
on the repository's **Security** tab ([the private form](https://github.com/Kaspler/wavescribe/security/advisories/new),
GitHub's private vulnerability reporting). Say what you
found, how to reproduce it, and which version or commit you used. You'll get an answer as soon as
the maintainers can give one; this is a volunteer project, so there is no fixed time.

## What Wavescribe trusts

- **This machine.** The control centre (`/api/manage/…`) answers only requests from the machine
  Wavescribe runs on, which name it in their `Host` header (against DNS rebinding), and every
  control-centre request needs the `X-Wavescribe` header (against cross-site forms and images).
  Every response forbids framing and sets a Content-Security-Policy. Anyone who can run programs
  on the machine can control Wavescribe.
- **The local network, read-only, with `serve --lan`.** The page is plain HTTP with no login.
  Other machines can read the transcripts of broadcast radio and streams, never change anything,
  and never see radio whose privacy class keeps it on this machine
  ([docs/RADIO.md](docs/RADIO.md)). Use `--lan` only on a network you trust. Requests from the
  LAN must name the machine too (address, `.local` name or the public URL's host). Other machines
  together get 64 connections, of which at most 32 may follow live events (the page's or `/feed/v1/events`), and 4
  audio decodes at a time; a connection that sends or takes nothing for 30 s is closed. Text of
  stations no longer configured is not given to other machines, and their alerts reach them
  without the words.
- **Feed tokens.** Every push to `/feed/v1/ingest` needs a token, from this machine too. Tokens
  are made on the Wavescribe machine only: on the page (Settings → Connections → *For other
  programs*) or with `wavescribe token make`. Reading `/feed/v1` from another machine needs one as soon as the first token
  exists; until then, under `--lan`, the LAN reads the feed as it reads the page
  ([docs/FEED.md](docs/FEED.md)). Only the tokens' hashes are stored. A push token can add text
  only to stations that pushes created (25 per sender, 100 in all), and every value it sends is
  checked before it is stored.
- **Behind a proxy or tunnel:** a proxy on the same machine makes its visitors look local. Start
  Wavescribe with `serve --behind-proxy` so every request counts as another machine's and the
  feed always needs a token, set the proxy's public name as the *Public URL* (Settings → Connections → *For other programs*),
  and put TLS and authentication on the proxy: Wavescribe has none of its own beyond the feed
  tokens. Requests that carry `Forwarded`, `X-Forwarded-For`, `X-Real-IP` or `Via` are treated
  as another machine's even without the option.
- **Not the web.** ffmpeg and ffprobe open a stream URL with web protocols only, so a playlist
  can't point them at local files or sockets. Answers from the station and model catalogues,
  from another Wavescribe's feed and from pushes are size-limited and checked before use.

Reports about any of these boundaries are welcome.
