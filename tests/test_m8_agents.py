"""M8 受理人身份归属：误并与散账的两头都要防住。

覆盖：
- 同一个人三种写法合并成一个坐席，缺口不再摊成多组；
- 同名不同工号不按姓名误并，只写姓名的进歧义桶并保留可核实行号；
- 歧义 / 冲突 / 未映射 / 受理人缺失四条未计入桶守恒，计入 + 未计入闭合；
- 映射更新后缺口与报告一起重算成新版本；同一附件同一映射重跑不新增版本；
- 发布中断整批回滚到上一完整旧版，失败尝试留痕不丢记录。
"""
from __future__ import annotations

import json

import pytest

from app.errors import PublishFailed
from app.modules.m6_score import service

SAMPLE = "tickets_agents.csv"

MAP_FIRST = """raw,agent_id,display_name
A1024,A1024,陈允
Alice Chen,A1024,陈允
B2007,B2007,陈允
陈允,,陈允
C3301,C3301,李迟
李迟,C3301,李迟
D4410,D4410,周砚
周砚,D4410,周砚
E5502,E5502,苏遥
E5502,F7711,苏遥
"""

MAP_RESOLVED = """raw,agent_id,display_name
A1024,A1024,陈允
Alice Chen,A1024,陈允
B2007,B2007,陈允
陈允,A1024,陈允
C3301,C3301,李迟
李迟,C3301,李迟
D4410,D4410,周砚
周砚,D4410,周砚
王柯,E5502,王柯
Zed,E5502,Zed
E5502,E5502,苏遥
"""


def run_with_mapping(settings, store, mapping: str, *, expected_version=None) -> dict:
    return service.run(settings, store, name=SAMPLE,
                       agent_mapping=mapping.encode("utf-8"),
                       expected_version=expected_version)


def sample_run(settings, store) -> dict:
    return service.run(settings, store, name=SAMPLE)


def bucket(payload: dict, key: str) -> dict:
    return next(item for item in payload["unresolved"] if item["key"] == key)


def test_three_spellings_merge_into_one_agent(settings, store):
    """工号 / 全名 / 英文名是同一个坐席：缺口归一处，不按写法拆成多组。"""
    payload = sample_run(settings, store)["agents"]
    agents = {item["agent_id"]: item for item in payload["agents"]}
    assert agents["A1024"]["display_name"] == "陈允"
    assert "A1024" in agents["A1024"]["aliases"]
    assert "Alice Chen" in agents["A1024"]["aliases"]
    assert agents["A1024"]["rows"] == 8
    assert agents["A1024"]["missing_cells"] == 1


def test_homonyms_are_not_merged_by_name(settings, store):
    """同名不同工号必须分开：陈允 A1024 与 B2007 各自成账。"""
    payload = sample_run(settings, store)["agents"]
    ids = {item["agent_id"] for item in payload["agents"]}
    assert {"A1024", "B2007"} <= ids
    by_id = {item["agent_id"]: item for item in payload["agents"]}
    assert by_id["A1024"]["rows"] == 8
    assert by_id["B2007"]["rows"] == 5
    assert payload["counted"]["agents"] == 4


def test_name_only_value_goes_to_ambiguous_with_linenos(settings, store):
    """映射里只有姓名 → 歧义桶，带行号与原值，不摊给任何一个同名坐席。"""
    payload = sample_run(settings, store)["agents"]
    ambiguous = bucket(payload, "ambiguous")
    assert ambiguous["rows"] == 4
    assert ambiguous["raw_values"] == {"陈允": 4}
    assert ambiguous["linenos"] == [15, 16, 17, 18]
    assert ambiguous["code"] == "K_AGENT_AMBIGUOUS"
    assert all(item["candidates"] for item in ambiguous["worklist"])


def test_mapping_conflict_rejects_whole_value(settings, store):
    """同一标记值映射到两个工号：整条拒用，两行进冲突桶而不是任选一个。"""
    payload = sample_run(settings, store)["agents"]
    conflict = bucket(payload, "conflict")
    assert conflict["rows"] == 2
    assert conflict["raw_values"] == {"E5502": 2}
    assert payload["mapping"]["conflict_count"] == 1
    assert payload["mapping"]["conflicts"][0]["agent_ids"] == ["E5502", "F7711"]


