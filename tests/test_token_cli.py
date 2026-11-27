# SPDX-License-Identifier: Apache-2.0
"""The control key and ``wavescribe token``: feed tokens made from the Wavescribe machine,
also behind a proxy, where the page's controls are off."""

import stat
from pathlib import Path

import pytest

from tests.test_app import Running, make_app
from wavescribe import token_cli
from wavescribe.cli import main
from wavescribe.control import KEY_HEADER, load_key


def test_the_key_is_made_once_and_only_the_user_can_read_it(tmp_path: Path) -> None:
    assert load_key(tmp_path) is None  # a command doesn't make one
    key = load_key(tmp_path, create=True)
    assert key and len(key) >= 40
    assert stat.S_IMODE((tmp_path / "control.key").stat().st_mode) == 0o600
    assert load_key(tmp_path, create=True) == key == load_key(tmp_path)  # every server shares it
    (tmp_path / "control.key").chmod(0o644)
    assert load_key(tmp_path, create=True) == key
    assert stat.S_IMODE((tmp_path / "control.key").stat().st_mode) == 0o600


def test_behind_a_proxy_only_the_key_makes_a_request_this_machines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wavescribe.web import server as web

    node = Running(make_app(tmp_path))
    try:
        node.server.behind_proxy = True  # serve --behind-proxy: loopback is no proof any more
        node.server.control_key = key = load_key(tmp_path / "data", create=True)
        body = {"label": "x", "scopes": ["read"]}
        assert node.call("POST", "/api/manage/tokens", body)[0] == 403
        wrong = {KEY_HEADER: key[:-1] + ("A" if key[-1] != "A" else "B")}
        assert node.call("POST", "/api/manage/tokens", body, headers=wrong)[0] == 403
        assert node.call("POST", "/api/manage/tokens", body, headers={KEY_HEADER: "é"})[0] == 403
        status, made = node.call("POST", "/api/manage/tokens", body, headers={KEY_HEADER: key})
        assert status == 200 and made["token"].startswith("wst_")
        # A proxy on this machine forwarding the key's header still needs the key itself.
        proxied = {"X-Forwarded-For": "203.0.113.5"}
        assert node.call("GET", "/api/manage/outputs", headers=proxied)[0] == 403
        assert (
            node.call("GET", "/api/manage/outputs", headers={**proxied, KEY_HEADER: key})[0] == 200
        )
        # The key counts from this machine's address only.
        monkeypatch.setattr(web, "_loopback", lambda _host: False)
        assert node.call("GET", "/api/manage/outputs", headers={KEY_HEADER: key})[0] == 403
    finally:
        node.close()


def test_token_make_list_and_revoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    node = Running(make_app(tmp_path))
    data = tmp_path / "data"
    monkeypatch.setattr(token_cli, "DATA_DIR", data)
    url = node.url.replace("127.0.0.1", "localhost")
    try:
        node.server.behind_proxy = True
        node.server.control_key = load_key(data, create=True)
        assert main(["token", "--url", url, "make", "--label", "kitchen screen"]) == 0
        out = capsys.readouterr()
        token = out.out.strip()
        assert token.startswith("wst_") and "kitchen screen (read)" in out.err
        [made] = node.app.tokens_view()
        assert node.app.check_token(token, "read") and made["label"] == "kitchen screen"

        assert main(["token", "--url", url, "list"]) == 0
        listed = capsys.readouterr().out
        assert made["id"] in listed and "kitchen screen" in listed and token not in listed

        with pytest.raises(SystemExit, match="no token 'ffff'"):
            main(["token", "--url", url, "revoke", "ffff"])
        assert main(["token", "--url", url, "revoke", made["id"]]) == 0
        assert node.app.tokens_view() == [] and not node.app.check_token(token, "read")
    finally:
        node.close()


def test_token_refuses_another_machine_and_a_missing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(token_cli, "DATA_DIR", tmp_path)
    with pytest.raises(SystemExit, match="isn't this machine"):
        main(["token", "--url", "http://192.0.2.7:8765", "list"])  # the key never leaves
    with pytest.raises(SystemExit, match="no control key"):
        main(["token", "list"])
    load_key(tmp_path, create=True)
    with pytest.raises(SystemExit, match="no Wavescribe answers"):
        main(["token", "--url", "http://localhost:9", "list"])


def test_the_key_goes_to_this_machine_only(tmp_path: Path) -> None:
    from wavescribe.control import headers_for

    key = load_key(tmp_path, create=True)
    for url in ("http://localhost:8765", "http://127.0.0.1:8790/", "http://[::1]:8765"):
        assert headers_for(url, tmp_path) == {KEY_HEADER: key}, url
    for url in ("http://192.0.2.7:8765", "http://localhost.example.org:8765",
                "http://127.0.0.1@example.org/", "https://kitchen-mac.local:8765"):  # fmt: skip
        assert headers_for(url, tmp_path) == {}, url
    assert headers_for("http://localhost:8765", tmp_path / "elsewhere") == {}


def test_agent_tools_reach_the_controls_behind_a_proxy_with_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wavescribe import control
    from wavescribe.agent.client import Client, Refused

    node = Running(make_app(tmp_path))
    data = tmp_path / "data"
    url = node.url.replace("127.0.0.1", "localhost")
    try:
        node.server.behind_proxy = True
        node.server.control_key = load_key(data, create=True)
        with pytest.raises(Refused) as refused:  # run from a folder without the key
            Client(url).open("GET", "/api/manage/outputs", control=True)
        assert refused.value.status == 403
        monkeypatch.setattr(control, "DATA_DIR", data)  # run from Wavescribe's folder
        with Client(url).open("GET", "/api/manage/outputs", control=True) as resp:
            assert resp.status == 200
    finally:
        node.close()


@pytest.mark.parametrize("port", ["99999", "0", "-1", "http"])
def test_serve_refuses_a_port_that_isnt_one(port: str, capsys) -> None:
    with pytest.raises(SystemExit) as e:
        main(["serve", "--port", port])
    assert e.value.code == 2 and "port" in capsys.readouterr().err
