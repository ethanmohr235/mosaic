"""api_common.py -- the boilerplate shared by every app's Api bridge.

synchronized() and coerce_properties() are already exercised indirectly
through every other app's test suite, one @_synchronized method at a
time. What earns a dedicated test here is open_app(): the one piece of
behavior that reaches outside this process, so it needs its own check
that it builds the right command rather than trusting each app's own
test suite to notice a typo in someone else's script name.
"""
import sys

import api_common


def _launched(monkeypatch):
    calls = []
    monkeypatch.setattr(
        api_common, "popen_detached", lambda args, **kwargs: calls.append((args, kwargs))
    )
    return calls


def test_open_app_launches_the_right_script(monkeypatch):
    calls = _launched(monkeypatch)
    result = api_common.open_app("monastery", "test")
    (args, kwargs) = calls[0]
    # The venv the app is running in, not a bare `python` off PATH --
    # see docs/ui-setup.md.
    assert args[0] == sys.executable
    assert args[1].endswith("run_monastery.py")
    assert args[2:] == ["--garden", "test"]
    assert kwargs == {"quiet": False}
    assert result == {"app": "monastery", "garden": "test", "focus": None}


def test_open_app_passes_the_focus_flag_each_script_expects(monkeypatch):
    calls = _launched(monkeypatch)
    api_common.open_app("mora", "test", "mote-1")
    assert calls[-1][0][-2:] == ["--note", "mote-1"]

    api_common.open_app("monastery", "test", "mote-1")
    assert calls[-1][0][-2:] == ["--focus", "mote-1"]

    api_common.open_app("motion", "test", "mote-1")
    assert calls[-1][0][-2:] == ["--focus", "mote-1"]


def test_open_app_ignores_mote_id_where_the_script_has_no_flag_for_it(monkeypatch):
    """run_monarch.py takes no such flag -- see api_common.APPS."""
    calls = _launched(monkeypatch)
    api_common.open_app("monarch", "test", "mote-1")
    _name, script, _flag = api_common.APPS["monarch"]
    assert calls[-1][0] == [sys.executable, str(script), "--garden", "test"]


def test_open_app_can_open_the_console(monkeypatch):
    """An old window opening Mosaic: run_console.py takes --garden and no
    focus flag (the console's selection lives in its own store)."""
    calls = _launched(monkeypatch)
    api_common.open_app("console", "test", "mote-1")
    assert calls[-1][0][1].endswith("run_console.py")
    assert calls[-1][0][2:] == ["--garden", "test"]
