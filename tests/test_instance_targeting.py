"""Process binding, concurrency and failed-selection regressions; no live CAD."""

from __future__ import annotations

import asyncio
import sys
import threading
import types

import pytest
from fastmcp import Client

import config
import server
from backends import com_backend as cb
from backends import instance_target as it
from backends.quarantine import DaemonExecutor
from engineering.errors import classify, describe


@pytest.mark.parametrize("value", [None, "", " "])
def test_unset_target_preserves_legacy_selection(value):
    assert it.parse_target(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [("pid:42", "pid:42"), (" PID:42 ", "pid:42"), ("hwnd:0xFF", "hwnd:255")],
)
def test_explicit_target_normalization(value, expected):
    assert it.parse_target(value).text == expected


@pytest.mark.parametrize(
    "value", ["auto", "2023", "pid:0", "pid:-1", "pid:0x42", "pid:4294967296", 42]
)
def test_invalid_target_never_falls_back(value):
    with pytest.raises(ValueError):
        it.parse_target(value)


def test_environment_target_is_read_live(monkeypatch):
    monkeypatch.delenv("CAD_TARGET", raising=False)
    assert config.settings.cad_target == ""
    monkeypatch.setenv("CAD_TARGET", "hwnd:0xFF")
    assert config.settings.cad_target == "hwnd:255"


def test_proxy_cache_is_local_to_its_sta():
    state = it.ThreadLocalState()
    state["app"] = "parent"
    answers = []

    def worker():
        answers.append(state.get("app"))
        state["app"] = "worker"
        answers.append(state.pop("app"))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert answers == [None, "worker"]
    assert state["app"] == "parent"


def candidate(pid=10, hwnd=100, started="first"):
    app = types.SimpleNamespace(HWND=hwnd, Version="24.2", tag=pid)
    return it.InstanceCandidate(
        it.InstanceIdentity(pid, hwnd, started), app, "24.2", "AutoCAD", "Drawing1.dwg", 1, True
    )


def test_missing_or_ambiguous_selector_refuses():
    with pytest.raises(it.InstanceTargetError) as missing:
        it.select_candidate([], it.parse_target("pid:10"))
    assert missing.value.kind == "cad_target_not_found"
    with pytest.raises(it.InstanceTargetError):
        it.select_candidate([candidate(), candidate(hwnd=200)], it.parse_target("pid:10"))


def test_pid_reuse_between_list_and_select_is_refused():
    with pytest.raises(it.InstanceTargetError) as changed:
        it.select_candidate([candidate(started="new")], it.parse_target("pid:10"), "old")
    assert changed.value.kind == "cad_target_changed"


def test_pid_reuse_or_window_loss_invalidates_cached_binding(monkeypatch):
    original = candidate().identity
    monkeypatch.setattr(it, "window_identity", lambda hwnd: candidate(started="new").identity)
    with pytest.raises(it.InstanceTargetError) as lost:
        it.validate_identity(original)
    assert lost.value.kind == "cad_target_lost"
    monkeypatch.setattr(it, "window_identity", lambda hwnd: (_ for _ in ()).throw(OSError()))
    with pytest.raises(it.InstanceTargetError):
        it.validate_identity(original)


@pytest.mark.parametrize("kind", it.INSTANCE_ERROR_KINDS)
def test_process_errors_survive_the_typed_envelope(kind):
    error = it.InstanceTargetError(kind, "target unavailable", target="pid:10")
    wrapped = RuntimeError("wrapper")
    wrapped.__cause__ = error
    result = classify(wrapped)
    assert result["kind"] == kind and result["target"] == "pid:10"
    assert describe(kind).category == "dependency"


@pytest.fixture
def instances(monkeypatch):
    first, second = candidate(), candidate(pid=20, hwnd=200)
    monkeypatch.setattr(cb, "discover_instances", lambda progid: [first, second])
    monkeypatch.setattr(cb, "validate_identity", lambda identity: None)
    monkeypatch.setattr(cb, "_COM_STATE", it.ThreadLocalState())
    return first, second


@pytest.fixture
def backend(instances):
    instance = cb.ComBackend()
    instance._executor = DaemonExecutor(max_workers=1, name="target-test")
    yield instance
    instance._executor.shutdown(wait=False)


@pytest.mark.asyncio
async def test_separate_backends_keep_their_processes_under_parallel_calls(instances):
    first, second = cb.ComBackend(), cb.ComBackend()
    for instance in (first, second):
        instance._executor = DaemonExecutor(max_workers=1, name="target-test")
    try:
        await first.system_select_instance("pid:10")
        await second.system_select_instance("pid:20")
        results = await asyncio.gather(
            *[instance._run(lambda: cb._acad_app().tag) for instance in (first, second) * 12]
        )
        assert results == [10, 20] * 12
    finally:
        for instance in (first, second):
            await instance.disconnect()


@pytest.mark.asyncio
async def test_failed_selection_preserves_previous_process_and_document(backend):
    await backend.system_select_instance("pid:10")
    sentinel = backend._bound = {"name": "keep", "document_id": "original"}
    with pytest.raises(it.InstanceTargetError):
        await backend.system_select_instance("pid:999")
    assert backend._bound is sentinel
    assert await backend._run(lambda: cb._acad_app().tag) == 10


@pytest.mark.asyncio
async def test_switch_clears_document_state_but_never_activates_or_closes_one(backend):
    await backend.system_select_instance("pid:10")
    backend._bound = {"name": "old"}
    backend._plan_spec = object()
    result = await backend.system_select_instance("hwnd:200")
    assert result["previous"]["pid"] == 10
    assert result["identity"]["pid"] == 20
    assert backend._bound is None and backend._plan_spec is None


