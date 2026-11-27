# SPDX-License-Identifier: Apache-2.0
"""What the page reads from the server instead of working it out: a station's radio in
``/api/state``, where an utterance was heard, the translations of what a watch caught, the codes
beside refusals, and what holds a receiver."""

from pathlib import Path

from tests.test_retune import running, stations, two_metres_centre

SLICE = {"id": "slice", "name": "Slice", "device": "a", "watch": [145.5, 145.6],
         "language": "auto", "languages": ["de-DE", "en"]}  # fmt: skip
FAR = {"id": "far", "name": "Far", "device": "a", "mhz": 432.5}


def test_the_state_says_where_each_station_listens(tmp_path: Path) -> None:
    r = running(tmp_path, stations(two_metres_centre(), SLICE, FAR))
    try:
        status, state = r.call("GET", "/api/state")
        assert status == 200
        by_id = {s["id"]: s for s in state["stations"]}
        keys = ("device", "watch", "languages", "privacy", "out_of_reach")
        assert {k: by_id["stream"][k] for k in keys} == {
            "device": None, "watch": None, "languages": None, "privacy": "broadcast",
            "out_of_reach": None}  # fmt: skip
        assert {k: by_id["ham2m"][k] for k in keys} == {
            "device": "a", "watch": "2m", "languages": ["de-DE"], "privacy": "amateur",
            "out_of_reach": None}  # fmt: skip
        assert by_id["slice"]["watch"] == [145.5, 145.6]
        assert by_id["slice"]["languages"] == ["de-DE", "en"]
        far = by_id["far"]
        assert (far["device"], far["watch"], far["languages"]) == ("a", None, None)
        assert far["out_of_reach"].startswith("Receiver A doesn't hear Far where it is tuned now")
        assert far["out_of_reach"] == r.app.config.get("far").out_of_reach
    finally:
        r.close()
