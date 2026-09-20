"""Keyboard input for Oracle Forms.

Why this exists: JAB's setTextContents() does not work on Forms text items -
the Forms Java client implements AccessibleText (read) but not
AccessibleEditableText (write), so the call returns false and nothing changes.
Verified against EBS 12.x Order Organizer.

Text therefore has to arrive as real keystrokes. JAB still does the hard part
(finding the field, focusing it, verifying the result); this module only
delivers the characters.

Consequence worth knowing: keystrokes go to whatever window is in the
foreground, so unlike reading, typing requires the Forms window to be fronted
and the desktop to be unlocked.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
import ctypes.wintypes as wt
import time

user32 = ctypes.windll.user32

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_EXTENDEDKEY = 0x0001

SW_RESTORE = 9


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_ubyte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("union", _INPUTunion)]


user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wt.UINT

VK = {
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "tab": 0x09, "enter": 0x0D, "return": 0x0D, "escape": 0x1B, "esc": 0x1B,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "delete": 0x2E, "del": 0x2E, "backspace": 0x08, "space": 0x20,
    "insert": 0x2D,
}
MODS = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12}

# Oracle Forms keymap - the operations that actually drive a Forms session
FORMS_KEYS = {
    "enter_query": "f11",
    "execute_query": "ctrl+f11",
    "exit": "f4",
    "clear_record": "f6",
    "next_field": "tab",
    "previous_field": "shift+tab",
    "next_record": "down",
    "previous_record": "up",
    "list_of_values": "ctrl+l",
    "save": "ctrl+s",
    "delete_record": "ctrl+up",
    "duplicate_record": "shift+f6",
    "help": "ctrl+h",
}


def _mk(vk=0, scan=0, flags=0):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union.ki = KEYBDINPUT(vk, scan, flags, 0, None)
    return inp


def _send(inputs):
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        raise RuntimeError(f"SendInput delivered {sent}/{n} events")


def foreground(hwnd: int, attempts: int = 3) -> bool:
    """Bring the Forms window to the front so keystrokes land in it.

    Windows frequently refuses SetForegroundWindow to a background process, and
    it does so silently. Callers MUST check the return value: sending keystrokes
    after a failed attempt sprays them into whatever window the user is actually
    working in.
    """
    for i in range(attempts):
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.15 + 0.1 * i)
        if user32.GetForegroundWindow() == hwnd:
            return True

    # Windows refuses SetForegroundWindow from a process that does not already
    # own the foreground. Borrowing the current foreground thread's input queue
    # lifts that restriction - the same AttachThreadInput trick used to push
    # Forms behind, run the other way.
    for i in range(attempts):
        cur = user32.GetForegroundWindow()
        if cur == hwnd:
            return True
        fg_thread = user32.GetWindowThreadProcessId(cur, None)
        mine = kernel32.GetCurrentThreadId()
        attached = bool(user32.AttachThreadInput(mine, fg_thread, True))
        try:
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(mine, fg_thread, False)
        time.sleep(0.2 + 0.1 * i)
        if user32.GetForegroundWindow() == hwnd:
            return True
    return False


def require_foreground(hwnd: int):
    """Front the window or refuse to continue. Use before any raw keystroke.

    Only `press()` still needs this. Text entry and actions go through
    `attached_to()` instead and do not disturb the foreground at all.
    """
    if not foreground(hwnd):
        cur = user32.GetForegroundWindow()
        n = user32.GetWindowTextLengthW(cur)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(cur, buf, n + 1)
        raise RuntimeError(
            f"refusing to send keystrokes: could not bring the Forms window to "
            f"the foreground (it is still {buf.value!r}). Keystrokes would have "
            f"gone to that window instead."
        )


# --------------------------------------------------------------------------
# Driving Forms without taking the foreground
#
# "Active window" is a property of an input queue, not of the whole system.
# AttachThreadInput merges this thread's input queue with the Forms UI thread's,
# and inside that merged queue SetActiveWindow/SetFocus can hand Forms the focus
# it needs to accept actions — while the real foreground window, and whatever
# the person at the keyboard is doing, are left alone.
#
# Measured with Notepad in the foreground receiving typing throughout: the value
# reached Forms, every character the user typed stayed in Notepad, and the
# foreground never changed.
#
# Raw SendInput still does NOT reach a background window even when attached, so
# text goes in by clipboard + Forms' own Edit > Paste, and function keys are
# replaced by their menu equivalents.
# --------------------------------------------------------------------------
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.AttachThreadInput.restype = wt.BOOL
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.SetActiveWindow.argtypes = [wt.HWND]
user32.SetActiveWindow.restype = wt.HWND
user32.SetFocus.argtypes = [wt.HWND]
user32.SetFocus.restype = wt.HWND


class attached_to:
    """Share an input queue with the Forms UI thread for the duration.

        with keys.attached_to(hwnd):
            ...            # Forms accepts actions, foreground is untouched

    Always detaches, including on error — leaving two input queues merged would
    make the user's own typing behave strangely.
    """

    def __init__(self, hwnd: int, activate: bool = True):
        self.hwnd = hwnd
        self.activate = activate
        self.mine = kernel32.GetCurrentThreadId()
        self.theirs = user32.GetWindowThreadProcessId(hwnd, None)
        self.attached = False

    def __enter__(self):
        # Remember who had the foreground. Forms raises its own window when a
        # push button is pressed - that is Forms doing it, not this code, and
        # the only remedy is to hand the foreground back afterwards. Menu and
        # paste operations do not trigger it.
        self.prev_fg = user32.GetForegroundWindow()
        self.attached = bool(
            user32.AttachThreadInput(self.mine, self.theirs, True))
        if self.attached and self.activate:
            # SetFocus only; SetActiveWindow was observed to raise the window.
            # Focus inside the shared queue is enough for Forms to accept
            # actions.
            user32.SetFocus(self.hwnd)
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        if self.attached:
            user32.AttachThreadInput(self.mine, self.theirs, False)
            self.attached = False
        self.restore_foreground()
        return False

    def restore_foreground(self):
        """Give the foreground back if Forms took it."""
        if not self.prev_fg or self.prev_fg == self.hwnd:
            return
        if user32.GetForegroundWindow() != self.hwnd:
            return
        # Windows only lets the thread that owns the foreground change it, so
        # borrow that thread's input queue for the moment it takes.
        fg_thread = user32.GetWindowThreadProcessId(
            user32.GetForegroundWindow(), None)
        attached = bool(user32.AttachThreadInput(self.mine, fg_thread, True))
        try:
            user32.SetForegroundWindow(self.prev_fg)
            user32.BringWindowToTop(self.prev_fg)
        finally:
            if attached:
                user32.AttachThreadInput(self.mine, fg_thread, False)
        time.sleep(0.1)


def type_text(text: str, per_char_delay=0.006):
    """Send text as unicode keystrokes - layout and IME independent."""
    for ch in text:
        code = ord(ch)
        _send([_mk(0, code, KEYEVENTF_UNICODE)])
        _send([_mk(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)])
        time.sleep(per_char_delay)


# --------------------------------------------------------------------------
# clipboard
#
# Pasting beats typing for Forms fields: one atomic write instead of N
# keystrokes, nothing for an IME to intercept (which matters for the non-ASCII
# customer names in this instance), and no chance of a half-entered value if
# something interrupts partway.
# --------------------------------------------------------------------------
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

kernel32 = ctypes.windll.kernel32
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wt.HANDLE
kernel32.GlobalLock.argtypes = [wt.HANDLE]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wt.HANDLE]
user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
user32.SetClipboardData.restype = wt.HANDLE


# Without these, ctypes assumes a 32-bit int return and truncates the handle
# and the pointer on 64-bit Python - which silently produced an empty clipboard
# often enough that Forms greyed out Edit > Paste for every field after the
# first, and looked exactly like Forms refusing to accept input.
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = [wt.UINT]


def get_clipboard() -> str | None:
    """Read the clipboard back, so a failed write cannot pass unnoticed."""
    for i in range(5):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.15 * (i + 1))
    else:
        return None
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.c_wchar_p(p).value
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def _write_clipboard(text: str) -> bool:
    for i in range(5):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.15 * (i + 1))
    else:
        raise RuntimeError("could not open the clipboard - another app holds it")
    try:
        user32.EmptyClipboard()
        data = text + chr(0)
        size = len(data) * 2
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not h:
            return False
        p = kernel32.GlobalLock(h)
        if not p:
            return False
        ctypes.memmove(p, ctypes.create_unicode_buffer(data), size)
        kernel32.GlobalUnlock(h)
        return bool(user32.SetClipboardData(CF_UNICODETEXT, h))
    finally:
        user32.CloseClipboard()


def set_clipboard(text: str, attempts: int = 5):
    """Put text on the clipboard and confirm it is there.

    Forms reads the real Windows clipboard, so a write that quietly failed
    leaves Edit > Paste greyed and the field empty - which is indistinguishable
    from Forms rejecting the input unless the value is read back.
    """
    for i in range(attempts):
        _write_clipboard(text)
        time.sleep(0.05 * (i + 1))
        if get_clipboard() == text:
            return True
    raise RuntimeError(f"clipboard would not hold {text!r} after "
                       f"{attempts} attempts")


def press(combo: str):
    """Press a key combination, e.g. 'f11', 'ctrl+f11', 'shift+tab'.

    Accepts the Forms aliases in FORMS_KEYS too, e.g. 'execute_query'.
    """
    combo = FORMS_KEYS.get(combo.strip().lower(), combo)
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key combination")

    mods = [p for p in parts if p in MODS]
    keys = [p for p in parts if p not in MODS]
    if len(keys) != 1:
        raise ValueError(f"expected exactly one non-modifier key in {combo!r}")

    key = keys[0]
    if key in VK:
        vk = VK[key]
    elif len(key) == 1:
        vk = user32.VkKeyScanW(ord(key)) & 0xFF
    else:
        raise ValueError(f"unknown key {key!r}")

    down = [_mk(MODS[m]) for m in mods] + [_mk(vk)]
    up = [_mk(vk, 0, KEYEVENTF_KEYUP)] + \
         [_mk(MODS[m], 0, KEYEVENTF_KEYUP) for m in reversed(mods)]

    # The key-up MUST happen even if something goes wrong in between. A key
    # left logically down auto-repeats: an arrow key scrolls a tree for
    # hundreds of rows, and a stuck Delete or modifier in an ERP session is
    # far worse than a failed keystroke.
    _send(down)
    try:
        time.sleep(0.03)
    finally:
        try:
            _send(up)
        except Exception:
            # last resort - release the keys individually and swallow nothing
            for inp in up:
                try:
                    _send([inp])
                except Exception:
                    pass
            raise
    time.sleep(0.05)


# --------------------------------------------------------------------------
# Mouse, for the few things JAB cannot reach
#
# Page tabs expose no AccessibleAction and their container's selection API is
# not safe to poke blindly (doing so once closed a live Forms session), so a
# real click is the only way to switch tabs. A click necessarily activates the
# window it lands on, so unlike everything else in this module it does take the
# foreground - use it only when there is no JAB route.
# --------------------------------------------------------------------------
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _MOUSEUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("padding", ctypes.c_ubyte * 32)]


class MINPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("union", _MOUSEUNION)]


def click_at(x: int, y: int, settle: float = 0.4):
    """Click one point in virtual-desktop coordinates.

    Coordinates are normalised against the whole virtual desktop, which is what
    makes this correct on a multi-monitor setup; passing raw pixels would land
    on the primary display.
    """
    vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    nx = int((x - vx) * 65535 / max(vw - 1, 1))
    ny = int((y - vy) * 65535 / max(vh - 1, 1))

    def ev(flags):
        i = MINPUT()
        i.type = INPUT_MOUSE
        i.union.mi = MOUSEINPUT(nx, ny, 0, flags, 0, None)
        return i

    base = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(MINPUT), ctypes.c_int]

    def send(flags):
        one = (MINPUT * 1)(ev(base | flags))
        return user32.SendInput(1, one, ctypes.sizeof(MINPUT))

    # Sent as three separate events with gaps. Batching move+down+up into one
    # SendInput call produces a click with no dwell time, which the Forms Java
    # client ignores.
    ok = send(MOUSEEVENTF_MOVE)
    time.sleep(0.08)
    ok += send(MOUSEEVENTF_LEFTDOWN)
    time.sleep(0.06)
    ok += send(MOUSEEVENTF_LEFTUP)
    user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    time.sleep(settle)
    return ok == 3


def release_all():
    """Force every key this module can press into the up state.

    Cheap insurance to run before a sequence: if an earlier failure left
    something held down, the next keystroke would otherwise be interpreted with
    that modifier still active.
    """
    for vk in list(MODS.values()) + list(VK.values()):
        try:
            _send([_mk(vk, 0, KEYEVENTF_KEYUP)])
        except Exception:
            pass
