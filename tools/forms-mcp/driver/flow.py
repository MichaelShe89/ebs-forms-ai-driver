"""State-aware runner for the order-to-cash test.

Each step checks what is already on screen and does only what is missing, so the
run can be repeated after a failure instead of being rebuilt step by step. The
Forms half is driven by injected events; nothing reaches the operating system's
input queue, so the user's mouse and keyboard stay theirs throughout.
"""
import pathlib
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from drive import Forms, modals
import bg
import jab
import nav
import server as S

ORDER = "<你的测试订单号>"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def close_all_but_navigator(f):
    """Forms refuses to open another window while some of these are up."""
    for _ in range(6):
        others = [x for x in f.forms() if not x.startswith("Navigator")]
        if not others:
            return True
        for x in others:
            if f.press(x, "cancel", settle=1.5, tries=1) or \
               f.press(x, "close", settle=1.5, tries=1):
                continue
            fr = next((n for n in f.sess.nodes()
                       if n.role == "internal frame" and n.form == x), None)
            if fr is not None:
                S._do_action(fr, "Close Window", f.sess.hwnd)
                jab.settle(2.0)
        for m in [x for x in f.forms()
                  if x.lower().startswith(("decision", "forms", "note",
                                           "caution", "error"))]:
            for b in ("discard", "no", "ok"):
                if f.press(m, b, settle=1.5, tries=1):
                    break
    return not [x for x in f.forms() if not x.startswith("Navigator")]


def open_order(f):
    """Bring up the Sales Orders window for ORDER, whatever is open now."""
    so = next((x for x in f.forms() if x.startswith("Sales Orders")), None)
    if so and ORDER in so:
        return so
    if not any(x.startswith("Order Organizer") for x in f.forms()):
        close_all_but_navigator(f)
        ok, opened = nav.tree_open(f, "Orders, Returns:Order Organizer")
        log(f"open Order Organizer: {ok} {opened}")
    for x in [n for n in f.forms() if n.startswith("Open Folder")]:
        f.press(x, "cancel", settle=1.5, tries=1)

    find = next((x for x in f.forms() if x.lower().startswith("find")), None)
    if find is None:
        org = next(x for x in f.forms() if x.startswith("Order Organizer"))
        bg.menu(f.sess, "view", "find")
        jab.settle(5.0)
        find = next((x for x in f.forms() if x.lower().startswith("find")), None)
    if find is None:
        return None

    f.press(find, "clear", settle=2.5)
    time.sleep(1.0)
    for _ in range(3):
        ok, back = f.set(find, "Order Number", ORDER, tab=True, settle=1.2)
        log(f"   find order number = {back!r}")
        if ok:
            break
    f.press(find, "find", settle=18.0)
    time.sleep(3)
    modals(f, known=tuple(f.forms()))
    org = next((x for x in f.forms() if x.startswith("Order Organizer")), None)
    if org:
        # "open" alone matches "Open Folder..." too; the order's button has alt-O
        f.press(org, "open alt", settle=22.0)
        time.sleep(3)
    return next((x for x in f.forms() if x.startswith("Sales Orders")), None)


def goto_lines(f, so):
    tab = next((t for t in f.sess.nodes() if t.form == so
                and t.role == "page tab" and t.name == "Line Items"), None)
    if tab is not None:
        f.cmd(f"click {tab.x + tab.w // 2} {tab.y + tab.h // 2}")
        time.sleep(1.5)
    so = next(x for x in f.forms() if x.startswith("Sales Orders"))
    line = next((n for n in f.sess.nodes() if n.form == so and n.role == "text"
                 and "Ordered Item" in (n.name or "")
                 and (n.value or "").strip()), None)
    if line is not None:
        f.click(line)
        time.sleep(0.6)
    return so


if __name__ == "__main__":
    f = Forms()
    t0 = time.time()
    log(f"forms: {f.forms()}")
    so = open_order(f)
    log(f"order window: {so!r}   ({time.time() - t0:.0f}s)")
    if so is None:
        raise SystemExit("could not open the order")
    so = goto_lines(f, so)
    log(f"on the lines: {so!r}")

    log(f"Tools > ATP CHECK -> {bg.menu(f.sess, 'tools', 'atp check')}")
    time.sleep(5)
    log(f"forms: {f.forms()}")
    for x in [n for n in f.forms()
              if not n.startswith(("Navigator", "Order Organizer",
                                   "Sales Orders", "Find"))]:
        print(f"\n[{x}]")
        for nd in f.sess.nodes():
            if nd.form == x and nd.role in ("push button", "text", "combo box",
                                            "check box"):
                val = (nd.value or "")[:22]
                if nd.role == "push button" or val.strip():
                    print(f"   {nd.role:<12} {(nd.name or '')[:30]!r:<32} "
                          f"@({nd.x},{nd.y}) val={val!r}")
    log(f"elapsed {time.time() - t0:.0f}s")
    f.close()
