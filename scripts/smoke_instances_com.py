"""Read-only multi-process smoke against already-running AutoCAD instances.

python scripts/smoke_instances_com.py --pids 1234 5678 --output out/instances.json
Starts two isolated STDIO servers with CAD_LAUNCH=never and CAD_TARGET. No
document is activated, created, modified, saved or closed by this smoke.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StdioTransport  # noqa: E402


def payload(result) -> dict:
    return result.data if isinstance(result.data, dict) else result.structured_content


async def smoke(pids: list[int], output: Path) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    clients = []
    for pid in pids:
        env = {
            **os.environ,
            "AUTOCAD_MCP_BACKEND": "com",
            "CAD_PROGID": "AutoCAD.Application.24.2",
            "CAD_LAUNCH": "never",
            "CAD_TARGET": f"pid:{pid}",
            "JOURNAL_DURABLE": "off",
            "DISCOVERY_MODE": "off",
            "COM_CALL_TIMEOUT": "20",
            "DANGEROUS_COMMANDS_ENABLED": "false",
            "FASTMCP_CHECK_FOR_UPDATES": "off",
            "FASTMCP_SHOW_SERVER_BANNER": "false",
        }
        transport = StdioTransport(
            command=sys.executable,
            args=[str(ROOT / "server.py")],
            env=env,
            cwd=str(ROOT),
            keep_alive=False,
            log_file=output.parent / f"instance-{pid}-stderr.log",
        )
        clients.append(Client(transport))
    from contextlib import AsyncExitStack

    async with AsyncExitStack() as stack:
        connections = [await stack.enter_async_context(client) for client in clients]
        initial = [
            payload(result)
            for result in await asyncio.gather(
                *[client.call_tool("system_status", {}) for client in connections]
            )
        ]
        for pid, status in zip(pids, initial, strict=True):
            assert status["connected"] and status["instance"]["identity"]["pid"] == pid
        discovery = payload(await connections[0].call_tool("system_list_instances", {}))
        reachable = {row["pid"] for row in discovery["instances"]}
        assert set(pids).issubset(reachable)
        # A failed selection cannot replace either connection.
        failure = await connections[0].call_tool(
            "system_select_instance", {"target": "pid:4294967295"}, raise_on_error=False
        )
        assert payload(failure)["kind"] == "cad_target_not_found"
        after_failed = payload(await connections[0].call_tool("system_status", {}))
        assert after_failed["instance"]["identity"] == initial[0]["instance"]["identity"]
        # Independent servers remain pinned while requests execute concurrently.
        rounds = []
        for _ in range(5):
            statuses = [
                payload(r)
                for r in await asyncio.gather(
                    *[c.call_tool("system_status", {}) for c in connections]
                )
            ]
            observed = [s["instance"]["identity"]["pid"] for s in statuses]
            assert observed == pids
            rounds.append(observed)
        # Explicitly switch one server; the other server must remain unchanged.
        selected = payload(
            await connections[0].call_tool(
                "system_select_instance",
                {
                    "target": f"pid:{pids[1]}",
                    "expected_started_at_utc": initial[1]["instance"]["identity"]["started_at_utc"],
                },
            )
        )
        assert selected["identity"]["pid"] == pids[1]
        both = [
            payload(r)
            for r in await asyncio.gather(*[c.call_tool("system_status", {}) for c in connections])
        ]
        assert [s["instance"]["identity"]["pid"] for s in both] == [pids[1], pids[1]]
        await connections[0].call_tool("system_select_instance", {"target": f"pid:{pids[0]}"})
        final = [
            payload(r)
            for r in await asyncio.gather(*[c.call_tool("system_status", {}) for c in connections])
        ]
        assert [s["active_document"] for s in final] == [s["active_document"] for s in initial]
    report = {
        "status": "pass",
        "autocad_versions": [s["autocad_version"] for s in initial],
        "targets": [s["instance"]["identity"] for s in initial],
        "parallel_rounds": rounds,
        "checks": [
            "startup_pin",
            "rot_discovery",
            "failed_selection_preserved",
            "parallel_isolation",
            "explicit_switch",
            "other_server_unchanged",
            "switch_back",
            "active_documents_unchanged",
        ],
        "document_mutations": 0,
    }
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pids", nargs=2, type=int, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "out" / "instances.json")
    args = parser.parse_args()
    if args.pids[0] == args.pids[1] or any(pid <= 0 for pid in args.pids):
        parser.error("choose two different positive process IDs")
    print(json.dumps(asyncio.run(smoke(args.pids, args.output)), indent=2))


if __name__ == "__main__":
    main()
