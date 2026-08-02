from __future__ import annotations

from pathlib import Path

from scripts.package_windows import release_files


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_release_allowlist_excludes_appledouble_files(tmp_path: Path):
    package = tmp_path / "gan_fet"
    scripts = tmp_path / "scripts"
    docs = tmp_path / "docs"
    package.mkdir()
    scripts.mkdir()
    docs.mkdir()
    (package / "__init__.py").write_text("__version__ = '1.0.0'\n")
    (package / "._metadata.py").write_bytes(b"\x00metadata")
    (scripts / "package_windows.py").write_text("# package\n")
    (docs / "architecture.md").write_text("# Architecture\n")
    (docs / "._metadata.md").write_bytes(b"\x00metadata")
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "RUN-GAN-FET.bat").write_text("@echo off\n")

    selected = release_files(tmp_path)

    assert package / "__init__.py" in selected
    assert docs / "architecture.md" in selected
    assert package / "._metadata.py" not in selected
    assert docs / "._metadata.md" not in selected


def test_windows_updater_manages_packaged_documentation_directory():
    launcher = (REPOSITORY_ROOT / "RUN-GAN-FET.bat").read_text(encoding="utf-8")
    managed_paths = launcher.split("$ManagedPaths = @(", 1)[1].split(")", 1)[0]

    assert "'docs'" in managed_paths
