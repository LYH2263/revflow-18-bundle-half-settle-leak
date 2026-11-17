"""多腿冲正净额捆扎（multi_leg_net）。

落库形态（二选一中的「独立捆扎表」）：
- ``reversal_bundles``：捆扎身份与捆状态（待归捆/整捆已归并/整捆已隔离）；
- ``reversal_bundle_members``：成员冲正键挂接，一笔冲正至多属于一捆。
  成员行在声明后即不可变快照：归并引擎不改写、不删除成员，按捆扎键永远查得全成员。

捆扎集合与散笔在落库层即可区分，不依赖请求体数组。
隔离项（``quarantine_items.bundle_key``）与归并运行（``settle_runs.scope/bundle_key``）
同样冗余捆扎键，保证捆扎记录、流水、隔离、余额、报告同源可查。

半捆收口（二选一中的「全捆预检」路径）：归并引擎先以 ``evaluate_bundle`` 纯预演
整捆可配平性，**预检通过后才允许写任何正式余额**；预检失败则整捆零落账——成员
全部进未平隔离（好腿标「连带隔离」），正式余额分毫不动，捆状态置「整捆已隔离」。
禁止好腿留账、坏腿只改隔离。成员状态、余额、隔离、捆状态、捆分桶证明在同一
SQLite 事务内提交，中途异常整体回滚。

捆分桶证明（``bundle_settlement_proofs``，基线所无的新增实体）：每个归并运行处理
过的每一捆留一行不可变证明，记录成功/失败分桶、完整成员键集合快照、逐成员结果、
各账户余额前后值与失败原因，挂归并运行 id 提供只读查询。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from fastapi import HTTPException

# ---- 捆状态 ----
BUNDLE_PENDING = "待归捆"
BUNDLE_SETTLED = "整捆已归并"
BUNDLE_QUARANTINED = "整捆已隔离"

BUNDLE_STATUSES = (BUNDLE_PENDING, BUNDLE_SETTLED, BUNDLE_QUARANTINED)

# ---- 捆分桶证明：成功桶 / 失败桶 ----
PROOF_BUCKET_SUCCESS = "成功捆"
PROOF_BUCKET_FAILED = "失败捆"
PROOF_BUCKETS = (PROOF_BUCKET_SUCCESS, PROOF_BUCKET_FAILED)

# 成员级结果（证明快照用）
MEMBER_SETTLED = "已归并"
MEMBER_QUARANTINED = "已隔离"
MEMBER_QUARANTINED_CASCADE = "已隔离·连带"

BUNDLE_DDL = """
CREATE TABLE IF NOT EXISTS reversal_bundles (
  id INTEGER PRIMARY KEY,
  bundle_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT '待归捆',
  member_count INTEGER NOT NULL,
  settle_run_id INTEGER,
  fail_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  settled_at TEXT
);
CREATE TABLE IF NOT EXISTS reversal_bundle_members (
  id INTEGER PRIMARY KEY,
  bundle_id INTEGER NOT NULL REFERENCES reversal_bundles(id),
  entry_key TEXT NOT NULL UNIQUE,
  seq INTEGER NOT NULL,
  UNIQUE(bundle_id, entry_key)
);
-- 捆分桶证明（基线所无的新增实体）：一捆一次归并一行，不可变，挂归并运行 id。
-- 成员键集合、逐成员结果、账户余额前后均为落库时快照，事后不改写。
CREATE TABLE IF NOT EXISTS bundle_settlement_proofs (
  id INTEGER PRIMARY KEY,
  settle_run_id INTEGER NOT NULL,
  bundle_id INTEGER NOT NULL REFERENCES reversal_bundles(id),
  bundle_key TEXT NOT NULL,
  bucket TEXT NOT NULL,
  member_keys_json TEXT NOT NULL,
  member_outcomes_json TEXT NOT NULL,
  account_balances_json TEXT NOT NULL,
  fail_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bundle_proofs_run
  ON bundle_settlement_proofs(settle_run_id);
CREATE INDEX IF NOT EXISTS idx_bundle_proofs_bucket
  ON bundle_settlement_proofs(settle_run_id, bucket);
"""

# 既有表追加列（幂等迁移）
_ALTER_COLUMNS = {
    "quarantine_items": [
        ("bundle_key", "TEXT"),
    ],
    "settle_runs": [
        ("scope", "TEXT NOT NULL DEFAULT '散笔'"),
        ("bundle_key", "TEXT"),
        ("bundle_success_count", "INTEGER NOT NULL DEFAULT 0"),
        ("bundle_fail_count", "INTEGER NOT NULL DEFAULT 0"),
        ("bundle_member_count", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建捆扎表并给既有表补列；可重复执行。"""
    conn.executescript(BUNDLE_DDL)
    for table, columns in _ALTER_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


# ---------------- 捆扎声明 / 查询 ----------------

def declare_bundle(
    conn: sqlite3.Connection,
    bundle_key: str,
    entry_keys: list[str],
) -> dict:
    """为若干待归并冲正声明同一捆扎键；校验与落库在调用方事务内完成。"""
    bundle_key = (bundle_key or "").strip()
    if not bundle_key:
        raise HTTPException(400, "捆扎键不能为空")
    if not entry_keys:
        raise HTTPException(400, "捆扎成员至少一笔冲正")
    if len(set(entry_keys)) != len(entry_keys):
        raise HTTPException(400, "捆扎成员存在重复冲正键")

    exists = conn.execute(
        "SELECT 1 FROM reversal_bundles WHERE bundle_key = ?", (bundle_key,)
    ).fetchone()
    if exists:
        raise HTTPException(400, f"捆扎键已存在：{bundle_key}")

    # 成员资格：存在、冲正、待归并、未挂其它捆
    placeholders = ",".join("?" for _ in entry_keys)
    rows = conn.execute(
        f"""
        SELECT entry_key, kind, status FROM ledger_entries
        WHERE entry_key IN ({placeholders})
        """,
        tuple(entry_keys),
    ).fetchall()
    found = {r["entry_key"]: r for r in rows}
    for key in entry_keys:
        row = found.get(key)
        if row is None:
            raise HTTPException(400, f"冲正流水不存在：{key}")
        if row["kind"] != "冲正":
            raise HTTPException(400, f"仅冲正流水可入捆，正向流水不可：{key}")
        if row["status"] != "待归并":
            raise HTTPException(400, f"冲正非待归并状态，不能入捆：{key}（{row['status']}）")

    owned = conn.execute(
        f"""
        SELECT m.entry_key, b.bundle_key
        FROM reversal_bundle_members m
        JOIN reversal_bundles b ON b.id = m.bundle_id
        WHERE m.entry_key IN ({placeholders})
        """,
        tuple(entry_keys),
    ).fetchall()
    if owned:
        first = owned[0]
        raise HTTPException(400, f"冲正已属于捆扎 {first['bundle_key']}：{first['entry_key']}")

    now = _now()
    cur = conn.execute(
        """
        INSERT INTO reversal_bundles(bundle_key, status, member_count, created_at)
        VALUES (?, '待归捆', ?, ?)
        """,
        (bundle_key, len(entry_keys), now),
    )
    bundle_id = cur.lastrowid
    for seq, key in enumerate(entry_keys, start=1):
        conn.execute(
            """
            INSERT INTO reversal_bundle_members(bundle_id, entry_key, seq)
            VALUES (?, ?, ?)
            """,
            (bundle_id, key, seq),
        )
    return get_bundle(conn, bundle_key)


def _row_to_bundle(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    bundle = dict(row)
    bundle["members"] = [
        dict(r)
        for r in conn.execute(
            """
            SELECT m.seq, m.entry_key, e.account_id, e.amount_cents,
                   e.target_entry_key, e.status AS entry_status,
                   a.code AS account_code
            FROM reversal_bundle_members m
            JOIN ledger_entries e ON e.entry_key = m.entry_key
            JOIN accounts a ON a.id = e.account_id
            WHERE m.bundle_id = ?
            ORDER BY m.seq
            """,
            (bundle["id"],),
        ).fetchall()
    ]
    return bundle


def get_bundle(conn: sqlite3.Connection, bundle_key: str) -> dict:
    row = conn.execute(
        "SELECT * FROM reversal_bundles WHERE bundle_key = ?", (bundle_key,)
    ).fetchone()
    if not row:
        raise HTTPException(404, f"捆扎不存在：{bundle_key}")
    return _row_to_bundle(conn, row)


def list_bundles(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM reversal_bundles ORDER BY id"
    ).fetchall()
    return [_row_to_bundle(conn, r) for r in rows]


def load_pending_bundles(conn: sqlite3.Connection) -> list[dict]:
    """全部待归捆（含成员流水行），按捆 id 稳定排序。"""
    bundle_rows = conn.execute(
        "SELECT * FROM reversal_bundles WHERE status = '待归捆' ORDER BY id"
    ).fetchall()
    out: list[dict] = []
    for b in bundle_rows:
        legs = conn.execute(
            """
            SELECT e.*
            FROM reversal_bundle_members m
            JOIN ledger_entries e ON e.entry_key = m.entry_key
            WHERE m.bundle_id = ? AND e.status = '待归并'
            ORDER BY m.seq
            """,
            (b["id"],),
        ).fetchall()
        out.append({"bundle": dict(b), "legs": [dict(r) for r in legs]})
    return out


def mark_bundle_result(
    conn: sqlite3.Connection,
    bundle_id: int,
    status: str,
    settle_run_id: int,
    fail_reason: str = "",
) -> None:
    conn.execute(
        """
        UPDATE reversal_bundles
        SET status = ?, settle_run_id = ?, fail_reason = ?,
            settled_at = CASE WHEN ? = '待归捆' THEN NULL ELSE ? END
        WHERE id = ?
        """,
        (status, settle_run_id, fail_reason, status, _now(), bundle_id),
    )


def load_bundle_member_keys(conn: sqlite3.Connection, bundle_id: int) -> list[str]:
    """不可变成员快照：按声明顺序返回该捆全部成员冲正键。

    不按流水状态过滤——成员行一经声明即固定，保证落库后按捆扎键查得全成员。
    """
    rows = conn.execute(
        """
        SELECT entry_key FROM reversal_bundle_members
        WHERE bundle_id = ?
        ORDER BY seq
        """,
        (bundle_id,),
    ).fetchall()
    return [r["entry_key"] for r in rows]


def insert_bundle_proof(
    conn: sqlite3.Connection,
    *,
    settle_run_id: int,
    bundle_id: int,
    bundle_key: str,
    bucket: str,
    member_keys: list[str],
    member_outcomes: list[dict],
    account_balances: list[dict],
    fail_reason: str = "",
) -> int:
    """写入一行捆分桶证明（不可变快照）。与归并事务同生共死。"""
    if bucket not in PROOF_BUCKETS:
        raise ValueError(f"未知捆分桶：{bucket}")
    cur = conn.execute(
        """
        INSERT INTO bundle_settlement_proofs(
          settle_run_id, bundle_id, bundle_key, bucket,
          member_keys_json, member_outcomes_json, account_balances_json,
          fail_reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            settle_run_id,
            bundle_id,
            bundle_key,
            bucket,
            json.dumps(member_keys, ensure_ascii=False),
            json.dumps(member_outcomes, ensure_ascii=False),
            json.dumps(account_balances, ensure_ascii=False),
            fail_reason,
            _now(),
        ),
    )
    return cur.lastrowid


def _row_to_proof(row: sqlite3.Row) -> dict:
    proof = dict(row)
    proof["member_keys"] = json.loads(proof.pop("member_keys_json"))
    proof["member_outcomes"] = json.loads(proof.pop("member_outcomes_json"))
    proof["account_balances"] = json.loads(proof.pop("account_balances_json"))
    return proof


def list_bundle_proofs(
    conn: sqlite3.Connection,
    settle_run_id: int,
    bucket: str | None = None,
) -> list[dict]:
    """按归并运行 id 只读列出捆分桶证明；可再按成功/失败桶过滤。"""
    if bucket is not None and bucket not in PROOF_BUCKETS:
        raise HTTPException(400, f"未知捆分桶：{bucket}")
    if bucket is not None:
        rows = conn.execute(
            """
            SELECT * FROM bundle_settlement_proofs
            WHERE settle_run_id = ? AND bucket = ?
            ORDER BY id
            """,
            (settle_run_id, bucket),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM bundle_settlement_proofs
            WHERE settle_run_id = ?
            ORDER BY id
            """,
            (settle_run_id,),
        ).fetchall()
    return [_row_to_proof(r) for r in rows]


def list_failed_bundle_proofs(conn: sqlite3.Connection) -> list[dict]:
    """只读列出全部失败捆证明（未平隔离界面按捆展示用）。"""
    rows = conn.execute(
        """
        SELECT * FROM bundle_settlement_proofs
        WHERE bucket = ?
        ORDER BY id DESC
        """,
        (PROOF_BUCKET_FAILED,),
    ).fetchall()
    return [_row_to_proof(r) for r in rows]


# ---------------- 腿级配平校验（散笔 / 捆内共用） ----------------

def evaluate_leg(
    leg: dict,
    forwards_by_key: dict[str, dict],
    capacity: dict[str, int],
    reserved: dict[str, int],
) -> tuple[bool, str]:
    """单腿是否可解析原单、账户一致且不超额。

    ``capacity`` 为原单扣除历史已归并冲正后的剩余额度；
    ``reserved`` 为本轮（含同捆先序腿）已临时占用额度。
    """
    target = leg.get("target_entry_key")
    if not target or target not in forwards_by_key:
        return False, "冲正无原单"
    fwd = forwards_by_key[target]
    if fwd["account_id"] != leg["account_id"]:
        return False, "冲正账户与原单不一致"
    remain = capacity[target] - reserved.get(target, 0)
    if leg["amount_cents"] > remain:
        return False, "冲正超额"
    return True, ""


def evaluate_bundle(
    legs: list[dict],
    forwards_by_key: dict[str, dict],
    capacity: dict[str, int],
    base_reserved: dict[str, int],
) -> tuple[bool, list[str]]:
    """整捆预演：通过临时占用表判断是否全腿满足；不动任何正式数据。

    返回 (是否整捆通过, 各腿原因)。失败时原因列表逐腿可解释。
    """
    tentative = dict(base_reserved)
    reasons: list[str] = []
    all_ok = True
    for leg in legs:
        ok, reason = evaluate_leg(leg, forwards_by_key, capacity, tentative)
        if not ok:
            all_ok = False
            reasons.append(reason)
        else:
            reasons.append("")
            tentative[leg["target_entry_key"]] = (
                tentative.get(leg["target_entry_key"], 0) + leg["amount_cents"]
            )
    return all_ok, reasons
