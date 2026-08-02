from __future__ import annotations

from pathlib import Path

from gan_fet.storage.migrate import migrate_legacy_tree


def _legacy_path(root: Path) -> Path:
    path = root / "TEST-FET" / "13MHz" / "50"
    path.mkdir(parents=True)
    return path / "TEST-FET_Single Device_25C_13MHz_200V_50duty.csv"


def test_empty_final_marker_is_preserved_as_partial(database, tmp_path):
    root = tmp_path / "Device Data"
    csv_path = _legacy_path(root)
    csv_path.write_text(
        "Timestamp,Device,Temp,Freq,Volt,Config,Duty,Current\n"
        "2025-01-01 00:00:00,TEST-FET,25,13000000,200,Single Device,50,0.1\n"
        "FINAL_READINGS,,,,,,\n"
    )

    report = migrate_legacy_tree(database, root)
    run = database.runs_for_device("TEST-FET")[0]

    assert report.runs_partial == 1
    assert run.status == "legacy_partial"


def test_legacy_migration_is_idempotent(database, tmp_path):
    root = tmp_path / "Device Data"
    csv_path = _legacy_path(root)
    csv_path.write_text(
        "2025-01-01 00:00:00,TEST-FET,25,13000000,200,Single Device,50,0.1\n"
        "FINAL_READINGS,60,0.2,0.1,13000000,0.05,200\n"
    )

    first = migrate_legacy_tree(database, root)
    second = migrate_legacy_tree(database, root)

    assert first.runs_imported == 1
    assert second.runs_imported == 0
    assert second.runs_skipped_existing == 1


def test_incomplete_final_marker_can_be_reimported_after_file_is_repaired(
    database, tmp_path
):
    root = tmp_path / "Device Data"
    csv_path = _legacy_path(root)
    csv_path.write_text(
        "2025-01-01 00:00:00,TEST-FET,25,13000000,200,Single Device,50,0.1\n"
        "FINAL_READINGS,60,,,,,\n"
    )

    partial = migrate_legacy_tree(database, root)
    assert partial.runs_partial == 1
    assert database.runs_for_device("TEST-FET")[0].status == "legacy_partial"

    csv_path.write_text(
        "2025-01-01 00:00:00,TEST-FET,25,13000000,200,Single Device,50,0.1\n"
        "FINAL_READINGS,60,0.2,0.1,13000000,,200\n"
    )
    repaired = migrate_legacy_tree(database, root)

    assert repaired.runs_imported == 1
    preferred = database.runs_for_device("TEST-FET")[0]
    assert preferred.status == "completed"
    assert preferred.attempt_no == 2
