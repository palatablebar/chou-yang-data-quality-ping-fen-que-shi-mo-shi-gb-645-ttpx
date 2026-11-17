# 运行手册 qualiscore

## 1 启动

```bash
docker compose up -d --build
curl -s http://127.0.0.1:18204/api/health
open http://127.0.0.1:18204/
```

首次打开库里没有任何运行记录，页面只有一个空态与采信表单；选样例点
「采信并评分」即产生第一条记录。数据落在命名卷 `qualiscore-data` 的
`/data/qualiscore.sqlite`。

## 2 重建数据库

```bash
docker compose down -v          # 删掉命名卷
docker compose up -d --build    # 新库，页面回到空态
```

只想清空运行记录、保留上传的数据集时，删 SQLite 文件即可（下一次启动会建表）。

## 3 跑主流程（命令行）

```bash
# 采信脏样例
curl -s -X POST http://127.0.0.1:18204/api/datasets/import \
  -H 'Content-Type: application/json' -d '{"sample":"tickets_dirty.csv"}'

# 评分（默认样例）
curl -s -X POST http://127.0.0.1:18204/api/score \
  -H 'Content-Type: application/json' -d '{"sample":"tickets_dirty.csv"}'

# 上传自己的文件
curl -s -X POST http://127.0.0.1:18204/api/datasets/upload \
  -F 'file=@sample_data/tickets_dirty.csv' -F 'encoding=utf-8'

# 导出
curl -s -o /tmp/report.csv http://127.0.0.1:18204/api/export/report.csv
curl -s http://127.0.0.1:18204/api/export/manifest.json
```

## 4 样例

| 文件 | 用途 | 期望结果 |
|---|---|---|
| `tickets_dirty.csv` | 主演示：哨兵、空白、重复、冲突、越界、分段、截断 | 65.24 分 C 级，隔离 3 行，2 段 |
| `tickets_agents.csv` | 受理人身份混用：同人三种写法、同名两人、仅姓名、映射冲突 | 96.25 分 A 级，计入 20 行 / 未计入 10 行 |
| `tickets_clean.csv` | 干净对照 | 100.00 分 A 级 |
| `tickets_gbk.csv` | GB18030 + 中文表头 | 采信成功，标 `K_ALIAS_MAPPED` |
| `tickets_bom.csv` | BOM + CRLF | 采信成功，标 `K_BOM_STRIPPED` |
| `tickets_truncated.csv` | 末行截断 | 隔离 1 行，标 `K_TRUNCATED_TAIL` |
| `tickets_constant.csv` | 常量列 + 全空列 | 70.00 分 C 级，缺失维封顶 |
| `tickets_header_only.csv` | 仅表头 | 422 `E_HEADER_ONLY` |
| `tickets_blank.csv` | 全空行 | 422 `E_ALL_BLANK` |
| `tickets_empty.csv` | 空文件 | 422 `E_EMPTY_FILE` |

## 5 受理人映射与归属

映射表列固定 `raw,agent_id,display_name`，只做显式对应，不做模糊匹配。

```bash
# 看当前映射（含摘要）
curl -s 'http://127.0.0.1:18204/api/agents/mapping?dataset=tickets_agents.csv'

# 只校验，不发布
curl -s -X PUT 'http://127.0.0.1:18204/api/agents/mapping?dataset=tickets_agents.csv&dry_run=true' \
  -F 'file=@sample_data/agent_identities.csv'

# 上传并重算发布：缺口、归属账、报告与导出一起换新版本
curl -s -X PUT 'http://127.0.0.1:18204/api/agents/mapping?dataset=tickets_agents.csv' \
  -F 'file=@sample_data/agent_identities.csv'

# 不改映射，按当前映射重算（同附件同映射会命中幂等，不新增版本）
curl -s -X POST http://127.0.0.1:18204/api/agents/recompute \
  -H 'Content-Type: application/json' -d '{"dataset":"tickets_agents.csv"}'

# 看未计入账与发布尝试
curl -s 'http://127.0.0.1:18204/api/agents/unresolved'
curl -s 'http://127.0.0.1:18204/api/publish/attempts'
```

裁决口径：

| 情况 | 处理 |
|---|---|
| 同一标记值映射到两个工号 | 整条映射拒用，行落「映射冲突」，改映射后重算 |
| 只给姓名、映射里没有工号 | 落「歧义」桶，补上工号再重算 |
| 标记值不在映射里 | 落「未映射」桶，补映射；不要指望按姓名自动合并 |
| 受理人单元格为空 | 落「受理人缺失」，不影响其他行的归属 |

发布中断（磁盘、进程被杀）时事务整体回滚，`/agents` 与 `/batches` 的
「未发布的尝试」会列出目标版本、原因与批次号；上一完整版本继续对外服务。

## 6 常见失败

| HTTP | 原因码 | 处理 |
|---|---|---|
| 422 | `E_EMPTY_FILE` | 文件是 0 字节，换一份 |
| 422 | `E_HEADER_ONLY` | 只有表头，导出补上数据行 |
| 422 | `E_ALL_BLANK` | 数据行全空，检查导出是否只写了分隔符 |
| 422 | `E_HEADER_INVALID` | 缺必填列；`detail.missing` 列出缺哪几列 |
| 422 | `E_DECODE` | 编码不在 UTF-8 / GB18030 内，先转码再传 |
| 404 | `E_SAMPLE_UNKNOWN` | 样例名不在白名单，用 `/api/datasets` 看可用项 |
| 409 | `E_STALE_RUN` | 页面版本落后；刷新后重跑，别用旧页面覆盖 |
| 400 | `E_MAPPING_INVALID` | 映射缺 `raw` 列或 JSON 不合法；表头应为 `raw,agent_id,display_name` |
| 503 | `E_PUBLISH_INTERRUPTED` | 发布中断，已回退上一完整版本；看 `/api/publish/attempts` 后重试 |
| 404 | `E_RUN_NOT_FOUND` | 还没有运行记录，先跑一次评分 |

## 7 测试

```bash
docker compose exec -T app python -m pytest -q
```

本地（不启容器）也可以：

```bash
python3 -m pytest -q
```

## 8 日志与排查

- 容器日志：`docker compose logs -f app`。
- 分数异常先看 `/batches` 的阈值表与五项对账；对账不平说明有单元格被重复计数。
- 受理人缺口总数对不上时先看 `/agents`：计入 + 未计入必须等于采纳行与缺失格；
  再看映射摘要是否与运行清单里的 `agent_mapping_digest` 一致。
- 离群数为 0 时先看 `/outliers` 的「退化列」：满意度一类 1-5 分刻度默认判退化。
- 每份导出都能在 `/api/export/manifest.json` 里找到同一次运行的 `run_id` 与输入摘要。
