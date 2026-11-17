from fastapi import APIRouter, Query

from app.services import settle as svc

router = APIRouter(tags=["归并"])


@router.get("/settle-runs")
def get_runs():
    return svc.list_settle_runs()


@router.get("/settle-runs/{run_id}")
def get_run(run_id: int):
    return svc.get_settle_run(run_id)


@router.post("/settle-runs")
def post_run():
    return svc.run_settle()


@router.get("/settle-runs/{run_id}/bundle-proofs")
def get_run_bundle_proofs(
    run_id: int,
    bucket: str | None = Query(None, description="可选分桶过滤：成功捆 / 失败捆"),
):
    """按归并运行 id 只读查询捆分桶证明（成员键集合、逐成员结果、余额前后）。"""
    return svc.list_run_bundle_proofs(run_id, bucket)


@router.get("/bundle-proofs/failed")
def get_failed_bundle_proofs():
    """只读列出全部失败捆证明（未平隔离界面按捆展示）。"""
    return svc.list_failed_bundle_proofs()
