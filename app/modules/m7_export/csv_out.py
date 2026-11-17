"""CSV 导出：页面上的每个数都能在这里找到对应的原始行与原因码。

约定：UTF-8 带 BOM（Excel 直接打开不乱码）、CRLF、Decimal 按列刻度输出为
字符串、行号是源文件物理行号。
"""
from __future__ import annotations

import csv
import io

BOM = "\ufeff"


def csv_text(header: list[str], rows: list[list]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if cell is None else str(cell) for cell in row])
    return BOM + buffer.getvalue()


def report_csv(result: dict) -> str:
    score = result["score"]
    counters = result["manifest"]["counters"]
    rows = [
        ["总分", score["score"], "分", "刻度 0.01"],
        ["等级", score["grade"], "", score["grade_label"]],
        ["扣分合计", score["penalty_sum"], "分", "总分 + 扣分 = 100"],
    ]
    for item in score["dims"]:
        rows.append([f"{item['label']}扣分", item["penalty"], "分",
                     f"比率 {item['rate']} 容忍度 {item['tolerance']} 封顶 {item['cap']}"])
    rows.extend([
        ["原始记录", counters["raw_records"], "行", "含空行与隔离行"],
        ["采纳行", counters["accepted"], "行", ""],
        ["唯一行", counters["rows_unique"], "行", "全行哈希去重后"],
        ["重复行", counters["rows_dup"], "行", ""],
        ["隔离行", counters["quarantine"], "行", ""],
        ["空行", counters["blank"], "行", ""],
        ["单元格", counters["cells_total"], "格", ""],
        ["缺失单元格", counters["cells_missing"], "格", ""],
        ["离群单元格", counters["outlier_cells"], "格", "有值的子集"],
        ["受理人计入行", counters.get("agent_rows_counted", 0), "行",
         "缺口已归属到具体坐席"],
        ["受理人未计入行", counters.get("agent_rows_uncounted", 0), "行",
         "歧义 / 冲突 / 未映射 / 受理人缺失"],
        ["计入缺口格", counters.get("agent_missing_counted", 0), "格", ""],
        ["未计入缺口格", counters.get("agent_missing_uncounted", 0), "格", ""],
    ])
    return csv_text(["项目", "数值", "单位", "备注"], rows)


def profile_csv(result: dict) -> str:
    rows = []
    for item in result["profile"]["profile"]:
        rows.append([
            item["name"], item["label"], item["kind"], item["total"],
            item["filled"], item["missing"], item["missing_rate"],
            item["format_bad"], item["format_rate"], item["distinct"],
            item.get("min") or "", item.get("max") or "",
        ])
    return csv_text(["列名", "列标题", "类型", "单元格", "有值", "缺失", "缺失率",
                     "格式不合格", "格式率", "基数", "最小", "最大"], rows)


def missing_csv(result: dict) -> str:
    summary = {item["column"]: item for item in result["missing"]["run_summary"]}
    rows = []
    for item in result["missing"]["account"]["by_column"]:
        extra = summary.get(item["name"], {})
        rows.append([item["name"], item["label"], item["missing"], item["filled"],
                     item["missing_rate"], extra.get("runs", 0),
                     extra.get("longest", 0)])
    return csv_text(["列名", "列标题", "缺失", "有值", "缺失率", "连续段数",
                     "最长段"], rows)


def duplicates_csv(result: dict) -> str:
    duplicates = result["duplicates"]
    rows = []
    for item in duplicates["hash_groups"]:
        rows.append(["全行哈希", item["hash"], item["count"],
                     " ".join(str(lineno) for lineno in item["linenos"]), ""])
    for item in duplicates["key_repeats"]:
        rows.append(["业务键重复", item["key"], item["count"],
                     " ".join(str(lineno) for lineno in item["linenos"]), ""])
    for item in duplicates["conflicts"]:
        rows.append(["业务键冲突", item["key"], item["count"],
                     " ".join(str(lineno) for lineno in item["linenos"]),
                     " ".join(item["fields"])])
    return csv_text(["账", "组或键", "行数", "行号", "冲突列"], rows)


