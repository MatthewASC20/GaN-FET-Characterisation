"""Build and publish a minimal, checksummed Windows source release."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

DEFAULT_RELEASE_SHARE = Path(
    r"C:\Bench_Software\Projects\Matthew_De_Jesus_Python_Sandbox"
    r"\GaN-FET-Releases"
)
VERSION_PATTERN = re.compile(
    r'(?m)^__version__\s*=\s*["\'](\d+\.\d+\.\d+)["\']\s*$'
)
ROOT_RELEASE_FILES = (
    "README.md",
    "RUN-GAN-FET.bat",
    "PACKAGE-GAN-FET.bat",
    "pyproject.toml",
)


def read_version(version_source: Path) -> str:
    match = VERSION_PATTERN.search(version_source.read_text(encoding="utf-8"))
    if not match:
        raise ValueError(f"Could not parse __version__ from {version_source}")
    return match.group(1)


def bump_version_string(current: str, bump_type: str) -> str:
    parts = [int(part) for part in current.split(".")]
    if len(parts) != 3:
        raise ValueError(f"Invalid semantic version: {current!r}")
    if bump_type == "patch":
        parts[2] += 1
    elif bump_type == "minor":
        parts[1:] = [parts[1] + 1, 0]
    elif bump_type == "major":
        parts = [parts[0] + 1, 0, 0]
    else:
        raise ValueError(f"Unknown bump type: {bump_type!r}")
    return ".".join(str(part) for part in parts)


def update_version(version_source: Path, new_version: str) -> None:
    if re.fullmatch(r"\d+\.\d+\.\d+", new_version) is None:
        raise ValueError("Version must have the form X.Y.Z")
    original = version_source.read_text(encoding="utf-8")
    updated, count = VERSION_PATTERN.subn(
        f'__version__ = "{new_version}"', original
    )
    if count != 1:
        raise ValueError(
            f"Expected one canonical __version__ in {version_source}, found {count}"
        )
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=version_source.parent,
        prefix=f".{version_source.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, version_source)
    finally:
        temp_path.unlink(missing_ok=True)


def compute_sha256(filepath: Path) -> str:
    hasher = hashlib.sha256()
    with filepath.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def release_files(root_dir: Path) -> list[Path]:
    """Return the explicit source allowlist; runtime/user data cannot enter it."""
    files = [
        path
        for path in (root_dir / "gan_fet").rglob("*.py")
        if "__pycache__" not in path.parts and not path.name.startswith("._")
    ]
    files.extend(
        path
        for path in (root_dir / "docs").rglob("*.md")
        if not path.name.startswith("._")
    )
    files.append(root_dir / "scripts" / "package_windows.py")
    files.extend(
        root_dir / name
        for name in ROOT_RELEASE_FILES
        if (root_dir / name).is_file()
    )
    missing = [
        path
        for path in (
            root_dir / "gan_fet" / "__init__.py",
            root_dir / "pyproject.toml",
            root_dir / "RUN-GAN-FET.bat",
        )
        if not path.is_file()
    ]
    if missing:
        raise FileNotFoundError(
            "Required release files are missing: "
            + ", ".join(str(path) for path in missing)
        )
    return sorted(set(files), key=lambda path: path.as_posix())


def _publish_atomically(source: Path, destination: Path) -> None:
    temp = destination.with_name(f".{destination.name}.tmp")
    try:
        shutil.copy2(source, temp)
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)


def package_release(
    root_dir: Path,
    output_dir: Path,
    network_share: Path | None,
    bump: str | None = None,
    set_version: str | None = None,
) -> Path:
    root_dir = root_dir.resolve()
    output_dir = output_dir.resolve()
    version_source = root_dir / "gan_fet" / "__init__.py"
    current_version = read_version(version_source)
    new_version = (
        set_version
        or (
            bump_version_string(current_version, bump)
            if bump is not None
            else current_version
        )
    )
    if re.fullmatch(r"\d+\.\d+\.\d+", new_version) is None:
        raise ValueError("Version must have the form X.Y.Z")

    package_name = f"GaN-FET-Characterisation-v{new_version}-windows"
    zip_path = output_dir / f"{package_name}.zip"
    sha_path = output_dir / f"{package_name}.zip.sha256"
    temp_zip = output_dir / f".{zip_path.name}.tmp"
    temp_sha = output_dir / f".{sha_path.name}.tmp"
    version_changed = new_version != current_version
    published_paths: list[Path] = []

    output_dir.mkdir(parents=True, exist_ok=True)
    if zip_path.exists() or sha_path.exists():
        raise FileExistsError(
            f"Release artifacts already exist for v{new_version}; "
            "versions are immutable"
        )
    try:
        if version_changed:
            update_version(version_source, new_version)

        with zipfile.ZipFile(
            temp_zip, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for source in release_files(root_dir):
                relative = source.relative_to(root_dir)
                archive.write(source, arcname=Path(package_name) / relative)

        digest = compute_sha256(temp_zip)
        temp_sha.write_text(
            f"{digest}  {zip_path.name}\n", encoding="ascii"
        )
        os.replace(temp_zip, zip_path)
        os.replace(temp_sha, sha_path)

        if network_share is not None:
            network_share.mkdir(parents=True, exist_ok=True)
            network_sha = network_share / sha_path.name
            network_zip = network_share / zip_path.name
            if network_sha.exists() or network_zip.exists():
                raise FileExistsError(
                    f"Network release v{new_version} already exists"
                )
            # The release scanner only sees ZIP files. Publish its checksum
            # first, then make the ZIP visible as the final commit step.
            _publish_atomically(sha_path, network_sha)
            published_paths.append(network_sha)
            _publish_atomically(zip_path, network_zip)
            published_paths.append(network_zip)

        print(f"Built {zip_path}")
        print(f"SHA256: {digest}")
        if network_share is not None:
            print(f"Published to {network_share}")
        return zip_path
    except Exception:
        if version_changed:
            update_version(version_source, current_version)
        for published in reversed(published_paths):
            published.unlink(missing_ok=True)
        for artifact in (temp_zip, temp_sha, zip_path, sha_path):
            artifact.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Package GaN FET Characterization for Windows"
    )
    versions = parser.add_mutually_exclusive_group()
    versions.add_argument(
        "--bump", choices=["patch", "minor", "major"], help="bump version"
    )
    versions.add_argument(
        "--set-version", help="set explicit semantic version X.Y.Z"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).parent.parent / "dist",
        help="local output directory",
    )
    parser.add_argument(
        "--network-share",
        type=Path,
        default=DEFAULT_RELEASE_SHARE,
        help="network release share",
    )
    parser.add_argument(
        "--no-network",
        action="store_true",
        help="build locally without publishing",
    )
    args = parser.parse_args(argv)

    try:
        package_release(
            root_dir=Path(__file__).parent.parent,
            output_dir=args.output_dir,
            network_share=None if args.no_network else args.network_share,
            bump=args.bump,
            set_version=args.set_version,
        )
        return 0
    except Exception as exc:
        print(f"Packaging failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
