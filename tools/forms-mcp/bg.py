"""Background-only driver for Oracle EBS Forms.

Everything here is measured against a live session with Notepad held in the
foreground as a witness: its content was read before and after each primitive,
so a stolen keystroke would have shown up in it. Nothing in this module injects
input, and `assert_no_injection` makes that a hard failure rather than a habit.

Two rules produce the whole design:

1. Never call SendInput. Keyboard injection follows the real foreground window,
   so it lands in whatever the user is typing into - proven, not assumed.
2. Never let `attached_to` call SetFocus. That is what moved the foreground all
   afternoon. `activate=False` everywhere.

What that leaves is enough to drive the UI: menus, buttons, tabs and check
boxes through doAccessibleActions; list rows through getVisibleChildren plus
the accessible-selection API; and text through the clipboard and Forms' own
Edit > Paste.

Typing into an arbitrary field used to be the one thing that needed real focus.
Java refuses to hold a focus owner while its top-level window is inactive, so
requestFocus only recorded a "most recent" owner and Forms never moved its
current item - which is why Edit > Paste read as greyed. Windows decides that
active/inactive state from WM_ACTIVATE, an ordinary window message, so `wake`
posts one. Posted messages go straight to the window's queue instead of the
shared input queue, which means the pointer never moves and the foreground
never changes, yet Java starts assigning a focus owner and requestFocus then
lands wherever it is aimed. Measured with the Notepad witness: field filled,
we_took_foreground=never, keys_stolen=never.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes as wt

import jab
import keys
import server as S

user32 = ctypes.windll.user32
JOBJECT64 = jab.JOBJECT64
_b = jab.bridge

MAX_VISIBLE_CHILDREN = 256

WM_ACTIVATE, WM_NCACTIVATE = 0x0006, 0x0086
WA_INACTIVE, WA_ACTIVE = 0, 1
FOCUSABLE_ROLES = ("text", "combo box", "check box", "push button",
                   "radio button")
SW_SHOWNOACTIVATE = 4


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long),
                ("r", ctypes.c_long), ("b", ctypes.c_long)]


class _PLACEMENT(ctypes.Structure):
    _fields_ = [("length", wt.UINT), ("flags", wt.UINT), ("showCmd", wt.UINT),
                ("ptMinPosition", _POINT), ("ptMaxPosition", _POINT),
                ("rcNormalPosition", _RECT)]


class _VisibleChildrenInfo(ctypes.Structure):
    _fields_ = [("returnedChildrenCount", ctypes.c_int),
                ("children", JOBJECT64 * MAX_VISIBLE_CHILDREN)]


_b.getVisibleChildren.argtypes = [ctypes.c_long, JOBJECT64, ctypes.c_int,
                                  ctypes.POINTER(_VisibleChildrenInfo)]
_b.getVisibleChildren.restype = wt.BOOL
_b.getVisibleChildrenCount.argtypes = [ctypes.c_long, JOBJECT64]
_b.getVisibleChildrenCount.restype = ctypes.c_int
_b.addAccessibleSelectionFromContext.argtypes = [ctypes.c_long, JOBJECT64,
                                                 ctypes.c_int]
_b.addAccessibleSelectionFromContext.restype = None


# --------------------------------------------------------------------------
# session
# --------------------------------------------------------------------------
def _wait_for_java_window(timeout: float = 60.0) -> int:
    """Resolve the Forms window, waiting for the Access Bridge to notice it.

    A freshly launched or freshly switched Forms client answers isJavaWindow
    with False for a while, which looks exactly like a dead session - a wrong
    call that cost a needless re-login once. So give the bridge time before
    concluding anything.
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            return S._resolve_hwnd(None)
        except RuntimeError as e:
            last = e
            time.sleep(3.0)
    raise RuntimeError(f"no Java window after {timeout:.0f}s: {last}")


