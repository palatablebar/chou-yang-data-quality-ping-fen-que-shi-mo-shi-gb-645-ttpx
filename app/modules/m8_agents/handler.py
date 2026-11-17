"""M8 入口：受理人归属页、映射更新（含重算发布）、待裁决工作台。

映射更新与「缺口 + 报告一起重算」是同一个动作：一次 PUT / POST 先在内存里
解析并校验映射，再带着这份映射跑完整条流水线，最后把「运行行 + 数据集原文
+ 映射」在同一个事务里发布。中途失败整体回滚：旧映射、旧缺口、旧报告继续
对外服务，失败的尝试单独留痕。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Request, UploadFile

from ...errors import BadRequest, NotFound
from ...web import Ctx, get_ctx, render
from ..m6_score import runstore, service as score_service
from . import attribution, service

router = APIRouter(tags=["M8 受理人归属"])

MAX_MAPPING_BYTES = 2 * 1024 * 1024


@router.get("/agents")
def page_agents(request: Request, run_id: str | None = None,
                ctx: Ctx = Depends(get_ctx)):
    run, result = ctx.current_result(run_id)
    agents = (result or {}).get("agents")
    stored = ctx.store.get_agent_mapping(run["name"]) if run is not None else None
    sample = service.sample_mapping(ctx.settings, run["name"]) if run is not None else None
    mapping_text = ""
    if stored is not None:
        mapping_text = stored["content"].decode("utf-8-sig", errors="replace")
    elif sample is not None:
        mapping_text = sample.decode("utf-8-sig", errors="replace")
    return render(request, "agents.html", page="agents", page_title="受理人归属",
                  run=run, result=result, agents=agents,
                  mapping=(agents or {}).get("mapping"),
                  mapping_text=mapping_text,
                  mapping_change=(result or {}).get("mapping_change"),
                  publish=(result or {}).get("publish"),
                  state_labels=attribution.STATE_LABELS,
                  attempts=[dict(item) for item in
                            ctx.store.list_publish_attempts(limit=8)])


@router.get("/api/agents")
def api_agents(run_id: str | None = None, ctx: Ctx = Depends(get_ctx)) -> dict:
    run, result = score_service.latest(ctx.store, run_id)
    payload = result.get("agents") or service.empty()
    return {"run": runstore.summary(run),
            "agents": service.public(payload),
            "mapping_change": result.get("mapping_change"),
            "publish": result.get("publish")}


@router.get("/api/agents/mapping")
def api_get_mapping(dataset: str | None = None,
                    ctx: Ctx = Depends(get_ctx)) -> dict:
    name = dataset or _default_name(ctx)
    _dataset_or_404(ctx.store, name)
    mapping, meta = service.load(ctx.store, name, ctx.settings)
    return {"dataset": name, "exists": meta["exists"], "source": meta["source"],
            "mapping": attribution.public_mapping(mapping, meta["digest"],
                                                  meta["bytes"]),
            "content": (meta["content"] or b"").decode("utf-8-sig", errors="replace")}


@router.put("/api/agents/mapping")
async def api_put_mapping(file: UploadFile | None = File(default=None),
                          dataset: str | None = None,
                          dry_run: bool = False,
                          expected_version: str = "",
                          ctx: Ctx = Depends(get_ctx)) -> dict:
    """上传映射；默认立即重算并发布，dry_run=true 只校验不落库。"""
    if file is None:
        raise BadRequest("需要上传映射文件", code="E_MAPPING_INVALID")
    data = await file.read()
    return _apply_mapping(ctx, data, dataset=dataset, source="upload",
                          dry_run=dry_run,
                          expected_version=expected_version or None)


@router.post("/api/agents/mapping")
def api_post_mapping(payload: dict | None = None,
                     ctx: Ctx = Depends(get_ctx)) -> dict:
    """JSON 内联映射：{"dataset": "...", "agents": [{raw, agent_id, display_name}]}"""
    body = payload or {}
    items = body.get("agents")
    if not isinstance(items, list):
        raise BadRequest("agents 需要是数组", code="E_MAPPING_INVALID")
    data = json.dumps({"agents": items}, ensure_ascii=False).encode("utf-8")
    return _apply_mapping(ctx, data, dataset=body.get("dataset"),
                          source="inline", dry_run=bool(body.get("dry_run")),
                          expected_version=body.get("expected_version"))


@router.post("/api/agents/recompute")
def api_recompute(payload: dict | None = None,
                  ctx: Ctx = Depends(get_ctx)) -> dict:
    """不改映射，只按当前映射重算发布一次（用于数据更新或复盘）。"""
    body = payload or {}
    name = str(body.get("dataset") or _default_name(ctx))
    _dataset_or_404(ctx.store, name)
    result = score_service.run(ctx.settings, ctx.store, name=name,
                               expected_version=body.get("expected_version"))
    return _public_run(result, ctx)


@router.get("/api/agents/unresolved")
def api_unresolved(run_id: str | None = None,
                   ctx: Ctx = Depends(get_ctx)) -> dict:
    run, result = score_service.latest(ctx.store, run_id)
    payload = result.get("agents") or service.empty()
    return {"run": runstore.summary(run),
            "unresolved": payload["unresolved"],
            "counted": payload["counted"],
            "uncounted": payload["uncounted"],
            "total": payload["total"],
            "accounts": payload["accounts"],
            "all_balanced": payload["all_balanced"]}


@router.get("/api/publish/attempts")
def api_publish_attempts(limit: int = 50, ctx: Ctx = Depends(get_ctx)) -> dict:
    return {"attempts": ctx.store.list_publish_attempts(limit=limit)}


@router.get("/api/publish/attempts/{attempt_id}")
def api_publish_attempt(attempt_id: int, ctx: Ctx = Depends(get_ctx)) -> dict:
    """未发布尝试的完整结果：回滚只撤销「已发布」，不撤销已算出的证据。"""
    attempt = ctx.store.get_publish_attempt(attempt_id)
    if attempt is None:
        raise NotFound(f"发布尝试不存在：{attempt_id}", code="E_ATTEMPT_NOT_FOUND")
    return attempt


def _apply_mapping(ctx: Ctx, data: bytes, *, dataset: str | None, source: str,
                   dry_run: bool = False, expected_version=None) -> dict:
    _check_size(data)
    name = str(dataset or _default_name(ctx))
    _dataset_or_404(ctx.store, name)
    mapping = attribution.parse_mapping(data, source=source)
    digest = attribution.mapping_digest(data)
    public = attribution.public_mapping(mapping, digest, len(data))
    if dry_run:
        return {"dataset": name, "saved": False, "published": False,
                "dry_run": True, "mapping": public}
    # 带着新映射整份重算；发布事务里同时固化运行行、原文与映射
    result = score_service.run(ctx.settings, ctx.store, name=name,
                               agent_mapping=data, mapping_source=source,
                               expected_version=expected_version)
    payload = _public_run(result, ctx)
    payload["mapping"] = public
    return payload


def _public_run(result: dict, ctx: Ctx) -> dict:
    return {
        "dataset": result["dataset"]["name"],
        "saved": True,
        "published": True,
        "replay": result.get("replay", False),
        "publish": result.get("publish"),
        "run": result["run"],
        "mapping_change": result.get("mapping_change"),
        "agents": service.public(result["agents"]),
        "counted": result["agents"]["counted"],
        "uncounted": result["agents"]["uncounted"],
        "score": result["score"]["score"],
        "grade": result["score"]["grade"],
        "reconcile": result["reconcile"],
    }


def _check_size(data: bytes) -> None:
    if not data:
        raise BadRequest("映射内容为空", code="E_MAPPING_INVALID")
    if len(data) > MAX_MAPPING_BYTES:
        raise BadRequest(f"映射文件超过 {MAX_MAPPING_BYTES // 1024} KB",
                         code="E_MAPPING_TOO_LARGE")


def _dataset_or_404(store, name: str) -> dict:
    dataset = store.get_dataset(name)
    if dataset is None:
        raise NotFound(f"数据集不存在：{name}", code="E_DATASET_NOT_FOUND")
    return dataset


def _default_name(ctx: Ctx) -> str:
    run = ctx.store.latest_run()
    if run is not None:
        return run["name"]
    raise NotFound("还没有运行记录，先跑一次评分", code="E_RUN_NOT_FOUND")
