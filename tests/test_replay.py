# SPDX-License-Identifier: Apache-2.0
"""Replaying stored transcripts through rules: counts what would be announced, reaches nobody."""

from pathlib import Path

import pytest

from tests.test_alerts import CONFIG
from tests.test_rules import pack, tracked_example
from wavescribe.alerts import parse_alerts
from wavescribe.alerts.replay import replay


def rows(lines: list[tuple[float, str]], lang: str = "pt-PT") -> list[tuple]:
    return [(t, "a", lang, text, ()) for t, text in lines]


def test_replay_counts_each_alert_once_at_its_highest_severity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_real_actions() -> None:
        raise AssertionError("a replay must not reach a person")

    monkeypatch.setattr("wavescribe.alerts.platform_actions", no_real_actions)
    rule = {"id": "water", "topics": ["water"], "severity": "high", "min_mentions": 2}
    config = parse_alerts({**CONFIG, "rule": [rule]}, None, pack(tmp_path))
    report = replay(config, rows([
        (0.0, "Corte de água amanhã."),       # held at low
        (60.0, "Sem água desde manhã."),      # confirmed: high
        (2000.0, "Corte de água amanhã."),    # cooldown over: a new alert, held
        (2100.0, "Hoje está sol."),
    ]))  # fmt: skip
    assert report.utterances == 4 and report.hits["water"] == 3
    assert report.announced == {("high", "water"): 1, ("low", "water"): 1}
    assert report.by_severity() == {"critical": 0, "high": 1, "medium": 0, "low": 1}
    assert report.hours == pytest.approx(2100 / 3600)


# Synthetic bulletins written for this test (never broadcast text): the tracked example rules
# must still escalate real emergencies, and keep quiet on the soak's false alarms.
EMERGENCIES = {
    ("high", "power"): ["Apagão em todo o país desde as onze e meia.",
                        "A REN diz que a reposição da energia pode demorar horas.",
                        "Continuamos sem eletricidade em quase todo o território."],
    ("critical", "civil-protection"): ["O Governo declarou situação de calamidade.",
                                       "Repetimos: foi declarada situação de calamidade."],
    ("critical", "water"): ["Na Vila Exemplo, a câmara pede para ferver a água antes de beber."],
    ("high", "water"): ["As autoridades pedem para ferver a água antes de beber.",
                        "A água da rede está imprópria para consumo até nova ordem."],
}  # fmt: skip
EVERYDAY = [
    "O carro tinha tornado a viagem mais longa.",  # tornado: "become"
    "As águias ganharam ao intervalo.",  # água: "eagles"
    "O cartão ajuda pessoas em situação de sem abrigo.",  # abrigo
    "Confina pneus ao melhor preço, mude já.",  # confinamento: a brand
    "A Proteção Civil apresentou a nova lei orgânica.",  # everyday news
    "Um incêndio numa casa em Aveiro deixou quatro desalojados.",  # far away
    "No debate falou-se do apagão do ano passado.",  # one passing mention
    "O avançado parte para o ataque.",  # football
    "A sala estava cheia de gente.",  # cheia: "full"
]


@pytest.mark.parametrize("expected", list(EMERGENCIES))
def test_tracked_rules_still_escalate_emergencies(tmp_path: Path, expected: tuple) -> None:
    report = replay(tracked_example(tmp_path), rows(
        [(30.0 * i, s) for i, s in enumerate(EMERGENCIES[expected])]
    ))  # fmt: skip
    assert expected in report.announced


def test_tracked_rules_keep_quiet_on_everyday_talk(tmp_path: Path) -> None:
    report = replay(tracked_example(tmp_path), rows(
        [(600.0 * i, s) for i, s in enumerate(EVERYDAY)]
    ))  # fmt: skip
    assert {s for s, _ in report.announced} <= {"low"}, report.examples
