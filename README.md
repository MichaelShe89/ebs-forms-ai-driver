# Driving Oracle EBS Forms with an AI assistant

> ## ⚠ For IT teams. Test environments only. Not for production.
>
> This is an internal tool for **IT / ERP / QA engineers** working against a
> **non-production** Oracle E-Business Suite instance — development, test, acceptance, sandbox.
> It is **not** for production systems and **not** for business end users.
> Read [SECURITY.md](SECURITY.md) before you run anything.

**The problem.** Setting up and regression-testing EBS eats enormous amounts of
engineering time, and the Forms client resists ordinary automation. Desktop
automation tools steal the keyboard and mouse, so nothing can run while you work.
Accessibility APIs can *read* the screen but cannot type into a Forms text item.
So teams fall back to clicking through it by hand, or to writing straight to the
database — which silently bypasses every custom validation the business depends
on, and therefore tests nothing.

**The approach here.** Attach a Java agent to the running Forms JVM and dispatch
AWT events *inside the process*. Forms cannot tell those events from a real
click, because they travel the same path — but they never enter the operating
system's input queue, so your keyboard and mouse stay yours.

---

## What it can do

- Drive Forms end to end: fill fields, pick from LOVs, tick checkboxes, choose
  from poplists, switch tabs, walk the Navigator tree, use menus, submit
  concurrent requests, save.
- **Run in the background.** You keep working in other windows while it does.
- **Trigger every customisation.** Because it goes through the UI, Forms
  Personalizations, `WHEN-VALIDATE-ITEM`, and custom validations all fire. This
  is the whole point — it is what writing to the database directly cannot do.
- **Verify against the database,** not against what the screen appears to say.

## What it cannot do

- **OA Framework web pages**, if your organisation uses single-device SSO. The
  documentation explains why and what to do about it.
- Anything needing visual judgement — whether a report *looks* right.

---

## Quick start

**1. A human opens and logs into Forms.** The tool does not log in, and should
not try to launch the client.

**2. Pre-flight:**

```powershell
.\setup.ps1 -CheckOnly
```

Everything must pass before you continue. If something fails, fix it rather than
pressing on — skipping a prerequisite here produces failures that look like
something else entirely, and that is how people lose an afternoon.

**3. Set up:**

```powershell
.\setup.ps1
```

Finds Python and a JDK 8, locates the Forms process, generates a token, compiles
the agent, attaches it, and verifies the handshake.

---

## What is in here

```
setup.ps1                      One-command setup.   ← start here
SECURITY.md                    Threat model and operator responsibilities
EBS-AI-Forms-自动化方案.md      The full manual (Chinese) — written for the AI assistant
tools/ebs-db/
  ├─ ebsql.py                  Run a SELECT from the command line (read-only)
  ├─ verify.py                 Score an unattended run against the database
  └─ db.py                     Connection handling and statement screening
tools/forms-mcp/
  ├─ jab.py bg.py server.py keys.py navigate.py   Reading the UI
  ├─ server.py                 An MCP server exposing the read side as tools
  ├─ agent/                    The Java agent
  │  ├─ Attach.java            Loads the agent into a running JVM
  │  └─ src/si/{Loader,Driver}.java
  └─ driver/
     ├─ drive.py               Main entry point (the `Forms` class)
     ├─ smoke.py               Three self-tests
     └─ nav.py flow.py witness.py cdp.py
```

Keep the directory structure intact — `drive.py` resolves its imports relatively.

> **The manual is currently in Chinese.** The source and its comments are in
> English, and the manual is the next thing to translate. If you would find an
> English edition useful, say so in an issue — it helps to know.

---

## How it works

Three channels, each doing what it is actually good at:

| | Channel | Why |
|---|---|---|
| **Read** | Java Access Bridge | Exposes the whole component tree: windows, fields, values, states, coordinates. An excellent reader. |
| **Write** | AWT events dispatched inside the JVM | The Access Bridge exposes **no action at all** on a text item — `setTextContents` is a no-op. Events from inside the process are what actually drive it. |
| **Verify** | Read-only SQL | The screen saying "success" and the row having changed are different facts. Only one of them is checkable. |

Approaches that do **not** work, all measured, so you need not re-test them:

| Attempt | Outcome |
|---|---|
| `SendInput` | Always lands in the foreground window — steals the operator's input |
| `PostMessage` keystrokes | Keyboard messages need focus ownership; discarded when the window is inactive |
| JAB `setTextContents` | Returns true, changes nothing |
| A separate Windows desktop | `SendInput` only affects the *displayed* desktop, so it buys nothing |

