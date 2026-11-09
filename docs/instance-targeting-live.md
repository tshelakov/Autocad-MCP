# AutoCAD 2023 instance targeting: live verification

Date: 2026-11-09. Host: Windows, two already-running AutoCAD 2023 instances,
`Application.Version` = `24.2s (LMS Tech)` on both. Python 3.14.6, pywin32 312,
FastMCP 3.4.5. This is one local verification environment, not coverage of other
AutoCAD releases or a long-duration stability test.

The ordinary Application ROT moniker exposed one instance. The other instance
was not consistently available through a drawing moniker. The optional
`InstancePublisher` was built against the installed AutoCAD 2023 references,
then explicitly loaded by the user in that instance. Both became reachable.
No trusted-path, `SECURELOAD` or other security setting was changed.

## Results

| Check | Result |
|---|---|
| Two independent STDIO servers, each with `CAD_TARGET` | Passed |
| List both reachable PID/HWND/start-time identities | Passed |
| Five rounds of simultaneous status reads retained separate PIDs | Passed |
| Missing PID refused and original binding retained | Passed |
| Switch one server to the other process, switch back | Passed |
| The other server's routing stayed unchanged | Passed |
| Open separate scratch DXFs in the chosen processes | Passed |
| Create a LINE in one and a CIRCLE in the other concurrently | Passed |
| Move the LINE and recolour the CIRCLE concurrently, then read back | Passed |
| Read handles after explicit process switches | Passed |
| Active transaction refuses a process switch | Passed |
| `document_list` agrees with the selected document's identity | Passed after fix |
| HWND/PID switching through `call_tool` in search mode | Passed |
| Wrong expected process start time refused without replacing binding | Passed |

The write smoke exposed an existing `document_list` lookup bug: the known-document
key includes application identity but the lookup omitted it, so IDs were null.
The lookup now passes `_app_identity(app)` and has a mocked regression test.

## Working drawing preservation

Writes were confined to new scratch DXFs. The scratch documents were closed
without saving. Original active drawings were restored, and their document
list, saved flags, model entity counts and on-disk SHA-256 values matched the
baseline. This checks those observed fields; it is not a full database diff.
No original drawing was saved or closed.

## Limits

Process exit / recycled IDs, abandoned jobs and request-generation races are
covered with mocked tests. The live checks did not kill or restart either user
AutoCAD process. A target that is invisible to the ROT still needs the optional
publisher. The Framework publisher was compiled and loaded on 2023 only;
AutoCAD 2025+ needs a matching runtime build.

Raw local logs retain machine-specific drawing names and paths and are excluded
from Git. The published evidence summary contains no customer drawing content.

## Startup loading verification

The explicit installer built and deployed the publisher into the current user's
AutoCAD `.bundle` folder. `PackageContents.xml` selects R24.2 only, sets
`LoadOnAutoCADStartup=True` and `LoadOnAppearance=False`. Existing processes
therefore retain their currently loaded assembly until their next start.

A separate AutoCAD 2023 process was launched with a blank local template. It
published `!autocad-mcp:pid:<its PID>` automatically; no `NETLOAD` or manual load
was used. A server from the built Python wheel connected to that process by PID
and read its status. The blank document and test process were then closed. The
two original AutoCAD processes were left running.

The complete `install-windows.ps1` flow was exercised in an independent Python
environment with a locally built wheel. The wheel carried both setup code and
C# source. Repeated bridge setup returned `unchanged`, the installed-file hashes
were verified, and the MCP client's configuration hash stayed unchanged.
This confirms automatic startup loading on this AutoCAD 2023 installation;
other versions and environments with stricter DLL trust policies were not run.
