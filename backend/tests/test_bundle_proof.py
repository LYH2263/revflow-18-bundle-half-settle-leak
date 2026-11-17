"""捆分桶证明 + 半捆收口回归测试（基线所无的新增实体与路径）。

覆盖：
- 任一腿失败 => 整捆零落账、成员全部进隔离共享捆扎键、捆状态「整捆已隔离」；
- 落库后按捆扎键经成员快照/隔离/证明三处都查得全成员；
- 成功/失败捆均有挂归并运行 id 的分桶证明，含成员键集合与余额前后；
- 证明只读接口按归并运行 id 可查、分桶过滤生效；
- 全量归并轮中的失败捆同样走整桶隔离；
- 证明为不可变快照，事后余额再变不回改证明。
"""
from pathlib import Path
import os
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    data_dir = ROOT / "backend" / "data_test_proof"
    if data_dir.exists():
        for f in data_dir.glob("*.db"):
            f.unlink()
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["FIXTURES_DIR"] = str(ROOT / "fixtures")
    with TestClient(app) as c:
        yield c


def _balance(client, account_id):
    for a in client.get("/api/accounts").json():
        if a["id"] == account_id:
            return a["balance_cents"]
    raise AssertionError("账户不存在")


def _entry(client, entry_key):
    rows = client.get("/api/entries").json()
    return next(e for e in rows if e["entry_key"] == entry_key)


