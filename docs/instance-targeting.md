# Selecting an AutoCAD process

The live COM backend can pin one running AutoCAD process for its lifetime.
This allows independent STDIO servers to work on different projects at once.
The original `GetActiveObject(CAD_PROGID)` / `CAD_LAUNCH` behaviour remains the
default when `CAD_TARGET` is unset.

## Tools

1. Call `system_list_instances` and inspect `pid`, `hwnd`, `started_at_utc`,
   window title, version and available active-document metadata.
2. Call `system_select_instance(target="pid:1234",
   expected_started_at_utc="<value from discovery>")`.
3. Call `system_get_instance` / `system_status` to verify the binding, then
   `document_list` / `document_activate` as needed inside the selected process.

`hwnd:1234` or `hwnd:0x4D2` is also accepted. PID is decimal. Start time is
optional at selection, but every bound instance retains its observed process
start time and validates it on subsequent jobs. Recycled PIDs or window handles
never silently select a replacement.

Selecting a process changes only this backend's routing. It never activates,
opens, saves or closes a drawing or raises the AutoCAD window. Failed selection
preserves the previous binding. A queued/running COM job, another in-flight MCP
request, or an active transaction refuses a switch with `cad_target_busy`.
`system_select_instance` is refused during whole-batch preflight; one batch
cannot route its later steps into another project.

## Separate connections for parallel projects

Each project should have its own STDIO server process. Set its environment to:

```text
AUTOCAD_MCP_BACKEND=com
CAD_PROGID=AutoCAD.Application.24.2
CAD_TARGET=pid:1234
CAD_LAUNCH=never
```

Use another `CAD_TARGET` for the other project. Existing timeout, tool-profile,
path, discovery and security settings are independent of process selection.
An explicit target never falls back to `Dispatch`, including `system_launch`.

The backend's Application, Visual LISP and export-related COM proxy caches are
STA-local. A timeout worker's later teardown cannot release another worker's
connection. Whole MCP requests capture the binding generation, so requests
queued before a route change cannot send their later jobs to the new process.

One backend owns one binding. An HTTP server shared by multiple clients is not
a per-client routing service: use separate server processes/endpoints when the
clients need independent targets.

## Discovery limitations

Discovery reads existing Running Object Table registrations. Some AutoCAD
versions publish only one Application moniker per release; open drawing
monikers can expose other instances through their `Application` property.
`ROT.GetObject` is used directly: no file is opened to obtain a connection.

Busy applications, inaccessible COM registrations, or an instance without an
Application/document ROT registration can be missing. The result explicitly
reports `complete: false` and a scope. It never guesses that a window-only
instance is connectable and never opens a DWG just to discover it. This is a
COM accessibility limitation, not evidence that no other AutoCAD process exists.

For an instance that does not publish its own COM object, the optional
[in-process publisher](../integrations/autocad-rot-bridge/README.md) registers
the existing Application under a unique PID moniker. Loading it is an explicit
AutoCAD-side step; the MCP server does not install or load plugins. Discovery
accepts that moniker only after checking the actual Application HWND/PID.

## Verification

The mocked suite tests independent backends, failures preserving old bindings,
PID reuse, pending/cancelled work, transaction refusal, request generations,
headless refusal and ROT document discovery. The live read-only smoke is:

```powershell
python scripts/smoke_instances_com.py --pids 1234 5678 --output out/instances.json
```

It starts two independent STDIO servers, verifies startup targets, discovers
both processes, refuses a missing PID, reads their statuses concurrently,
switches one server and switches it back. It does not modify any DWG. A live
pass applies only to the instances and AutoCAD versions recorded in its output.
