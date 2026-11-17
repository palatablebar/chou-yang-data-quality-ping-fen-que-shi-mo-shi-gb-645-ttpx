"""SQLite 存储：数据集原文、受理人映射、运行记录与完整结果快照。

约定：
- 数据集按名字唯一：同名文件重新采信即覆盖原文，历史运行仍按 digest 保留；
- 运行记录按 (dataset, digest, rules_version) 幂等：同输入重跑不新增行；
- 受理人映射按数据集名保存，内容摘要参与 input_digest：映射一改就是新输入；
- 发布是一个事务：版本号、运行行、数据集指针、原文一起提交；任何一步失败
  整体回滚，上一版继续对外服务，失败的尝试单独记一条 publish_attempts；
- 分数与比率落 TEXT（Decimal 字符串），不落 float；
- 页面永远只展示一条已发布的运行记录，导出读的是同一条快照。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    name TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    encoding TEXT NOT NULL,
    declared_encoding TEXT,
    bytes INTEGER NOT NULL,
    content BLOB NOT NULL,
    updated_at TEXT NOT NULL,
    published_run_id TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    digest TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'published',
    published_at TEXT,
    score TEXT NOT NULL,
    grade TEXT NOT NULL,
    rows_in INTEGER NOT NULL,
    rows_accepted INTEGER NOT NULL,
    rows_unique INTEGER NOT NULL,
    rows_dup INTEGER NOT NULL,
    rows_quarantine INTEGER NOT NULL,
    rows_blank INTEGER NOT NULL,
    cells_total INTEGER NOT NULL,
    cells_missing INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    seen_seq INTEGER NOT NULL,
    summary_json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_identity ON runs(name, digest, rules_version);
CREATE INDEX IF NOT EXISTS idx_runs_version ON runs(name, version DESC);
CREATE TABLE IF NOT EXISTS agent_mappings (
    name TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    source TEXT NOT NULL,
    content BLOB NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publish_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    digest TEXT NOT NULL,
    version INTEGER NOT NULL,
    code TEXT NOT NULL,
    detail TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    attempted_at TEXT NOT NULL
);
"""


def _dec(text):
    return None if text is None else Decimal(str(text))


class VersionRace(Exception):
    """事务内发现版本号被别的发布占掉；调用方用 exc.version 重试。"""

    def __init__(self, version: int):
        super().__init__(f"版本已被占用，下一可用版本 {version}")
        self.version = version