### What still touches your input

"Your keyboard and mouse stay yours" is true of the agent path, and it is worth
being exact about where it stops:

| | |
|---|---|
| `driver/drive.py` — fields, clicks, keys, tabs | **Nothing.** Events are dispatched inside the JVM and never reach the OS input queue. |
| The MCP write tools — menus, buttons, LOVs | **Nothing.** They share an input queue with the Forms UI thread but never call `SetFocus`. |
| `forms_key` | **Takes your keyboard.** A function key must be a real keystroke. Use the menu route or `drive.py` instead; the tool says so. |
| First call of a session, if Forms is minimized | **One brief flash.** Windows will not give an active state to an iconic window, so it is restored and the foreground handed straight back — once per session, not once per action. |
| `forms_type(method="paste")` | **Overwrites your clipboard.** No focus is taken. `drive.py` does not use the clipboard at all. |

The Forms window also has to stay on screen — not minimized, on the displayed
desktop. It will not steal your focus, but it is not invisible either.

---

## Working with an AI assistant

Give the assistant the manual and let it use the tools. Two things are worth
knowing before you start.

**Read the troubleshooting method first.** The manual has 39 catalogued failures
and 12 dead ends, but §14 — *how to debug something not in the catalogue* — is
more valuable than either. You will hit something undocumented.

**And the failure mode that costs the most time:**

> **When an assistant says "this cannot be done", it is usually wrong.**

Measured across one project: assistants produced conclusions like *"this feature
does not exist in this instance"*, *"permissions are restricted"*, *"the field is
read-only and cannot be changed through the UI"*, *"this needs a PL/SQL API"* —
**five times, and all five were wrong.** The real causes were: a minimised
window, a reused agent package name, a responsibility with no operating unit
bound, searching the wrong *kind* of control, and a button disabled because a
precondition was unmet.

Every one of those explanations was specific, plausible, and delivered with
confidence. Someone who does not know EBS well cannot tell them apart from real
findings.

So:

| The assistant says | What to do |
|---|---|
| "Done." | Run `verify.py`. Ten seconds, objective answer. |
| "Can't be done, because X." | **X is probably wrong.** Do not raise a ticket or change a configuration on the strength of it. |

`verify.py` exists for exactly this. It scores a run against the database, so you
never have to adjudicate the assistant's reasoning:

```powershell
python tools\ebs-db\verify.py snapshot --order <order number>   # before
#   ... the assistant works, unattended ...
python tools\ebs-db\verify.py report   --order <order number>   # one screen
```

It answers two questions: **did the pipeline actually advance**, and **did it
touch anything outside the task**. The second matters more. An assistant that
completes the task and quietly ships someone else's delivery has failed, however
well its report reads.

---

## Requirements

| | |
|---|---|
| Oracle EBS R12 | Forms client, already logged in by a human |
| JDK 8 | Must match the Forms JRE's major version. A JRE is not enough — the Attach API lives in `tools.jar` |
| Java Access Bridge | `jabswitch -enable`, then **restart Forms** |
| Python 3 | **Bit-width must match the JVM.** The Access Bridge does not bridge bitness |
| A read-only database account | For verification. Read-only is not optional — see SECURITY.md |

Credentials live in `~/.ebs/connections.json`, outside this repository:

```json
{"default": {"user": "...", "password": "...", "dsn": "host:port/service"}}
```

The key is a label you choose; add more for more environments and select with
`--env`.

---

## Adapting it to your instance

Everything here is generic. What makes it save real time is a record of **your**
instance's flows and custom gates — which only you can build. §14.10 of the
manual describes how: query first, walk a flow by hand, write down the exact
wording of every refusal, then turn each into a *symptom → cause → remedy* entry.

That appendix is the part that compounds. The rest is scaffolding for it.

---

## Licence and attribution

Apache License 2.0 — see [LICENSE](LICENSE).

**Oracle**, **Oracle E-Business Suite**, and **Oracle Forms** are trademarks of
Oracle Corporation. This project is independent work, **not affiliated with,
endorsed by, or supported by Oracle**, and contains no Oracle source or binaries.

Provided **as is**, without warranty. You are responsible for ensuring your use
complies with your organisation's policies and with the licence terms of the
software you run it against. **Test environments only.**
