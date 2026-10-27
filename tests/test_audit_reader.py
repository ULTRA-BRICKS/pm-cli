"""The reader seam: the format marker a reader stamps on a native payload."""

from __future__ import annotations

import pytest

from pmlib.chatgpt_control import audit_reader as reader


def test_mark_native_returns_a_stamped_copy():
    payload = {"messages": []}
    marked = reader.mark_native(payload, "flat")
    assert marked == {"messages": [], "native_format": "flat"}
    assert "native_format" not in payload


def test_mark_native_overwrites_a_server_supplied_marker():
    assert reader.mark_native({"native_format": "mapping"}, "flat")["native_format"] == "flat"


@pytest.mark.parametrize("bad", ["", "hybrid", "FLAT", None, 3])
def test_mark_native_refuses_an_unknown_format(bad):
    with pytest.raises(ValueError):
        reader.mark_native({}, bad)
