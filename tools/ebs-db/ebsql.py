"""Run one SELECT against EBS from the command line.

The methodology's rule 4 - "what the screen cannot answer, ask the database" -
is worthless to an agent that has no way to ask. This is that way: the same
read-only path the MCP server uses, wrapped so anything that can spawn a
process can query.

    python ebsql.py "select order_number, flow_status_code
                       from apps.oe_order_headers_all
                      where order_number = <订单号>"

    python ebsql.py -f query.sql --json --limit 500 --env <环境键名>

Credentials are never in here or in the delivery package - db.py reads them
from ~/.ebs/connections.json, and every statement is screened so only SELECT
and WITH get through.
"""
import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import db


def table(result):
    """Print rows as an aligned table - the form a human or an agent skims."""
    cols = result["columns"]
    rows = result["rows"]
    if not rows:
        return "(0 rows)"
    width = {c: len(str(c)) for c in cols}
    for r in rows:
        for c in cols:
            width[c] = max(width[c], len(str(r.get(c, ""))))
    out = [" | ".join(str(c).ljust(width[c]) for c in cols),
           "-+-".join("-" * width[c] for c in cols)]
    for r in rows:
        out.append(" | ".join(str(r.get(c, "")).ljust(width[c]) for c in cols))
    out.append(f"({len(rows)} rows"
               + (", truncated - raise --limit" if result.get("truncated") else "")
               + ")")
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser(description="Read-only SELECT against EBS.")
    p.add_argument("sql", nargs="?", help="the statement; omit when using -f")
    p.add_argument("-f", "--file", help="read the statement from a file")
    p.add_argument("--env", default="default",
                   help="connections.json 里的环境键名")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--json", action="store_true", help="emit JSON, not a table")
    a = p.parse_args()

    sql = pathlib.Path(a.file).read_text(encoding="utf-8") if a.file else a.sql
    if not sql:
        p.error("give a statement, or -f FILE")

    try:
        with db.ReadOnly(a.env) as ro:
            result = ro.query(sql, limit=a.limit)
    except Exception as e:
        # A failure here is nearly always one of three things, and saying which
        # saves the caller from guessing at the driver.
        print(f"ERROR {type(e).__name__}: {e}", file=sys.stderr)
        print("check: statement is a SELECT/WITH; ~/.ebs/connections.json "
              f"exists and has env {a.env!r}; Oracle client library present.",
              file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str)
          if a.json else table(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