def _declare(client, key, members):
    r = client.post(
        "/api/reversal-bundles",
        json={"bundle_key": key, "entry_keys": members},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_failed_bundle_proof_records_members_and_unchanged_balances(client):
    # REV-003 好腿（可配平）+ REV-004 坏腿（无原单）=> 整捆隔离、零落账
    bal_before = {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}
    _declare(client, "B-PROOF-FAIL", ["REV-20261116-003", "REV-20261116-004"])

    run = client.post("/api/reversal-bundles/B-PROOF-FAIL/settle")
    assert run.status_code == 200, run.text
    body = run.json()
    run_id = body["id"]
    assert body["bundle_fail_count"] == 1
    assert body["bundle_success_count"] == 0
    assert body["quarantine_count"] == 2
    assert body["settled_count"] == 0

    # 正式余额分毫不动
    for a in client.get("/api/accounts").json():
        assert a["balance_cents"] == bal_before[a["id"]]

    # 捆状态必须是整捆已隔离（不得标完成）
    b = client.get("/api/reversal-bundles/B-PROOF-FAIL").json()
    assert b["status"] == "整捆已隔离"
    assert b["settle_run_id"] == run_id
    # 成员快照查得全成员
    assert [m["entry_key"] for m in b["members"]] == [
        "REV-20261116-003",
        "REV-20261116-004",
    ]

    # 隔离按捆扎键查得全成员（好腿连带 + 坏腿自身原因）
    q = client.get("/api/quarantine", params={"bundle_key": "B-PROOF-FAIL"}).json()
    assert len(q) == 2
    assert {x["entry_key"] for x in q} == {"REV-20261116-003", "REV-20261116-004"}
    good = next(x for x in q if x["entry_key"] == "REV-20261116-003")
    bad = next(x for x in q if x["entry_key"] == "REV-20261116-004")
    assert "连带隔离" in good["reason"]
    assert bad["reason"] == "冲正无原单"

    # 只读证明接口：按归并运行 id 查
    proofs = client.get(f"/api/settle-runs/{run_id}/bundle-proofs").json()
    assert len(proofs) == 1
    p = proofs[0]
    assert p["settle_run_id"] == run_id
    assert p["bundle_key"] == "B-PROOF-FAIL"
    assert p["bucket"] == "失败捆"
    assert p["member_keys"] == ["REV-20261116-003", "REV-20261116-004"]
    outcomes = {o["entry_key"]: o for o in p["member_outcomes"]}
    assert outcomes["REV-20261116-003"]["outcome"] == "已隔离·连带"
    assert outcomes["REV-20261116-004"]["outcome"] == "已隔离"
    # 失败桶余额前后必须相等（零落账）
    for ab in p["account_balances"]:
        assert ab["balance_before_cents"] == ab["balance_after_cents"]
        assert ab["delta_cents"] == 0
    assert "冲正无原单" in p["fail_reason"]

    # 分桶过滤：只看失败捆有一行，只看成功捆为空
    failed = client.get(
        f"/api/settle-runs/{run_id}/bundle-proofs", params={"bucket": "失败捆"}
    ).json()
    assert len(failed) == 1 and failed[0]["id"] == p["id"]
    ok = client.get(
        f"/api/settle-runs/{run_id}/bundle-proofs", params={"bucket": "成功捆"}
    ).json()
    assert ok == []

    # 运行详情内嵌同一证明，与独立只读接口同源
    assert body["捆分桶证明"][0]["id"] == p["id"]

    # 未知运行 404
    assert client.get("/api/settle-runs/999999/bundle-proofs").status_code == 404


def test_success_bundle_proof_records_balance_before_after(client):
    # REV-001/002 同捆两腿均可配平
    _declare(client, "B-PROOF-OK", ["REV-20261116-001", "REV-20261116-002"])
    bal_before = _balance(client, 1)
    body = client.post("/api/reversal-bundles/B-PROOF-OK/settle").json()
    run_id = body["id"]
    assert body["bundle_success_count"] == 1
    assert _balance(client, 1) == bal_before - 15000

    proofs = client.get(
        f"/api/settle-runs/{run_id}/bundle-proofs", params={"bucket": "成功捆"}
    ).json()
    assert len(proofs) == 1
    p = proofs[0]
    assert p["bucket"] == "成功捆"
    assert p["fail_reason"] == ""
    assert p["member_keys"] == ["REV-20261116-001", "REV-20261116-002"]
    assert all(o["outcome"] == "已归并" for o in p["member_outcomes"])
    acc1 = next(x for x in p["account_balances"] if x["account_id"] == 1)
    assert acc1["balance_after_cents"] - acc1["balance_before_cents"] == -15000
    assert acc1["delta_cents"] == -15000


def test_full_run_failed_bundle_quarantines_whole_and_proofs(client):
    # 造一捆带坏腿的待归捆，交给全量归并：必须整桶隔离而非好腿留账
    from app.db import connect
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO ledger_entries(entry_key, account_id, kind, amount_cents, biz_ref,
              target_entry_key, booked_at, status)
            VALUES ('REV-PROOF-A', 1, '冲正', 1000, 'ORD-9003', 'FWD-20261116-005',
              '2026-11-16T21:00:00Z', '待归并')
            """
        )
        conn.execute(
            """
            INSERT INTO ledger_entries(entry_key, account_id, kind, amount_cents, biz_ref,
              target_entry_key, booked_at, status)
            VALUES ('REV-PROOF-B', 1, '冲正', 1000, 'ORD-X', 'FWD-GHOST',
              '2026-11-16T21:05:00Z', '待归并')
            """
        )
        conn.commit()
    _declare(client, "B-PROOF-MIX", ["REV-PROOF-A", "REV-PROOF-B"])
    bal_before = {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}

    body = client.post("/api/settle-runs").json()
    run_id = body["id"]
    assert body["bundle_fail_count"] >= 1

    # 本运行总余额增量只来自散笔/正向；失败捆零增量（其 delta 恒为 0）
    bal_after = {a["id"]: a["balance_cents"] for a in client.get("/api/accounts").json()}
    total_delta = sum(bal_after.values()) - sum(bal_before.values())
    assert total_delta == body["net_cents"]
    proofs = client.get(f"/api/settle-runs/{run_id}/bundle-proofs").json()
    mix = next(p for p in proofs if p["bundle_key"] == "B-PROOF-MIX")
    assert all(x["delta_cents"] == 0 for x in mix["account_balances"])

    # 好腿不得留账：两腿全部已隔离
    assert _entry(client, "REV-PROOF-A")["status"] == "已隔离"
    assert _entry(client, "REV-PROOF-B")["status"] == "已隔离"
    q = client.get("/api/quarantine", params={"bundle_key": "B-PROOF-MIX"}).json()
    assert {x["entry_key"] for x in q} == {"REV-PROOF-A", "REV-PROOF-B"}
    assert mix["bucket"] == "失败捆"
    assert set(mix["member_keys"]) == {"REV-PROOF-A", "REV-PROOF-B"}


def test_failed_proof_list_endpoint_and_immutability(client):
    failed = client.get("/api/bundle-proofs/failed").json()
    keys = {p["bundle_key"] for p in failed}
    assert {"B-PROOF-FAIL", "B-PROOF-MIX"} <= keys
    assert all(p["bucket"] == "失败捆" for p in failed)

    # 证明不可变：取失败证明快照，再跑一轮归并改变余额，证明内容不变
    target = next(p for p in failed if p["bundle_key"] == "B-PROOF-FAIL")
    snapshot = {
        "member_keys": target["member_keys"],
        "account_balances": target["account_balances"],
    }
    from app.db import connect
    with connect() as conn:
        conn.execute(
            "UPDATE accounts SET balance_cents = balance_cents + 12345 WHERE id = 2"
        )
        conn.commit()
    again = next(
        p
        for p in client.get("/api/bundle-proofs/failed").json()
        if p["id"] == target["id"]
    )
    assert again["member_keys"] == snapshot["member_keys"]
    assert again["account_balances"] == snapshot["account_balances"]
    # 复位余额，避免污染后续互证
    with connect() as conn:
        conn.execute(
            "UPDATE accounts SET balance_cents = balance_cents - 12345 WHERE id = 2"
        )
        conn.commit()


def test_verify_report_counts_proofs_consistent_with_bundle_status(client):
    report = client.post("/api/verify-reports").json()
    assert report["consistent"] == 1
    d = report["detail"]
    # 证明计数与捆状态累计同源一致
    assert d["累计成功捆证明数"] == d["累计成功捆数"]
    assert d["累计失败捆证明数"] == d["累计失败捆数"]
    assert d["累计失败捆证明数"] >= 2
