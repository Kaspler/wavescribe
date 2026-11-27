# SPDX-License-Identifier: Apache-2.0
"""`wavescribe alerts check` answers with the rules the server runs, the page's included."""

from pathlib import Path

import pytest

from wavescribe.cli import main


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config" / "keywords").mkdir(parents=True)
    (tmp_path / "config" / "keywords" / "pt.toml").write_text(
        'language = "pt"\n[topics]\nwater = ["corte de água"]\n', encoding="utf-8"
    )
    (tmp_path / "config" / "alerts.toml").write_text(
        '[[rule]]\nid = "water"\ntopics = ["water"]\nseverity = "medium"\n', encoding="utf-8"
    )
    return tmp_path


def test_alerts_check_uses_the_rules_made_on_the_page(project: Path, capsys) -> None:
    (project / "config" / "ui.toml").write_text(
        '[[alerts.rule]]\nid = "bridge"\nseverity = "high"\n'
        '[alerts.rule.terms]\nes = ["puente cerrado"]\n'
        '[alerts]\ndisabled_rules = ["water"]\n',
        encoding="utf-8",
    )
    assert main(["alerts", "check", "--language", "es", "El", "puente", "cerrado", "hoy"]) == 0
    out = capsys.readouterr()
    assert "bridge" in out.out and "[puente cerrado]" in out.out
    assert "pack" not in out.err  # no word about topic lists when a rule matched
    # A file rule switched off on the page is off here too.
    assert main(["alerts", "check", "--language", "pt", "Corte de água amanhã"]) == 1
    assert "no rule matches" in capsys.readouterr().err


def test_alerts_check_says_plainly_when_a_language_has_no_topic_words(
    project: Path, capsys
) -> None:
    assert main(["alerts", "check", "--language", "es", "Corte", "de", "agua"]) == 1
    err = capsys.readouterr().err
    assert "no rule matches" in err and "Spanish" in err and "config/keywords/es.toml" in err


def test_listen_counts_every_kind_of_rule_as_serve_does(project: Path, capsys) -> None:
    import argparse

    from wavescribe.cli import open_alerter

    with (project / "config" / "alerts.toml").open("a", encoding="utf-8") as f:
        f.write('[[rule]]\nid = "leave"\nkind = "llm"\nprompt = "people are told to leave"\n'
                '[[rule]]\nid = "quiet"\nkind = "silence"\nafter_minutes = 5\n')  # fmt: skip
    args = argparse.Namespace(no_alerts=False, alerts=None)
    alerter = open_alerter(args, {}, None)
    assert alerter is not None
    try:
        err = capsys.readouterr().err
        assert "# alerts: 1 keyword, 1 LLM and 1 station rules (" in err
        assert "LLM rules are checked under serve only" in err  # listen runs no LLM for them
    finally:
        alerter.close()


def test_check_and_replay_read_the_page_settings_serve_was_given(project: Path, capsys) -> None:
    """serve --settings elsewhere: check and replay take the same option, so they answer with
    that page's rules, not the default file's."""
    from tests.test_store import result
    from wavescribe.store import Store

    elsewhere = project / "other-ui.toml"
    elsewhere.write_text(
        '[[alerts.rule]]\nid = "bridge"\nseverity = "high"\n'
        '[alerts.rule.terms]\nes = ["puente cerrado"]\n',
        encoding="utf-8",
    )
    said = ["alerts", "check", "--language", "es", "El puente cerrado"]
    assert main(said) == 1  # config/ui.toml has no such rule
    assert main([*said, "--settings", str(elsewhere)]) == 0
    assert "bridge" in capsys.readouterr().out

    (project / "data").mkdir()
    store = Store(project / "data" / "wavescribe.db")
    store.add_transcript(result("a", "El puente cerrado hasta mañana", 1000.0, lang="es"))
    store.close()
    capsys.readouterr()
    assert main(["alerts", "replay", "--settings", str(elsewhere)]) == 0
    assert "1 high" in capsys.readouterr().out


def test_replay_says_what_its_rate_counts() -> None:
    from wavescribe.cli import _announced_per_day

    actions = {"high": ("notify", "speak:en", "log"), "medium": ("notify",), "low": ("log",)}
    by_severity = {"critical": 0, "high": 2, "medium": 1, "low": 5}
    said = _announced_per_day(actions, by_severity, 24 / 6)
    assert said == "announced: 12.0 alerts a day (high, medium); only logged: low"
    assert "[" not in said