class Session:
    """A Forms window addressed by a handle that is re-resolved when it dies.

    The handle changes whenever the Java client is relaunched, and a stale one
    turns every later call into "hwnd is not a Java window".
    """

    def __init__(self, hwnd: int | None = None, timeout: float = 60.0):
        # The default gives a freshly launched client time to answer; a smoke
        # test passes a short one because there it wants a verdict, not a wait.
        self.hwnd = hwnd or _wait_for_java_window(timeout=timeout)

    def nodes(self, tries: int = 20):
        last = None
        for _ in range(tries):
            try:
                return jab.call(lambda: jab.snapshot(self.hwnd), timeout=180)
            except RuntimeError as exc:
                last = exc
                try:
                    self.hwnd = S._resolve_hwnd(None)
                except Exception:
                    time.sleep(2)
        raise last

    def forms(self, nodes=None) -> list[str]:
        nodes = self.nodes() if nodes is None else nodes
        return sorted({n.form for n in nodes if n.form})

    def node(self, form=None, role=None, name=None, name_ends=None,
             value=None, nodes=None):
        for n in (self.nodes() if nodes is None else nodes):
            if form and not (n.form or "").startswith(form):
                continue
            if role and n.role != role:
                continue
            if name and (n.name or "") != name:
                continue
            if name_ends and not (n.name or "").endswith(name_ends):
                continue
            if value is not None and (n.value or "") != value:
                continue
            return n
        return None


# --------------------------------------------------------------------------
# the no-injection guard
# --------------------------------------------------------------------------
_INJECTION_ALLOWED = False

#: Every place that needed a brief SetFocus because a canvas refused to open
#: its Edit menu in the background. A run reports this so the cost is visible
#: instead of hidden.
FOCUS_GRABS: list[str] = []


def assert_no_injection():
    """Fail loudly rather than quietly stealing the user's keyboard.

    Call this from anywhere tempted to reach for keys.press / type_text /
    click_at. Those all route through SendInput, which delivers to the real
    foreground window - i.e. to whatever the user is typing into.
    """
    if not _INJECTION_ALLOWED:
        raise RuntimeError(
            "input injection is disabled: SendInput reaches the foreground "
            "window, not Forms. Use paste(), pick() or press() instead.")


# --------------------------------------------------------------------------
# primitives - all background
# --------------------------------------------------------------------------
def rows(sess: Session, node) -> list[tuple[int, str, str, str]]:
    """Rows of a list, tree or open combo: (index, role, name, states).

    getAccessibleChildFromContext returns nothing for these; getVisibleChildren
    is the call that works, and not binding it is why LOV rows looked invisible.
    """
    info = _VisibleChildrenInfo()
    jab.call(lambda: bool(_b.getVisibleChildren(node.vmid, node.ac, 0,
                                                ctypes.byref(info))))
    out = []
    for i in range(info.returnedChildrenCount):
        ci = jab.AccessibleContextInfo()
        jab.call(lambda c=info.children[i], ci=ci:
                 _b.getAccessibleContextInfo(node.vmid, c, ctypes.byref(ci)))
        out.append((i, ci.role_en_US, ci.name, ci.states_en_US))
    return out


def select(node, index: int, settle: float = 1.2):
    jab.call(lambda: _b.addAccessibleSelectionFromContext(node.vmid, node.ac,
                                                          index))
    jab.settle(settle)


def selected(sess: Session, node) -> list[str]:
    return [nm for _, _, nm, st in rows(sess, node) if "selected" in (st or "")]


def press(sess: Session, form: str, name_starts: str, settle: float = 5.0,
          tries: int = 2) -> bool:
    """Press a button by name prefix. Re-reads the node each attempt: ids move.

    Forms raises its own window on a button press, and it does so during the
    settle - so the foreground is handed back afterwards, not by `attached_to`.
    """
    prev = foreground_guard(sess.hwnd)
    try:
        for _ in range(tries):
            btn = next((n for n in sess.nodes() if n.role == "push button"
                        and (n.form or "").startswith(form)
                        and (n.name or "").lower().startswith(
                            name_starts.lower())), None)
            if btn is None:
                return False
            if S._do_action(btn, "Click", sess.hwnd):
                jab.settle(settle)
                return True
            jab.settle(1.0)
        return False
    finally:
        restore_foreground(prev, sess.hwnd)


def _items(sess: Session):
    return [n for n in jab.call(lambda: jab.snapshot(sess.hwnd), timeout=120)
            if n.role == "menu item"]


def foreground_guard(hwnd: int):
    """Remember the foreground so it can be handed back after Forms grabs it.

    Forms raises its own window when a push button is pressed, and it does so
    while the action settles - after `attached_to` has already exited and run
    its own restore. So the restore has to happen later, from here.
    """
    return user32.GetForegroundWindow()