@pytest.mark.asyncio
async def test_switch_refused_while_transaction_is_active(backend):
    backend._transaction_active = True
    with pytest.raises(it.InstanceTargetError) as refused:
        await backend.system_select_instance("pid:10")
    assert refused.value.kind == "cad_target_busy"
    assert backend._instance_selector is None


@pytest.mark.asyncio
async def test_switch_refused_while_a_worker_is_running(backend):
    began, release = threading.Event(), threading.Event()

    def hold():
        began.set()
        assert release.wait(5)

    task = asyncio.create_task(backend._run(hold))
    await asyncio.to_thread(began.wait, 3)
    try:
        with pytest.raises(it.InstanceTargetError) as refused:
            await backend.system_select_instance("pid:20")
        assert refused.value.kind == "cad_target_busy"
    finally:
        release.set()
        await task
    assert backend._instance_pending_jobs == 0


@pytest.mark.asyncio
async def test_cancelled_running_job_still_blocks_switch_until_worker_finishes(backend):
    began, release = threading.Event(), threading.Event()

    def hold():
        began.set()
        release.wait(5)

    task = asyncio.create_task(backend._run(hold))
    await asyncio.to_thread(began.wait, 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    try:
        with pytest.raises(it.InstanceTargetError):
            await backend.system_select_instance("pid:20")
    finally:
        release.set()
        await backend._run(lambda: None)
    assert backend._instance_pending_jobs == 0


@pytest.mark.asyncio
async def test_whole_request_remains_pinned_between_worker_jobs(backend):
    await backend.system_select_instance("pid:10")
    with backend.instance_request_scope():
        backend._instance_pending_requests += 1  # an independent in-flight request
        try:
            with pytest.raises(it.InstanceTargetError):
                await backend.system_select_instance("pid:20")
        finally:
            backend._instance_pending_requests -= 1
        backend._instance_generation += 1
        with pytest.raises(it.InstanceTargetError) as stale:
            await backend._run(lambda: cb._acad_app().tag)
        assert stale.value.kind == "cad_target_changed"


@pytest.mark.asyncio
async def test_search_proxy_reentry_reuses_the_request_pin(backend):
    with backend.instance_request_scope():
        with backend.instance_request_scope():
            assert backend._instance_pending_requests == 1
            result = await backend.system_select_instance("pid:20")
            assert result["identity"]["pid"] == 20
    assert backend._instance_pending_requests == 0


@pytest.mark.asyncio
async def test_startup_target_reacquires_same_process_without_dispatch(backend, monkeypatch):
    backend._instance_selector = it.parse_target("pid:10")
    assert await backend._run(lambda: cb._acad_app().tag) == 10
    await backend._run(cb._drop_app)
    assert await backend._run(lambda: cb._acad_app().tag) == 10
    replacement = candidate(started="replacement")
    monkeypatch.setattr(cb, "discover_instances", lambda progid: [replacement])
    await backend._run(cb._drop_app)
    with pytest.raises(it.InstanceTargetError) as lost:
        await backend._run(lambda: cb._acad_app().tag)
    assert lost.value.kind == "cad_target_lost"


@pytest.mark.asyncio
async def test_headless_refuses_and_batch_cannot_change_process(full_catalog):
    async with Client(server.mcp) as client:
        result = await client.call_tool(
            "system_select_instance", {"target": "pid:10"}, raise_on_error=False
        )
        assert result.structured_content["capability"] == "instance_targeting"
        result = await client.call_tool(
            "cad_batch",
            {
                "steps": [{"tool": "system_select_instance", "args": {"target": "pid:10"}}],
                "dry_run": True,
            },
            raise_on_error=False,
        )
        assert result.data["results"][0]["error"]["kind"] == "denied"


@pytest.mark.parametrize("source", ["other.dwg", "!autocad-mcp:pid:10"])
def test_rot_discovers_other_process_through_document_and_deduplicates(monkeypatch, source):
    first, second = candidate(), candidate(pid=20, hwnd=200)
    clsid = "{00000000-0000-0000-0000-000000000001}"
    objects = {
        "!" + clsid: second.app,
        source: types.SimpleNamespace(Application=first.app)
        if source.endswith(".dwg")
        else first.app,
    }

    class Proxy:
        def __init__(self, value):
            self.value = value

        def QueryInterface(self, iid):  # noqa: N802
            return self.value

    class Moniker:
        def __init__(self, name):
            self.name = name

        def GetDisplayName(self, context, unused):  # noqa: N802
            return self.name

    rot = types.SimpleNamespace(
        EnumRunning=lambda: [Moniker(n) for n in ["!" + clsid, source, source, "x.xlsx"]],
        GetObject=lambda moniker: Proxy(objects[moniker.name]),
    )
    monkeypatch.setitem(
        sys.modules,
        "pythoncom",
        types.SimpleNamespace(
            GetRunningObjectTable=lambda: rot,
            CreateBindCtx=lambda flags: None,
            IID_IDispatch="dispatch",
        ),
    )
    monkeypatch.setitem(sys.modules, "pywintypes", types.SimpleNamespace(IID=lambda name: clsid))
    fake_client = types.SimpleNamespace(Dispatch=lambda proxy: proxy)
    fake_win32com = types.ModuleType("win32com")
    fake_win32com.client = fake_client
    monkeypatch.setitem(sys.modules, "win32com", fake_win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", fake_client)
    monkeypatch.setattr(it, "_candidate", lambda app, progid: first if app is first.app else second)
    assert [c.identity.pid for c in it.discover_instances("AutoCAD.Application.24.2")] == [10, 20]
