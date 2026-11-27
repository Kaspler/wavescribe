# SPDX-License-Identifier: Apache-2.0
"""``/api/manage/…``: the page's control centre. Local only (the machine itself): the
handler refuses other addresses before a request gets here, and changes need the
``X-Wavescribe`` header, which another website's form can't send.

Each route is (method, pattern) -> handler(app, match, query, body) -> JSON-able. A
``ChangeError`` or ``AdminError`` becomes a 400 with its message for the page to show, and the
``code`` of a technical refusal beside it (wavescribe/refusal.py), so the page needn't judge from
the words.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from wavescribe import admin, refusal
from wavescribe.admin import AdminError
from wavescribe.app import ChangeError, NotFound
from wavescribe.config import ConfigError
from wavescribe.radio import stills
from wavescribe.radio.service import RadioError
from wavescribe.registry import ModelError

if TYPE_CHECKING:
    from wavescribe.app import App

Handler = Callable[["App", re.Match[str], dict[str, str], Any], Any]
ROUTES: list[tuple[str, re.Pattern[str], Handler]] = []


def route(method: str, pattern: str) -> Callable[[Handler], Handler]:
    def add(fn: Handler) -> Handler:
        ROUTES.append((method, re.compile(f"^/api/manage{pattern}$"), fn))
        return fn

    return add


def dispatch(app: App, method: str, path: str, query: dict[str, str], body: Any) -> tuple[int, Any]:
    matched_path = False
    for m, pattern, fn in ROUTES:
        found = pattern.match(path)
        if not found:
            continue
        matched_path = True
        if m != method:
            continue
        try:
            return 200, fn(app, found, query, body)
        except NotFound as e:
            return 404, {"error": str(e), **refusal.fields(e)}
        except RadioError as e:  # carries what the page needs to ask (acknowledge, retune)
            return 400, {"error": str(e), "code": e.code, **e.extra}
        except (ChangeError, AdminError, ConfigError, ModelError) as e:
            return 400, {"error": str(e), **refusal.fields(e)}
        except (KeyError, TypeError, ValueError, AttributeError) as e:  # a field of the wrong type
            return 400, {"error": f"bad request: {e}"}
    return (405 if matched_path else 404), {"error": "no such endpoint"}


def _body(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ChangeError("expected a JSON object", "bad_body")
    return body


# -- overview and status ----------------------------------------------------------------------


@route("GET", "/status")
def _status(app: App, m, q, b) -> Any:
    return app.status_view()


@route("GET", "/node")
def _node(app: App, m, q, b) -> Any:
    return {**app.node(), "settings": app.settings.get("node") or {},
            "lan": app.host not in ("127.0.0.1", "::1", "localhost"), "port": app.port,
            "tokens_required": app.tokens_required()}  # fmt: skip


@route("POST", "/node")
def _set_node(app: App, m, q, b) -> Any:
    b = _body(b)
    return app.set_node(b.get("name"), b.get("public_url"))


@route("POST", "/retention")
def _retention(app: App, m, q, b) -> Any:
    return app.set_retention(_body(b))


# -- stations ---------------------------------------------------------------------------------


@route("GET", "/stations")
def _stations(app: App, m, q, b) -> Any:
    return {"stations": app.stations_view(), "errors": app.config_errors,
            "devices": [d.id for d in app.config.devices],
            "defaults": app.defaults_view()}  # fmt: skip


@route("POST", "/stations")
def _add_station(app: App, m, q, b) -> Any:
    """A radio station or watch its receiver doesn't hear moves the receiver, and asks first
    when that leaves stations out (code ``retune``, as ``/radio/tune`` asks it); ``retune:
    true`` is the yes."""
    b = dict(_body(b))
    start = bool(b.pop("start", False))
    retune = b.pop("retune", False)
    if not isinstance(retune, bool):
        raise ChangeError("retune must be true or false")
    return app.radio.add_station(b, start=start, retune=retune)


@route("POST", "/stations/(?P<id>[a-z0-9_-]+)")
def _update_station(app: App, m, q, b) -> Any:
    return app.update_station(m["id"], _body(b))


@route("DELETE", "/stations/(?P<id>[a-z0-9_-]+)")
def _delete_station(app: App, m, q, b) -> Any:
    app.delete_station(m["id"])
    return {"ok": True}


@route("POST", "/stations/(?P<id>[a-z0-9_-]+)/(?P<action>start|stop)")
def _start_stop(app: App, m, q, b) -> Any:
    """A station its receiver doesn't hear moves the receiver to start, and asks first when
    that leaves stations out (code ``retune``, as ``/radio/tune`` asks it); ``retune: true`` is
    the yes. ``listen_only: true`` is for this run only; the saved mode stays."""
    if m["action"] == "stop":
        app.stop_station(m["id"])
        return {"ok": True}
    b = b if isinstance(b, dict) else {}
    listen_only, retune = b.get("listen_only", False), b.get("retune", False)
    for key, value in (("listen_only", listen_only), ("retune", retune)):
        if not isinstance(value, bool):
            raise ChangeError(f"{key} must be true or false")
    return app.radio.start_station(m["id"], listen_only=listen_only, retune=retune)


@route("POST", "/probe")
def _probe(app: App, m, q, b) -> Any:
    b = _body(b)
    return admin.probe(app, str(b.get("url", "")).strip(), bool(b.get("detect", False)))


@route("POST", "/remote/probe")
def _remote_probe(app: App, m, q, b) -> Any:
    b = _body(b)
    return admin.remote_stations(str(b.get("url", "")), b.get("token") or None)


@route("POST", "/playlist")
def _playlist(app: App, m, q, b) -> Any:
    b = _body(b)
    return {"entries": admin.import_playlist(b.get("text"), b.get("url"))}


@route("GET", "/catalog/stations")
def _catalog_stations(app: App, m, q, b) -> Any:
    return admin.station_catalog(q.get("q", ""), q.get("language") or None,
                                 q.get("country") or None, q.get("online") == "1")  # fmt: skip


@route("POST", "/catalog/stations/refresh")
def _catalog_refresh(app: App, m, q, b) -> Any:
    return admin.refresh_station_catalog()


# -- models -----------------------------------------------------------------------------------


@route("GET", "/models")
def _models(app: App, m, q, b) -> Any:
    return admin.models_view(app)


@route("POST", "/models/chain")
def _chain(app: App, m, q, b) -> Any:
    b = _body(b)
    app.set_chain(str(b["capability"]), [str(i) for i in b["ids"]])
    return {"ok": True}


@route("POST", "/models/(?P<id>[A-Za-z0-9_.-]+)/fetch")
def _fetch(app: App, m, q, b) -> Any:
    return app.downloads.start(m["id"])


@route("POST", "/models/(?P<id>[A-Za-z0-9_.-]+)/accept")
def _accept(app: App, m, q, b) -> Any:
    b = _body(b)
    admin.accept_model(app, m["id"], str(b.get("license", "")), bool(b.get("non_commercial")))
    return {"ok": True}


@route("GET", "/catalog/models")
def _catalog_models(app: App, m, q, b) -> Any:
    return {"results": admin.search_models(q.get("q", ""), q.get("capability", "asr"))}


@route("POST", "/catalog/models")
def _catalog_add(app: App, m, q, b) -> Any:
    b = _body(b)
    return admin.add_catalog_model(app, b["model"], str(b["capability"]))


# -- alerts -----------------------------------------------------------------------------------


@route("GET", "/rules")
def _rules(app: App, m, q, b) -> Any:
    return app.rules_view()


@route("POST", "/rules")
def _save_rule(app: App, m, q, b) -> Any:
    app.save_rule(_body(b))
    return app.rules_view()


@route("DELETE", "/rules/(?P<id>[a-z0-9_-]+)")
def _delete_rule(app: App, m, q, b) -> Any:
    app.delete_rule(m["id"])
    return app.rules_view()


@route("POST", "/rules/(?P<id>[a-z0-9_-]+)/enable")
def _enable_rule(app: App, m, q, b) -> Any:
    app.enable_rule(m["id"], bool(_body(b).get("on", True)))
    return app.rules_view()


@route("POST", "/rules/try")
def _try_rules(app: App, m, q, b) -> Any:
    b = _body(b)
    return {"hits": app.try_rules(str(b.get("text", "")), str(b.get("language", "en")),
                                  b.get("station"))}  # fmt: skip


@route("POST", "/rules/suggest")
def _suggest(app: App, m, q, b) -> Any:
    b = _body(b)
    return suggest_terms(app, [str(t) for t in b.get("terms", [])],
                         [str(x) for x in b.get("languages", [])])  # fmt: skip


@route("POST", "/alerts/settings")
def _alert_settings(app: App, m, q, b) -> Any:
    b = _body(b)

    def mutate(a: dict[str, Any]) -> None:
        for key in ("delivery", "actions", "speak"):
            if key in b and isinstance(b[key], dict):
                a[key] = {**a.get(key, {}), **b[key]}

    app.change_alerts(mutate)
    return app.rules_view()


@route("GET", "/digest/schedule")
def _digest_schedule(app: App, m, q, b) -> Any:
    """Digests on a schedule: what is set and where (``source``: ``page``, ``file`` or null),
    whether they run, when the next comes (``next_at``), and what they may be set to."""
    return app.digest_schedule_view()


@route("POST", "/digest/schedule")
def _set_digest_schedule(app: App, m, q, b) -> Any:
    """``{every_minutes, stations, language, speak, quiet_hours}`` as ``[digest]`` takes them,
    in place of alerts.toml's and at once; ``enabled: false`` switches them off. A refusal has
    the ``code`` and ``field`` of what to change (digest/schedule.py, ``parse_schedule``)."""
    return app.set_digest_schedule(_body(b))


@route("DELETE", "/digest/schedule")
def _reset_digest_schedule(app: App, m, q, b) -> Any:
    """Forget the page's schedule: alerts.toml's ``[digest]``, if any, runs again."""
    return app.reset_digest_schedule()


# -- outputs and feed tokens ------------------------------------------------------------------


@route("GET", "/outputs")
def _outputs(app: App, m, q, b) -> Any:
    return {**app.outputs_view(), "tokens": app.tokens_view()}


@route("POST", "/outputs")
def _save_output(app: App, m, q, b) -> Any:
    return app.save_output(_body(b))


@route("DELETE", "/outputs/(?P<id>[a-z0-9_-]+)")
def _delete_output(app: App, m, q, b) -> Any:
    app.delete_output(m["id"])
    return {"ok": True}


@route("POST", "/outputs/(?P<id>[a-z0-9_-]+)/test")
def _test_output(app: App, m, q, b) -> Any:
    return app.test_output(m["id"])


@route("GET", "/deliveries")
def _deliveries(app: App, m, q, b) -> Any:
    return {"deliveries": app.outputs.recent(q.get("output") or None)}


@route("POST", "/tokens")
def _create_token(app: App, m, q, b) -> Any:
    b = _body(b)
    return app.create_token(str(b.get("label", "")), [str(s) for s in b.get("scopes", [])])


@route("DELETE", "/tokens/(?P<id>[a-f0-9]+)")
def _delete_token(app: App, m, q, b) -> Any:
    app.delete_token(m["id"])
    return {"ok": True}


# -- receivers --------------------------------------------------------------------------------


@route("GET", "/receivers")
def _receivers(app: App, m, q, b) -> Any:
    return admin.receivers_view(app, scan=q.get("scan") == "1")


@route("POST", "/receivers")
def _add_receiver(app: App, m, q, b) -> Any:
    return admin.add_device(app, _body(b))


@route("DELETE", "/receivers/(?P<id>[a-z0-9_-]+)")
def _delete_receiver(app: App, m, q, b) -> Any:
    admin.delete_device(app, m["id"])
    return {"ok": True}


@route("POST", "/receivers/(?P<id>[a-z0-9_-]+)")
def _update_receiver(app: App, m, q, b) -> Any:
    """``retune: true`` answers yes to the question a retune asks before it leaves stations out
    (code ``retune``, as ``/radio/tune`` asks it)."""
    b = dict(_body(b))
    retune = b.pop("retune", False)
    if not isinstance(retune, bool):
        raise ChangeError("retune must be true or false")
    return admin.update_device(app, m["id"], b, retune=retune)


@route("GET", "/receivers/(?P<id>[a-z0-9_-]+)/spectrum")
def _spectrum(app: App, m, q, b) -> Any:
    return {"spectrum": admin.spectrum(app, m["id"])}


@route("POST", "/receivers/(?P<id>[a-z0-9_-]+)/monitor")
def _monitor(app: App, m, q, b) -> Any:
    admin.monitor(app, m["id"], bool(_body(b).get("on", True)))
    return {"ok": True}


# -- radio: scan, what was heard, tune ---------------------------------------------------------


@route("GET", "/radio")
def _radio(app: App, m, q, b) -> Any:
    return app.radio.view(dongles=q.get("dongles") == "1")


@route("POST", "/radio/policy/(?P<cls>[a-z]+)")
def _radio_policy(app: App, m, q, b) -> Any:
    app.radio.set_policy(m["cls"], _body(b))
    return app.radio.view()


@route("DELETE", "/radio/policy")
def _radio_policy_reset(app: App, m, q, b) -> Any:
    app.radio.reset_policies()
    return app.radio.view()


@route("POST", "/radio/region")
def _radio_region(app: App, m, q, b) -> Any:
    app.radio.set_region(_body(b).get("region") or None)
    return app.radio.view()


@route("GET", "/radio/scan")
def _radio_scan_status(app: App, m, q, b) -> Any:
    return {"scan": app.radio.scan_status()}


@route("POST", "/radio/scan")
def _radio_scan(app: App, m, q, b) -> Any:
    return {"scan": app.radio.start_scan(_body(b))}


@route("DELETE", "/radio/scan")
def _radio_scan_cancel(app: App, m, q, b) -> Any:
    app.radio.cancel_scan()
    return {"scan": app.radio.scan_status()}


@route("POST", "/radio/tune")
def _radio_tune(app: App, m, q, b) -> Any:
    return app.radio.tune(_body(b))


@route("GET", "/radio/scans")
def _radio_still(app: App, m, q, b) -> Any:
    """A still picture of ``lo`` to ``hi`` MHz from the saved scans (radio/stills.py)."""
    try:
        lo, hi = float(q["lo"]), float(q["hi"])
    except (KeyError, TypeError, ValueError):
        raise ChangeError(
            "give lo and hi, the stretch of the dial in MHz, e.g. ?lo=87.5&hi=108"
        ) from None
    if not (math.isfinite(lo) and math.isfinite(hi)):
        raise ChangeError("lo and hi must be numbers of MHz, e.g. ?lo=87.5&hi=108")
    return {"scan": stills.still(app.radio.catalog.root / "scans", lo, hi)}


# -- listening by hand (radio/live.py) ---------------------------------------------------------


@route("GET", "/live")
def _live_all(app: App, m, q, b) -> Any:
    return {"receivers": [app.live.state(d.id) for d in app.config.devices]}


@route("GET", "/live/(?P<id>[a-z0-9_-]+)")
def _live_state(app: App, m, q, b) -> Any:
    """Polled by the Listen tab a few times a second: keeps the receiver open and the
    waterfall computed (``lo``/``hi`` in MHz zoom it)."""
    return app.live.state(m["id"], lo_mhz=q.get("lo"), hi_mhz=q.get("hi"), watch=True)


@route("POST", "/live/(?P<id>[a-z0-9_-]+)")
def _live_tune(app: App, m, q, b) -> Any:
    return app.live.tune(m["id"], _body(b))


@route("DELETE", "/live/(?P<id>[a-z0-9_-]+)")
def _live_close(app: App, m, q, b) -> Any:
    app.live.close(m["id"])
    return app.live.state(m["id"])


@route("POST", "/live/(?P<id>[a-z0-9_-]+)/scan")
def _live_scan(app: App, m, q, b) -> Any:
    return app.live.start_scan(m["id"], _body(b))


@route("DELETE", "/live/(?P<id>[a-z0-9_-]+)/scan")
def _live_scan_stop(app: App, m, q, b) -> Any:
    return app.live.stop_scan(m["id"])


@route("POST", "/live/(?P<id>[a-z0-9_-]+)/lockout")
def _live_lockout(app: App, m, q, b) -> Any:
    b = _body(b)
    return app.live.lock_out(m["id"], b["mhz"], bool(b.get("on", True)))


@route("POST", "/live/(?P<id>[a-z0-9_-]+)/station")
def _live_station(app: App, m, q, b) -> Any:
    return app.live.make_station(m["id"], _body(b))


@route("POST", "/radio/heard/(?P<freq>[0-9]+)")
def _radio_name(app: App, m, q, b) -> Any:
    app.radio.catalog.name(int(m["freq"]), (_body(b).get("name") or "").strip() or None)
    return {"ok": True}


@route("GET", "/radio/notes")
def _radio_notes(app: App, m, q, b) -> Any:
    return {"notes": sorted(app.radio.catalog.notes().values(), key=lambda n: n["freq_hz"])}


@route("POST", "/radio/notes/(?P<freq>[0-9]+)")
def _radio_note(app: App, m, q, b) -> Any:
    """Name a frequency, note something about it, tag it (a watch's catches show it). A field
    left out stays as it is; "" or [] clears it."""
    b = _body(b)
    for key in ("name", "note"):
        if b.get(key) is not None and not isinstance(b[key], str):
            raise ChangeError(f"'{key}' is text")
    tags = b.get("tags")
    if tags is not None and not (isinstance(tags, list) and all(isinstance(t, str) for t in tags)):
        raise ChangeError("'tags' is a list of words")
    from wavescribe.dsp import check_tunable
    from wavescribe.radio.watch import on_raster

    try:
        check_tunable(int(m["freq"]))  # in the words `radio note` uses
    except ValueError as e:
        raise ChangeError(str(e)) from None
    hz = on_raster(int(m["freq"]), app.radio.region()[0])
    return app.radio.catalog.note(hz, name=b.get("name"), note=b.get("note"), tags=tags)


@route("DELETE", "/radio/heard(?:/(?P<freq>[0-9]+))?")
def _radio_forget(app: App, m, q, b) -> Any:
    app.radio.catalog.forget(int(m["freq"]) if m["freq"] else None)
    return {"ok": True}


# -- helpers that use the LLM -----------------------------------------------------------------


def suggest_terms(app: App, terms: list[str], languages: list[str]) -> dict[str, Any]:
    """The user's keywords in each station language, from the LLM, for them to check
    (rules/suggest.py): per language the words or why there are none, and how many in all.
    When no word came back at all the answer is a refusal that says why, with the same fields
    beside it, so nothing reads as suggestions added."""
    import json

    from wavescribe.rules import suggest

    terms = [t.strip() for t in terms if t.strip()][: suggest.MAX_TERMS]
    langs = [x for x in dict.fromkeys(languages) if x][: suggest.MAX_LANGUAGES]
    if not terms or not langs:
        raise ChangeError("give some words and at least one language")
    mt = app.plan.mt
    if mt is None or mt[0].backend != "mlx-lm":
        using = f"; this run translates with {mt[0].id}" if mt else ""
        raise ChangeError(f"suggestions need an LLM{using}", suggest.NO_LLM)
    out = suggest.suggest(lambda run: app.pipeline.request_llm(run, "suggest", 0.4), terms, langs)
    out["note"] = json.dumps(terms, ensure_ascii=False)
    out["at"] = time.time()
    if not out["count"]:
        whys = {e["why"] for e in out["languages"].values()}
        code = suggest.NO_LLM if whys == {suggest.NO_LLM} else "no_suggestions"
        raise ChangeError(out.pop("message"), code, **out)
    return out
