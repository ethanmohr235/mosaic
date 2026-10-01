"""api_common.py -- the boilerplate the console's and the Gardens
window's bridges share.

synchronized() is already exercised indirectly through the Gardens
window's test suite, one @_synchronized method at a time. What earns a dedicated test here is open_app(): the one piece of
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
    result = api_common.open_app("console", "test")
    (args, kwargs) = calls[0]
    # The venv the app is running in, not a bare `python` off PATH --
    # see docs/ui-setup.md.
    assert args[0] == sys.executable
    assert args[1].endswith("run_console.py")
    assert args[2:] == ["--garden", "test"]
    assert kwargs == {"quiet": False}
    assert result == {"app": "console", "garden": "test", "focus": None}


def test_open_app_passes_the_focus_flag_each_script_expects(monkeypatch):
    """No window left takes one today; an app that does gets it."""
    calls = _launched(monkeypatch)
    monkeypatch.setitem(api_common.APPS, "focused", ("Focused", api_common.REPO_ROOT / "run_x.py", "--focus"))
    api_common.open_app("focused", "test", "mote-1")
    assert calls[-1][0][-2:] == ["--focus", "mote-1"]


def test_open_app_ignores_mote_id_where_the_script_has_no_flag_for_it(monkeypatch):
    """run_console.py takes --garden and no focus flag (the console's
    selection lives in its own store) -- see api_common.APPS."""
    calls = _launched(monkeypatch)
    api_common.open_app("console", "test", "mote-1")
    _name, script, _flag = api_common.APPS["console"]
    assert calls[-1][0] == [sys.executable, str(script), "--garden", "test"]
