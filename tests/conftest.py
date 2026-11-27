# SPDX-License-Identifier: Apache-2.0
"""What every test shares: nothing taken from the machine the tests run on."""

import os
from collections.abc import Iterator

import pytest

from wavescribe.radio import bandplan

_DEFAULT_REGION = bandplan.default_region  # tests may swap it; its cache is this one's


@pytest.fixture(autouse=True)
def _a_fixed_country(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """With no radio region chosen, the band plan follows the computer's region setting. Tests
    get the same one on every machine, a CEPT country, and a test about another sets its own.
    WAVESCRIBE_TEST_COUNTRY runs them all as a computer set to another country would."""
    country = os.environ.get("WAVESCRIBE_TEST_COUNTRY", "DE")
    monkeypatch.setattr(bandplan, "system_country", lambda: country)
    _DEFAULT_REGION.cache_clear()  # kept for a run: each test starts from its own
    yield
    _DEFAULT_REGION.cache_clear()
