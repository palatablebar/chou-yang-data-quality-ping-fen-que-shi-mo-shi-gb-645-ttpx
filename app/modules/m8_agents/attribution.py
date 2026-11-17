"""M8 受理人身份归属：把工号 / 全名 / 英文名归到同一个坐席，歧义单列。

裁决（唯一规则来源）：
1. **只认显式映射**（标记值 → 工号）。绝不按姓名相似度自动合并：同名不同人
   靠工号区分；映射里只有姓名、没有工号的值一律进「歧义」，不摊给任何坐席。
2. **歧义不消失，只换账**。每条未计入记录保留原始行号、原值与候选工号，
   进「未计入」账；页面与导出同时给「计入 / 未计入」两栏，可核实记录随时
   能翻回源文件，不会长期散落在多个伪坐席下。
3. **整份重算**。映射摘要参与运行输入摘要：映射一改，缺口与报告一起按新
   映射重算并作为新版本发布；同一附件 + 同一映射重跑命中幂等，不新增版本。
4. **冲突不猜**。同一标记值被映射到多个工号 → 整条映射拒用，行落
   「映射冲突」，等人工改映射，而不是任选一个工号并入。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import unicodedata
from decimal import Decimal, ROUND_HALF_UP

from ... import config
from ...errors import BadRequest

MAPPING_HEADER = config.AGENT_MAPPING_COLUMNS
STATE_LABELS = {
    "attributed": "已归属",
    "ambiguous": "歧义待裁决",
    "conflict": "映射冲突",
    "unmapped": "未映射",
    "no_identity": "受理人缺失",
}
BUCKET_ORDER = ("ambiguous", "conflict", "unmapped", "no_identity")


# ---------------------------------------------------------------- 映射摘要
def mapping_digest(content: bytes | None) -> str:
    """空映射也参与摘要：『没有映射』与『映射被清空』必须是两个不同输入。"""
    payload = b"" if content is None else bytes(content)
    return hashlib.sha256(b"agent-mapping\x00" + payload).hexdigest()


def normalize(raw: str | None) -> str:
    return unicodedata.normalize("NFKC", raw or "").strip()


# ---------------------------------------------------------------- 解析映射
def parse_mapping(content: bytes | str | None, *, source: str = "stored") -> dict:
    """解析映射表：CSV（raw,agent_id,display_name）或 JSON 数组/对象。"""
    if content is None:
        return _assemble([], source=source, raw_rows=0, malformed=[])
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else \
        str(content).lstrip("﻿")
    stripped = text.strip()
    if not stripped:
        return _assemble([], source=source, raw_rows=0, malformed=[])
    if stripped[0] in "[{":
        return _parse_json(stripped, source)
    return _parse_csv(text, source)


def _parse_csv(text: str, source: str) -> dict:
    rows = [row for row in csv.reader(io.StringIO(text))
            if any(cell.strip() for cell in row)]
    if not rows:
        return _assemble([], source=source, raw_rows=0, malformed=[])
    header = [cell.strip().lower() for cell in rows[0]]
    if "raw" not in header:
        raise BadRequest("映射表缺少 raw 列；表头应为 raw,agent_id,display_name",
                         code="E_MAPPING_INVALID", detail={"header": rows[0]})
    position = {name: header.index(name) for name in header}
    records: list[dict] = []
    malformed: list[dict] = []
    for lineno, row in enumerate(rows[1:], start=2):
        def pick(name: str) -> str:
            index = position.get(name)
            if index is None or index >= len(row):
                return ""
            return normalize(row[index])

        raw = pick("raw")
        if not raw:
            malformed.append({"lineno": lineno, "detail": "raw 为空"})
            continue
        records.append({"raw": raw, "agent_id": pick("agent_id"),
                        "display_name": pick("display_name"), "lineno": lineno})
    return _assemble(records, source=source, raw_rows=len(rows) - 1,
                     malformed=malformed)


def _parse_json(text: str, source: str) -> dict:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BadRequest(f"映射 JSON 无法解析：{exc}", code="E_MAPPING_INVALID")
    items = payload.get("agents") or payload.get("mappings") if isinstance(payload, dict) \
        else payload
    if not isinstance(items, list):
        raise BadRequest("映射 JSON 需要是数组，或含 agents 数组的对象",
                         code="E_MAPPING_INVALID")
    records: list[dict] = []
    malformed: list[dict] = []
    for lineno, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            malformed.append({"lineno": lineno, "detail": "不是对象"})
            continue
        raw = normalize(str(item.get("raw") or ""))
        if not raw:
            malformed.append({"lineno": lineno, "detail": "raw 为空"})
            continue
        records.append({"raw": raw,
                        "agent_id": normalize(str(item.get("agent_id") or "")),
                        "display_name": normalize(str(item.get("display_name") or "")),
                        "lineno": lineno})
    return _assemble(records, source=source, raw_rows=len(items), malformed=malformed)


def _assemble(records: list[dict], *, source: str, raw_rows: int,
              malformed: list[dict]) -> dict:
    """同一标记值的多行：同工号合并为别名，不同工号整条判冲突。"""
    buckets: dict[str, list[dict]] = {}
    for record in records:
        buckets.setdefault(record["raw"], []).append(record)
    entries: list[dict] = []
    conflicts: list[dict] = []
    for raw in sorted(buckets):
        group = buckets[raw]
        ids = sorted({item["agent_id"] for item in group if item["agent_id"]})
        names = sorted({item["display_name"] for item in group if item["display_name"]})
        linenos = [item["lineno"] for item in group]
        if len(ids) > 1:
            conflicts.append({
                "raw": raw, "agent_ids": ids, "linenos": linenos,
                "code": "K_AGENT_CONFLICT",
                "detail": "同一标记值被映射到多个工号，整条映射拒用",
            })
            entries.append({"raw": raw, "agent_id": "", "display_name": "",
                            "status": "conflict", "agent_ids": ids,
                            "linenos": linenos, "aliases": [raw]})
        elif len(ids) == 1:
            entries.append({"raw": raw, "agent_id": ids[0],
                            "display_name": names[0] if names else "",
                            "status": "mapped", "agent_ids": ids,
                            "linenos": linenos, "aliases": [raw]})
        else:
            entries.append({"raw": raw, "agent_id": "",
                            "display_name": names[0] if names else "",
                            "status": "name_only", "agent_ids": [],
                            "linenos": linenos, "aliases": [raw]})
    aliases: dict[str, list[str]] = {}
    for entry in entries:
        if entry["status"] == "mapped":
            aliases.setdefault(entry["agent_id"], []).append(entry["raw"])
    for entry in entries:
        if entry["status"] == "mapped":
            entry["aliases"] = sorted(aliases[entry["agent_id"]])
    return {
        "entries": entries,
        "conflicts": conflicts,
        "malformed": malformed,
        "source": source,
        "raw_rows": raw_rows,
        "entry_count": len(entries),
        "mapped_count": sum(1 for item in entries if item["status"] == "mapped"),
        "name_only_count": sum(1 for item in entries if item["status"] == "name_only"),
        "conflict_count": len(conflicts),
    }


def public_mapping(mapping: dict, digest: str, content_bytes: int) -> dict:
    return {
        "entries": mapping["entries"],
        "conflicts": mapping["conflicts"],
        "malformed": mapping["malformed"],
        "source": mapping["source"],
        "digest": digest,
        "bytes": content_bytes,
        "raw_rows": mapping["raw_rows"],
        "entry_count": mapping["entry_count"],
        "mapped_count": mapping["mapped_count"],
        "name_only_count": mapping["name_only_count"],
        "conflict_count": mapping["conflict_count"],
    }
# ---------------------------------------------------------------- 归属
def build(profile: dict, mapping: dict, *, digest: str,
          content_bytes: int = 0) -> dict:
    """逐行裁决归属，产出「计入 / 未计入」两本账与逐行归属表。"""
    entries = {normalize(item["raw"]): item for item in mapping["entries"]}
    name_index: dict[str, set[str]] = {}
    id_set: set[str] = set()
    for entry in mapping["entries"]:
        if entry["status"] != "mapped":
            continue
        id_set.add(entry["agent_id"])
        if entry["display_name"]:
            name_index.setdefault(normalize(entry["display_name"]), set()).add(
                entry["agent_id"])

    agents: dict[str, dict] = {}
    buckets = {
        key: {"key": key, "label": STATE_LABELS[key], "rows": 0,
              "missing_cells": 0, "linenos": [], "samples": [], "raw_values": {}}
        for key in BUCKET_ORDER
    }
    attribution_rows: list[dict] = []
    for row in profile["rows"]:
        cell = row["cells"].get("agent") or {}
        raw = "" if cell.get("state") == "missing" else str(cell.get("raw") or "")
        missing_cells = sum(1 for item in row["cells"].values()
                            if item.get("state") == "missing")
        verdict = _verdict(row, raw, cell.get("state"), entries, name_index, id_set)
        verdict["missing_cells"] = missing_cells
        verdict["raw"] = raw
        verdict["state_label"] = STATE_LABELS[verdict["state"]]
        attribution_rows.append(verdict)
        if verdict["state"] == "attributed":
            agent = agents.setdefault(verdict["agent_id"], {
                "agent_id": verdict["agent_id"],
                "display_name": verdict["display_name"],
                "aliases": verdict["aliases"],
                "rows": 0, "missing_rows": 0, "missing_cells": 0,
                "by_column": {}, "linenos": [],
            })
            agent["rows"] += 1
            agent["missing_cells"] += missing_cells
            if missing_cells:
                agent["missing_rows"] += 1
            for name, item in row["cells"].items():
                if item.get("state") == "missing":
                    agent["by_column"][name] = agent["by_column"].get(name, 0) + 1
            if len(agent["linenos"]) < 40:
                agent["linenos"].append(row["lineno"])
        else:
            bucket = buckets[verdict["state"]]
            bucket["rows"] += 1
            bucket["missing_cells"] += missing_cells
            bucket["raw_values"][raw] = bucket["raw_values"].get(raw, 0) + 1
            if len(bucket["linenos"]) < 40:
                bucket["linenos"].append(row["lineno"])
            if len(bucket["samples"]) < 15:
                bucket["samples"].append({
                    "lineno": row["lineno"], "raw": raw,
                    "reason": verdict["reason"],
                    "candidates": verdict["candidates"],
                    "missing_cells": missing_cells,
                })

    cells_per_row = len(profile["columns"]) or len(config.COLUMN_NAMES)
    agent_rows = []
    for agent in agents.values():
        agent["missing_rate"] = _rate(agent["missing_cells"],
                                      agent["rows"] * cells_per_row)
        agent["by_column_rows"] = _column_rows(profile, agent["by_column"])
        agent["code"] = "K_AGENT_ATTR"
        agent_rows.append(agent)
    agent_rows.sort(key=lambda item: (-item["missing_cells"], item["agent_id"]))

    unresolved = []
    for key in BUCKET_ORDER:
        bucket = buckets[key]
        bucket["row_count"] = bucket["rows"]
        bucket["value_count"] = len(bucket["raw_values"])
        bucket["worklist"] = _worklist(bucket, name_index)
        bucket["code"] = {
            "ambiguous": "K_AGENT_AMBIGUOUS",
            "conflict": "K_AGENT_CONFLICT",
            "unmapped": "K_AGENT_UNMAPPED",
            "no_identity": "K_AGENT_NO_IDENTITY",
        }[key]
        unresolved.append(bucket)

    counted_rows = sum(item["rows"] for item in agent_rows)
    counted_cells = sum(item["missing_cells"] for item in agent_rows)
    uncounted_rows = sum(item["rows"] for item in unresolved)
    uncounted_cells = sum(item["missing_cells"] for item in unresolved)
    # 口径以采纳行为准：账不平要暴露，而不是用「计入 + 未计入」自证自洽
    total_rows = len(profile["rows"])
    total_cells = sum(1 for row in profile["rows"]
                      for cell in row["cells"].values()
                      if cell.get("state") == "missing")
    accounts = [
        _account("agent_rows", "归属行账", total_rows, counted_rows + uncounted_rows,
                 "采纳行 = 计入 + 未计入"),
        _account("agent_cells", "归属缺口账", total_cells,
                 counted_cells + uncounted_cells,
                 "缺失格 = 已归属坐席缺口 + 未计入缺口"),
        _account("agent_counted", "计入行账", counted_rows,
                 sum(item["rows"] for item in agent_rows), "计入行 = 各坐席行之和"),
    ]
    return {
        "agents": agent_rows,
        "unresolved": unresolved,
        "counted": {"rows": counted_rows, "agents": len(agent_rows),
                    "missing_cells": counted_cells},
        "uncounted": {"rows": uncounted_rows, "missing_cells": uncounted_cells},
        "total": {"rows": total_rows, "missing_cells": total_cells,
                  "cells_per_row": cells_per_row},
        "accounts": accounts,
        "all_balanced": all(item["balanced"] for item in accounts),
        "mapping": public_mapping(mapping, digest, content_bytes),
        "rows": attribution_rows,
        "columns": [{"name": column["name"], "label": column["label"]}
                    for column in profile["columns"]],
    }


def _verdict(row: dict, raw: str, state: str, entries: dict,
             name_index: dict, id_set: set) -> dict:
    base = {"lineno": row["lineno"], "agent_id": None, "display_name": "",
            "aliases": [], "candidates": [], "reason": ""}
    if state == "missing":
        return {**base, "state": "no_identity",
                "reason": "受理人单元格缺失，没有身份可归属"}
    entry = entries.get(normalize(raw))
    if entry is None:
        candidates = sorted(id_set) if normalize(raw) in id_set else \
            sorted(name_index.get(normalize(raw), set()))
        reason = "标记值不在映射内"
        if candidates:
            reason += "；候选工号需人工确认"
        return {**base, "state": "unmapped", "candidates": candidates,
                "reason": reason}
    if entry["status"] == "conflict":
        return {**base, "state": "conflict", "aliases": [raw],
                "candidates": entry.get("agent_ids", []),
                "reason": "同一标记值映射到多个工号，整条映射拒用"}
    if entry["status"] == "name_only":
        candidates = sorted(name_index.get(normalize(entry["raw"]), set()))
        reason = "映射里只有姓名、没有工号，无法确认是哪个同名坐席"
        if candidates:
            reason += "；候选工号需人工确认"
        return {**base, "state": "ambiguous",
                "display_name": entry.get("display_name", ""),
                "aliases": [raw], "candidates": candidates, "reason": reason}
    return {**base, "state": "attributed", "agent_id": entry["agent_id"],
            "display_name": entry.get("display_name", "") or entry["agent_id"],
            "aliases": entry.get("aliases", [raw])}


def _worklist(bucket: dict, name_index: dict) -> list[dict]:
    """待裁决工作台：按出现次数排序，带上候选工号，便于一次性核实。"""
    items = []
    for raw, count in bucket["raw_values"].items():
        candidates = sorted(name_index.get(normalize(raw), set()))
        items.append({"raw": raw, "count": count, "candidates": candidates})
    items.sort(key=lambda item: (-item["count"], item["raw"]))
    return items


def _column_rows(profile: dict, counts: dict) -> list[dict]:
    return [{"name": column["name"], "label": column["label"],
             "missing": counts.get(column["name"], 0)}
            for column in profile["columns"]]


def _account(key: str, label: str, left, right, note: str) -> dict:
    left_dec = Decimal(str(left))
    right_dec = Decimal(str(right))
    diff = left_dec - right_dec
    return {"key": key, "label": label, "left": left_dec, "right": right_dec,
            "diff": diff, "balanced": diff == 0, "note": note}


def _rate(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0:
        return Decimal("0.0000")
    return (Decimal(numerator) / Decimal(denominator)).quantize(
        config.RATE_QUANT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- 差异
def diff_against(previous: dict | None, current: dict) -> dict:
    """映射更新后看归属怎么变：新增 / 消失的坐席与迁移行数。"""
    if not previous:
        return {"changed": False, "added_agents": [], "removed_agents": [],
                "moved_rows": 0, "uncounted_before": None,
                "uncounted_after": current["uncounted"]["rows"],
                "code": "K_AGENT_MAPPING_CHANGED"}
    before = {item["agent_id"]: item for item in previous.get("agents", [])}
    after = {item["agent_id"]: item for item in current["agents"]}
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    moved = sum(abs(after[key]["rows"] - before[key]["rows"])
                for key in set(after) & set(before))
    moved += sum(after[key]["rows"] for key in added)
    moved += sum(before[key]["rows"] for key in removed)
    before_uncounted = previous.get("uncounted", {}).get("rows", 0)
    moved += abs(current["uncounted"]["rows"] - before_uncounted)
    return {
        "changed": bool(moved or added or removed),
        "added_agents": added,
        "removed_agents": removed,
        "moved_rows": moved,
        "uncounted_before": before_uncounted,
        "uncounted_after": current["uncounted"]["rows"],
        "code": "K_AGENT_MAPPING_CHANGED",
    }


def summary(payload: dict, limit: int = 100) -> dict:
    return {
        "agents": payload["agents"][:limit],
        "unresolved": payload["unresolved"],
        "counted": payload["counted"],
        "uncounted": payload["uncounted"],
        "total": payload["total"],
        "accounts": payload["accounts"],
        "all_balanced": payload["all_balanced"],
        "mapping": payload["mapping"],
    }
