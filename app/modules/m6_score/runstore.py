"""运行记录写入与版本闸门：幂等复用、版本自增、陈旧提交拒绝、发布回退。

同名数据集每次内容（或受理人映射）变化就是一次新版本；客户端手里的版本比
库里旧，说明它读到的是过期页面，直接 409，不允许旧页面把新结果覆盖掉。

发布语义：
- 一条运行记录 = 一个不可分割的版本：运行行、数据集原文与已发布指针在同一个
  事务里提交；中途失败整体回滚，上一完整旧版继续对外服务；
- 失败的尝试不当作已发布版本，但计算证据单独落到 publish_attempts，
  记录不丢失；
- 同一附件 + 同一映射 + 同一规则重跑命中幂等，返回原运行记录，不新增版本，
  也不重复计数。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from ... import config
from ...errors import Conflict, NotFound, PublishFailed
from ...store import VersionRace

PUBLISH_RETRIES = 3


def stale_check(store, name: str, expected_version: int | None) -> int:
    latest = store.latest_version(name)
    if expected_version is None:
        return latest
    if expected_version < latest:
        raise Conflict(
            f"提交版本 {expected_version} 落后于当前版本 {latest}", code="E_STALE_RUN",
            detail={"expected_version": expected_version, "latest_version": latest})
    return latest


def replay(store, existing: dict) -> dict:
    store.touch_run(existing["run_id"], _now())
    payload = store.load_result(existing["run_id"])
    payload["replay"] = True
    payload["run"] = _run_row(existing)
    payload["publish"] = {
        "status": "replayed",
        "run_id": existing["run_id"],
        "version": int(existing["version"]),
        "attempts": 0,
        "message": "同输入同映射重跑，复用原运行记录，未新增版本",
    }
    return payload


def require_run(store, run_id: str | None) -> dict:
    if not run_id:
        latest = store.latest_run()
        if latest is None:
            raise NotFound("还没有运行记录", code="E_RUN_NOT_FOUND")
        return latest
    run = store.get_run(run_id)
    if run is None:
        raise NotFound(f"运行记录不存在：{run_id}", code="E_RUN_NOT_FOUND")
    return run


def _run_row(run: dict) -> dict:
    return {
        "run_id": run["run_id"], "name": run["name"], "digest": run["digest"],
        "rules_version": run["rules_version"], "version": int(run["version"]),
        "status": run.get("status", "published"),
        "score": str(run["score"]), "grade": run["grade"],
        "rows_in": int(run["rows_in"]), "rows_accepted": int(run["rows_accepted"]),
        "rows_unique": int(run["rows_unique"]), "rows_dup": int(run["rows_dup"]),
        "rows_quarantine": int(run["rows_quarantine"]),
        "rows_blank": int(run["rows_blank"]),
        "cells_total": int(run["cells_total"]),
        "cells_missing": int(run["cells_missing"]),
        "created_at": run["created_at"], "finished_at": run["finished_at"],
        "published_at": run.get("published_at"),
    }


def build_publish_payload(run: dict, *, dataset: dict | None,
                          attempts: int, status: str = "published",
                          message: str = "") -> dict:
    return {
        "status": status,
        "run_id": run["run_id"],
        "version": int(run["version"]),
        "attempts": attempts,
        "dataset_digest": dataset["digest"] if dataset else None,
        "message": message or "版本已发布；运行行、原文与已发布指针同一事务提交",
    }


def persist(store, result: dict, *, dataset: dict | None = None,
            mapping: dict | None = None) -> dict:
    """带版本重试的发布：版本被抢占时重算新版本号再发，绝不覆盖已发布版本。

    其它失败（磁盘、序列化、进程中断）会让整个事务回滚。此前已发布的版本、
    数据集原文与映射都保持原样，错误以 PublishFailed 上抛；失败的尝试已经在
    事务外落了一条 publish_attempts，计算证据不丢失。
    """
    run = result["run"]
    summary = result
    for attempt in range(PUBLISH_RETRIES):
        try:
            store.publish_run(
                {"run_id": run["run_id"], "name": run["name"],
                 "digest": run["digest"], "rules_version": run["rules_version"],
                 "version": run["version"], "score": Decimal(run["score"]),
                 "grade": run["grade"], "rows_in": run["rows_in"],
                 "rows_accepted": run["rows_accepted"],
                 "rows_unique": run["rows_unique"], "rows_dup": run["rows_dup"],
                 "rows_quarantine": run["rows_quarantine"],
                 "rows_blank": run["rows_blank"],
                 "cells_total": run["cells_total"],
                 "cells_missing": run["cells_missing"],
                 "created_at": run["created_at"],
                 "finished_at": run["finished_at"]},
                summary, dataset=dataset, mapping=mapping, stamp=_now())
        except VersionRace as exc:
            run["version"] = exc.version
            result["run"]["version"] = exc.version
            result["manifest"]["version"] = exc.version
            continue
        except Exception as exc:
            # publish_run 已整体回滚；这里只做指针复核与失败尝试留痕
            previous = rollback(store, run["name"], code="E_PUBLISH_INTERRUPTED",
                                detail=f"{type(exc).__name__}: {exc}",
                                payload=result)
            raise PublishFailed(
                "发布中断，已回退上一完整版本；本次结果未发布但已留痕",
                code="E_PUBLISH_INTERRUPTED",
                detail={"previous_run_id": previous["run_id"],
                        "previous_version": previous["version"],
                        "cause": type(exc).__name__, "message": str(exc)}) from exc
        # 事务已提交；后续只做内存里的发布元数据，失败不能反过来回滚已提交版本
        result["publish"] = build_publish_payload(
            run, dataset=dataset, attempts=attempt)
        return result
    raise Conflict("发布版本连续冲突，请刷新后重试", code="E_PUBLISH_CONFLICT")


def summary(run: dict) -> dict:
    return _run_row(run)


def _now() -> str:
    return dt.datetime.now(config.UTC).isoformat(timespec="seconds")


def thresholds() -> dict:
    return {item["key"]: {"cap": item["cap"], "tolerance": item["tolerance"]}
            for item in config.WEIGHTS}


def rollback(store, name: str, *, code: str, detail: str,
             payload: dict | None = None) -> dict:
    """发布失败时把数据集指针指回上一完整已发布版本，并留尝试记录。"""
    previous = store.published_run(name)
    if previous is not None:
        with store.connect() as conn:
            conn.execute("UPDATE datasets SET published_run_id=? WHERE name=?",
                         (previous["run_id"], name))
    if payload is not None:
        store.record_publish_attempt(
            run=payload.get("run", {}),
            summary=payload, code=code, detail=detail,
            stamp=_now())
    return {"status": "rolled_back", "run_id": previous["run_id"] if previous else None,
            "version": int(previous["version"]) if previous else 0,
            "code": code, "detail": detail}
