"""A site adapter that cannot load says why, instead of "not built yet"."""

import sys

import pytest

from carwatch.adapters.base import AdapterError, get_adapter
from carwatch.config import Settings


@pytest.mark.parametrize("site, module", [
    ("olx", "carwatch.adapters.olx"),
    ("mobilede", "carwatch.adapters.mobilede"),
])
def test_a_missing_package_is_named_with_what_to_do(site, module, monkeypatch):
    """The first Mac install: autovit worked, olx and mobile.de claimed to be
    "not built yet" - they were, and a package simply had not loaded."""
    monkeypatch.delitem(sys.modules, module, raising=False)
    monkeypatch.setitem(sys.modules, "selectolax.lexbor", None)  # import fails

    with pytest.raises(AdapterError) as caught:
        get_adapter(site, Settings())

    message = str(caught.value)
    assert "could not load" in message and "selectolax" in message
    assert "run setup again" in message
    assert "not built yet" not in message


def test_autovit_does_not_need_that_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "selectolax.lexbor", None)

    assert get_adapter("autovit", Settings()) is not None