def test_unmapped_and_missing_agent_are_separate_buckets(settings, store):
    payload = sample_run(settings, store)["agents"]
    unmapped = bucket(payload, "unmapped")
    missing = bucket(payload, "no_identity")
    assert unmapped["rows"] == 3
    assert set(unmapped["raw_values"]) == {"王柯", "Zed"}
    assert missing["rows"] == 1
    assert missing["linenos"] == [31]


def test_counted_plus_uncounted_closes(settings, store):
    payload = sample_run(settings, store)["agents"]
    assert payload["counted"]["rows"] + payload["uncounted"]["rows"] == 30
    assert payload["total"]["rows"] == 30
    assert payload["counted"]["missing_cells"] + \
        payload["uncounted"]["missing_cells"] == payload["total"]["missing_cells"]
    for account in payload["accounts"]:
        assert account["balanced"] is True, account
    assert payload["all_balanced"] is True


def test_accounts_are_grounded_in_profile_not_self_evident(settings, store):
    """两边都用「计入 + 未计入」会自证自洽：左侧必须取采纳行与画像缺失格。"""
    result = sample_run(settings, store)
    agents = result["agents"]
    profile = result["profile"]["counts"]
    assert agents["total"]["rows"] == profile["rows"]
    assert agents["total"]["missing_cells"] == profile["cells_missing"]
    assert agents["counted"]["missing_cells"] + \
        agents["uncounted"]["missing_cells"] == profile["cells_missing"]


def test_mapping_only_changes_attribution_not_quality_score(settings, store):
    """映射影响归属与报告版本，但不篡改原始质量分：质量账本身没变。"""
    first = sample_run(settings, store)
    changed = run_with_mapping(settings, store, MAP_RESOLVED)
    assert changed["score"]["score"] == first["score"]["score"]
    assert changed["score"]["penalty_sum"] == first["score"]["penalty_sum"]
    assert changed["reconcile"]["all_balanced"] is True
    keys = [item["key"] for item in changed["reconcile"]["accounts"]]
    assert keys == ["rows", "accepted", "cells", "value", "score"]
    assert changed["reconcile"]["agent_all_balanced"] is True


def test_row_level_attribution_traces_every_row(settings, store):
    payload = sample_run(settings, store)["agents"]
    rows = {item["lineno"]: item for item in payload["rows"]}
    assert rows[2]["state"] == "attributed"
    assert rows[2]["agent_id"] == "A1024"
    assert rows[15]["state"] == "ambiguous"
    assert rows[29]["state"] == "conflict"
    assert rows[31]["state"] == "no_identity"
    assert len(rows) == 30


def test_mapping_digest_changes_input_identity(settings, store):
    first = sample_run(settings, store)
    changed = run_with_mapping(settings, store, MAP_RESOLVED)
    assert changed["run"]["digest"] != first["run"]["digest"]
    assert changed["run"]["version"] == 2
    assert changed["manifest"]["agent_mapping_digest"] != \
        first["manifest"]["agent_mapping_digest"]


def test_mapping_update_recomputes_gaps_and_report_together(settings, store):
    first = sample_run(settings, store)
    before = first["agents"]
    after = run_with_mapping(settings, store, MAP_RESOLVED)["agents"]
    assert before["uncounted"]["rows"] == 10
    assert after["uncounted"]["rows"] == 1
    assert after["counted"]["agents"] == 5
    # 缺口与报告读的是同一条运行快照：版本换了，两边的归属账同时换
    latest = store.load_result(store.latest_run(SAMPLE)["run_id"])
    assert latest["agents"]["counted"] == after["counted"]
    assert latest["mapping_change"]["changed"] is True
    assert latest["mapping_change"]["uncounted_before"] == 10
    assert latest["mapping_change"]["uncounted_after"] == 1


def test_same_attachment_same_mapping_is_idempotent(settings, store):
    first = run_with_mapping(settings, store, MAP_RESOLVED)
    second = run_with_mapping(settings, store, MAP_RESOLVED)
    assert second["replay"] is True
    assert second["run"]["run_id"] == first["run"]["run_id"]
    assert second["run"]["version"] == 1
    assert store.count_runs() == 1


def test_replay_after_publish_does_not_double_count(settings, store):
    first = sample_run(settings, store)
    second = sample_run(settings, store)
    assert second["replay"] is True
    assert second["agents"]["counted"] == first["agents"]["counted"]
    assert second["agents"]["uncounted"] == first["agents"]["uncounted"]
    assert second["agents"]["total"] == first["agents"]["total"]


