"""Explicit Windows installation of the optional AutoCAD instance publisher.

No installation runs at import, server startup or pip's build hook. The Windows
installer invokes this after pip; an installed server can invoke it separately.
Only the current user's owned .bundle is changed. AutoCAD/client configuration
and trust/security settings are never edited.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

BUNDLE_NAME = "AutoCAD.MCP.InstanceBridge.bundle"
OWNER = "autocad-mcp-pro.instance-bridge"
PRODUCT_CODE = "{6BB562D4-090B-48A0-9F17-632562ACCF53}"
DLL_NAME = "AutoCadMcp.InstancePublisher.dll"
MANIFEST = "autocad-mcp-install.json"
SUPPORTED_SERIES = {2022: "R24.1", 2023: "R24.2", 2024: "R24.3"}
SOURCE = Path(__file__).with_name("instance_bridge") / "InstancePublisher.cs"
REFERENCES = ("accoremgd.dll", "acdbmgd.dll", "acmgd.dll")


@dataclass(frozen=True)
class AutoCADInstall:
    year: int
    directory: Path


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def default_plugins_root() -> Path:
    roaming = os.environ.get("APPDATA")
    if not roaming or not Path(roaming).is_absolute():
        raise ValueError("APPDATA must name an absolute current-user folder.")
    return Path(roaming) / "Autodesk" / "ApplicationPlugins"


def detect_installs() -> list[AutoCADInstall]:
    """Installed, supported releases from the 64-bit Autodesk registry; read only."""
    import winreg

    found: dict[tuple[int, str], AutoCADInstall] = {}
    try:
        root = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Autodesk\AutoCAD",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        )
    except FileNotFoundError:
        return []
    with root:
        for year, series in SUPPORTED_SERIES.items():
            try:
                version = winreg.OpenKey(root, series)
            except FileNotFoundError:
                continue
            with version:
                for i in range(winreg.QueryInfoKey(version)[0]):
                    with winreg.OpenKey(version, winreg.EnumKey(version, i)) as product:
                        try:
                            folder = Path(winreg.QueryValueEx(product, "AcadLocation")[0])
                        except FileNotFoundError:
                            continue
                        if (folder / "acad.exe").is_file():
                            found[year, str(folder.resolve()).casefold()] = AutoCADInstall(
                                year, folder
                            )
    return sorted(found.values(), key=lambda item: (item.year, str(item.directory)))


def validate_installs(installs: list[AutoCADInstall]) -> list[AutoCADInstall]:
    if not installs:
        raise ValueError(
            "No supported AutoCAD 2022-2024 install found. Specify "
            "--autocad-directory 2023=C:\\path\\to\\AutoCAD. AutoCAD 2025+ needs a different runtime."
        )
    found: dict[int, AutoCADInstall] = {}
    for install in installs:
        if install.year not in SUPPORTED_SERIES:
            raise ValueError("The instance bridge installer currently supports AutoCAD 2022-2024.")
        directory = install.directory.resolve()
        for name in ("acad.exe", *REFERENCES):
            if not (directory / name).is_file():
                raise ValueError(f"AutoCAD {install.year}: missing reference/executable {name}.")
        previous = found.get(install.year)
        if previous and previous.directory != directory:
            raise ValueError(
                f"Multiple AutoCAD {install.year} installs; select one explicit directory."
            )
        found[install.year] = AutoCADInstall(install.year, directory)
    return sorted(found.values(), key=lambda item: item.year)


def package_xml(years: list[int]) -> bytes:
    package = ET.Element(
        "ApplicationPackage",
        {
            "SchemaVersion": "1.0",
            "AppVersion": "1.0.0",
            "AutodeskProduct": "AutoCAD",
            "Name": "AutoCAD MCP Instance Bridge",
            "ProductCode": PRODUCT_CODE,
            "Description": "Publish this AutoCAD process for local MCP COM discovery",
        },
    )
    ET.SubElement(package, "CompanyDetails", {"Name": "AutoCAD MCP Pro"})
    for year in sorted(years):
        component = ET.SubElement(package, "Components")
        series = SUPPORTED_SERIES[year]
        ET.SubElement(
            component,
            "RuntimeRequirements",
            {
                "OS": "Win64",
                "Platform": "AutoCAD",
                "SeriesMin": series,
                "SeriesMax": series,
            },
        )
        ET.SubElement(
            component,
            "ComponentEntry",
            {
                "AppName": f"AutoCadMcp.InstancePublisher.{year}",
                "ModuleName": f"./Contents/{year}/{DLL_NAME}",
                "AppType": ".Net",
                "LoadOnAutoCADStartup": "True",
                "LoadOnAppearance": "False",
            },
        )
    ET.indent(package, space="  ")
    return ET.tostring(package, encoding="utf-8", xml_declaration=True) + b"\n"


def compile_bridge(install: AutoCADInstall, output: Path, source: Path) -> None:
    compiler = Path(os.environ.get("WINDIR", "C:\\Windows")) / (
        "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    )
    if not compiler.is_file():
        raise ValueError("The .NET Framework 4.x C# compiler was not found.")
    result = subprocess.run(
        [
            str(compiler),
            "/nologo",
            "/target:library",
            "/platform:x64",
            f"/out:{output}",
            *[f"/reference:{install.directory / name}" for name in REFERENCES],
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode or not output.is_file():
        raise RuntimeError(
            f"AutoCAD {install.year} bridge build failed:\n{result.stdout}{result.stderr}"
        )


def _plain_tree(root: Path, *, contents: bool = True) -> None:
    """Refuse links/junctions before copying, hashing, replacing or removing a tree."""

    def linked(path: Path) -> bool:
        try:
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            return False
        return path.is_symlink() or bool(
            attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )

    for path in (root, *root.parents):
        if linked(path):
            raise ValueError(f"Refusing a linked installation path: {path}")
    if contents and root.exists():
        for path in root.rglob("*"):
            if linked(path):
                raise ValueError("Refusing linked files inside the instance bridge bundle.")


def _owned_manifest(bundle: Path) -> dict | None:
    _plain_tree(bundle)
    if not bundle.exists():
        return None
    try:
        value = json.loads((bundle / MANIFEST).read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("owner") != OWNER or value.get("schema") != 1:
            raise ValueError("Not an installer-owned instance bridge bundle.")
        files = value["files"]
        if not isinstance(files, dict) or not files:
            raise ValueError("Empty bundle file manifest.")
        for relative, expected in files.items():
            if not isinstance(relative, str) or not isinstance(expected, str):
                raise ValueError("Invalid bundle file manifest.")
            path = bundle / relative
            if relative == MANIFEST or not path.resolve().is_relative_to(bundle.resolve()):
                raise ValueError("Bundle manifest path is outside the owned bundle.")
            if not path.is_file() or digest(path) != expected:
                raise ValueError(
                    "An installed instance bridge file changed; preserve it before setup."
                )
        actual = {
            str(path.relative_to(bundle)).replace("\\", "/")
            for path in bundle.rglob("*")
            if path.is_file()
        }
        if actual != {*files, MANIFEST}:
            raise ValueError("The instance bridge bundle has unknown files; refusing replacement.")
        return value
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "Existing instance bridge bundle is incomplete or not installer-owned."
        ) from exc


def _signature(installs: list[AutoCADInstall], source: Path) -> dict:
    return {
        "source_sha256": digest(source),
        "package_xml_sha256": hashlib.sha256(
            package_xml([item.year for item in installs])
        ).hexdigest(),
        "years": {
            str(item.year): {name: digest(item.directory / name) for name in REFERENCES}
            for item in installs
        },
    }


def _paths(plugins_root: Path) -> tuple[Path, Path]:
    root = plugins_root.absolute()
    _plain_tree(root, contents=False)
    return root, root / BUNDLE_NAME


def install_bridge(
    installs: list[AutoCADInstall],
    plugins_root: Path,
    *,
    source: Path = SOURCE,
    builder=compile_bridge,
    dry_run: bool = False,
) -> dict:
    installs = validate_installs(installs)
    root, bundle = _paths(plugins_root)
    previous = _owned_manifest(bundle)
    signature = _signature(installs, source)
    result = {
        "bundle": str(bundle),
        "years": [item.year for item in installs],
        "restart_autocad_required": True,
        "client_settings_changed": False,
    }
    if previous and previous.get("build") == signature:
        return {**result, "status": "unchanged"}
    if dry_run:
        return {**result, "status": "would_update" if previous else "would_install"}
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".autocad-mcp-instance-setup.lock"
    with _setup_lock(lock_path):
        # Recheck under the lock: another installer may have changed the bundle.
        previous = _owned_manifest(bundle)
        if previous and previous.get("build") == signature:
            return {**result, "status": "unchanged"}
        stage = Path(tempfile.mkdtemp(prefix=".autocad-mcp-stage-", dir=root))
        backup = None
        try:
            for item in installs:
                folder = stage / "Contents" / str(item.year)
                folder.mkdir(parents=True)
                builder(item, folder / DLL_NAME, source)
            (stage / "PackageContents.xml").write_bytes(package_xml(result["years"]))
            files = {
                str(path.relative_to(stage)).replace("\\", "/"): digest(path)
                for path in stage.rglob("*")
                if path.is_file()
            }
            manifest = {
                "schema": 1,
                "owner": OWNER,
                "build": signature,
                "files": files,
                "installed_at_utc": datetime.now(UTC).isoformat(),
            }
            (stage / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            _owned_manifest(stage)
            if bundle.exists():
                backup = root / f".autocad-mcp-backup-{uuid.uuid4().hex}"
                bundle.rename(backup)
            try:
                stage.rename(bundle)
            except BaseException:
                if backup is not None:
                    backup.rename(bundle)
                raise
            return {
                **result,
                "status": "updated" if previous else "installed",
                "backup": None if backup is None else str(backup),
                "loaded_plugin_verified": False,
            }
        finally:
            if stage.exists():
                if stage.resolve().parent != root.resolve():
                    raise ValueError("Staging folder moved outside the intended plugin root.")
                _plain_tree(stage)
                shutil.rmtree(stage)


@contextlib.contextmanager
def _setup_lock(path: Path):
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError(
            "Another instance bridge setup is active; retry after it finishes."
        ) from exc
    try:
        os.close(descriptor)
        yield
    finally:
        path.unlink()


def uninstall_bridge(plugins_root: Path, *, dry_run: bool = False) -> dict:
    root, bundle = _paths(plugins_root)
    if _owned_manifest(bundle) is None:
        return {"status": "absent", "bundle": str(bundle)}
    if dry_run:
        return {"status": "would_uninstall", "bundle": str(bundle)}
    with _setup_lock(root / ".autocad-mcp-instance-setup.lock"):
        _owned_manifest(bundle)
        # Keep a recoverable directory; only .bundle directories autoload.
        backup = root / f".autocad-mcp-backup-{uuid.uuid4().hex}"
        bundle.rename(backup)
    return {
        "status": "uninstalled",
        "bundle": str(bundle),
        "backup": str(backup),
        "restart_autocad_required": True,
        "client_settings_changed": False,
    }


def setup_main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Install AutoCAD MCP instance bridge autoload")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--install-instance-bridge", action="store_true")
    action.add_argument("--uninstall-instance-bridge", action="store_true")
    parser.add_argument("--autocad-directory", action="append", default=[], metavar="YEAR=PATH")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(arguments)
    if sys.platform != "win32":
        parser.error("Instance bridge installation requires Windows; the DXF server is unchanged.")
    try:
        root = default_plugins_root()
        if args.uninstall_instance_bridge:
            if args.autocad_directory:
                raise ValueError("--autocad-directory is for installation only.")
            result = uninstall_bridge(root, dry_run=args.dry_run)
        else:
            installs = []
            for item in args.autocad_directory:
                year, separator, path = item.partition("=")
                if not separator or not path:
                    raise ValueError("--autocad-directory must be YEAR=PATH.")
                installs.append(AutoCADInstall(int(year), Path(path)))
            result = install_bridge(installs or detect_installs(), root, dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Instance bridge setup failed: {exc}", file=sys.stderr)
        return 1