class Store:
    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).resolve().parent.mkdir(parents=True, exist_ok=True)
        # 发布提交前的测试钩子（生产为 None）：用来验证中断回滚
        self.publish_hook = None
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _migrate(conn) -> None:
        """老库补列：滚动升级时不清库、不丢已发布记录。"""
        run_cols = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
        if "status" not in run_cols:
            conn.execute("ALTER TABLE runs ADD COLUMN status TEXT NOT NULL DEFAULT 'published'")
        if "published_at" not in run_cols:
            conn.execute("ALTER TABLE runs ADD COLUMN published_at TEXT")
        data_cols = {row["name"] for row in conn.execute("PRAGMA table_info(datasets)")}
        if "published_run_id" not in data_cols:
            conn.execute("ALTER TABLE datasets ADD COLUMN published_run_id TEXT")
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_name_version "
                "ON runs(name, version) WHERE status='published'")
        except sqlite3.IntegrityError:
            # 老库里若已有重复版本，先不建唯一索引，发布时仍用事务内版本校验
            pass

    # ---------- 数据集 ----------
    def save_dataset(self, *, name: str, digest: str, encoding: str,
                     declared_encoding: str | None, data: bytes, stamp: str) -> None:
        """存原始字节：GB18030 文件重算摘要必须与首次采信逐位相同。"""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO datasets (name,digest,encoding,declared_encoding,bytes,
                        content,updated_at) VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET
                     digest=excluded.digest, encoding=excluded.encoding,
                     declared_encoding=excluded.declared_encoding,
                     bytes=excluded.bytes, content=excluded.content,
                     updated_at=excluded.updated_at""",
                (name, digest, encoding, declared_encoding, len(data), data, stamp))

    def get_dataset(self, name: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM datasets WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def list_datasets(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT name,digest,encoding,bytes,updated_at,published_run_id "
                "FROM datasets ORDER BY name"
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 受理人映射 ----------
    def save_agent_mapping(self, *, name: str, digest: str, source: str,
                           content: bytes, stamp: str) -> None:
        with self.connect() as conn:
            self._write_mapping(conn, {"name": name, "digest": digest,
                                       "source": source, "content": content}, stamp)

    @staticmethod
    def _write_mapping(conn, mapping: dict, stamp: str) -> None:
        conn.execute(
            """INSERT INTO agent_mappings (name,digest,source,content,updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 digest=excluded.digest, source=excluded.source,
                 content=excluded.content, updated_at=excluded.updated_at""",
            (mapping["name"], mapping["digest"], mapping["source"],
             mapping["content"], stamp))

    def get_agent_mapping(self, name: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_mappings WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def delete_agent_mapping(self, name: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM agent_mappings WHERE name=?", (name,))

    # ---------- 运行记录 ----------
    def latest_version(self, name: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM runs WHERE name=? AND status='published'",
                (name,)).fetchone()
        return int(row["v"] or 0)

    def next_version(self, name: str) -> int:
        return self.latest_version(name) + 1

    def find_run(self, name: str, digest: str, rules_version: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE name=? AND digest=? AND rules_version=? "
                "AND status='published'",
                (name, digest, rules_version)).fetchone()
        return dict(row) if row else None

    def publish_run(self, run: dict, summary: dict, *, dataset: dict | None = None,
                    mapping: dict | None = None, stamp: str) -> None:
        """一个事务发布：版本校验 → 运行行 → 数据集原文与已发布指针 → 映射。

        任何一步抛错都整体回滚：库里仍是上一完整版本（含上一版映射），读取方
        不会看到半条记录，也不会出现「映射换了、报告还是旧的」。
        失败的尝试由 publish_run 外层记到 publish_attempts，计算结果不丢。
        """
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT COALESCE(MAX(version), 0) AS v FROM runs "
                    "WHERE name=? AND status='published'", (run["name"],)).fetchone()
                current = int(row["v"])
                if int(run["version"]) != current + 1:
                    raise VersionRace(current + 1)
                conn.execute(
                    """INSERT INTO runs (run_id,name,digest,rules_version,version,
                            status,published_at,score,grade,rows_in,rows_accepted,
                            rows_unique,rows_dup,rows_quarantine,rows_blank,
                            cells_total,cells_missing,created_at,finished_at,
                            last_seen_at,seen_seq,summary_json)
                       VALUES (?,?,?,?,?,'published',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run["run_id"], run["name"], run["digest"], run["rules_version"],
                     int(run["version"]), stamp, str(run["score"]), run["grade"],
                     int(run["rows_in"]), int(run["rows_accepted"]),
                     int(run["rows_unique"]), int(run["rows_dup"]),
                     int(run["rows_quarantine"]), int(run["rows_blank"]),
                     int(run["cells_total"]), int(run["cells_missing"]),
                     run["created_at"], run["finished_at"], run["created_at"],
                     self._next_seq(conn),
                     json.dumps(summary, ensure_ascii=False)))
                if dataset is not None:
                    conn.execute(
                        """INSERT INTO datasets (name,digest,encoding,declared_encoding,
                                bytes,content,updated_at,published_run_id)
                           VALUES (?,?,?,?,?,?,?,?)
                           ON CONFLICT(name) DO UPDATE SET
                             digest=excluded.digest, encoding=excluded.encoding,
                             declared_encoding=excluded.declared_encoding,
                             bytes=excluded.bytes, content=excluded.content,
                             updated_at=excluded.updated_at,
                             published_run_id=excluded.published_run_id""",
                        (dataset["name"], dataset["digest"], dataset["encoding"],
                         dataset["declared_encoding"], len(dataset["content"]),
                         dataset["content"], stamp, run["run_id"]))
                else:
                    conn.execute(
                        "UPDATE datasets SET published_run_id=? WHERE name=?",
                        (run["run_id"], run["name"]))
                if mapping is not None:
                    self._write_mapping(conn, mapping, stamp)
                if self.publish_hook is not None:
                    self.publish_hook()
        except VersionRace:
            raise
        except Exception:
            # 事务已整体回滚；失败的尝试由调用方在事务外留痕（含计算结果）
            raise

    def record_publish_attempt(self, *, run: dict, summary: dict, code: str,
                               detail: str, stamp: str) -> None:
        """失败尝试单独落库：回滚丢掉的是“未发布”，不是这次的计算证据。"""
        try:
            with self.connect() as conn:
                conn.execute(
                    """INSERT INTO publish_attempts (name,digest,version,code,detail,
                            payload_json,attempted_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (run.get("name", ""), run.get("digest", ""),
                     int(run.get("version", 0)), code, detail,
                     json.dumps(summary, ensure_ascii=False), stamp))
        except Exception:
            # 记录失败不能反过来把主流程的异常掩盖掉
            pass

    def list_publish_attempts(self, limit: int = 50) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM publish_attempts ORDER BY attempt_id DESC LIMIT ?",
                (limit,)).fetchall()
        return [self._attempt_row(row, full=False) for row in rows]

    def get_publish_attempt(self, attempt_id: int) -> dict | None:
        """未发布尝试的完整结果：发布回滚了，这次计算证据仍可离线复看。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM publish_attempts WHERE attempt_id=?",
                (attempt_id,)).fetchone()
        return None if row is None else self._attempt_row(row, full=True)

    @staticmethod
    def _attempt_row(row, *, full: bool) -> dict:
        item = dict(row)
        try:
            payload = json.loads(item.pop("payload_json"))
        except (ValueError, TypeError):
            payload = {}
        item["run"] = payload.get("run") if isinstance(payload, dict) else None
        item["result"] = payload if full else None
        return item

    def touch_run(self, run_id: str, stamp: str) -> None:
        """命中幂等复用时刷新「最近一次查看」，导航回各页仍停在这条记录上。"""
        with self.connect() as conn:
            conn.execute("UPDATE runs SET last_seen_at=?, seen_seq=? WHERE run_id=?",
                         (stamp, self._next_seq(conn), run_id))

    def _next_seq(self, conn) -> int:
        row = conn.execute("SELECT COALESCE(MAX(seen_seq), 0) + 1 AS n FROM runs").fetchone()
        return int(row["n"])

    def latest_run(self, name: str | None = None) -> dict | None:
        with self.connect() as conn:
            if name:
                row = conn.execute(
                    "SELECT * FROM runs WHERE name=? AND status='published' "
                    "ORDER BY version DESC LIMIT 1", (name,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM runs WHERE status='published' "
                    "ORDER BY seen_seq DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def published_run(self, name: str) -> dict | None:
        """该数据集对外服务的版本：指针优先，指针失效时退回最新已发布版本。"""
        with self.connect() as conn:
            dataset = conn.execute(
                "SELECT published_run_id FROM datasets WHERE name=?", (name,)).fetchone()
            if dataset and dataset["published_run_id"]:
                row = conn.execute(
                    "SELECT * FROM runs WHERE run_id=? AND status='published'",
                    (dataset["published_run_id"],)).fetchone()
                if row:
                    return dict(row)
        return self.latest_run(name)

    def get_run(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE status='published' "
                "ORDER BY seen_seq DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def load_result(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT summary_json FROM runs WHERE run_id=?",
                               (run_id,)).fetchone()
        return json.loads(row["summary_json"]) if row else None

    def count_runs(self) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) AS n FROM runs WHERE status='published'"
            ).fetchone()["n"])
