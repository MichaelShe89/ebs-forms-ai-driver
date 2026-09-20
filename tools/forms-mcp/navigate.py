"""Navigate the EBS Navigator tree to a function, by path, without coordinates.

Why not just click the node: JAB's screen coordinates are not reliable on this
machine's multi-monitor mixed-DPI setup — measured offsets between what JAB
reports and where the element actually is were +975/+191 in one reading and
+969/+289 minutes later, so they cannot be calibrated away.

What is reliable is that the Navigator publishes the selected node's full path
in its "Hierarchy" field. So: move with the keyboard, read the path back, and
compare. Every step is verified, and the loop cannot silently end up somewhere
unexpected.
"""
from __future__ import annotations

import ctypes
import sys

import jab
import keys


def _nodes(hwnd):
    return jab.call(lambda: jab.snapshot(hwnd), timeout=120)


def _find(nodes, role=None, name=None, contains=None, exact=None):
    for n in nodes:
        if role and n.role != role:
            continue
        if name and n.name != name:
            continue
        if exact and (n.name or "") != exact:
            continue
        if contains and contains.lower() not in (n.name or "").lower():
            continue
        return n
    return None


def _do(node, action="Click"):
    todo = jab.AccessibleActionsToDo()
    todo.actionsCount = 1
    todo.actions[0].name = action
    fail = ctypes.c_int(-1)
    ok = jab.call(lambda: bool(jab.bridge.doAccessibleActions(
        node.vmid, node.ac, ctypes.byref(todo), ctypes.byref(fail))))
    jab.settle(0.8)
    return ok


def hierarchy(hwnd) -> str:
    n = _find(_nodes(hwnd), role="text", name="Hierarchy")
    return (n.value or "") if n else ""


def _norm(path: str) -> list[str]:
    return [p for p in (path or "").split(":") if p.strip()]


def forms_hwnd() -> int:
    wins = [w for w in jab.call(jab.java_windows) if w["is_java"]]
    if not wins:
        raise SystemExit("no Forms window found")
    return wins[0]["hwnd"]


def navigate(target: str, hwnd: int | None = None, max_steps: int = 120,
             verbose: bool = True):
    """Select `target` (e.g. "Standard:Orders, Returns:Sales Orders").

    Returns True when the Hierarchy field matches the target.
    """
    hwnd = hwnd or forms_hwnd()
    want = _norm(target)

    # start from a known state so the walk is deterministic
    ca = _find(_nodes(hwnd), role="push button", exact="Collapse All")
    if ca:
        _do(ca)

    seen = set()
    for step in range(max_steps):
        if not keys.foreground(hwnd):
            raise SystemExit("Forms window will not come to the foreground; "
                             "cannot send keys")
        here = hierarchy(hwnd)
        cur = _norm(here)
        if verbose:
            print(f"  step {step:>3}: {here}")

        if cur == want:
            print(f"\nARRIVED: {here}")
            return True

        # if the selection is an ancestor of the target, open it
        if cur == want[:len(cur)] and len(cur) < len(want):
            exp = _find(_nodes(hwnd), role="push button", exact="Expand")
            if exp and here not in seen:
                seen.add(here)
                _do(exp)
                continue

        keys.press("down")
        jab.settle(0.35)

    print(f"\nNOT FOUND after {max_steps} steps; last was {hierarchy(hwnd)!r}")
    return False


def open_selected(hwnd: int | None = None):
    """Press the Navigator's Open button for the current selection."""
    hwnd = hwnd or forms_hwnd()
    btn = _find(_nodes(hwnd), role="push button", contains="Open alt")
    if btn is None:
        raise SystemExit("Open button not found")
    ok = _do(btn)
    jab.settle(4.0)
    forms = sorted({n.form for n in _nodes(hwnd) if n.form})
    return ok, forms


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 \
        else "Standard:Orders, Returns:Sales Orders"
    h = forms_hwnd()
    print(f"navigating to {target!r} in hwnd={h}\n")
    if navigate(target, h):
        if "--open" in sys.argv:
            ok, forms = open_selected(h)
            print(f"\nOpen -> {ok}; forms now open: {forms}")
