from __future__ import annotations

from pathlib import Path

import pytest

from gan_fet.core.models import MatrixPoint
from gan_fet.settings import Settings
from gan_fet.storage.db import Database


@pytest.fixture
def matrix_point() -> MatrixPoint:
    return MatrixPoint(
        device_name="TEST-FET",
        config="Single Device",
        frequency_hz=13_000_000,
        duty_pct=50,
        temperature_c=25,
        voltage_v=200,
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    configured = Settings()
    configured.data_dir = str(tmp_path / "data")
    configured._settings_path = tmp_path / "settings.json"
    configured._data_base_dir = tmp_path
    return configured


@pytest.fixture
def database(tmp_path: Path):
    project = tmp_path / "project"
    db = Database(project / "gan_fet.db")
    try:
        yield db
    finally:
        db.close()