def restore_foreground(prev: int, hwnd: int):
    if not prev or prev == hwnd:
        return
    if user32.GetForegroundWindow() == prev:
        return
    mine = ctypes.windll.kernel32.GetCurrentThreadId()
    fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(),
                                                None)
    attached = bool(user32.AttachThreadInput(mine, fg_thread, True))
    try:
        user32.SetForegroundWindow(prev)
        user32.BringWindowToTop(prev)
    finally:
        if attached:
            user32.AttachThreadInput(mine, fg_thread, False)
    time.sleep(0.1)


def _placement(hwnd: int) -> _PLACEMENT:
    wp = _PLACEMENT()
    wp.length = ctypes.sizeof(wp)
    user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
    return wp


def ensure_awake(sess: Session) -> str:
    """Take the frame out of the minimized state, once, at session start.

    wake() has no effect on an iconic window - Windows will not hand an active
    state to something that is not on screen - so a minimized Forms has to be
    restored first. Every route out of iconic briefly takes the foreground, so
    it is handed straight back and the cost stays one blip per session instead
    of one per action. The original placement is remembered for `restore`.
    """
    if not user32.IsIconic(sess.hwnd):
        return "already restored"
    sess._placement = _placement(sess.hwnd)
    prev = user32.GetForegroundWindow()
    wp = _PLACEMENT()
    wp.length = ctypes.sizeof(wp)
    wp.flags, wp.showCmd = 0, SW_SHOWNOACTIVATE
    wp.ptMinPosition = sess._placement.ptMinPosition
    wp.ptMaxPosition = sess._placement.ptMaxPosition
    wp.rcNormalPosition = sess._placement.rcNormalPosition
    user32.SetWindowPlacement(sess.hwnd, ctypes.byref(wp))
    time.sleep(0.8)
    restore_foreground(prev, sess.hwnd)
    return "restored from minimized (one brief foreground blip)"


def restore_window(sess: Session) -> bool:
    """Put the frame back exactly how the user left it."""
    saved = getattr(sess, "_placement", None)
    if saved is None:
        return False
    prev = user32.GetForegroundWindow()
    user32.SetWindowPlacement(sess.hwnd, ctypes.byref(saved))
    time.sleep(0.5)
    restore_foreground(prev, sess.hwnd)
    return True


def focus_owner(sess: Session, nodes=None):
    """The component Java currently reports as focused, if any."""
    return [n for n in (nodes if nodes is not None else sess.nodes())
            if "focused" in (n.states or "") and n.role in FOCUSABLE_ROLES]


def has_focus_owner(sess: Session) -> bool:
    """Does Java hold a focus owner of any kind?

    `focus_owner` deliberately filters to the roles a value can be typed into,
    which makes it the wrong test for whether the window is awake at all - on a
    Navigator the owner is a tree node, and wake would then keep re-posting
    activation for a window that was already active.
    """
    return any("focused" in (n.states or "") for n in sess.nodes())


def wake(sess: Session, tries: int = 4) -> bool:
    """Make Java treat its frame as active, without taking the foreground.

    A bare WA_ACTIVE is a no-op whenever AWT already believes it is active, and
    that stale belief survives losing the foreground to another app - so the
    pair is posted as a real transition, inactive first and then active.
    """
    if user32.IsIconic(sess.hwnd):
        ensure_awake(sess)
    for _ in range(tries):
        if has_focus_owner(sess):
            return True
        sleep_window(sess)
        _post_active(sess)
        time.sleep(0.25)
    return has_focus_owner(sess)


def _post_active(sess: Session):
    user32.PostMessageW(sess.hwnd, WM_NCACTIVATE, 1, 0)
    user32.PostMessageW(sess.hwnd, WM_ACTIVATE, WA_ACTIVE,
                        user32.GetForegroundWindow())
    time.sleep(0.4)


def sleep_window(sess: Session):
    """Hand the "active" belief back, so Forms stops drawing a focused caret."""
    user32.PostMessageW(sess.hwnd, WM_ACTIVATE, WA_INACTIVE, 0)
    user32.PostMessageW(sess.hwnd, WM_NCACTIVATE, 0, 0)
    time.sleep(0.2)


