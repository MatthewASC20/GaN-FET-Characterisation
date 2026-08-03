from __future__ import annotations

from gan_fet.instruments.discovery import probe_configured_instruments
from gan_fet.instruments.rig import (
    RigConfigurationError,
    build_instrument_rig,
    resolve_instrument_roles,
)
from gan_fet.settings import SCOPE_INSTRUMENT_KEY, Settings


class _Connection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_fast_probe_checks_tcp_and_skips_direct_visa(settings):
    calls = []

    def connect(endpoint, *, timeout):
        calls.append((endpoint, timeout))
        return _Connection()

    report = probe_configured_instruments(settings, connector=connect)

    assert report.reachable
    assert report.checked_tcp == ["SDG6022X"]
    assert report.skipped_non_tcp == [SCOPE_INSTRUMENT_KEY, "K2410"]
    assert [endpoint for endpoint, _ in calls] == [
        ("10.11.13.230", 5025),
    ]


def test_fast_probe_reports_failure_without_aborting_other_endpoints(settings):
    calls = []

    def connect(endpoint, *, timeout):
        calls.append(endpoint)
        if endpoint == ("10.11.13.230", 5025):
            raise OSError("offline")
        return _Connection()

    report = probe_configured_instruments(settings, connector=connect)

    assert not report.reachable
    assert len(report.failures) == 1
    assert report.failures[0].instrument == "SDG6022X"
    assert len(calls) == 1


def test_simulated_rig_shares_one_electrical_plant(settings):
    rig = build_instrument_rig(settings, simulate=True)
    try:
        assert resolve_instrument_roles(settings)["scope"] == SCOPE_INSTRUMENT_KEY
        assert rig.wavegen.client.shared_plant is rig.scope.client.shared_plant
        assert rig.scope.client.shared_plant is rig.smu.client.shared_plant
        assert rig.scope.client.name == SCOPE_INSTRUMENT_KEY
        assert rig.dmm is None
    finally:
        rig.close_clients()


def test_missing_required_role_has_actionable_error():
    settings = Settings(instruments={})

    try:
        resolve_instrument_roles(settings)
    except RigConfigurationError as exc:
        assert "oscilloscope" in str(exc)
    else:
        raise AssertionError("missing role must be rejected")
