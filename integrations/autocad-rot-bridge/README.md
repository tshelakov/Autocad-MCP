# Optional AutoCAD COM instance publisher

Use this only when a running AutoCAD instance does not publish a reachable
Application or document in the Running Object Table. It registers the existing
`Application.AcadApplication` COM object as `!autocad-mcp:pid:<PID>`. The Python
backend validates the published object's actual HWND/PID/process start time.

There is no network listener, custom command executor or drawing/database work.
The publisher does not save, close, open or activate any drawing. It publishes
on AutoCAD's first Idle callback after loading and revokes on unload/exit.
`MCPREGISTERINSTANCE` retries publishing; `MCPUNREGISTERINSTANCE` revokes it.

## Install with autoload (AutoCAD 2022–2024)

The repository's Windows installer installs the MCP package and sets up the
current user's AutoCAD bundle by default:

```powershell
.\install-windows.ps1 -PythonExecutable 'C:\path\to\venv\Scripts\python.exe'
```

It detects supported installed releases from the 64-bit Autodesk registry,
compiles against that release's local reference DLLs, and installs only the
publisher DLL and `PackageContents.xml` under
`%APPDATA%\Autodesk\ApplicationPlugins\AutoCAD.MCP.InstanceBridge.bundle`.
`LoadOnAutoCADStartup=True` makes each new AutoCAD instance publish its own
PID. Already-running instances keep their currently loaded plugin until restart.

Use `-Preview` to inspect before writing, `-SkipInstanceBridge` to install the
Python server only, `-NoDependencies` to preserve existing dependency versions,
or `-AutoCADDirectory '2023=C:\Program Files\Autodesk\AutoCAD 2023'` to select
an explicit release/directory. Specify the same Python environment for updates.
Client configuration, trust paths, `SECURELOAD` and other AutoCAD settings are
not changed. A failed bridge setup retains the old bridge and reports that the
Python package was installed separately.

For a server already installed from this fork, setup is available without the
repository's PowerShell installer:

```powershell
autocad-mcp --install-instance-bridge --dry-run
autocad-mcp --install-instance-bridge
autocad-mcp --uninstall-instance-bridge
```

Repeated setup with unchanged source/references leaves the bundle untouched.
Updates retain the old owned bundle in a backup directory; uninstallation
renames the bundle to a recoverable backup that AutoCAD no longer autoloads.
Unknown or modified bundle files are preserved and cause setup to refuse.
Direct `pip install` does not perform OS/plugin side effects; use the Windows
installer or the explicit setup command to enable autoload.

An unsigned plugin may require a normal AutoCAD trust decision. Setup never
disables security, changes trusted paths or tries to bypass a policy that blocks
the DLL. A blocked load needs the user's normal plugin approval process.

## Manual development build

Build against the matching installed AutoCAD reference assemblies:

```powershell
.\integrations\autocad-rot-bridge\build-net48.ps1 -AutoCADDirectory 'C:\Program Files\Autodesk\AutoCAD 2023'
```

The output is `out/instance-bridge/AutoCadMcp.InstancePublisher.dll`. When the
intended AutoCAD instance is idle, load that DLL with `NETLOAD` in that instance.
The standard server startup does not install or load a plugin.
Use the normal AutoCAD plugin trust process; the server never changes
`SECURELOAD`, trusted paths or security settings.

AutoCAD 2025+ uses a different .NET runtime and needs its own project/build;
this Framework helper is deliberately limited to 2022–2024. Compiling against
one release is not live verification on the other releases.
