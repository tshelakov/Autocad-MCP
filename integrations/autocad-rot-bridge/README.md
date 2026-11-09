# Optional AutoCAD COM instance publisher

Use this only when a running AutoCAD instance does not publish a reachable
Application or document in the Running Object Table. It registers the existing
`Application.AcadApplication` COM object as `!autocad-mcp:pid:<PID>`. The Python
backend validates the published object's actual HWND/PID/process start time.

There is no network listener, custom command executor or drawing/database work.
The publisher does not save, close, open or activate any drawing. It publishes
on AutoCAD's first Idle callback after loading and revokes on unload/exit.
`MCPREGISTERINSTANCE` retries publishing; `MCPUNREGISTERINSTANCE` revokes it.

## AutoCAD 2022–2024 / .NET Framework 4.8

Build against the matching installed AutoCAD reference assemblies:

```powershell
.\integrations\autocad-rot-bridge\build-net48.ps1 -AutoCADDirectory 'C:\Program Files\Autodesk\AutoCAD 2023'
```

The output is `out/instance-bridge/AutoCadMcp.InstancePublisher.dll`. When the
intended AutoCAD instance is idle, load that DLL with `NETLOAD` in that instance.
The publisher is optional and is not installed or autoloaded by the MCP server.
Use the normal AutoCAD plugin trust process; the server never changes
`SECURELOAD`, trusted paths or security settings.

AutoCAD 2025+ uses a different .NET runtime and needs its own project/build;
this Framework helper is deliberately limited to 2022–2024. Compiling against
one release is not live verification on the other releases.
