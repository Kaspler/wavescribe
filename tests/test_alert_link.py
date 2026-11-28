# SPDX-License-Identifier: Apache-2.0
"""An alert's link to the page (``page_url``: ``#/alert/<id>``) and the one alert the page
asks for when it opens there (``GET /api/alerts/<id>``)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from tests.test_app import Running, make_app, wait_for
from wavescribe.events import alert_event


def test_an_alert_event_links_to_the_page_open_at_it() -> None:
    alert = {"id": 77, "rule": "water", "severity": "high", "stations": ["a"], "segment_id": 5}
    node = {"name": "n", "url": "http://192.0.2.4:8766"}
    ev = alert_event(alert, {}, node)
    assert ev["page_url"] == "http://192.0.2.4:8766/#/alert/77"
    assert ev["cap_url"] == "http://192.0.2.4:8766/api/alerts/77.cap"
    assert "page_url" not in alert_event(alert, {}, {"name": "n", "url": None})


def test_the_page_reads_one_alert_by_its_id(tmp_path: Path, monkeypatch) -> None:
    from wavescribe.web.server import Handler

    node = Running(make_app(tmp_path, start=["a"]))
    try:
        wait_for(lambda: node.app.store.alerts())
        [listed] = node.call("GET", "/api/alerts")[1]
        status, one = node.call("GET", f"/api/alerts/{listed['id']}")
        assert status == 200 and one == listed and one["quote"]
        assert node.call("GET", "/api/alerts/999999") == (404, {"error": "no such alert"})
        assert node.call("GET", "/api/alerts/x")[0] == 404

        # Another machine gets it as it may leave this one: without the words of radio that
        # stays here.
        app = node.app
        utility = dataclasses.replace(app.config.get("a"), privacy="utility")
        stations = tuple(utility if s.id == "a" else s for s in app.hub.config.stations)
        app.hub.config = dataclasses.replace(app.hub.config, stations=stations)
        monkeypatch.setattr(Handler, "_is_local", lambda self: False)
        status, there = node.call("GET", f"/api/alerts/{listed['id']}")
        assert status == 200 and there["id"] == listed["id"] and there["quote"] == ""
    finally:
        node.close()
