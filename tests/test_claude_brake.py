"""Provider `claude` (headless Claude Code, the weekly plan allowance) runs only when the weekly
brake is green. A fake brake script stands in for the real one: exit 0 green, 1 amber, 2 red,
3 stop. Missing or crashing script means skip (fail safe). Other providers are never affected."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import research_loop as rl
from research_state import WorkflowState

CFG = {'daily_call_budgets': {'gemini': 100, 'agy_claude': 100, 'claude': 100, 'codex': 100},
       'provider_fallback_order': ['gemini', 'agy_claude', 'claude', 'codex'],
       'provider_low_budget_threshold': 0.10}
STATE = {'calls': {'gemini': 0, 'agy_claude': 0, 'claude': 0, 'codex': 0}}


@pytest.fixture
def workflow(tmp_path, monkeypatch):
    wf = WorkflowState(str(tmp_path / 'wf.sqlite3'))
    monkeypatch.setattr(rl, 'get_workflow', lambda: wf)
    return wf


def fake_brake(tmp_path, monkeypatch, exit_code):
    """Install a fake budget.py that exits with exit_code and records each run in calls.txt."""
    script = tmp_path / 'fake_budget.py'
    count = tmp_path / 'calls.txt'
    script.write_text(
        f"import sys\nopen({str(count)!r}, 'a').write('x')\n"
        f"print('fake weekly line')\nsys.exit({exit_code})\n")
    monkeypatch.setattr(rl, 'CLAUDE_BRAKE_SCRIPT', str(script))
    monkeypatch.setitem(rl._brake_cache, 'at', None)
    return count


@pytest.mark.parametrize('code,band,allowed', [
    (0, 'green', True), (1, 'amber', False), (2, 'red', False), (3, 'stop', False),
    (7, 'unavailable', False)])
def test_exit_code_decides_claude(tmp_path, monkeypatch, workflow, capsys, code, band, allowed):
    fake_brake(tmp_path, monkeypatch, code)
    assert rl.claude_weekly_band() == band
    assert rl.provider_eligible(CFG, STATE, 'claude') is allowed
    out = capsys.readouterr().out
    if allowed:
        assert 'claude skipped' not in out
    else:
        assert f'claude skipped: weekly band {band}' in out


def test_missing_brake_script_skips_claude(tmp_path, monkeypatch, workflow, capsys):
    monkeypatch.setattr(rl, 'CLAUDE_BRAKE_SCRIPT', str(tmp_path / 'nope.py'))
    monkeypatch.setitem(rl._brake_cache, 'at', None)
    assert rl.provider_eligible(CFG, STATE, 'claude') is False
    assert 'claude skipped: weekly band unavailable' in capsys.readouterr().out


def test_other_providers_never_asked_about_the_brake(tmp_path, monkeypatch, workflow):
    count = fake_brake(tmp_path, monkeypatch, 3)
    for p in ('gemini', 'agy_claude', 'codex'):
        assert rl.provider_eligible(CFG, STATE, p) is True
    assert not count.exists()  # the brake script never ran


def test_answer_cached_and_skip_logged_once(tmp_path, monkeypatch, workflow, capsys):
    count = fake_brake(tmp_path, monkeypatch, 3)
    for _ in range(20):
        assert rl.provider_eligible(CFG, STATE, 'claude') is False
    assert count.read_text() == 'x'  # one run for 20 checks
    assert capsys.readouterr().out.count('claude skipped') == 1


def test_cache_expires_after_ten_minutes(tmp_path, monkeypatch, workflow):
    count = fake_brake(tmp_path, monkeypatch, 3)
    assert rl.claude_weekly_band(now=1000.0) == 'stop'
    assert rl.claude_weekly_band(now=1000.0 + 599) == 'stop'
    assert count.read_text() == 'x'
    fake_brake(tmp_path, monkeypatch, 0)  # week reset: green now
    monkeypatch.setitem(rl._brake_cache, 'at', 1000.0)
    assert rl.claude_weekly_band(now=1000.0 + 601) == 'green'


def test_select_provider_falls_back_when_not_green(tmp_path, monkeypatch, workflow):
    fake_brake(tmp_path, monkeypatch, 3)
    state = {'calls': {'gemini': 100, 'agy_claude': 100, 'claude': 0, 'codex': 0}}
    assert rl.select_provider(CFG, state, 'claude') == 'codex'
    assert rl.select_provider(CFG, state, 'claude', excluded=('codex',)) is None


def test_select_provider_picks_claude_when_green(tmp_path, monkeypatch, workflow):
    fake_brake(tmp_path, monkeypatch, 0)
    state = {'calls': {'gemini': 100, 'agy_claude': 100, 'claude': 0, 'codex': 100}}
    assert rl.select_provider(CFG, state, 'claude') == 'claude'
