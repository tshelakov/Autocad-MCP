"""Two-process write isolation smoke; writes only to freshly generated scratch DXFs.

python scripts/smoke_instance_writes_com.py --pids 1234 5678 --output out/instance-writes.json
Original drawings are read for a baseline and restored as active on cleanup.
They are never written, saved or closed. Scratch documents are closed unsaved.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ezdxf  # noqa: E402
from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StdioTransport  # noqa: E402


def data(result):
    return result.data if result.data is not None else result.structured_content


def file_digest(path: str | None) -> str | None:
    if not path or not Path(path).is_file():
        return None
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


async def documents(client) -> list[dict]:
    result = data(await client.call_tool("document_list", {}))
    return result if isinstance(result, list) else result["documents"]


async def smoke(pids: list[int], output: Path) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="acadmcp-instance-writes-"))
    scratch = []
    for pid in pids:
        path = work / f"instance-{pid}.dxf"
        doc = ezdxf.new("R2010")
        doc.header["$INSUNITS"] = 4
        doc.saveas(path)
        scratch.append(path)
    originals, cleanup, checks = [], [], []
    report = {"status": "fail", "checks": checks, "cleanup": cleanup}
    async with AsyncExitStack() as stack:
        clients = []
        for pid in pids:
            env = {
                **os.environ,
                "AUTOCAD_MCP_BACKEND": "com",
                "CAD_PROGID": "AutoCAD.Application.24.2",
                "CAD_TARGET": f"pid:{pid}",
                "CAD_LAUNCH": "never",
                "DISCOVERY_MODE": "off",
                "JOURNAL_DURABLE": "off",
                "ALLOWED_PATHS": str(work),
                "COM_CALL_TIMEOUT": "30",
                "DANGEROUS_COMMANDS_ENABLED": "false",
                "FASTMCP_CHECK_FOR_UPDATES": "off",
                "FASTMCP_SHOW_SERVER_BANNER": "false",
            }
            transport = StdioTransport(
                sys.executable,
                [str(ROOT / "server.py")],
                env=env,
                cwd=str(ROOT),
                keep_alive=False,
                log_file=output.parent / f"write-{pid}-stderr.log",
            )
            clients.append(await stack.enter_async_context(Client(transport)))
        try:
            for client in clients:
                rows = await documents(client)
                originals.append(
                    {
                        "documents": rows,
                        "active": next((row["name"] for row in rows if row["active"]), None),
                        "disk_hashes": {row["name"]: file_digest(row["path"]) for row in rows},
                    }
                )
            for index, client in enumerate(clients):
                await client.call_tool("drawing_open", {"path": str(scratch[index])})
                status = data(await client.call_tool("system_status", {}))
                assert status["instance"]["identity"]["pid"] == pids[index]
                assert status["active_document"] == scratch[index].name
            checks.append("scratch_opened_in_selected_processes")
            first, second = await asyncio.gather(
                clients[0].call_tool("entity_create_line", {"x1": 0, "y1": 0, "x2": 10, "y2": 0}),
                clients[1].call_tool("entity_create_circle", {"cx": 100, "cy": 200, "radius": 7}),
            )
            first, second = data(first), data(second)
            assert first["properties"]["length"] == 10
            assert second["properties"]["radius"] == 7
            checks.append("parallel_creates_reached_separate_scratch_documents")
            await asyncio.gather(
                clients[0].call_tool("entity_move", {"handle": first["handle"], "dx": 5, "dy": 0}),
                clients[1].call_tool(
                    "entity_set_properties", {"handle": second["handle"], "color": 3}
                ),
            )
            reread = [
                data(r)
                for r in await asyncio.gather(
                    clients[0].call_tool("entity_get", {"handle": first["handle"]}),
                    clients[1].call_tool("entity_get", {"handle": second["handle"]}),
                )
            ]
            assert reread[0]["properties"]["start"] == [5.0, 0.0]
            assert reread[0]["color"] != 3
            assert reread[1]["properties"]["center"] == [100.0, 200.0]
            assert reread[1]["color"] == 3
            checks.append("parallel_edits_and_readback_isolated")
            # Same handles are common across projects; routing must decide their meaning.
            switched = data(
                await clients[0].call_tool(
                    "system_select_instance",
                    {
                        "target": f"pid:{pids[1]}",
                    },
                )
            )
            assert switched["identity"]["pid"] == pids[1]
            seen = data(await clients[0].call_tool("entity_get", {"handle": second["handle"]}))
            assert seen["type"] == "CIRCLE" and seen["color"] == 3
            await clients[0].call_tool("system_select_instance", {"target": f"pid:{pids[0]}"})
            seen = data(await clients[0].call_tool("entity_get", {"handle": first["handle"]}))
            assert seen["type"] == "LINE" and seen["properties"]["start"] == [5.0, 0.0]
            checks.append("handle_lookup_follows_explicit_process_switch")
            identity = data(await clients[0].call_tool("drawing_info", {}))
            row = next(row for row in await documents(clients[0]) if row["active"])
            assert row["document_id"] == identity["document_id"]
            checks.append("document_list_reports_selected_application_identity")
            begin = data(await clients[0].call_tool("transaction_begin", {}))
            assert begin["ok"]
            try:
                refusal = await clients[0].call_tool(
                    "system_select_instance",
                    {
                        "target": f"pid:{pids[1]}",
                    },
                    raise_on_error=False,
                )
                assert data(refusal)["kind"] == "cad_target_busy"
                assert (
                    data(await clients[0].call_tool("system_status", {}))["instance"]["identity"][
                        "pid"
                    ]
                    == pids[0]
                )
            finally:
                await clients[0].call_tool("transaction_commit", {})
            checks.append("active_transaction_refuses_switch")
            report["status"] = "pass"
        except Exception as error:
            report["error"] = str(error)
            raise
        finally:
            for index, client in enumerate(clients):
                try:
                    await client.call_tool(
                        "system_select_instance", {"target": f"pid:{pids[index]}"}
                    )
                    if index < len(originals):
                        rows = await documents(client)
                        if any(
                            row["path"] and Path(row["path"]).resolve() == scratch[index].resolve()
                            for row in rows
                        ):
                            await client.call_tool(
                                "document_close",
                                {
                                    "name_or_path": str(scratch[index]),
                                    "discard": True,
                                },
                            )
                    if index < len(originals) and originals[index]["active"]:
                        await client.call_tool(
                            "document_activate", {"name_or_path": originals[index]["active"]}
                        )
                    if index < len(originals):
                        after = await documents(client)
                        before = originals[index]
                        assert all(
                            any(
                                all(
                                    new[k] == old[k]
                                    for k in ("name", "path", "active", "saved", "entity_count")
                                )
                                for new in after
                            )
                            for old in before["documents"]
                        ) and len(after) == len(before["documents"])
                        assert {row["name"]: file_digest(row["path"]) for row in after} == before[
                            "disk_hashes"
                        ]
                    cleanup.append({"pid": pids[index], "status": "pass"})
                except Exception as error:
                    cleanup.append({"pid": pids[index], "status": "fail", "error": str(error)})
                    report["status"] = "fail"
            report["scratch_only"] = True
            report["original_drawings_preserved"] = len(originals) == len(pids) and all(
                row["status"] == "pass" for row in cleanup
            )
            report["pids"] = pids
            output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pids", nargs=2, type=int, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "out" / "instance-writes.json")
    args = parser.parse_args()
    if args.pids[0] == args.pids[1] or any(pid <= 0 for pid in args.pids):
        parser.error("choose two different positive process IDs")
    report = asyncio.run(smoke(args.pids, args.output))
    print(json.dumps(report, indent=2))
    if report["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