def test_publish_interruption_rolls_back_to_previous_version(settings, store):
    first = sample_run(settings, store)
    store.publish_hook = lambda: (_ for _ in ()).throw(RuntimeError("disk full"))
    with pytest.raises(PublishFailed) as info:
        service.run(settings, store, name="tickets_clean.csv")
    store.publish_hook = None
    assert info.value.code == "E_PUBLISH_INTERRUPTED"
    assert info.value.status_code == 503
    assert store.latest_run()["run_id"] == first["run"]["run_id"]
    assert store.latest_run("tickets_clean.csv") is None
    assert [item["name"] for item in store.list_runs()] == [SAMPLE]
    assert store.get_dataset(SAMPLE)["published_run_id"] == first["run"]["run_id"]
    attempts = store.list_publish_attempts()
    assert len(attempts) == 1
    assert attempts[0]["code"] == "E_PUBLISH_INTERRUPTED"
    assert attempts[0]["run"]["name"] == "tickets_clean.csv"
    assert attempts[0]["run"]["run_id"]


def test_failed_mapping_update_keeps_old_mapping_and_report(settings, store):
    first = sample_run(settings, store)
    old_mapping = store.get_agent_mapping(SAMPLE)
    store.publish_hook = lambda: (_ for _ in ()).throw(RuntimeError("disk full"))
    with pytest.raises(PublishFailed):
        run_with_mapping(settings, store, MAP_RESOLVED)
    store.publish_hook = None
    assert store.get_agent_mapping(SAMPLE)["digest"] == old_mapping["digest"]
    latest = store.load_result(store.latest_run(SAMPLE)["run_id"])
    assert latest["agents"]["uncounted"] == first["agents"]["uncounted"]


def test_version_race_retries_with_next_version(settings, store):
    """并发发布抢同一个版本号时，后到者改用下一可用版本，不覆盖已发布版本。"""
    from app.store import VersionRace
    calls = {"n": 0}
    original = store.publish_run

    def racing(run, summary, *, dataset=None, mapping=None, stamp=""):
        calls["n"] += 1
        if calls["n"] == 1:
            # 第一次发布前，模拟另一个发布已经占掉 v1
            other = {**run, "run_id": "QS-OTHER-0001", "digest": "other-digest"}
            original(other, summary, dataset=dataset, mapping=mapping, stamp=stamp)
            raise VersionRace(2)
        return original(run, summary, dataset=dataset, mapping=mapping, stamp=stamp)

    store.publish_run = racing
    result = run_with_mapping(settings, store, MAP_RESOLVED)
    store.publish_run = original
    assert calls["n"] == 2
    assert result["run"]["version"] == 2
    assert result["publish"]["attempts"] == 1
    versions = sorted(item["version"] for item in store.list_runs())
    assert versions == [1, 2]


def test_agent_sample_runs_through_http(client):
    response = client.post("/api/score", json={"sample": SAMPLE})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run"]["version"] == 1
    agents = client.get("/api/agents").json()["agents"]
    assert agents["counted"]["rows"] == 20
    assert agents["uncounted"]["rows"] == 10
    assert client.get("/agents").status_code == 200


def test_mapping_update_over_http_recomputes_and_redownloads(client):
    client.post("/api/score", json={"sample": SAMPLE})
    response = client.put(
        "/api/agents/mapping?dataset=" + SAMPLE,
        files={"file": ("agent_identities.csv", MAP_RESOLVED.encode("utf-8"),
                        "text/csv")})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run"]["version"] == 2
    assert body["uncounted"]["rows"] == 1
    assert body["mapping"]["conflict_count"] == 0
    manifest = json.loads(client.get("/api/export/manifest.json").content)
    assert manifest["agent_attribution"]["uncounted"]["rows"] == 1
    assert manifest["publish"]["version"] == 2
    for name in ("agents.csv", "agents_unresolved.csv", "agents_rows.csv",
                 "mapping.csv"):
        exported = client.get(f"/api/export/{name}")
        assert exported.status_code == 200, name
        assert exported.content.decode("utf-8").startswith("﻿")