def candidates(sess: Session, field):
    """Every node that shares this field's window, role and name.

    Find Orders/Quotes carries a header block and a line block that reuse labels
    like Order Number, and only one of each pair will accept focus - so a lookup
    by name alone silently aims at the wrong item. Coordinates order the list:
    the node the caller actually passed comes first.
    """
    same = [n for n in sess.nodes() if n.form == field.form
            and n.role == field.role and n.name == field.name]
    same.sort(key=lambda n: (n.x != field.x or n.y != field.y, n.y, n.x))
    return same


def focus_field(sess: Session, field, rounds: int = 3):
    """Make `field` Forms' current item; returns the node that took it, or None.

    requestFocus by itself moves Java's focus owner and nothing else - Forms
    keeps its own current item and ignores the change, which is why Edit > Paste
    stayed greyed on a field JAB happily reported as focused. What does move it
    is the deactivate/reactivate pair afterwards: a request issued to an
    inactive window records the target as the window's most recent focus owner,
    and on reactivation Forms restores its current item to exactly that.

    So the cycle is mandatory here, not a fallback. `wake` returns early once
    any owner exists, so relying on it to supply the cycle meant it almost never
    ran, and the poll then confirmed Java focus that Forms had not followed.

    Node ids move between snapshots, so a landed focus is confirmed by window,
    role and position. Ambiguous names are tried candidate by candidate: this
    window stacks its tab canvases, and the twin on a hidden canvas returns True
    from requestFocus just as convincingly as the reachable one.
    """
    for cand in candidates(sess, field):
        here = (cand.form, cand.role, cand.x, cand.y)
        for _ in range(rounds):
            wake(sess)
            with keys.attached_to(sess.hwnd, activate=False):
                live = next((n for n in jab.call(
                    lambda: jab.snapshot(sess.hwnd), timeout=120)
                    if n.id == cand.id), None)
                if live is None:
                    break
                jab.call(lambda: bool(_b.requestFocus(live.vmid, live.ac)))
            time.sleep(0.35)
            sleep_window(sess)
            _post_active(sess)
            got = next((o for o in focus_owner(sess)
                        if (o.form, o.role, o.x, o.y) == here), None)
            if got is not None:
                return got
    return None


def close_menus(sess: Session, tries: int = 6) -> bool:
    """Collapse any menu left expanded. Assumes an attachment is already open.

    A menu that stays open swallows focus: requestFocus keeps reporting success
    while the current item never moves, which is what made every field after the
    third one fail once pasting had opened the Edit menu a few times.
    """
    for _ in range(tries):
        if not _items(sess):
            return True
        ns = jab.call(lambda: jab.snapshot(sess.hwnd), timeout=120)
        opened = next((n for n in ns if n.role == "menu"
                       and "expanded" in (n.states or "")), None)
        if opened is None:
            opened = next((n for n in ns if n.role == "menu"), None)
        if opened is None:
            return False
        S._do_action(opened, "Toggle Drop Down")
        jab.settle(0.5)
    return not _items(sess)


def _menu_click(sess: Session, menu_prefix: str, item_prefix: str) -> bool:
    """Menu invocation that assumes an attachment is already open.

    Opening a nested `attached_to` detaches the threads on exit and destroys
    the caller's focus, which is what made paste silently do nothing.
    """
    for _ in range(4):
        if _items(sess):
            break
        ns = jab.call(lambda: jab.snapshot(sess.hwnd), timeout=120)
        m = S._menu(ns, menu_prefix)
        if m is None:
            return False
        S._do_action(m, "Toggle Drop Down")
        jab.settle(0.9)
    items = _items(sess)
    if not items:
        return False
    item = next((n for n in items
                 if (n.name or "").lower().startswith(item_prefix.lower())),
                None)
    if item is None:
        m = S._menu(jab.call(lambda: jab.snapshot(sess.hwnd), timeout=120),
                    menu_prefix)
        if m:
            S._do_action(m, "Toggle Drop Down")
        return False
    ok = S._do_action(item, "Click")
    jab.settle(0.6)
    if _items(sess):
        m = S._menu(jab.call(lambda: jab.snapshot(sess.hwnd), timeout=120),
                    menu_prefix)
        if m:
            S._do_action(m, "Toggle Drop Down")
    return ok


