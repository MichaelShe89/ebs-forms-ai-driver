"""Forms driver: read through the Access Bridge, act through injected events.

The Access Bridge is a good reader - it names every window, item, value and
state - but it exposes no action at all on a text item, so it could only ever
move the caret once per Forms-driven navigation. The events come from inside the
JVM instead, where a MouseEvent to a component moves focus to it and KeyEvents
fill it, along the same path a real click takes. Nothing enters the operating
system input queue, so the foreground never moves and no keystroke is diverted.
"""
import pathlib
import re
import socket
import sys
import time

# driver/ 位于 forms-mcp/ 之下,bg/jab/server 在其父目录。相对定位,
# 任何机器上解包即可运行 - 不要改回绝对路径。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import bg
import jab

VK_TAB, VK_ENTER, VK_END, VK_HOME, VK_DELETE, VK_ESCAPE = 9, 10, 35, 36, 127, 27
VK_DOWN, VK_UP, VK_F11 = 40, 38, 122
SHIFT, CTRL = 64, 128

AGENT = pathlib.Path(__file__).parents[1] / "agent"
SECRET = AGENT / "drive" / "secret.txt"


def _token():
    """Read the agent password.

    Read at call time, not at import: a fresh checkout has no secret yet, and a
    missing file used to break the import itself, so nothing in the package
    could even be inspected before the password existed.
    """
    if not SECRET.exists():
        raise RuntimeError(
            f"未找到 agent 口令文件: {SECRET}\n"
            f"生成方法（每台机器一次，不要复用别人的）:\n"
            f'  powershell -c "[Guid]::NewGuid().ToString(\'N\') | '
            f'Out-File -Encoding ascii -NoNewline \'{SECRET}\'"\n'
            f"附加 agent 时必须用同一个值。")
    return SECRET.read_text(encoding="ascii").strip()


class DeadSessionError(RuntimeError):
    """The Forms client lost its server connection (FRM-92102).

    Raised rather than returned because a dead session is not a failure you can
    work around: every window, field and poplist still reads back, and all of it
    is the snapshot from before the line dropped. Code that carries on gets
    confident, detailed, wrong answers - one agent read a poplist here and
    reported that an action "does not exist in this instance". Nothing short of
    a human logging in again fixes it, so the driver refuses to keep going.
    """


