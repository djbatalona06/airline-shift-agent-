"""browsers_available() must catch a frozen build shipped without its
browsers/ folder - see main.py for why the env-var check alone fail-opens.
"""

from __future__ import annotations

from shift_agent import main as m


def _freeze(monkeypatch, executable):
    monkeypatch.setattr(m.sys, "frozen", True, raising=False)
    monkeypatch.setattr(m.sys, "executable", str(executable))


def test_source_install_assumes_playwrights_own_default(monkeypatch):
    monkeypatch.delattr(m.sys, "frozen", raising=False)
    assert m.browsers_available() is True


def test_frozen_with_browsers_folder_present(tmp_path, monkeypatch):
    exe = tmp_path / "ShiftAgent.exe"
    (tmp_path / "browsers" / "chromium-1187").mkdir(parents=True)
    _freeze(monkeypatch, exe)
    assert m.browsers_available() is True


def test_frozen_with_browsers_folder_missing(tmp_path, monkeypatch):
    exe = tmp_path / "ShiftAgent.exe"
    _freeze(monkeypatch, exe)
    assert m.browsers_available() is False


def test_frozen_with_empty_browsers_folder(tmp_path, monkeypatch):
    exe = tmp_path / "ShiftAgent.exe"
    (tmp_path / "browsers").mkdir()
    _freeze(monkeypatch, exe)
    assert m.browsers_available() is False