def test_mapping_dry_run_does_not_publish(client):
    client.post("/api/score", json={"sample": SAMPLE})
    response = client.put(
        "/api/agents/mapping?dataset=" + SAMPLE + "&dry_run=true",
        files={"file": ("m.csv", MAP_RESOLVED.encode("utf-8"), "text/csv")})
    assert response.status_code == 200
    assert response.json()["published"] is False
    assert client.get("/api/score/latest").json()["run"]["version"] == 1


def test_invalid_mapping_rejected_without_touching_run(client):
    client.post("/api/score", json={"sample": SAMPLE})
    response = client.put(
        "/api/agents/mapping?dataset=" + SAMPLE,
        files={"file": ("m.csv", b"agent_id,display_name\nA1024,x\n", "text/csv")})
    assert response.status_code == 400
    assert response.json()["error"] == "E_MAPPING_INVALID"
    assert client.get("/api/score/latest").json()["run"]["version"] == 1


def test_without_mapping_nothing_is_mismerged(settings, store):
    """没有映射时不猜身份：全部进未计入，绝不按姓名或工号相似度自动归属。"""
    payload = service.run(settings, store, name="tickets_dirty.csv")["agents"]
    assert payload["counted"]["rows"] == 0
    assert payload["counted"]["agents"] == 0
    assert payload["uncounted"]["rows"] == 51
    assert payload["uncounted"]["missing_cells"] == 14
    assert payload["mapping"]["source"] == "none"
    assert payload["mapping"]["entry_count"] == 0
    assert payload["all_balanced"] is True


def test_partial_mapping_leaves_everything_else_unmapped(settings, store):
    """只映射一个标记值时，其余写法仍进未计入；不因「看起来像工号」自动并入。"""
    mapping = "raw,agent_id,display_name\nA1024,A1024,陈允\n"
    result = run_with_mapping(settings, store, mapping)["agents"]
    attributed = [item for item in result["rows"] if item["state"] == "attributed"]
    assert {item["raw"] for item in attributed} == {"A1024"}
    assert len(attributed) == 6
    assert result["counted"]["agents"] == 1
    assert result["uncounted"]["rows"] == 24
    unmapped = bucket(result, "unmapped")
    assert set(unmapped["raw_values"]) == {
        "Alice Chen", "B2007", "陈允", "C3301", "李迟", "周砚", "D4410",
        "王柯", "Zed", "E5502"}
    assert result["all_balanced"] is True


def test_name_only_mapping_never_merges_homonyms(settings, store):
    """映射只写姓名时，同名两人都不能被认领：全部进歧义，计入为 0。"""
    mapping = "raw,agent_id,display_name\n陈允,,陈允\n"
    result = run_with_mapping(settings, store, mapping)["agents"]
    assert result["counted"]["rows"] == 0
    ambiguous = bucket(result, "ambiguous")
    assert ambiguous["rows"] == 4
    assert ambiguous["raw_values"] == {"陈允": 4}


def test_mapping_update_rejects_stale_version(client):
    client.post("/api/score", json={"sample": SAMPLE})
    first = client.put(
        "/api/agents/mapping?dataset=" + SAMPLE,
        files={"file": ("m.csv", MAP_RESOLVED.encode("utf-8"), "text/csv")})
    assert first.status_code == 200
    assert first.json()["run"]["version"] == 2
    stale = client.put(
        "/api/agents/mapping?dataset=" + SAMPLE + "&expected_version=1",
        files={"file": ("m.csv", MAP_FIRST.encode("utf-8"), "text/csv")})
    assert stale.status_code == 409
    assert stale.json()["error"] == "E_STALE_RUN"
    assert client.get("/api/score/latest").json()["run"]["version"] == 2


def test_same_mapping_upload_twice_keeps_one_run(client):
    client.post("/api/score", json={"sample": SAMPLE})
    payload = MAP_RESOLVED.encode("utf-8")
    first = client.put("/api/agents/mapping?dataset=" + SAMPLE,
                       files={"file": ("m.csv", payload, "text/csv")})
    second = client.put("/api/agents/mapping?dataset=" + SAMPLE,
                        files={"file": ("m.csv", payload, "text/csv")})
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["replay"] is True
    assert second.json()["run"]["run_id"] == first.json()["run"]["run_id"]
    runs = client.get("/api/batches").json()["runs"]
    assert [item["version"] for item in runs] == [2, 1]