def outliers_csv(result: dict) -> str:
    stats = {item["name"]: item for item in result["outliers"]["columns"]}
    rows = []
    for item in result["outliers"]["cells"]:
        column = stats.get(item["column"], {})
        rows.append([item["column"], column.get("label", item["column"]),
                     item["lineno"], item["raw"], item["value"], item["z"],
                     column.get("method", ""), "K_OUTLIER"])
    return csv_text(["列名", "列标题", "行号", "原值", "归一值", "稳健z", "方法",
                     "原因码"], rows)


def quarantine_csv(result: dict) -> str:
    rows = []
    for item in result["dataset"]["quarantine"]:
        rows.append([item["lineno"], item["segment"], item["code"],
                     item["field"], item["raw_value"], item["detail"]])
    return csv_text(["行号", "段", "原因码", "字段", "原值", "说明"], rows)


def flags_csv(result: dict) -> str:
    rows = []
    for item in result["dataset"]["flags"]:
        rows.append([item["code"], item["label"], item["level"], item["count"]])
    return csv_text(["原因码", "含义", "级别", "次数"], rows)


def _agents_section(result: dict) -> dict:
    return result.get("agents") or {}


def agents_csv(result: dict) -> str:
    """只列计入的坐席：工号、展示名、别名、行数、缺口。"""
    agents = _agents_section(result)
    rows = []
    for item in agents.get("agents", []):
        per_column = " ".join(f"{column['name']}={column['missing']}"
                              for column in item["by_column_rows"] if column["missing"])
        rows.append(["计入", item["agent_id"], item["display_name"],
                     " / ".join(item["aliases"]), item["rows"],
                     item["missing_rows"], item["missing_cells"],
                     item["missing_rate"], per_column,
                     " ".join(str(lineno) for lineno in item["linenos"])])
    return csv_text(["归属", "工号", "展示名", "别名", "行数", "有缺口行",
                     "缺失格", "缺失率", "缺口列", "行号"], rows)


def agents_unresolved_csv(result: dict) -> str:
    """未计入账：歧义 / 冲突 / 未映射 / 受理人缺失，逐桶带行号。"""
    agents = _agents_section(result)
    rows = []
    for bucket in agents.get("unresolved", []):
        rows.append([bucket["key"], bucket["label"], bucket["code"],
                     bucket["rows"], bucket["missing_cells"],
                     "; ".join(f"{raw or '（空）'}×{count}"
                               for raw, count in bucket["raw_values"].items()),
                     " ".join(str(lineno) for lineno in bucket["linenos"])])
    return csv_text(["桶", "说明", "原因码", "行数", "缺口格", "标记值",
                     "行号"], rows)


def agents_rows_csv(result: dict) -> str:
    """逐行归属：每一行都能追回原始行号、标记值、归属状态与原因。"""
    agents = _agents_section(result)
    rows = []
    for item in agents.get("rows", []):
        rows.append([item["lineno"], item["raw"], item["state"],
                     item["state_label"], item.get("agent_id") or "",
                     item.get("display_name") or "",
                     item.get("missing_cells", 0), item.get("reason", ""),
                     " ".join(item.get("candidates") or [])])
    return csv_text(["行号", "受理人原值", "状态", "状态说明", "工号", "展示名",
                     "缺失格", "原因", "候选工号"], rows)


def mapping_csv(result: dict) -> str:
    """本次运行实际使用的映射：内容和摘要都在这里，可离线复算归属。"""
    agents = _agents_section(result)
    mapping = agents.get("mapping", {})
    rows = [["摘要", mapping.get("digest", ""), "", ""],
            ["来源", mapping.get("source", ""), "", ""],
            ["条目", mapping.get("entry_count", 0), "", ""]]
    for item in mapping.get("entries", []):
        status = {"mapped": "有工号", "name_only": "仅姓名",
                  "conflict": "自相矛盾"}.get(item["status"], item["status"])
        rows.append([item["raw"], item["agent_id"], item["display_name"], status])
    return csv_text(["标记值", "工号", "展示名", "状态"], rows)
