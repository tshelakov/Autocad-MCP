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

## Review fixes and regression verification (2026-11-09)

The follow-up review fixes cancellation/timeout during selection, explicit
target initialization falling back to headless, setup locks surviving crashes,
and preview requiring dependencies that have not been installed yet.

Selection now prepares a proxy on its STA and commits only plain routing state
on the event loop after the await succeeds. A pending selection excludes other
jobs through the prepare/commit gap. Cancellation keeps both explicit and legacy
connections intact, and a disconnect invalidates the prepared result. The
regressions include a slow worker finishing after cancellation or timeout and
another call arriving after preparation but before commit.

Verification on Windows / Python 3.14:

- All 75 tests in the instance-targeting, installer and backend-selection files
  passed, including 18 newly added parametrized cases.
- The full 7,655-test suite completed with 7,649 passed, two skipped, one expected
  failure and three failures caused by putting pytest's temporary folder inside
  the repository. Those three belong to the field-takeoff tests, which correctly
  refuse report output inside the repository. All four tests in that file passed
  with a temporary folder outside the repository. No remaining unexpected test
  failure was observed; the aggregate is 7,652 passed, two skipped, one expected
  failure. These are results of the full run plus that targeted rerun, not a
  claim that the first full invocation was green.
- A separate-process lock test refused concurrent access and allowed access
  after both normal exit and forced exit without cleanup. An old empty lock file
  no longer blocks installation.
- The PowerShell preview passed in a clean venv without pip/server dependencies.
  The packaged standalone setup also ran with the real installed AutoCAD 2023
  references in dry-run mode and reported the existing bundle unchanged.
- The wheel and sdist built successfully and passed the distribution-content
  checker. Imports, source inclusion and mocked switching passed from a wheel
  installed into a separate test folder. Ruff, tool inventory, plugin copy and
  documentation-number checks passed.
- `smoke_instances_com.py` passed again on the two running AutoCAD 2023 instances:
  separate startup bindings, discovery, refusal preserving the old binding,
  five concurrent status rounds, switching one server and switching it back.
  The other server retained its target and the active document names matched
  their initial values. This smoke creates no documents or geometry.

The installed MCP environment was not upgraded by these checks. Cancellation
and crash cases use controlled mocks/child processes; neither user AutoCAD
process was terminated. The live write smoke and plugin startup loading were
not repeated in this review round.
