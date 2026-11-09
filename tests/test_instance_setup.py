"""Installer lifecycle regressions with fake CAD references and compiler; no live CAD."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import venv
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import server
from backends import instance_setup as setup


@pytest.fixture
def install(tmp_path):
    folder = tmp_path / "AutoCAD 2023"
    folder.mkdir()
    for name in ("acad.exe", *setup.REFERENCES):
        (folder / name).write_bytes(name.encode())
    return setup.AutoCADInstall(2023, folder)


@pytest.fixture
def plugins(tmp_path):
    return tmp_path / "ApplicationPlugins"


def builder(install, output, source):
    output.write_bytes(b"MZ test bridge:" + setup.digest(source).encode())


def installed(install, plugins):
    return setup.install_bridge([install], plugins, builder=builder)


def test_install_sets_startup_loading_and_only_the_matching_release(install, plugins):
    result = installed(install, plugins)
    bundle = Path(result["bundle"])
    xml = ET.parse(bundle / "PackageContents.xml").getroot()
    requirements = xml.find("Components/RuntimeRequirements")
    entry = xml.find("Components/ComponentEntry")
    assert requirements.attrib["SeriesMin"] == requirements.attrib["SeriesMax"] == "R24.2"
    assert entry.attrib["LoadOnAutoCADStartup"] == "True"
    assert entry.attrib["LoadOnAppearance"] == "False"
    assert entry.attrib["AppType"] == ".Net"
    assert (bundle / entry.attrib["ModuleName"]).is_file()
    assert result["status"] == "installed" and not result["client_settings_changed"]


def test_repeat_install_is_idempotent_and_does_not_compile_again(install, plugins):
    first = installed(install, plugins)
    bundle = Path(first["bundle"])
    before = (bundle / setup.MANIFEST).read_bytes()

    def never(*args):
        pytest.fail("unchanged bundle rebuilt")

    result = setup.install_bridge([install], plugins, builder=never)
    assert result["status"] == "unchanged"
    assert (bundle / setup.MANIFEST).read_bytes() == before
    assert set(plugins.iterdir()) == {bundle, plugins / ".autocad-mcp-instance-setup.lock"}


def test_update_retains_recoverable_previous_bundle(install, plugins, tmp_path):
    first = installed(install, plugins)
    bundle = Path(first["bundle"])
    old = (bundle / "Contents/2023" / setup.DLL_NAME).read_bytes()
    source = tmp_path / "revised.cs"
    source.write_text("new source")
    update = setup.install_bridge([install], plugins, source=source, builder=builder)
    assert update["status"] == "updated"
    backup = Path(update["backup"])
    assert not backup.name.endswith(".bundle")
    assert (backup / "Contents/2023" / setup.DLL_NAME).read_bytes() == old
    assert (bundle / "Contents/2023" / setup.DLL_NAME).read_bytes() != old


def test_compile_failure_preserves_old_bundle_and_cleans_staging(install, plugins, tmp_path):
    result = installed(install, plugins)
    bundle = Path(result["bundle"])
    before = (bundle / setup.MANIFEST).read_bytes()
    source = tmp_path / "changed.cs"
    source.write_text("new source")

    def failure(*args):
        raise RuntimeError("compiler failed")

    with pytest.raises(RuntimeError, match="compiler failed"):
        setup.install_bridge([install], plugins, source=source, builder=failure)
    assert (bundle / setup.MANIFEST).read_bytes() == before
    assert set(plugins.iterdir()) == {bundle, plugins / ".autocad-mcp-instance-setup.lock"}


def test_failed_final_rename_rolls_back(install, plugins, tmp_path, monkeypatch):
    result = installed(install, plugins)
    bundle = Path(result["bundle"])
    before = (bundle / setup.MANIFEST).read_bytes()
    source = tmp_path / "changed.cs"
    source.write_text("new source")
    rename = Path.rename

    def fail_stage(path, target):
        if path.name.startswith(".autocad-mcp-stage-"):
            raise PermissionError("locked")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_stage)
    with pytest.raises(PermissionError, match="locked"):
        setup.install_bridge([install], plugins, source=source, builder=builder)
    assert (bundle / setup.MANIFEST).read_bytes() == before
    assert set(plugins.iterdir()) == {bundle, plugins / ".autocad-mcp-instance-setup.lock"}


def test_preview_writes_nothing(install, plugins):
    result = setup.install_bridge([install], plugins, builder=builder, dry_run=True)
    assert result["status"] == "would_install"
    assert not plugins.exists()


def test_other_plugins_and_client_files_are_preserved(install, plugins, tmp_path):
    unrelated = plugins / "Another.bundle"
    unrelated.mkdir(parents=True)
    (unrelated / "settings.json").write_text('{"enabled": true}')
    client = tmp_path / "config.toml"
    client.write_text('[mcp_servers.autocad]\ncommand="old"\n')
    installed(install, plugins)
    assert (unrelated / "settings.json").read_text() == '{"enabled": true}'
    assert client.read_text() == '[mcp_servers.autocad]\ncommand="old"\n'


@pytest.mark.parametrize(
    "alteration", ["foreign_owner", "extra_file", "changed_dll", "invalid_json"]
)
def test_unowned_or_modified_bundle_is_never_replaced_or_uninstalled(install, plugins, alteration):
    result = installed(install, plugins)
    bundle = Path(result["bundle"])
    if alteration == "extra_file":
        (bundle / "local-note.txt").write_text("preserve")
    elif alteration == "changed_dll":
        (bundle / "Contents/2023" / setup.DLL_NAME).write_bytes(b"user changes")
    elif alteration == "invalid_json":
        (bundle / setup.MANIFEST).write_text("[]")
    else:
        manifest = json.loads((bundle / setup.MANIFEST).read_text())
        manifest["owner"] = "another installer"
        (bundle / setup.MANIFEST).write_text(json.dumps(manifest))
    before = {str(p.relative_to(bundle)): p.read_bytes() for p in bundle.rglob("*") if p.is_file()}
    with pytest.raises(ValueError):
        installed(install, plugins)
    with pytest.raises(ValueError):
        setup.uninstall_bridge(plugins)
    assert {
        str(p.relative_to(bundle)): p.read_bytes() for p in bundle.rglob("*") if p.is_file()
    } == before


def test_uninstall_disables_autoload_and_retains_files(install, plugins):
    first = installed(install, plugins)
    result = setup.uninstall_bridge(plugins)
    assert result["status"] == "uninstalled"
    assert not Path(first["bundle"]).exists()
    assert (Path(result["backup"]) / setup.MANIFEST).is_file()
    assert setup.uninstall_bridge(plugins)["status"] == "absent"


def test_missing_references_refused_before_writes(install, plugins):
    (install.directory / "acmgd.dll").unlink()
    with pytest.raises(ValueError, match="missing reference"):
        installed(install, plugins)
    assert not plugins.exists()


def test_unsupported_year_refused_before_writes(install, plugins):
    with pytest.raises(ValueError, match="2022-2024"):
        setup.install_bridge(
            [setup.AutoCADInstall(2025, install.directory)], plugins, builder=builder
        )
    assert not plugins.exists()


def test_two_releases_have_independent_components(install, plugins, tmp_path):
    other = tmp_path / "AutoCAD 2024"
    other.mkdir()
    for name in ("acad.exe", *setup.REFERENCES):
        (other / name).write_bytes(name.encode())
    result = setup.install_bridge(
        [install, setup.AutoCADInstall(2024, other)], plugins, builder=builder
    )
    tree = ET.parse(Path(result["bundle"]) / "PackageContents.xml")
    entries = tree.findall("Components/ComponentEntry")
    assert len(entries) == 2
    assert all((Path(result["bundle"]) / entry.attrib["ModuleName"]).is_file() for entry in entries)


def test_existing_setup_lock_refuses_without_touching_bundle(install, plugins):
    plugins.mkdir()
    lock = plugins / ".autocad-mcp-instance-setup.lock"
    lock.write_text("another setup")
    with setup._setup_lock(lock):
        with pytest.raises(ValueError, match="Another instance bridge setup"):
            installed(install, plugins)
    assert lock.read_text() == "another setup"
    assert not (plugins / setup.BUNDLE_NAME).exists()


def test_stale_lock_file_does_not_block_installation(install, plugins):
    plugins.mkdir()
    (plugins / ".autocad-mcp-instance-setup.lock").touch()
    assert installed(install, plugins)["status"] == "installed"


@pytest.mark.parametrize("crash", [False, True])
def test_setup_lock_excludes_other_process_and_recovers_after_exit(tmp_path, crash):
    lock = tmp_path / "setup.lock"
    script = "\n".join(
        [
            "import os, runpy, sys",
            "from pathlib import Path",
            f"setup = runpy.run_path({str(setup.__file__)!r})",
            f"with setup['_setup_lock'](Path({str(lock)!r})):",
            " print('locked', flush=True)",
            " sys.stdin.readline()",
            f" if {crash!r}: os._exit(19)",
        ]
    )
    child = subprocess.Popen(
        [sys.executable, "-S", "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(ValueError, match="Another instance bridge setup"):
            with setup._setup_lock(lock):
                pytest.fail("another process acquired the held lock")
        child.communicate("done\n", timeout=10)
        assert child.returncode == (19 if crash else 0)
        with setup._setup_lock(lock):
            pass
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows installer preview")
def test_preview_entrypoint_needs_only_the_standard_library(install, tmp_path):
    roaming = tmp_path / "roaming"
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            str(setup.__file__),
            "--install-instance-bridge",
            "--dry-run",
            "--autocad-directory",
            f"2023={install.directory}",
        ],
        env={**os.environ, "APPDATA": str(roaming)},
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "would_install"
    assert not roaming.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell Windows installer")
def test_powershell_preview_works_in_a_clean_venv(install, tmp_path):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("PowerShell is unavailable")
    clean = tmp_path / "clean venv"
    venv.EnvBuilder(with_pip=False).create(clean)
    roaming = tmp_path / "roaming"
    result = subprocess.run(
        [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(Path(setup.__file__).resolve().parents[1] / "install-windows.ps1"),
            "-PythonExecutable",
            str(clean / "Scripts/python.exe"),
            "-Preview",
            "-AutoCADDirectory",
            f"2023={install.directory}",
        ],
        env={**os.environ, "APPDATA": str(roaming)},
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert b'"status": "would_install"' in result.stdout
    assert not roaming.exists()


def test_cli_setup_does_not_start_server(install, plugins, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(setup, "default_plugins_root", lambda: plugins)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "autocad-mcp",
            "--install-instance-bridge",
            "--dry-run",
            "--autocad-directory",
            f"2023={install.directory}",
        ],
    )
    monkeypatch.setattr(server.mcp, "run", lambda: pytest.fail("server started"))
    with pytest.raises(SystemExit) as result:
        server.main()
    assert result.value.code == 0 and not plugins.exists()


def test_source_is_available_to_installed_setup():
    assert setup.SOURCE.is_file()
    text = setup.SOURCE.read_text()
    assert "AcApplication.Idle += PublishWhenIdle" in text
    assert "GetCurrentProcess().Id" in text


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction regression")
def test_bundle_junction_cannot_escape_install_root(install, plugins, tmp_path):
    import subprocess

    plugins.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(plugins / setup.BUNDLE_NAME), str(outside)],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0
    try:
        with pytest.raises(ValueError, match="linked"):
            installed(install, plugins)
        assert (outside / "keep.txt").read_text() == "keep"
    finally:
        os.rmdir(plugins / setup.BUNDLE_NAME)  # remove junction only, never its target
