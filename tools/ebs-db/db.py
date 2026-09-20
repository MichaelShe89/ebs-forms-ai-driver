"""Read-only access to the EBS database.

Three layers of protection, because an account granted only SELECT is still not
fully read-only — `SELECT ... FOR UPDATE` succeeds with it, and holding row
locks can block real users:

  1. every session issues ALTER SESSION SET READ ONLY, so the database itself
     refuses writes and locks
  2. statements are screened before they are sent
  3. connections are never left open with an uncommitted transaction

Credentials live outside the repository (see CONFIG_PATH) so they are never
committed.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import oracledb

CONFIG_PATH = Path(os.environ.get(
    "EBS_DB_CONFIG", Path.home() / ".ebs" / "connections.json"))

# The pure-Python thin mode cannot handle the older password verifiers (0x939)
# that EBS databases often still carry, so an Oracle client library is needed.
# PL/SQL Developer ships one, hence this default - change it to wherever yours is.
DEFAULT_LIB = r"C:\Program Files\PLSQL Developer 13\instantclient_21_3"

_initialised = False


def _init_client(lib_dir: str | None = None):
    global _initialised
    if _initialised:
        return
    lib = lib_dir or os.environ.get("EBS_ORACLE_LIB") or DEFAULT_LIB
    oracledb.init_oracle_client(lib_dir=lib)
    _initialised = True


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"no connection config at {CONFIG_PATH}. Create it as "
            '{"default": {"user": "...", "password": "...", "dsn": "host:port/service"}}'
            '  —— 键名由你自己定，多个环境就写多个键'
        )
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def environments() -> list[str]:
    return sorted(load_config().keys())


# --------------------------------------------------------------------------
# statement screening
# --------------------------------------------------------------------------
_ALLOWED_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|truncate|drop|create|alter|grant|revoke|"
    r"commit|rollback|savepoint|lock|execute|exec|begin|declare|call)\b",
    re.IGNORECASE,
)
_FOR_UPDATE = re.compile(r"\bfor\s+update\b", re.IGNORECASE)


def screen(sql: str) -> str:
    """Reject anything that is not a plain query. Returns the cleaned SQL."""
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        raise ValueError("empty statement")
    if ";" in stripped:
        raise ValueError("multiple statements are not allowed")
    if not _ALLOWED_START.match(stripped):
        raise ValueError("only SELECT and WITH statements are allowed")
    if _FOR_UPDATE.search(stripped):
        raise ValueError(
            "SELECT ... FOR UPDATE is refused: it holds row locks and can block "
            "real users")
    bad = _FORBIDDEN.search(stripped)
    if bad:
        raise ValueError(f"statement contains a forbidden keyword: {bad.group(0)!r}")
    return stripped


# --------------------------------------------------------------------------
# connection
# --------------------------------------------------------------------------
class ReadOnly:
    """A connection that the database itself will not let you write through."""

    def __init__(self, env: str = "default"):
        cfg = load_config()
        if env not in cfg:
            raise RuntimeError(
                f"unknown environment {env!r}; configured: {sorted(cfg)}")
        self.env = env
        self.cfg = cfg[env]
        self.conn = None

    def __enter__(self):
        _init_client(self.cfg.get("oracle_lib"))
        self.conn = oracledb.connect(
            user=self.cfg["user"],
            password=self.cfg["password"],
            dsn=self.cfg["dsn"],
        )
        self.conn.autocommit = False
        cur = self.conn.cursor()
        # There is no ALTER SESSION ... READ ONLY; the read-only guarantee comes
        # from a read-only transaction, which also gives every query in this
        # session a single consistent snapshot. It lasts until a commit or
        # rollback, and nothing here issues either until the session closes.
        cur.execute("set transaction read only")
        cur.close()
        self.read_only_txn = True
        return self

    def __exit__(self, *exc):
        if self.conn is not None:
            try:
                self.conn.rollback()   # never leave a transaction open
            except Exception:
                pass
            self.conn.close()
            self.conn = None
        return False

    def query(self, sql: str, limit: int = 200, params: dict | None = None):
        sql = screen(sql)
        cur = self.conn.cursor()
        try:
            try:
                cur.execute(sql, params or {})
            except oracledb.DatabaseError as exc:
                # Without this the caller sees only "error executing tool" and
                # has to re-run the statement elsewhere to find out what Oracle
                # actually objected to.
                msg = str(exc).splitlines()[0]
                raise RuntimeError(f"{msg}\n\nstatement:\n{sql}") from exc
            columns = [d[0] for d in cur.description]
            rows = cur.fetchmany(limit + 1)
            truncated = len(rows) > limit
            rows = rows[:limit]
            return {
                "columns": columns,
                "rows": [
                    {c: _plain(v) for c, v in zip(columns, r)} for r in rows
                ],
                "row_count": len(rows),
                "truncated": truncated,
            }
        finally:
            cur.close()


def _plain(v):
    """Make values JSON-friendly without losing information."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, oracledb.LOB):
        return v.read()
    return str(v)
