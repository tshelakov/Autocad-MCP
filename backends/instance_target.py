"""Discover running AutoCAD COM applications without launching or opening files.

AutoCAD may register just one Application moniker for a release. Documents in
the Running Object Table can expose the other instances through Application.
All discovery, binding and proxy lifetime work must run on the caller's STA.
Only plain identity records leave that thread.
"""

from __future__ import annotations

import contextvars
import logging
import re
import threading
from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass
from typing import Any

from engineering.errors import INSTANCE_ERROR_KINDS, InstanceTargetError  # noqa: F401

log = logging.getLogger(__name__)
REQUEST_INSTANCE = contextvars.ContextVar("request_instance", default=None)


@dataclass(frozen=True)
class TargetSelector:
    kind: str
    value: int

    @property
    def text(self) -> str:
        return f"{self.kind}:{self.value}"


def parse_target(raw: str | None) -> TargetSelector | None:
    """Only an explicit PID or HWND; an empty environment value keeps legacy mode."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise ValueError("CAD_TARGET must be pid:<positive integer> or hwnd:<integer>.")
    value = raw.strip()
    if not value:
        return None
    match = re.fullmatch(r"(pid|hwnd):([0-9]+|0[xX][0-9a-fA-F]+)", value, re.IGNORECASE)
    if match is None:
        raise ValueError("CAD_TARGET must be pid:<positive integer> or hwnd:<integer>.")
    kind, number = match.groups()
    if kind.lower() == "pid" and number.lower().startswith("0x"):
        raise ValueError("CAD_TARGET pid must be a decimal positive integer.")
    parsed = int(number, 16 if number.lower().startswith("0x") else 10)
    if parsed <= 0 or parsed > (0xFFFFFFFF if kind.lower() == "pid" else 0xFFFFFFFFFFFFFFFF):
        raise ValueError("CAD_TARGET is outside the PID/HWND range.")
    return TargetSelector(kind.lower(), parsed)


class ThreadLocalState(MutableMapping[str, Any]):
    """COM proxies are owned by one STA, including abandoned timeout workers."""

    def __init__(self):
        self._local = threading.local()

    def _data(self) -> dict:
        if not hasattr(self._local, "data"):
            self._local.data = {}
        return self._local.data

    def __getitem__(self, key):
        return self._data()[key]

    def __setitem__(self, key, value):
        self._data()[key] = value

    def __delitem__(self, key):
        del self._data()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data())

    def __len__(self):
        return len(self._data())


@dataclass(frozen=True)
class InstanceIdentity:
    pid: int
    hwnd: int
    started_at_utc: str

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "hwnd": self.hwnd,
            "started_at_utc": self.started_at_utc,
        }


@dataclass
class InstanceCandidate:
    identity: InstanceIdentity
    app: Any
    version: str
    title: str
    active_document: str | None
    open_documents: int | None
    documents_available: bool

    def to_dict(self) -> dict:
        return {
            **self.identity.to_dict(),
            "target": f"pid:{self.identity.pid}",
            "title": self.title,
            "version": self.version,
            "active_document": self.active_document,
            "open_documents": self.open_documents,
            "documents_available": self.documents_available,
            "connectable": True,
        }


def window_identity(hwnd: int) -> InstanceIdentity:
    """Read process creation time as well as PID: Windows reuses both IDs."""
    import win32api
    import win32gui
    import win32process

    if not win32gui.IsWindow(hwnd):
        raise RuntimeError("AutoCAD's window no longer exists.")
    _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
    if not pid:
        raise RuntimeError("AutoCAD's window has no process.")
    handle = win32api.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    try:
        created = win32process.GetProcessTimes(handle)["CreationTime"]
    finally:
        handle.Close()
    return InstanceIdentity(int(pid), int(hwnd), created.isoformat())


def _version_matches(progid: str, version: str) -> bool:
    suffix = re.search(r"\.Application\.(\d+\.\d+)$", progid, re.IGNORECASE)
    observed = re.match(r"\d+\.\d+", version)
    return suffix is None or (observed is not None and observed.group() == suffix.group(1))


def _candidate(app, progid: str) -> InstanceCandidate:
    import win32gui

    hwnd = int(app.HWND)
    identity = window_identity(hwnd)
    version = str(app.Version)
    if not _version_matches(progid, version):
        raise ValueError("Application release does not match CAD_PROGID.")
    title = win32gui.GetWindowText(hwnd)
    # Document monikers belonging to other software must never become CAD targets.
    if "autocad" in progid.lower() and "autocad" not in title.lower():
        raise ValueError("The COM object's window is not an AutoCAD frame.")
    count, active, available = None, None, False
    try:
        count = int(app.Documents.Count)
        active = str(app.ActiveDocument.Name) if count else None
        available = True
    except Exception as exc:
        log.debug("Instance document metadata is unavailable: %s", type(exc).__name__)
    return InstanceCandidate(identity, app, version, title, active, count, available)


def discover_instances(progid: str) -> list[InstanceCandidate]:
    """Get running proxies through ROT.GetObject; never Dispatch a name or file."""
    import pythoncom
    import pywintypes
    import win32com.client

    clsid = str(pywintypes.IID(progid)).upper()
    rot = pythoncom.GetRunningObjectTable()
    context = pythoncom.CreateBindCtx(0)
    found: dict[InstanceIdentity, InstanceCandidate] = {}
    for moniker in rot.EnumRunning():
        try:
            name = moniker.GetDisplayName(context, None)
            document = name.lower().endswith((".dwg", ".dxf", ".dwt"))
            published = re.fullmatch(r"!autocad-mcp:pid:[1-9][0-9]*", name) is not None
            if name.upper() != f"!{clsid}" and not document and not published:
                continue
            proxy = win32com.client.Dispatch(
                rot.GetObject(moniker).QueryInterface(pythoncom.IID_IDispatch)
            )
            app = proxy.Application if document else proxy
            candidate = _candidate(app, progid)
            if published and name != f"!autocad-mcp:pid:{candidate.identity.pid}":
                continue
            found.setdefault(candidate.identity, candidate)
        except Exception as exc:
            # A busy/unregistered instance remains unavailable, not substituted.
            log.debug("ROT candidate unavailable: %s", type(exc).__name__)
    return sorted(found.values(), key=lambda item: (item.identity.pid, item.identity.hwnd))


def select_candidate(
    candidates: list[InstanceCandidate],
    selector: TargetSelector,
    expected_started_at_utc: str | None = None,
) -> InstanceCandidate:
    matches = [c for c in candidates if getattr(c.identity, selector.kind) == selector.value]
    if len(matches) != 1:
        raise InstanceTargetError(
            "cad_target_not_found",
            f"{selector.text} is not one reachable AutoCAD COM instance. "
            "Call system_list_instances; nothing was launched or switched.",
            target=selector.text,
        )
    candidate = matches[0]
    if expected_started_at_utc is not None and (
        candidate.identity.started_at_utc != expected_started_at_utc
    ):
        raise InstanceTargetError(
            "cad_target_changed",
            "The process start time changed; inspect system_list_instances before selecting it.",
            target=selector.text,
            expected_started_at_utc=expected_started_at_utc,
            observed=candidate.identity.to_dict(),
        )
    return candidate


def validate_identity(identity: InstanceIdentity) -> None:
    """A cached target can never migrate to a new process, even if its PID is reused."""
    try:
        actual = window_identity(identity.hwnd)
    except Exception:
        actual = None
    if actual != identity:
        raise InstanceTargetError(
            "cad_target_lost",
            "The selected AutoCAD process/window ended or was replaced. No other instance "
            "was selected. Call system_list_instances and explicitly select a current target.",
            selected=identity.to_dict(),
            observed=None if actual is None else actual.to_dict(),
        )