def menu(sess: Session, menu_prefix: str, item_prefix: str) -> bool:
    """Invoke a menu item. Menus are the background route to Paste, Save etc."""
    prev = foreground_guard(sess.hwnd)
    with keys.attached_to(sess.hwnd, activate=False):
        ok = _menu_click(sess, menu_prefix, item_prefix)
    restore_foreground(prev, sess.hwnd)
    return ok


def _edit_menu(sess: Session) -> dict:
    """Open the Edit menu and read every item's enabled state, then close it.

    Assumes an attachment is already open - a nested one would detach the
    threads on exit and throw away the focus that was just established.
    """
    for _ in range(5):
        if _items(sess):
            break
        m = S._menu(jab.call(lambda: jab.snapshot(sess.hwnd), timeout=120), "edit")
        if m is None:
            return {}
        S._do_action(m, "Toggle Drop Down")
        jab.settle(0.7)
    return {(n.name or "").split(" mnemonic")[0]: ("enabled" in (n.states or ""))
            for n in _items(sess)}


def unpin(sess: Session, gap: float = 1.2):
    """Release a current item that Forms has pinned after a value went in.

    Once a paste lands, Forms holds the cursor on that item and no amount of
    requestFocus moves it. Deactivating the window, waiting, and reactivating
    puts the cursor back on the window's default item with Edit > Paste enabled
    again - a navigation Forms performs itself, and one that costs no keystroke
    and no focus change. Unlike the Clear button it leaves entered values alone.
    """
    sleep_window(sess)
    time.sleep(gap)
    _post_active(sess)
    time.sleep(0.8)
    return focus_owner(sess)


def paste(sess: Session, field, text: str) -> tuple[bool, str]:
    """Fill an EMPTY field in the background, with no focus grab at all.

    Java reports a field as focused as soon as Java focus moves, which happens
    before Forms has finished making it the current item - so waiting on that
    state alone meant clicking Paste while it was still greyed, and the value
    went nowhere. Forms' own enablement of Edit > Paste is the honest signal
    that the item is current and enterable, so that is what is waited on here.

    Paste appends when the field already holds something, so a populated field
    is refused - clear it with the window's own Clear button and refill.
    """
    if (field.value or "").strip():
        return False, (f"field already holds {field.value!r}; paste would "
                       f"append. Use the window's Clear button and refill.")
    keys.set_clipboard(text)
    why = "could not make the field the current item"
    for attempt in range(3):
        # A previous paste leaves the cursor pinned; release it first, or this
        # field can never become current no matter how the focus is requested.
        unpin(sess)
        landed = focus_field(sess, field)
        if landed is None:
            continue
        with keys.attached_to(sess.hwnd, activate=False):
            items = _edit_menu(sess)
            ready = items.get("Paste", False)
            if ready:
                item = next((n for n in _items(sess)
                             if (n.name or "").lower().startswith("paste")), None)
                if item is not None:
                    S._do_action(item, "Click", sess.hwnd)
            close_menus(sess)
        if not ready:
            why = "Forms kept Edit > Paste greyed; the item never became current"
            time.sleep(1.0)
            continue
        jab.settle(0.8)
        # Read back the node that took focus, not the first one with this name:
        # with duplicate names the value lands in one twin while the other
        # stays empty, which reads as a failed paste when it actually worked.
        now = next((n for n in sess.nodes()
                    if (n.form, n.role, n.x, n.y)
                    == (landed.form, landed.role, landed.x, landed.y)), None)
        got = (now.value or "") if now else ""
        if got.strip() == text.strip():
            return True, ""
        why = f"paste did not take; field is {got!r}"
    return False, why


def pick(sess: Session, lov_form: str, wanted: str,
         ok_button: str = "ok") -> bool:
    """Choose an LOV row by name and commit it. No typing, no mouse.

    The selection survives pressing OK through doAccessibleActions - checked by
    switching responsibility end to end.
    """
    lst = sess.node(form=lov_form, role="list")
    if lst is None:
        return False
    data = rows(sess, lst)
    idx = next((i for i, _, nm, _ in data
                if (nm or "").strip() == wanted), None)
    if idx is None:
        idx = next((i for i, _, nm, _ in data if wanted in (nm or "")), None)
    if idx is None:
        return False
    select(lst, idx)
    if wanted not in " ".join(selected(sess, sess.node(form=lov_form,
                                                       role="list"))):
        return False
    return press(sess, lov_form, ok_button, settle=8.0)


