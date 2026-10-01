from __future__ import annotations

from importlib.metadata import EntryPoint
from typing import Any

import pytest

from ta_plugin_api import discovery
from ta_plugin_api.discovery import EXECUTION_GROUP, available, load_providers
from ta_plugin_api.errors import PluginError


class _Factory:
    def __init__(self, name: str) -> None:
        self.name = name

    def missing_settings(self, settings: Any) -> list[str]:
        return []


GOOD = _Factory("good")
MISNAMED = _Factory("other")
NOT_A_FACTORY = object()


def _broken() -> None:
    raise ImportError("MetaTrader5 is not installed")


def _point(name: str, attr: str) -> EntryPoint:
    return EntryPoint(name=name, value=f"{__name__}:{attr}", group=EXECUTION_GROUP)


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch) -> list[EntryPoint]:
    points: list[EntryPoint] = []
    monkeypatch.setattr(discovery, "entry_points", lambda group: [p for p in points])
    return points


def test_loads_the_named_provider(published: list[EntryPoint]) -> None:
    published.append(_point("good", "GOOD"))

    assert load_providers(EXECUTION_GROUP, ["good"]) == {"good": GOOD}


def test_available_lists_names_without_importing(published: list[EntryPoint]) -> None:
    published.append(_point("good", "GOOD"))
    published.append(EntryPoint(name="lazy", value="not.importable:X", group=EXECUTION_GROUP))

    assert available(EXECUTION_GROUP) == {"good", "lazy"}


def test_unknown_provider_fails_closed(published: list[EntryPoint]) -> None:
    published.append(_point("good", "GOOD"))

    with pytest.raises(PluginError, match="no provider 'mt5'.*available: good"):
        load_providers(EXECUTION_GROUP, ["mt5"])


def test_a_provider_that_fails_to_import_fails_closed(published: list[EntryPoint]) -> None:
    published.append(EntryPoint(name="good", value="not.importable:X", group=EXECUTION_GROUP))

    with pytest.raises(PluginError, match="failed to load"):
        load_providers(EXECUTION_GROUP, ["good"])


def test_duplicate_publication_fails_closed(published: list[EntryPoint]) -> None:
    published.append(_point("good", "GOOD"))
    published.append(_point("good", "MISNAMED"))

    with pytest.raises(PluginError, match="published twice"):
        load_providers(EXECUTION_GROUP, ["good"])


def test_the_same_publication_seen_twice_is_not_a_duplicate(
    published: list[EntryPoint],
) -> None:
    published.append(_point("good", "GOOD"))
    published.append(_point("good", "GOOD"))

    assert load_providers(EXECUTION_GROUP, ["good"]) == {"good": GOOD}


def test_entry_point_name_must_match_factory_name(published: list[EntryPoint]) -> None:
    published.append(_point("good", "MISNAMED"))

    with pytest.raises(PluginError, match="resolves to 'other'"):
        load_providers(EXECUTION_GROUP, ["good"])


def test_rejects_an_object_that_is_not_a_factory(published: list[EntryPoint]) -> None:
    published.append(_point("good", "NOT_A_FACTORY"))

    with pytest.raises(PluginError, match="not a ProviderFactory"):
        load_providers(EXECUTION_GROUP, ["good"])
