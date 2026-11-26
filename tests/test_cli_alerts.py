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
