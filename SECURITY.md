# Security Policy

## 1. Intended use — read this before anything else

> ### ⚠ TEST ENVIRONMENTS ONLY. NOT FOR PRODUCTION.
>
> This is an **internal tool for IT / ERP teams**, for driving a **non-production
> Oracle E-Business Suite instance** — development, test, acceptance, sandbox, training.
>
> **Do not run it against a production instance.** Not "carefully", not "just a
> read", not "only this once". Production is out of scope, untested, and
> explicitly unsupported.

**Who this is for**

- IT / ERP / QA engineers who **administer the instance themselves**, or who have
  **written authorisation** from whoever does.

**Who this is not for**

- Business end users.
- Anyone operating an instance they do not administer and have no permission to
  automate.
- Anyone who has not obtained approval from their own IT and information-security
  functions.

**Before first use, obtain**

1. Approval from your IT / information-security function — this tool attaches a
   Java agent to a running application process, and that is a change your
   security team is entitled to review.
2. A **dedicated automation account**, not a personal one. Actions are auditable
   to the account that performs them; you want them attributable to automation.
3. A **read-only database account**. Never give the verification channel write
   access — see §4.

---

## 2. What this tool actually does

It attaches a Java agent to an **already-running, already-logged-in** Forms
client on the **same machine**, and dispatches AWT events inside that process.

Understanding the shape of that is the whole threat model:

| | |
|---|---|
| **Requires** | An interactive session a human already authenticated, on the local machine |
| **Requires** | Local OS access to that machine, sufficient to attach to the process |
| **Uses** | The JDK's public Attach API — no exploit, no patched binaries, no reverse engineering |
| **Scope** | Whatever that logged-in session could already do by hand |

### It does not

- **Authenticate.** It cannot log in. A human must already be signed in.
- **Escalate privileges.** It acts strictly as the logged-in user, through the
  application's own UI, with every validation and personalisation firing
  normally. It cannot do anything that user could not do manually.
- **Bypass application controls.** Going through the UI is the point: custom
  validations are triggered, not circumvented.
- **Harvest credentials.** It never reads, stores, or transmits passwords.
- **Persist.** The agent lives in the host process and dies with it. Nothing is
  installed, no service is registered, no startup hook is added.
- **Reach the network.** The control port binds to loopback only (§3).

### Realistic risk

The honest framing: this **lowers the effort** for someone who *already* has an
authenticated session on a machine they *already* control. It is not a remote
vulnerability and it grants no access that was not already present.

The material risk is **operator error, not attacker capability** — driving the
wrong record, or running an irreversible business action against real data. That
is exactly why production is excluded, and why §4 exists.

---

## 3. Safety properties built into the design

| Property | How it is enforced |
|---|---|
| **Not reachable from the network** | The agent's `ServerSocket` binds `127.0.0.1` explicitly. Never change this to `0.0.0.0`. |
| **Authenticated control channel** | Every connection must present a per-machine random token before any command is accepted. |
| **No persistence** | The agent is loaded into a running JVM and is gone when that JVM exits. |
| **No credentials in the repository** | The agent token and the database connection file both live outside the tree and are git-ignored. |
| **Read-only verification** | The database helper accepts `SELECT` / `WITH` only; anything else is refused before it reaches the driver. |
| **Does not steal input** | Events are dispatched inside the JVM and never enter the OS input queue, so the operator keeps their keyboard and mouse — and therefore stays able to intervene. Two exceptions, both documented and both with a background alternative: `forms_key` needs a real keystroke, and restoring a minimized Forms window takes the foreground once per session. See "What still touches your input" in the README. |

---

## 4. Operator responsibilities

The tool cannot enforce these. You have to.

1. **Test instances only.** Stated again because it is the one that matters most.
2. **Verify the target record before any irreversible action.** Confirm the
   primary key on screen, compare it against your task, and confirm it in the
   database. An automation driving the *wrong* record is the failure mode that
   actually causes damage.
3. **Keep the database account read-only.** It is both a safety boundary and a
   correctness guarantee: it makes it impossible for the automation to fake a
   result by writing directly to tables.
4. **Never widen the bind address or drop the token.** Both defaults exist for a
   reason.
5. **Treat an AI assistant's explanations as hypotheses, not findings.** Assistants
   driving this tool have produced confident, detailed, and wrong root-cause
   claims. Verify outcomes against the database, not against the narrative.
6. **Delete the token when finished.** It is a live capability for as long as the
   session is open.

---

## 5. Reporting a vulnerability

If you find a security problem, please open a GitHub issue describing the impact
and how to reproduce it. If you believe the issue should not be public before a
fix exists, say so in the issue without the details and a private channel will be
arranged.

Please do not include credentials, hostnames, real record identifiers, or any
other data from your own environment in an issue. Redact before posting.

---

## 6. Disclaimer

This project is provided **as is**, without warranty of any kind, under the terms
of the Apache License 2.0 in `LICENSE`. The authors accept no liability for any
loss or damage arising from its use.

You are responsible for ensuring that your use complies with your organisation's
policies and with the licence terms of the software you operate it against.

**Oracle**, **Oracle E-Business Suite**, and **Oracle Forms** are trademarks of
Oracle Corporation. This project is an independent work. It is **not affiliated
with, endorsed by, sponsored by, or supported by Oracle Corporation**, and it
contains no Oracle source code or binaries.
