from __future__ import annotations

import json

import pytest

from gan_fet.core.models import MatrixPoint
from gan_fet.settings import GoogleSettings
from gan_fet.sheets import drive
from gan_fet.sheets.sync import SheetsSync, UnmappedSheetPoint, column_letter


def test_column_letters_cover_multi_letter_columns():
    assert column_letter(1) == "A"
    assert column_letter(26) == "Z"
    assert column_letter(27) == "AA"


def test_template_mapping_is_explicit_for_supported_point(matrix_point):
    cells = SheetsSync._cells_for_point(matrix_point)

    assert cells["vin"] == "25°C!V11"
    assert cells["vds_pk"] == "25°C!Y11"


def test_unmapped_custom_matrix_point_fails_clearly(matrix_point):
    custom = MatrixPoint(
        device_name=matrix_point.device_name,
        config=matrix_point.config,
        frequency_hz=matrix_point.frequency_hz,
        duty_pct=33,
        temperature_c=matrix_point.temperature_c,
        voltage_v=250,
    )

    with pytest.raises(UnmappedSheetPoint, match="duty 33"):
        SheetsSync._cells_for_point(custom)


def test_mapping_file_round_trip_is_atomic(tmp_path):
    settings = GoogleSettings(mapping_file=str(tmp_path / "mapping.json"))
    mappings = {drive.mapping_key("device_name_with_underscores", 13_500_000): "sheet-id"}

    drive.save_mappings(settings, mappings)

    assert drive.load_mappings(settings) == mappings
    assert json.loads((tmp_path / "mapping.json").read_text()) == mappings


def test_disabled_sync_never_starts_worker(tmp_path):
    sync = SheetsSync(
        GoogleSettings(enabled=False, credentials_file=str(tmp_path / "missing.json")),
        [13_000_000],
    )

    sync.enqueue_device_setup("TEST-FET")

    assert sync._worker is None
    assert sync.close(timeout=0.1)
