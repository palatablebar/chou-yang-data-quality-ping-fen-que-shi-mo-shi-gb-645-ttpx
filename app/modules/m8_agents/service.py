"""M8 编排：读映射 → 逐行归属 → 与上一版比差异。"""
from __future__ import annotations

from pathlib import Path

from ... import config
from . import attribution


def sample_mapping(settings, dataset_name: str) -> bytes | None:
    """样例自带的映射：只在数据集有对应样例映射时读取。"""
    filename = config.AGENT_SAMPLE_MAPPINGS.get(dataset_name)
    if not filename:
        return None
    path = Path(settings.sample_dir) / filename
    return path.read_bytes() if path.exists() else None


def load(store, dataset_name: str, settings=None) -> tuple[dict, dict]:
    """读该数据集的映射：库里已发布 > 样例自带 > 空映射。"""
    stored = store.get_agent_mapping(dataset_name) if store is not None else None
    if stored is not None:
        mapping = attribution.parse_mapping(stored["content"],
                                            source=stored["source"])
        return mapping, {"digest": attribution.mapping_digest(stored["content"]),
                         "bytes": len(stored["content"]),
                         "source": stored["source"],
                         "content": stored["content"], "exists": True}
    content = sample_mapping(settings, dataset_name) if settings else None
    if content is not None:
        mapping = attribution.parse_mapping(content, source="sample")
        return mapping, {"digest": attribution.mapping_digest(content),
                         "bytes": len(content), "source": "sample",
                         "content": content, "exists": True}
    mapping = attribution.parse_mapping(None, source="none")
    return mapping, {"digest": attribution.mapping_digest(None), "bytes": 0,
                     "source": "none", "content": None, "exists": False}


def build(profile: dict, mapping: dict, digest: str, content_bytes: int = 0) -> dict:
    return attribution.build(profile, mapping, digest=digest,
                             content_bytes=content_bytes)


def public(payload: dict) -> dict:
    return {
        "agents": payload["agents"],
        "unresolved": payload["unresolved"],
        "counted": payload["counted"],
        "uncounted": payload["uncounted"],
        "total": payload["total"],
        "accounts": payload["accounts"],
        "all_balanced": payload["all_balanced"],
        "mapping": payload["mapping"],
        "columns": payload["columns"],
    }


def summary(payload: dict, limit: int = 100) -> dict:
    return attribution.summary(payload, limit=limit)


def empty() -> dict:
    """无运行记录时的空态，页面和导出都能直接渲染。"""
    mapping = attribution.parse_mapping(None, source="none")
    digest = attribution.mapping_digest(None)
    return {
        "agents": [],
        "unresolved": [
            {"key": key, "label": attribution.STATE_LABELS[key], "rows": 0,
             "missing_cells": 0, "linenos": [], "samples": [], "raw_values": {},
             "row_count": 0, "value_count": 0, "worklist": []}
            for key in attribution.BUCKET_ORDER
        ],
        "counted": {"rows": 0, "agents": 0, "missing_cells": 0},
        "uncounted": {"rows": 0, "missing_cells": 0},
        "total": {"rows": 0, "missing_cells": 0,
                  "cells_per_row": len(config.COLUMN_NAMES)},
        "accounts": [],
        "all_balanced": True,
        "mapping": attribution.public_mapping(mapping, digest, 0),
        "rows": [],
        "columns": [{"name": column.name, "label": column.label}
                    for column in config.COLUMNS],
    }
