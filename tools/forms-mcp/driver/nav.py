"""Navigate the Navigator by clicking tree rows.

bg.go walks the tree through the accessible-selection API and gives up when the
wanted row has not been scrolled into view. The rows are ordinary components
with real coordinates, so clicking one selects it exactly as a person would,
and the Expand and Open buttons do the rest.
"""
import pathlib
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from drive import Forms
import bg
import jab


def rows(f, nav):
    return [n for n in f.sess.nodes()
            if n.form == nav and n.role == "label" and (n.name or "").strip()]


def find_row(f, nav, text):
    """Match on the label text with the expand marker stripped off."""
    for n in rows(f, nav):
        clean = (n.name or "").strip().lstrip("+-").strip()
        if clean == text:
            return n
    for n in rows(f, nav):
        if text in (n.name or ""):
            return n
    return None


def scroll_to(f, nav, text, limit=30):
    """Scroll the tree until the wanted row is visible.

    Expand opens the whole tree, so the child of the branch just expanded is
    usually below the visible window; the list scrolls on a wheel event exactly
    as it would for a person.
    """
    rows_now = rows(f, nav)
    if not rows_now:
        return None
    anchor = rows_now[len(rows_now) // 2]
    for _ in range(limit):
        hit = find_row(f, nav, text)
        if hit is not None:
            return hit
        f.cmd(f"wheel {anchor.x + 60} {anchor.y} -3")
        time.sleep(0.35)
    return find_row(f, nav, text)


def tree_open(f, path, settle=18.0, verbose=True):
    """Select each branch, expand it, scroll to the next part, open the leaf.

    `path` is a list of tree labels, or a "a:b" string for the common case.
    Pass a list whenever a label contains a colon of its own - the custom
    custom functions are often named that way ("XX:Some Inquiry Form"), and
    splitting one of those on ":" looks for a branch that does not exist.
    """
    nav = next(x for x in f.forms() if x.startswith("Navigator"))
    parts = list(path) if isinstance(path, (list, tuple)) else path.split(":")
    before = set(f.forms())

    f.press(nav, "collapse all", settle=2.5)
    time.sleep(1.0)
    for i, part in enumerate(parts):
        row = scroll_to(f, nav, part)
        if row is None:
            visible = [(n.name or "").strip() for n in rows(f, nav)]
            return False, f"could not find {part!r}; visible: {visible}"
        if verbose:
            print(f"   {(row.name or '').strip()!r}")
        # Double click expands just this branch, so its children land right
        # below and stay visible. The Expand button opens the entire tree and
        # pushes them out of view instead.
        f.cmd(f"dclick {row.x + 60} {row.y + row.h // 2}")
        time.sleep(2.0 if i == len(parts) - 1 else 1.2)
        if i < len(parts) - 1 and find_row(f, nav, parts[i + 1]) is None:
            f.cmd(f"click {row.x + 60} {row.y + row.h // 2}")
            time.sleep(0.5)
            f.press(nav, "expand", settle=2.5)
            time.sleep(1.0)
    jab.settle(settle)
    opened = [x for x in f.forms() if x not in before]
    if not opened:
        f.press(nav, "open alt", settle=settle)
        jab.settle(3.0)
        opened = [x for x in f.forms() if x not in before]
    return bool(opened), opened


def describe(f, window):
    print(f"\n[{window}]")
    for nd in f.sess.nodes():
        if nd.form == window and nd.role in ("text", "push button", "combo box",
                                             "check box", "list", "page tab"):
            ed = "ed" if "editable" in (nd.states or "") else "  "
            print(f"   [{ed}] {nd.role:<12} {(nd.name or '')[:34]!r:<36} "
                  f"@({nd.x},{nd.y}) val={(nd.value or '')[:20]!r}")


if __name__ == "__main__":
    f = Forms()
    t0 = time.time()
    ok, res = tree_open(f, sys.argv[1])
    print(f"{ok} {res}   ({time.time() - t0:.0f}s)")
    print(f"forms: {f.forms()}")
    for n in (res if isinstance(res, list) else []):
        describe(f, n)
    f.close()
