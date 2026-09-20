"""MCP server exposing Oracle EBS Forms through the Java Access Bridge.

Read tools are safe. Write tools (forms_focus / forms_invoke / forms_set_text)
drive the real application and are marked as such in their descriptions.

Node addressing: call forms_read or forms_find first — both refresh the window
snapshot and return stable `id` values that the write tools take.
"""
from __future__ import annotations

import sys
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

import jab
import keys

mcp = MCPServer(
    "forms",
    instructions=(
        "Reads and drives an Oracle EBS Forms session through the Java Access "
        "Bridge. Call forms_read or forms_find before any write tool - they "
        "refresh the snapshot that node ids refer to."
    ),
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

EBS_TITLE_HINT = "Oracle Applications"


def _quiet(hwnd: int):
    """Share the Forms input queue WITHOUT taking focus.

    `keys.attached_to` defaults to activate=True, which calls SetFocus for the
    duration of the block — and while the queues are attached, that is enough
    to land the operator's keystrokes in Forms instead of in whatever they were
    typing into. Nothing in this module needs it: menus, doAccessibleActions
    and requestFocus all work from the shared queue alone.

    `bg.py` drives the same three operations with activate=False and was
    measured against a Notepad witness held in the foreground — content read
    before and after every primitive, no keystroke ever stolen. This routes the
    MCP tools down that same path, so the two channels give the same guarantee.

    Every call site here goes through this helper rather than `attached_to`
    directly, so if focus ever does turn out to be necessary for some form, it
    is one line to change and one line to find.
    """
    return keys.attached_to(hwnd, activate=False)


def _resolve_hwnd(hwnd: int | None) -> int:
    if hwnd:
        return hwnd
    wins = jab.call(jab.java_windows)
    java = [w for w in wins if w["is_java"]]
    if not java:
        raise RuntimeError("no Java window found - is the Forms session running?")
    preferred = [w for w in java if EBS_TITLE_HINT.lower() in w["title"].lower()]
    chosen = preferred or java
    if len(chosen) > 1:
        titles = ", ".join(f'{w["title"]!r} (hwnd={w["hwnd"]})' for w in chosen)
        raise RuntimeError(f"several Java windows match, pass hwnd explicitly: {titles}")
    return chosen[0]["hwnd"]


@mcp.tool(annotations=READ_ONLY)
def forms_windows() -> dict:
    """List desktop windows and flag which ones the Java Access Bridge can read.

    Run this first if you are unsure whether the EBS Forms session is up.
    """
    wins = jab.call(jab.java_windows)
    return {
        "java_windows": [w for w in wins if w["is_java"]],
        "other_windows": [w["title"] for w in wins if not w["is_java"]],
    }


@mcp.tool(annotations=READ_ONLY)
def forms_read(
    hwnd: int | None = None,
    form: str | None = None,
    roles: str | None = None,
    include_empty: bool = False,
    limit: int = 400,
) -> dict:
    """Read the visible control tree of the Forms window. SAFE - read only.

    Refreshes the snapshot, so the returned `id` values are what the write
    tools expect.

    form:   only return nodes inside this Forms window (e.g. "Order Organizer").
    roles:  comma-separated role filter, e.g. "text,push button,menu".
    include_empty: keep nodes that carry neither a name nor a value.
    """
    h = _resolve_hwnd(hwnd)
    nodes = jab.call(lambda: jab.snapshot(h), timeout=120)

    role_set = {r.strip() for r in roles.split(",")} if roles else None
    out = []
    for n in nodes:
        if form and n.form != form:
            continue
        if role_set and n.role not in role_set:
            continue
        if not include_empty and not n.name and not n.value:
            continue
        out.append(n.as_dict())

    forms_present = sorted({n.form for n in nodes if n.form})
    return {
        "hwnd": h,
        "total_nodes": len(nodes),
        "forms_open": forms_present,
        "returned": len(out[:limit]),
        "truncated": len(out) > limit,
        "nodes": out[:limit],
    }


@mcp.tool(annotations=READ_ONLY)
def forms_find(
    name: str | None = None,
    value_contains: str | None = None,
    role: str | None = None,
    form: str | None = None,
    hwnd: int | None = None,
    limit: int = 50,
) -> dict:
    """Find fields, buttons or menus by name/value. SAFE - read only.

    Refreshes the snapshot. Matching on `name` is case-insensitive substring,
    which is what you want because Forms decorates names with accelerators
    (e.g. the Open button is named "Open alt O").
    """
    h = _resolve_hwnd(hwnd)
    nodes = jab.call(lambda: jab.snapshot(h), timeout=120)

    def match(n):
        if role and n.role != role:
            return False
        if form and n.form != form:
            return False
        if name and name.lower() not in (n.name or "").lower():
            return False
        if value_contains and value_contains.lower() not in (n.value or "").lower():
            return False
        return bool(name or value_contains or role or form)

    hits = [n for n in nodes if match(n)]
    return {
        "hwnd": h,
        "matches": len(hits),
        "nodes": [n.as_dict(with_geometry=True) for n in hits[:limit]],
    }


@mcp.tool(annotations=READ_ONLY)
def forms_grid(form: str | None = None, hwnd: int | None = None,
               limit: int = 100) -> dict:
    """Read a Forms multi-record block as rows. SAFE - read only.

    Forms renders a grid as loose cells; this groups them back into records by
    their y coordinate and orders columns by x. It returns every column the
    block holds, including ones scrolled out of view.
    """
    h = _resolve_hwnd(hwnd)
    nodes = jab.call(lambda: jab.snapshot(h), timeout=120)

    cells = [n for n in nodes
             if n.role == "text" and n.name and n.value
             and (form is None or n.form == form)]
    if not cells:
        return {"hwnd": h, "rows": [], "note": "no populated grid cells found"}

    rows: dict[int, dict[str, Any]] = {}
    col_x: dict[str, int] = {}
    for c in cells:
        rows.setdefault(c.y, {})[c.name] = c.value
        col_x.setdefault(c.name, c.x)

    ordered_y = sorted(rows)
    columns = sorted(col_x, key=lambda k: col_x[k])
    return {
        "hwnd": h,
        "form": form,
        "columns": columns,
        "row_count": len(ordered_y),
        "rows": [rows[y] for y in ordered_y[:limit]],
        "truncated": len(ordered_y) > limit,
    }


@mcp.tool(annotations=READ_ONLY)
def forms_actions(node_id: int, hwnd: int | None = None) -> dict:
    """List the actions a node accepts. SAFE - enumerates without invoking."""
    h = _resolve_hwnd(hwnd)
    n = jab.node_by_id(h, node_id)
    acts = jab.call(lambda: jab.actions_of(n.vmid, n.ac))
    return {"node": n.as_dict(), "actions": acts}


@mcp.tool(annotations=WRITES)
def forms_focus(node_id: int, hwnd: int | None = None) -> dict:
    """Move the cursor to a field. WRITE - changes the application's focus.

    Equivalent to clicking into the field. Does not alter data by itself, but
    leaving a field can fire Forms validation triggers.
    """
    h = _resolve_hwnd(hwnd)
    n = jab.node_by_id(h, node_id)
    ok = jab.call(lambda: bool(jab.bridge.requestFocus(n.vmid, n.ac)))
    jab.settle(0.4)
    after = jab.call(lambda: jab.snapshot(h), timeout=120)
    focused = [x.as_dict() for x in after if "focused" in x.states]
    return {"requested": n.as_dict(), "returned_ok": ok, "now_focused": focused}


@mcp.tool(annotations=WRITES)
def forms_invoke(node_id: int, action: str = "Click",
                 hwnd: int | None = None, settle_seconds: float = 1.0) -> dict:
    """Invoke an action on a button, menu or tree node. WRITE - drives the app.

    Use forms_actions first to see what the node accepts. This really does press
    the button, so treat it with the same care as a user click.
    """
    import ctypes
    h = _resolve_hwnd(hwnd)
    n = jab.node_by_id(h, node_id)
    target = n.as_dict()

    def _do():
        todo = jab.AccessibleActionsToDo()
        todo.actionsCount = 1
        todo.actions[0].name = action
        failure = ctypes.c_int(-1)
        ok = jab.bridge.doAccessibleActions(
            n.vmid, n.ac, ctypes.byref(todo), ctypes.byref(failure))
        return bool(ok), failure.value

    # share an input queue with the Forms UI thread, otherwise Forms refuses
    # component activation whenever another window holds the foreground
    with _quiet(h):
        ok, failed_at = jab.call(_do)
    jab.settle(settle_seconds)
    after = jab.call(lambda: jab.snapshot(h), timeout=120)
    return {
        "invoked": target,
        "action": action,
        "ok": ok,
        "failed_at_index": failed_at,
        "nodes_after": len(after),
        "forms_open": sorted({x.form for x in after if x.form}),
    }


@mcp.tool(annotations=WRITES)
def forms_type(node_id: int, text: str, clear_first: bool = True,
               method: str = "paste", hwnd: int | None = None) -> dict:
    """Put a value into a field and verify it landed. WRITE - modifies data.

    JAB's setTextContents() does not work on Forms items (they implement
    AccessibleText but not AccessibleEditableText), so the value has to arrive
    the way a user's would. Two ways, both verified by reading the field back:

      method="paste"      (default) sets the clipboard and triggers Forms'
                          own Edit > Paste through JAB. No keystrokes, so an
                          IME cannot mangle non-ASCII text, the value lands in
                          one step, and the operator keeps their keyboard.
                          It does overwrite the clipboard.
      method="keystrokes" sends the characters one at a time. THIS FRONTS THE
                          WINDOW AND TAKES THE OPERATOR'S KEYBOARD. Only ask
                          for it when nobody is at the machine.

    Paste does NOT fall back to keystrokes. Falling back would quietly turn a
    background call into one that grabs the foreground, so a declined paste is
    reported instead and the choice is left to the caller. `method_used` says
    which one delivered the value, or "none" if neither did.
    """
    h = _resolve_hwnd(hwnd)
    n = jab.node_by_id(h, node_id)
    before = n.value
    field_name, fx, fy = n.name, n.x, n.y

    focus_ok = jab.call(lambda: bool(jab.bridge.requestFocus(n.vmid, n.ac)))
    jab.settle(0.3)

    method_used, note = method, ""
    if method == "paste":
        ok, note = _replace_text(h, field_name, fx, fy, text)
        if not ok:
            # Keystrokes need the foreground, which interrupts whoever is at
            # the machine. Never do that behind the caller's back: report the
            # failure and let them ask for method="keystrokes" explicitly.
            return {
                "field": field_name,
                "before": before,
                "requested": text,
                "after": before,
                "verified": False,
                "method_used": "none",
                "note": note + "  (not falling back to keystrokes: that would "
                               "take the foreground; pass method='keystrokes' "
                               "if that is acceptable)",
                "focus_ok": focus_ok,
            }
    else:
        keys.require_foreground(h)
        if clear_first:
            keys.press("ctrl+a")
            keys.press("delete")
        keys.type_text(text)
    jab.settle(0.4)

    fresh = jab.call(lambda: jab.snapshot(h), timeout=120)
    now = next((x.value for x in fresh
                if x.name == field_name and x.x == fx and x.y == fy), None)
    return {
        "field": field_name,
        "before": before,
        "requested": text,
        "after": now,
        "verified": now == text,
        "method_used": method_used,
        "note": note or None,
        "focus_ok": focus_ok,
    }


def _open_forms(nodes) -> set[str]:
    return {n.form for n in nodes if n.form}


def _do_action(node, action: str = "Click", hwnd: int | None = None) -> bool:
    """Invoke an accessible action.

    Component activation (a button Click, a menu item) is refused by Forms
    unless it considers itself active, which it is not while another window
    holds the foreground. Sharing an input queue with the Forms UI thread makes
    it active without changing the real foreground — so this works while
    somebody else is using the machine. Measured: without the attach the same
    Click returns false.
    """
    import ctypes

    def _go():
        todo = jab.AccessibleActionsToDo()
        todo.actionsCount = 1
        todo.actions[0].name = action
        failure = ctypes.c_int(-1)
        return bool(jab.bridge.doAccessibleActions(
            node.vmid, node.ac, ctypes.byref(todo), ctypes.byref(failure)))

    if hwnd:
        with _quiet(hwnd):
            return jab.call(_go)
    return jab.call(_go)


def _menu(nodes, prefix: str):
    return next((n for n in nodes if n.role == "menu"
                 and (n.name or "").lower().startswith(prefix)), None)


def _menu_item(h, menu_prefix: str, item_prefix: str) -> bool:
    """Open a Forms menu and invoke one item, without touching the foreground.

    Forms exposes menu equivalents for most function keys, so this is how to
    reach Save, Enter Query, Select All and so on while somebody else is using
    the machine — raw keystrokes cannot be delivered to a background window.
    """
    with _quiet(h):
        ns = jab.call(lambda: jab.snapshot(h), timeout=120)
        menu = _menu(ns, menu_prefix)
        if menu is None or not _do_action(menu, "Toggle Drop Down"):
            return False
        jab.settle(1.0)
        ns = jab.call(lambda: jab.snapshot(h), timeout=120)
        item = next((n for n in ns if n.role == "menu item"
                     and (n.name or "").lower().startswith(item_prefix)), None)
        if item is None:
            again = _menu(ns, menu_prefix)
            if again:
                _do_action(again, "Toggle Drop Down")
            return False
        ok = _do_action(item, "Click")
        jab.settle(0.5)
        return ok


def _replace_text(h, field_name: str, fx: int, fy: int, text: str
                  ) -> tuple[bool, str]:
    """Put `text` into a field, replacing what is there, using no keystrokes.

    Everything happens inside ONE shared input queue, and the field is re-focused
    immediately before the paste. Both matter: walking a menu moves Forms'
    current item, and once the field is no longer current, Forms greys Paste out
    and the whole thing silently degrades to "Paste is not offered".
    """
    import ctypes

    def refocus():
        fresh = jab.call(lambda: jab.snapshot(h), timeout=120)
        node = next((x for x in fresh if x.name == field_name
                     and x.x == fx and x.y == fy), None)
        if node is None:
            return False
        return jab.call(lambda: bool(
            jab.bridge.requestFocus(node.vmid, node.ac)))

    def items_visible():
        ns = jab.call(lambda: jab.snapshot(h), timeout=120)
        return [n for n in ns if n.role == "menu item"]

    def open_menu(menu_prefix):
        """Open a menu and confirm it opened.

        Toggling blind is wrong: clicking a greyed-out item fails and leaves the
        menu open, so the next toggle CLOSES it and every subsequent lookup
        finds nothing. Check the items instead of assuming.
        """
        for _ in range(3):
            if items_visible():
                return True
            ns = jab.call(lambda: jab.snapshot(h), timeout=120)
            menu = _menu(ns, menu_prefix)
            if menu is None:
                return False
            _do_action(menu, "Toggle Drop Down")
            jab.settle(0.9)
        return bool(items_visible())

    def close_menu(menu_prefix):
        for _ in range(3):
            if not items_visible():
                return
            ns = jab.call(lambda: jab.snapshot(h), timeout=120)
            menu = _menu(ns, menu_prefix)
            if menu is None:
                return
            _do_action(menu, "Toggle Drop Down")
            jab.settle(0.6)

    def click_menu_item(menu_prefix, item_prefix):
        if not open_menu(menu_prefix):
            return False
        item = next((n for n in items_visible()
                     if (n.name or "").lower().startswith(item_prefix)), None)
        if item is None:
            close_menu(menu_prefix)
            return False
        ok = _do_action(item, "Click")
        jab.settle(0.5)
        close_menu(menu_prefix)          # a failed click leaves it open
        return ok

    keys.set_clipboard(text)
    with _quiet(h):
        if not refocus():
            return False, "field vanished before it could be focused"

        # A populated field cannot be cleared without the keyboard. Measured:
        # Edit > Select All and Edit > Delete both return false through
        # doAccessibleActions even on a populated field, and JAB's
        # selectTextRange collapses the selection to the caret. Only Paste
        # works, and Paste with no selection appends.
        current = next((x.value for x in
                        jab.call(lambda: jab.snapshot(h), timeout=120)
                        if x.name == field_name and x.x == fx and x.y == fy),
                       None)
        if current:
            return False, (
                f"field already contains {current!r}; pasting would append. "
                f"Clear it first (the Find window's Clear button works in the "
                f"background), or pass method='keystrokes' to accept taking "
                f"the foreground.")

        if not click_menu_item("edit", "paste"):
            return False, ("Paste was not offered - the field may not be "
                           "editable, or it was not the current item")
        jab.settle(0.6)
    return True, ""


def _paste_via_menu(h) -> tuple[bool, str]:
    """Paste the clipboard into the focused field using Forms' own Edit menu.

    This is how text gets into Forms at all. No keystrokes are involved, so
    nothing depends on the foreground and an IME cannot mangle non-ASCII text;
    the value also lands atomically rather than character by character.

    The whole sequence runs inside one shared input queue with the Forms UI
    thread, which is what makes Forms accept it while another application has
    the foreground.
    """
    with _quiet(h):
        ns = jab.call(lambda: jab.snapshot(h), timeout=120)
        edit = _menu(ns, "edit")
        if edit is None:
            return False, "Edit menu not found"
        if not _do_action(edit, "Toggle Drop Down"):
            return False, "could not open the Edit menu"
        jab.settle(1.0)

        ns = jab.call(lambda: jab.snapshot(h), timeout=120)
        paste = next((n for n in ns if n.role == "menu item"
                      and (n.name or "").lower().startswith("paste")), None)
        if paste is None:
            again = _menu(ns, "edit")
            if again:
                _do_action(again, "Toggle Drop Down")  # never leave one open
            return False, "Paste is not offered — the field may not be editable"

        ok = _do_action(paste, "Click")
        jab.settle(0.6)
    if not ok:
        return False, "Forms refused the Paste action"
    return True, ""


def _click_named(nodes, form: str, name_startswith: str) -> bool:
    """Click a button by name inside one form. Used to dismiss LOV popups."""
    import ctypes
    target = next((n for n in nodes
                   if n.form == form and n.role == "push button"
                   and n.name.lower().startswith(name_startswith.lower())), None)
    if target is None:
        return False

    def _do():
        todo = jab.AccessibleActionsToDo()
        todo.actionsCount = 1
        todo.actions[0].name = "Click"
        failure = ctypes.c_int(-1)
        return bool(jab.bridge.doAccessibleActions(
            target.vmid, target.ac, ctypes.byref(todo), ctypes.byref(failure)))

    ok = jab.call(_do)
    jab.settle(1.2)
    return ok


@mcp.tool(annotations=WRITES)
def forms_set_field(node_id: int, text: str, auto_cancel: bool = True,
                    hwnd: int | None = None,
                    allow_foreground: bool = False) -> dict:
    """Fill a field and report whether Forms ACCEPTED it. WRITE - modifies data.

    This is the primitive to use for batch entry, because typing alone proves
    nothing: Forms validates on navigation out of the field, not on input.

    It types the value, tabs out, and then decides:
      accepted  - no new window appeared, focus moved on
      rejected  - Forms popped a list of values or an error window, which is
                  what it does when a value fails validation

    On rejection with auto_cancel it dismisses the popup so the session is not
    left blocked, and reports which window objected in `rejected_by`.

    Fields whose name ends in "List of Values" are LOV-backed; an exact valid
    value passes, anything else is rejected here.

    Two steps have no background route: typing when Forms declines the paste,
    and tabbing out when the form has no second text item to move focus to.
    Both need raw keystrokes, which means fronting the window and taking the
    operator's keyboard. `allow_foreground` is off by default, so instead of
    doing that silently the call stops and says which step needed it. Pass True
    only when nobody is at the machine.
    """
    h = _resolve_hwnd(hwnd)
    n = jab.node_by_id(h, node_id)
    field_name, fx, fy = n.name, n.x, n.y

    before_nodes = jab.call(lambda: jab.snapshot(h), timeout=120)
    forms_before = _open_forms(before_nodes)
    n = next((x for x in before_nodes
              if x.name == field_name and x.x == fx and x.y == fy), None)
    if n is None:
        raise RuntimeError(f"field {field_name!r} vanished before it could be set")

    jab.call(lambda: bool(jab.bridge.requestFocus(n.vmid, n.ac)))
    jab.settle(0.3)
    pasted, note = _replace_text(h, field_name, fx, fy, text)
    if not pasted and not allow_foreground:
        # The only remaining route is raw keystrokes, and those go to whatever
        # window is in front. Stop rather than type into the operator's work.
        return {
            "field": field_name,
            "requested": text,
            "typed_value": n.value,
            "typed_ok": False,
            "accepted": False,
            "rejected_by": None,
            "note": (f"{note}  (stopping here: the fallback is raw keystrokes, "
                     f"which would front the window and take the operator's "
                     f"keyboard. Pass allow_foreground=True if nobody is at "
                     f"the machine.)"),
            "focus_after": None,
        }
    if not pasted:
        keys.require_foreground(h)
        fresh = jab.call(lambda: jab.snapshot(h), timeout=120)
        again = next((x for x in fresh if x.name == field_name
                      and x.x == fx and x.y == fy), None)
        if again is not None:
            jab.call(lambda: bool(jab.bridge.requestFocus(again.vmid, again.ac)))
            jab.settle(0.3)
        keys.press("ctrl+a")
        keys.press("delete")
        keys.type_text(text)
    jab.settle(0.3)

    typed = jab.call(lambda: jab.snapshot(h), timeout=120)
    typed_value = next((x.value for x in typed
                        if x.name == field_name and x.x == fx and x.y == fy), None)

    # Forms validates when the cursor LEAVES the field. Moving focus with JAB
    # does that just as a Tab would, and unlike a keystroke it works while
    # another application has the foreground.
    neighbour = next((x for x in typed
                      if x.role == "text" and x.name and x.form == n.form
                      and not (x.name == field_name and x.x == fx and x.y == fy)),
                     None)
    left_field, tab_note = True, ""
    if neighbour is not None:
        with _quiet(h):
            jab.call(lambda: bool(
                jab.bridge.requestFocus(neighbour.vmid, neighbour.ac)))
    elif allow_foreground:
        keys.require_foreground(h)
        keys.press("tab")
    else:
        # The value is in, but the cursor never left the field, so Forms has
        # not validated it. Reporting "accepted" here would be a guess dressed
        # up as a finding - the caller is told the check did not run instead.
        left_field = False
        tab_note = ("the cursor was not moved out, so Forms has not validated "
                    "this value - no other text item was available to move to, "
                    "and the alternative is a Tab keystroke that would take the "
                    "operator's keyboard. Pass allow_foreground=True, or move "
                    "off the field yourself and re-read.")
    jab.settle(1.5)
    after = jab.call(lambda: jab.snapshot(h), timeout=120)
    forms_after = _open_forms(after)
    intruders = sorted(forms_after - forms_before)

    result = {
        "field": field_name,
        "requested": text,
        "typed_value": typed_value,
        "typed_ok": typed_value == text,
        # None, not False: "Forms rejected it" and "nobody asked Forms" are
        # different answers, and only one of them means the value is bad.
        "accepted": (not intruders) if left_field else None,
        "validated": left_field,
        "note": tab_note or None,
        "rejected_by": intruders or None,
        "focus_after": [x.as_dict() for x in after
                        if "focused" in x.states and x.role != "label"],
    }

    if intruders and auto_cancel:
        dismissed = []
        for form in intruders:
            if _click_named(after, form, "Cancel"):
                dismissed.append(form)
        result["dismissed"] = dismissed
        final = jab.call(lambda: jab.snapshot(h), timeout=120)
        result["recovered"] = not (_open_forms(final) - forms_before)
    return result


@mcp.tool(annotations=WRITES)
def forms_key(key: str, hwnd: int | None = None,
              settle_seconds: float = 1.0) -> dict:
    """Press a Forms function key. WRITE - drives the app.

    Accepts raw combinations ("f11", "ctrl+f11", "shift+tab") or Forms aliases:
    enter_query, execute_query, exit, clear_record, next_field, previous_field,
    next_record, previous_record, list_of_values, save, delete_record,
    duplicate_record.

    save (Ctrl+S) commits the current block to the database - treat it as the
    point of no return in any batch flow.

    THIS TOOL TAKES THE OPERATOR'S KEYBOARD. It is the one tool here that does:
    a function key has to be a real keystroke, so the window is fronted first
    and anything typed during the call goes to Forms. Do not use it while
    somebody is working at the machine. Two background alternatives exist and
    are preferred: `_menu_item` reaches Save, Enter Query, Select All and most
    other function keys through Forms' own menus, and `driver/drive.py` sends
    key events inside the JVM, which never touches the OS input queue at all.
    """
    h = _resolve_hwnd(hwnd)
    keys.require_foreground(h)
    resolved = keys.FORMS_KEYS.get(key.strip().lower(), key)
    keys.press(key)
    jab.settle(settle_seconds)
    after = jab.call(lambda: jab.snapshot(h), timeout=120)
    focused = [x.as_dict() for x in after if "focused" in x.states]
    return {
        "key": key,
        "resolved_to": resolved,
        "nodes_after": len(after),
        "forms_open": sorted({x.form for x in after if x.form}),
        "now_focused": focused,
    }


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        print(forms_windows())
        sys.exit(0)
    mcp.run()