class Forms:
    def __init__(self, port=8159):
        self.sess = bg.Session()
        # A minimized frame reports placeholder coordinates near -32000, and the
        # driver locates components by name plus screen position, so nothing
        # would match. Restoring costs one brief foreground blip per session.
        bg.ensure_awake(self.sess)
        self._assert_live()
        self.s = socket.create_connection(("127.0.0.1", port), timeout=60)
        self.f = self.s.makefile("rw", encoding="utf-8", newline="\n")
        self.f.write(f"auth {_token()}\n")
        self.f.flush()
        if (self.f.readline() or "").strip() != "OK auth":
            raise RuntimeError("driver handshake refused")

    # ---- the injected-event half -------------------------------------------

    def cmd(self, line):
        self.f.write(line + "\n")
        self.f.flush()
        return (self.f.readline() or "").strip()

    def click(self, node):
        return self.cmd(f"clickn {node.name}|{node.x}|{node.y}")

    def dclick(self, node):
        """Double click - how a tree row expands, and how it opens."""
        return self.cmd(f"dclickn {node.name}|{node.x}|{node.y}")

    def key(self, code, mods=0):
        return self.cmd(f"key {code} {mods}" if mods else f"key {code}")

    def type(self, text):
        return self.cmd(f"type {text}")

    def wipe(self):
        """Empty the focused item. Typing appends, so this comes first."""
        self.key(VK_END)
        self.key(VK_HOME, SHIFT)
        self.key(VK_DELETE)

    # ---- the Access Bridge half --------------------------------------------

    def forms(self):
        return self.sess.forms()

    def fields(self, form, editable_only=True):
        return [n for n in self.sess.nodes()
                if n.form == form and n.role == "text" and (n.name or "")
                and (not editable_only or "editable" in (n.states or ""))]

    def field(self, form, name, index=0, editable=True):
        """Locate a field by name. Booked lines are read-only, so clicking a
        line to place the cursor has to be able to target a protected item."""
        pool = self.fields(form, editable_only=editable)
        c = [n for n in pool if n.name == name]
        if not c:
            c = [n for n in pool if name in (n.name or "")]
        if not c:
            # "No editable field" is the wrong story when the field is right
            # there but Forms has protected it - and it is the story that sends
            # people looking for a typo, or rebuilding the event plumbing. Say
            # which of the two it is, and let Forms speak for itself.
            if editable:
                seen = [n for n in self.fields(form, editable_only=False)
                        if n.name == name or name in (n.name or "")]
                if seen:
                    why = [m for m in self.status() if "FRM-" in m or "APP-" in m]
                    raise LookupError(
                        f"{name!r} exists in {form!r} but is not editable"
                        + (f" - Forms says: {'; '.join(why)}" if why else "")
                        + ". A protected record is a form-state problem, not a"
                          " driver problem: put the form into a state that"
                          " accepts input (see the handbook), or pass"
                          " editable=False if you only need to place the cursor.")
            # "That field is not here" is also what a dead session looks like,
            # so rule that out before the caller starts doubting the name.
            self._assert_live()
            raise LookupError(f"no field like {name!r} in {form!r}")
        return sorted(c, key=lambda n: (n.y, n.x))[index]

    def value(self, node):
        return next((m.value or "" for m in self.sess.nodes()
                     if (m.form, m.role, m.x, m.y)
                     == (node.form, node.role, node.x, node.y)), "")

    # ---- combined ----------------------------------------------------------

    def _assert_live(self, forms=None):
        """Refuse to operate on a session that has lost the server.

        Checked when the driver attaches, and again whenever a write did not
        take - those are the two moments where a stale screen would otherwise be
        mistaken for a real answer. Not checked on the happy path: a snapshot
        costs time, and a write that reads back correctly already proves the
        session is alive.
        """
        forms = self.forms() if forms is None else forms
        if any("FRM-92102" in w for w in forms):
            raise DeadSessionError(
                "Forms 会话已断开（FRM-92102）。窗口和字段都还读得到，但全是"
                "断线前的快照 —— 现在的任何观测都不可信。\n"
                "解法：请【人工】重新登录 Forms，再重跑 setup.ps1 附加新 agent。\n"
                "已落库的工作不会丢，用只读 SQL 确认真实进度后从那里继续。")

    def status(self):
        """Forms' own status line - the first thing to read when a write fails.

        Forms answers a refused keystroke on the status line and nowhere else:
        "FRM-40200: Field is protected against update." is what a protected item
        looks like, and no amount of focus or event work will get past it. The
        line lives in labels whose `form` is None - they belong to the frame, not
        to any window - which is why it is easy to miss entirely and to go
        chasing the event plumbing instead.
        """
        return [n.value or n.name for n in self.sess.nodes()
                if n.role == "label" and n.form is None and (n.value or n.name)]

    def probe(self, form, name=None, sentinel="ZQX"):
        """Will this form take typed input right now? Try it; do not guess.

        Static state does not answer this question. JAB reports zero `editable`
        fields on a Sales Orders form whether it was opened correctly or opened
        into a protected record - measured on both, they look identical from the
        outside. What does answer it is writing a sentinel and reading it back.

        Returns (writable, evidence). Anything found in the field is put back.
        """
        if name:
            try:
                cands = [self.field(form, name, editable=False)]
            except LookupError:
                return False, f"{form!r} has no field like {name!r}"
        else:
            # One field is not a verdict on a window. A Purchase Orders form
            # reports 30 editable items and still refuses the first one, because
            # that first one is LOV-only - probing it alone would call the whole
            # form unwritable. Prefer items Forms itself marks editable, and try
            # a few before concluding anything.
            pool = self.fields(form, editable_only=False)
            marked = [n for n in pool if "editable" in (n.states or "")]
            cands = (marked or pool)[:4]
        if not cands:
            return False, f"{form!r} has no text field to probe"

        refused = []
        for node in cands:
            before = self.value(node)
            if self.click(node).startswith("ERR"):
                refused.append(f"{node.name!r}: cursor would not go there")
                continue
            self.wipe()
            self.type(sentinel)
            time.sleep(0.6)
            took = sentinel in self.value(node)
            self.wipe()                   # leave every field as it was found
            if before:
                self.type(before)
            if took:
                return True, f"{node.name!r} accepted input"
            why = [m for m in self.status() if "FRM-" in m or "APP-" in m]
            refused.append(f"{node.name!r}: " + ("; ".join(why) if why
                                                 else "refused, status silent"))
        return False, f"tried {len(cands)} field(s) - " + " | ".join(refused)

    def diagnose(self, form=None):
        """What is the session's state, and what should happen next?

        Meant to be the first call after anything unexpected, and the one an
        agent makes instead of asking a human which window accepts typing.
        """
        forms = self.forms()
        report = {"windows": forms, "status": self.status(), "writable": {}}

        # A dead session first: FRM-92102 means the client gave up reconnecting
        # to the server. Everything still on screen is a corpse - windows,
        # fields and poplists all read back, and all of it is stale. Probing
        # here produces confident nonsense, so say so and stop.
        dead = [w for w in forms if "FRM-92102" in w] + \
               [m for m in report["status"] if "FRM-92102" in m]
        if dead:
            report["dead_session"] = True
            report["next"] = (
                "Forms 会话已断开（FRM-92102）。屏幕上的窗口和字段都还读得到，"
                "但全是过期的 —— 现在做的任何观测和结论都不可信。"
                "唯一的解法是【人工重新登录 Forms】，然后重跑 setup.ps1 附加新 agent。"
                "已保存到数据库的工作不会丢，用 SQL 确认进度后从那里继续。")
            return report
        for w in (forms if form is None else [form]):
            if w.lower().startswith(("find", "note", "decision", "caution",
                                     "error", "forms")):
                continue                  # a modal blocks everything behind it
            report["writable"][w] = self.probe(w)
        modal = [w for w in forms
                 if w.lower().startswith(("find", "note", "decision", "caution",
                                          "error"))]
        if modal:
            report["next"] = (f"dismiss the modal {modal[0]!r} first - nothing "
                              "behind it can be driven while it is up")
        elif any(ok for ok, _ in report["writable"].values()):
            report["next"] = "a window accepts input; proceed"
        else:
            msgs = "; ".join(report["status"])
            report["next"] = (
                "no window accepts input. Forms' status line is the instruction: "
                f"{msgs}. FRM-40200 means the record is protected - reopen the "
                "form the way that creates an enterable record. An APP- message "
                "usually names a prerequisite field to fill first.")
        return report

    def locate(self, wanted, form=None, sweep_tabs=True):
        """Where is the control that does `wanted`? Search every kind, not one.

        This exists because the expensive failure is not "the control is hard to
        drive" - it is looking for it in the wrong kind of control and
        concluding the feature is absent. Measured: an agent searched the
        Actions poplist on all four tabs of Shipping Transactions, 104 entries,
        decided Ship Confirm "does not exist in this instance" and blamed
        responsibility permissions. Ship Confirm is a push button on the
        Delivery tab, enabled, in plain sight.

        So the search covers buttons, menu items, tabs, checkboxes and every
        poplist entry (read reflectively, because JAB shows only a scrolled
        slice), and by default visits each tab, because a control on an
        unselected tab is not in the tree at all.

        Returns a list of hits, each saying what kind it is and how to invoke
        it. Finding nothing is then real evidence, not an assumption.
        """
        want = wanted.lower()
        hits, seen = [], set()

        def scan(tab_name):
            for n in self.sess.nodes():
                if form and n.form != form:
                    continue
                nm = (n.name or "").strip()
                if n.role in ("push button", "menu item", "page tab",
                              "check box", "radio button",
                              "text") and want in nm.lower():
                    k = (n.role, nm, n.x, n.y)
                    if k in seen:
                        continue
                    seen.add(k)
                    hits.append({
                        "kind": n.role, "name": nm, "window": n.form,
                        "tab": tab_name, "x": n.x, "y": n.y,
                        "enabled": "enabled" in (n.states or ""),
                        "how": {"push button": "f.press(window, name) 或 clickn",
                                "menu item": "菜单里点它",
                                "page tab": "点中心 (x+w//2, y+h//2)",
                                "check box": "toggle name|x|y|true",
                                "radio button": "clickn",
                                "text": "f.set(window, name, 值)"}[n.role],
                        # A disabled control is the single most misread signal
                        # here: it means a precondition is unmet, not that the
                        # feature is absent. Measured: an agent pressed a
                        # Currency button while the cursor sat in the lines
                        # block, got the wrong window, and concluded the field
                        # could not be changed through the UI at all. Putting
                        # the cursor in the header block enabled the button and
                        # the change went through.
                        "note": "" if "enabled" in (n.states or "") else
                                "当前禁用 —— 前提没满足，不是功能不存在。"
                                "先把光标放进相关的块（表头/行），再看它是否变为可用"})
                # A poplist hides its entries behind reflection - ask the agent.
                if n.role == "combo box" and n.name:
                    key = f"{n.name}|{n.x}|{n.y}"
                    for i, txt in re.findall(r"\[(\d+)\]([^\[]+)",
                                             self.cmd(f"items {key}")):
                        if want not in txt.strip().lower():
                            continue
                        k = ("poplist", txt.strip(), n.x, n.y)
                        if k in seen:
                            continue
                        seen.add(k)
                        hits.append({
                            "kind": f"poplist item ({n.name})",
                            "name": txt.strip(), "window": n.form,
                            "tab": tab_name, "x": n.x, "y": n.y,
                            "enabled": True, "index": int(i),
                            "how": f"gclick 中心 → pick {key}|{i} → key 10 → 按 Go"})

        scan(self._current_tab(form))
        if sweep_tabs:
            for t in self._tabs(form):
                if t["selected"]:
                    continue
                self.cmd(f"click {t['x'] + t['w'] // 2} {t['y'] + t['h'] // 2}")
                time.sleep(1.5)
                self.sess = bg.Session()
                scan(t["name"])
        return hits

    def _tabs(self, form=None):
        return [{"name": n.name, "x": n.x, "y": n.y, "w": n.w, "h": n.h,
                 "selected": "selected" in (n.states or "")}
                for n in self.sess.nodes()
                if n.role == "page tab" and (not form or n.form == form) and n.name]

    def _current_tab(self, form=None):
        return next((t["name"] for t in self._tabs(form) if t["selected"]), None)

    def set(self, form, name, text, index=0, tab=False, settle=0.35):
        """Click the field, empty it, type, and read the value back."""
        node = self.field(form, name, index)
        got = self.click(node)
        if got.startswith("ERR"):
            return False, got
        self.wipe()
        self.type(text)
        if tab:
            self.key(VK_TAB)
        time.sleep(settle)
        back = self.value(node)
        if back.strip() == text.strip():
            return True, back
        # A write that did not take is the moment to ask whether the session is
        # still there - a dead one fails exactly like a protected field.
        self._assert_live()
        # The keystrokes went somewhere; Forms just would not take them. Say why
        # rather than leaving the caller to suspect the event channel.
        why = [m for m in self.status() if "FRM-" in m or "APP-" in m]
        return False, (f"{back!r} | Forms says: {'; '.join(why)}" if why else back)

    def tab(self, form, title, settle=2.0):
        """Select a page tab.

        Tabs are drawn inside the toolkit's own tab bar rather than as separate
        components, so a click by name finds nothing - but the Access Bridge
        exposes them as selectable rows, and selecting one is a navigation Forms
        performs itself. Each channel is used for what it is good at.
        """
        lists = [n for n in self.sess.nodes()
                 if n.form == form and n.role == "page tab list"]
        for tl in lists:
            rows = bg.rows(self.sess, tl)
            hit = next((i for i, role, nm, st in rows if nm == title), None)
            if hit is None:
                continue
            fresh = next(n for n in self.sess.nodes()
                         if n.form == form and n.role == "page tab list"
                         and (n.x, n.y) == (tl.x, tl.y))
            bg.select(fresh, hit, settle=settle)
            return True
        return False

    def choose(self, form, combo_name, option, tries=4):
        """Pick an option from a Forms combo by name.

        bg.combo_set reads the list before it has settled and then reports the
        option missing; the visible window can also be scrolled part-way down,
        so the list is re-opened until the wanted entry actually appears.
        """
        import server as S
        for _ in range(tries):
            combo = next((n for n in self.sess.nodes()
                          if n.form == form and n.role == "combo box"
                          and n.name == combo_name), None)
            if combo is None:
                return False
            S._do_action(combo, "Toggle Drop Down", self.sess.hwnd)
            time.sleep(1.2)
            combo = next((n for n in self.sess.nodes()
                          if n.form == form and n.role == "combo box"
                          and n.name == combo_name), None)
            rows = bg.rows(self.sess, combo)
            hit = next((i for i, r, nm, st in rows if nm == option), None)
            if hit is None:
                hit = next((i for i, r, nm, st in rows if option in nm), None)
            if hit is not None:
                bg.select(combo, hit, settle=1.2)
                return True
            S._do_action(combo, "Toggle Drop Down", self.sess.hwnd)
            time.sleep(0.6)
        return False

    def press(self, form, label, settle=5.0, tries=2):
        return bg.press(self.sess, form, label, settle=settle, tries=tries)

    def pick(self, lov_form, wanted):
        return bg.pick(self.sess, lov_form, wanted)

    def close(self):
        try:
            self.f.write("quit\n")
            self.f.flush()
        finally:
            self.s.close()


def modals(f, answer=("ok", "yes"), known=()):
    """Report and answer any note or decision window that appeared."""
    seen = []
    for _ in range(6):
        ms = [x for x in f.forms()
              if x.lower().startswith(("note", "decision", "error", "caution",
                                       "forms"))
              and x not in known]
        if not ms:
            return seen
        for m in ms:
            texts = [n.value for n in f.sess.nodes()
                     if n.form == m and n.role == "text" and n.value]
            seen.append((m, texts[:2]))
            for a in answer:
                if f.press(m, a, settle=2.0):
                    break
            else:
                return seen
    return seen
