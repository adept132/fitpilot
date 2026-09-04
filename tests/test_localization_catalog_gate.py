import pytest

from scripts import localization_catalog_gate as gate


@pytest.mark.asyncio
async def test_gate_applies_then_checks_catalog(monkeypatch):
    calls = []

    async def fake_run(apply, translations_path):
        calls.append(apply)
        return 0

    monkeypatch.setattr(gate.backfill, "_run", fake_run)

    assert await gate.run_gate() == 0
    assert calls == [True, False]


@pytest.mark.asyncio
async def test_gate_stops_before_check_when_apply_fails(monkeypatch):
    calls = []

    async def fake_run(apply, translations_path):
        calls.append(apply)
        return 1

    monkeypatch.setattr(gate.backfill, "_run", fake_run)

    assert await gate.run_gate() == 1
    assert calls == [True]
