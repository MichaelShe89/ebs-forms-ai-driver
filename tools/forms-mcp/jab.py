"""Java Access Bridge bindings for Oracle EBS Forms.

Two things make this non-trivial and are handled here:

1. JAB is a message-driven COM-ish API. It only works on a thread that runs a
   Windows message pump. An MCP server runs on an asyncio loop, so all bridge
   calls are funnelled to a dedicated worker thread that owns the bridge and
   pumps messages continuously.

2. AccessibleContext handles are only valid until released and are re-issued on
   every traversal. Callers therefore address nodes by a snapshot id rather than
   by raw handle; snapshots are cached per window and released on replacement.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import queue
import threading
import time

def _bridge_dll():
    """Pick the Access Bridge matching this interpreter's bitness.

    The bridge is not a bridge between bitnesses: a 64-bit Python can only load
    WindowsAccessBridge-64.dll and can only see a 64-bit JVM. If Forms runs on a
    32-bit JRE, this interpreter has to be 32-bit too - hence the explicit error
    rather than a bare OSError from CDLL.
    """
    bits = 64 if ctypes.sizeof(ctypes.c_void_p) == 8 else 32
    name = f"WindowsAccessBridge-{bits}.dll"
    for d in (r"C:\Windows\System32", r"C:\Windows\SysWOW64"):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    raise RuntimeError(
        f"未找到 {name}。\n"
        f"本 Python 是 {bits} 位，必须与 Forms 的 JRE 位数一致。\n"
        f"检查：1) 已执行 jabswitch -enable 并重启 Forms；\n"
        f"      2) Forms 的 JRE 位数（jps -l 后看进程），"
        f"与本 Python 位数相同。")


DLL = _bridge_dll()

user32 = ctypes.windll.user32
bridge = ctypes.CDLL(DLL)

JOBJECT64 = ctypes.c_int64
MAX_STRING = 1024
SHORT_STRING = 256
MAX_ACTION_INFO = 256
MAX_ACTIONS_TO_DO = 32


class AccessibleContextInfo(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_wchar * MAX_STRING),
        ("description", ctypes.c_wchar * MAX_STRING),
        ("role", ctypes.c_wchar * SHORT_STRING),
        ("role_en_US", ctypes.c_wchar * SHORT_STRING),
        ("states", ctypes.c_wchar * SHORT_STRING),
        ("states_en_US", ctypes.c_wchar * SHORT_STRING),
        ("indexInParent", ctypes.c_int),
        ("childrenCount", ctypes.c_int),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("accessibleComponent", wt.BOOL),
        ("accessibleAction", wt.BOOL),
        ("accessibleSelection", wt.BOOL),
        ("accessibleText", wt.BOOL),
        ("accessibleInterfaces", wt.BOOL),
    ]


class AccessibleTextInfo(ctypes.Structure):
    _fields_ = [
        ("charCount", ctypes.c_int),
        ("caretIndex", ctypes.c_int),
        ("indexAtPoint", ctypes.c_int),
    ]


class AccessibleActionInfo(ctypes.Structure):
    _fields_ = [("name", ctypes.c_wchar * SHORT_STRING)]


class AccessibleActions(ctypes.Structure):
    _fields_ = [
        ("actionsCount", ctypes.c_int),
        ("actionInfo", AccessibleActionInfo * MAX_ACTION_INFO),
    ]


class AccessibleActionsToDo(ctypes.Structure):
    _fields_ = [
        ("actionsCount", ctypes.c_int),
        ("actions", AccessibleActionInfo * MAX_ACTIONS_TO_DO),
    ]


_SIGS = [
    ("Windows_run", [], None),
    ("isJavaWindow", [wt.HWND], wt.BOOL),
    ("getAccessibleContextFromHWND",
     [wt.HWND, ctypes.POINTER(ctypes.c_long), ctypes.POINTER(JOBJECT64)], wt.BOOL),
    ("getAccessibleContextInfo",
     [ctypes.c_long, JOBJECT64, ctypes.POINTER(AccessibleContextInfo)], wt.BOOL),
    ("getAccessibleChildFromContext",
     [ctypes.c_long, JOBJECT64, ctypes.c_int], JOBJECT64),
    ("getAccessibleTextInfo",
     [ctypes.c_long, JOBJECT64, ctypes.POINTER(AccessibleTextInfo),
      ctypes.c_int, ctypes.c_int], wt.BOOL),
    ("getAccessibleTextRange",
     [ctypes.c_long, JOBJECT64, ctypes.c_int, ctypes.c_int,
      ctypes.c_wchar_p, ctypes.c_short], wt.BOOL),
    ("requestFocus", [ctypes.c_long, JOBJECT64], wt.BOOL),
    ("getAccessibleActions",
     [ctypes.c_long, JOBJECT64, ctypes.POINTER(AccessibleActions)], wt.BOOL),
    ("doAccessibleActions",
     [ctypes.c_long, JOBJECT64, ctypes.POINTER(AccessibleActionsToDo),
      ctypes.POINTER(ctypes.c_int)], wt.BOOL),
    ("setTextContents", [ctypes.c_long, JOBJECT64, ctypes.c_wchar_p], wt.BOOL),
    ("releaseJavaObject", [ctypes.c_long, JOBJECT64], None),
]
for _n, _a, _r in _SIGS:
    _f = getattr(bridge, _n)
    _f.argtypes = _a
    _f.restype = _r


# --------------------------------------------------------------------------
# worker thread: owns the bridge, pumps messages, runs submitted callables
# --------------------------------------------------------------------------
class _Worker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="jab-worker")
        self._jobs: queue.Queue = queue.Queue()
        self._ready = threading.Event()

    def run(self):
        bridge.Windows_run()
        self._pump(1.5)
        self._ready.set()
        msg = wt.MSG()
        while True:
            while user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            try:
                fn, result_q = self._jobs.get(timeout=0.01)
            except queue.Empty:
                continue
            try:
                result_q.put(("ok", fn()))
            except Exception as exc:  # surfaced to the caller verbatim
                result_q.put(("err", exc))

    @staticmethod
    def _pump(seconds: float):
        msg = wt.MSG()
        end = time.time() + seconds
        while time.time() < end:
            while user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.005)

    def submit(self, fn, timeout=60):
        self._ready.wait(10)
        rq: queue.Queue = queue.Queue()
        self._jobs.put((fn, rq))
        status, payload = rq.get(timeout=timeout)
        if status == "err":
            raise payload
        return payload


_worker: _Worker | None = None
_worker_lock = threading.Lock()


def call(fn, timeout=60):
    """Run fn on the JAB worker thread and return its result."""
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = _Worker()
            _worker.start()
    return _worker.submit(fn, timeout=timeout)


def settle(seconds: float):
    """Let the Forms UI react (menus render, blocks re-query) while pumping."""
    call(lambda: _Worker._pump(seconds), timeout=seconds + 30)


# --------------------------------------------------------------------------
# tree model
# --------------------------------------------------------------------------
class Node:
    __slots__ = ("id", "vmid", "ac", "role", "name", "value", "desc",
                 "x", "y", "w", "h", "states", "depth", "form", "actionable")

    def as_dict(self, with_geometry=False):
        d = {"id": self.id, "role": self.role}
        if self.name:
            d["name"] = self.name
        if self.value is not None and self.value != "":
            d["value"] = self.value
        if self.form:
            d["form"] = self.form
        if "focused" in self.states:
            d["focused"] = True
        if self.actionable:
            d["actionable"] = True
        if with_geometry:
            d["rect"] = [self.x, self.y, self.w, self.h]
        return d


def _text_of(vmid, ac):
    ti = AccessibleTextInfo()
    if not bridge.getAccessibleTextInfo(vmid, ac, ctypes.byref(ti), 0, 0):
        return None
    if ti.charCount <= 0:
        return ""
    n = min(ti.charCount, 2000)
    buf = ctypes.create_unicode_buffer(n + 1)
    if bridge.getAccessibleTextRange(vmid, ac, 0, n - 1, buf, n + 1):
        return buf.value
    return None


def actions_of(vmid, ac):
    acts = AccessibleActions()
    if not bridge.getAccessibleActions(vmid, ac, ctypes.byref(acts)):
        return []
    return [acts.actionInfo[i].name
            for i in range(min(acts.actionsCount, MAX_ACTION_INFO))
            if acts.actionInfo[i].name]


def java_windows():
    """All visible top-level windows, flagged by whether JAB recognises them."""
    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def _cb(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        found.append({
            "hwnd": int(hwnd),
            "title": buf.value,
            "is_java": bool(bridge.isJavaWindow(wt.HWND(hwnd))),
        })
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found


def _root_of(hwnd):
    if not bridge.isJavaWindow(wt.HWND(hwnd)):
        raise RuntimeError(f"hwnd={hwnd} is not a Java window")
    vmid = ctypes.c_long()
    ac = JOBJECT64()
    if not bridge.getAccessibleContextFromHWND(
            wt.HWND(hwnd), ctypes.byref(vmid), ctypes.byref(ac)):
        raise RuntimeError("getAccessibleContextFromHWND failed")
    return vmid.value, ac


# per-window snapshot cache so tools can address nodes by stable id
_snapshots: dict[int, list[Node]] = {}


def _release(nodes):
    for n in nodes:
        try:
            bridge.releaseJavaObject(n.vmid, n.ac)
        except Exception:
            pass


def snapshot(hwnd, max_nodes=4000, visible_only=True):
    """Walk the tree and cache it. Returns the node list."""
    vmid, root = _root_of(hwnd)
    nodes: list[Node] = []
    # the enclosing Forms window title, threaded down so every field knows
    # which form it belongs to
    def rec(ac, depth, form):
        if len(nodes) >= max_nodes:
            return
        info = AccessibleContextInfo()
        if not bridge.getAccessibleContextInfo(vmid, ac, ctypes.byref(info)):
            return
        states = info.states_en_US or ""
        if visible_only and ("showing" not in states or info.width <= 0):
            return
        role = (info.role_en_US or "").strip()
        name = (info.name or "").strip()
        if role == "internal frame" and name:
            form = name
        n = Node()
        n.id = len(nodes)
        n.vmid, n.ac = vmid, ac
        n.role, n.name = role, name
        n.desc = (info.description or "").strip()
        n.value = _text_of(vmid, ac) if info.accessibleText else None
        n.x, n.y, n.w, n.h = info.x, info.y, info.width, info.height
        n.states = states
        n.depth = depth
        n.form = form
        n.actionable = bool(info.accessibleAction)
        nodes.append(n)
        for i in range(info.childrenCount):
            child = bridge.getAccessibleChildFromContext(vmid, ac, i)
            if child:
                rec(child, depth + 1, form)

    rec(root, 0, None)
    old = _snapshots.get(hwnd)
    _snapshots[hwnd] = nodes
    if old:
        _release(old)
    return nodes


def cached(hwnd):
    nodes = _snapshots.get(hwnd)
    if not nodes:
        raise RuntimeError(
            "no snapshot for this window - call forms_read first")
    return nodes


def node_by_id(hwnd, node_id):
    nodes = cached(hwnd)
    if not 0 <= node_id < len(nodes):
        raise RuntimeError(f"node id {node_id} out of range (0..{len(nodes)-1})")
    return nodes[node_id]
