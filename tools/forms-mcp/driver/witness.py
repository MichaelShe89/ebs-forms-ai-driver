"""Passive interference witness.

The previous version opened a Notepad and pulled it to the foreground at the
start of every script, so the tool meant to prove "nothing was disturbed" was
itself the thing disturbing the user. Nothing is raised or opened here: it only
records which window holds the foreground and reports when the Forms client
takes it, which is the only interference this driver could actually cause.
"""
import ctypes
from ctypes import wintypes as wt

user32 = ctypes.windll.user32


def describe(h):
    t = ctypes.create_unicode_buffer(200)
    c = ctypes.create_unicode_buffer(120)
    user32.GetWindowTextW(h, t, 200)
    user32.GetClassNameW(h, c, 120)
    safe = t.value[:44].encode("ascii", "replace").decode("ascii")
    return f"{c.value}:{safe}"


def start():
    fg = user32.GetForegroundWindow()
    return {"start_fg": int(fg), "start_desc": describe(fg), "samples": []}


def check(w, step, forms_hwnd=None):
    fg = int(user32.GetForegroundWindow())
    ours = forms_hwnd is not None and fg == forms_hwnd
    w["samples"].append((step, fg, describe(fg), ours))
    if ours:
        print(f"    [{step:<30}] <-- WE TOOK THE FOREGROUND")
    return not ours


def report(w):
    ours = [s[0] for s in w["samples"] if s[3]]
    print(f"\n  samples={len(w['samples'])}  "
          f"we_took_foreground={ours or 'never'}")
    return len(ours), 0


def _find(part):
    res = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, _):
        buf = ctypes.create_unicode_buffer(300)
        user32.GetWindowTextW(h, buf, 300)
        if part.lower() in buf.value.lower() and user32.IsWindowVisible(h):
            res.append(int(h))
        return True

    user32.EnumWindows(cb, 0)
    return res