def combo_set(sess: Session, form: str, name: str, option: str) -> bool:
    """Set a poplist by option name.

    The combo's selection index does not match the visible-children index, so
    this selects, reads back, and shifts until the right option is current
    instead of trusting the first guess.
    """
    def cb():
        return sess.node(form=form, role="combo box", name=name)

    for _ in range(3):
        data = rows(sess, cb())
        labels = [(i, nm) for i, r, nm, _ in data if r == "label"]
        if labels:
            break
        S._do_action(cb(), "Toggle Drop Down", sess.hwnd)
        jab.settle(1.2)
    else:
        return False

    target = next((i for i, nm in labels if (nm or "").strip() == option), None)
    if target is None:
        return False
    for shift in (0, -1, 1, -2, 2):
        select(cb(), target + shift, settle=1.0)
        if option in " ".join(selected(sess, cb())):
            return True
    return False


# --------------------------------------------------------------------------
# navigation - the tree is a list, so the same two calls drive it
# --------------------------------------------------------------------------
def tree(sess: Session):
    nav = next(f for f in sess.forms() if f.startswith("Navigator"))
    return min((n for n in sess.nodes() if n.form == nav and n.role == "list"),
               key=lambda n: n.x)


def hierarchy(sess: Session) -> str | None:
    return next((n.value for n in sess.nodes()
                 if (n.form or "").startswith("Navigator")
                 and n.role == "text" and n.name == "Hierarchy"), None)


def open_selected(sess: Session, settle: float = 8.0, tries: int = 3) -> bool:
    """Press the Navigator's Open button. On a folder this expands it."""
    nav = next(f for f in sess.forms() if f.startswith("Navigator"))
    for _ in range(tries):
        before = set(sess.forms())
        rows_before = len(rows(sess, tree(sess)))
        if press(sess, nav, "open", settle=settle, tries=1):
            if set(sess.forms()) != before or len(rows(sess, tree(sess))) != rows_before:
                return True
    return False


def go(sess: Session, path: str, verbose: bool = False) -> bool:
    """Walk 'Folder:Entry' by selecting each label; Open expands a folder."""
    parts = [p for p in path.split(":") if p.strip()]
    for pos, part in enumerate(parts):
        hit = None
        for _ in range(3):
            data = rows(sess, tree(sess))
            hit = next((i for i, _, nm, _ in data
                        if (nm or "").strip().lstrip("+- ").startswith(part)),
                       None)
            if hit is not None:
                break
            open_selected(sess, settle=4.0, tries=1)
        if hit is None:
            return False
        select(tree(sess), hit)
        if verbose:
            print(f"    {part!r} -> {hierarchy(sess)!r}")
        if pos < len(parts) - 1:
            open_selected(sess, settle=4.0, tries=1)
    return True


# --------------------------------------------------------------------------
# handoff detection - EBS drops out of Forms into OA Framework pages
# --------------------------------------------------------------------------
def top_level_windows() -> dict[int, str]:
    """Every visible top-level window title on the desktop.

    A Forms action that opens a JSP leaves no new Forms window - the page
    appears in a browser. Watching only the JAB tree makes that look like
    "nothing happened", which cost an hour on Fulfillment Acceptance.
    """
    found: dict[int, str] = {}

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, _):
        if user32.IsWindowVisible(h):
            buf = ctypes.create_unicode_buffer(300)
            user32.GetWindowTextW(h, buf, 300)
            if buf.value:
                found[int(h)] = buf.value
        return True

    user32.EnumWindows(cb, 0)
    return found


class Watcher:
    """Snapshot Forms windows AND the desktop, so a handoff cannot be missed."""

    def __init__(self, sess: Session):
        self.sess = sess
        self.forms = set(sess.forms())
        self.windows = top_level_windows()

    def diff(self) -> dict:
        forms_now = set(self.sess.forms())
        wins_now = top_level_windows()
        return {
            "new_forms": sorted(forms_now - self.forms),
            "gone_forms": sorted(self.forms - forms_now),
            "new_windows": [t for h, t in wins_now.items()
                            if h not in self.windows],
            "retitled": [(self.windows[h], t) for h, t in wins_now.items()
                         if h in self.windows and self.windows[h] != t],
        }
