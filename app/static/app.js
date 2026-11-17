/* 采信与评分：一个按钮跑完「采信 → 评分」，失败就地提示并保留已填内容。 */
(function () {
  "use strict";

  const state = { busy: false };

  function byId(id) {
    return document.getElementById(id);
  }

  function status(text, kind) {
    const node = byId("form-status");
    if (!node) return;
    node.textContent = text;
    node.className = "status" + (kind ? " status--" + kind : "");
  }

  function buttons(disabled) {
    [byId("run-btn"), byId("replay-btn")].forEach(function (button) {
      if (button) button.disabled = disabled;
    });
  }

  async function postJSON(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const payload = await response.json().catch(function () { return {}; });
    if (!response.ok) {
      const message = payload.message || payload.detail || response.statusText;
      throw new Error(message + "（HTTP " + response.status + "）");
    }
    return payload;
  }

  async function runPipeline(body) {
    if (state.busy) return;
    state.busy = true;
    buttons(true);
    status("采信中…");
    try {
      await postJSON("/api/datasets/import", {
        sample: body.sample,
        encoding: body.encoding || null
      });
      status("评分中…");
      const scored = await postJSON("/api/score", {
        sample: body.sample,
        encoding: body.encoding || null,
        expected_version: body.expected_version
      });
      status("完成，正在刷新…", "good");
      window.location.assign("/?run_id=" + encodeURIComponent(scored.run.run_id));
    } catch (error) {
      status(error.message, "bad");
      buttons(false);
    } finally {
      state.busy = false;
    }
  }

  function bind() {
    const form = byId("ingest-form");
    if (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        const data = new FormData(form);
        runPipeline({
          sample: String(data.get("sample") || ""),
          encoding: String(data.get("encoding") || ""),
          expected_version: null
        });
      });
    }
    const replay = byId("replay-btn");
    if (replay && form) {
      replay.addEventListener("click", function () {
        runPipeline({
          sample: form.getAttribute("data-sample") || "",
          encoding: String((byId("encoding-input") || {}).value || ""),
          expected_version: form.getAttribute("data-version") || null
        });
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();

/* 受理人映射：只校验 / 保存并重算。保存后整份重算并回跳到同一数据集的新运行。 */
(function () {
  "use strict";

  function byId(id) {
    return document.getElementById(id);
  }

  function status(text, kind) {
    const node = byId("mapping-status");
    if (!node) return;
    node.textContent = text;
    node.className = "status" + (kind ? " status--" + kind : "");
  }

  function buttons(disabled) {
    [byId("mapping-check"), byId("mapping-save")].forEach(function (button) {
      if (button) button.disabled = disabled;
    });
  }

  function datasetName() {
    const form = byId("mapping-form");
    return form ? form.getAttribute("data-dataset") || "" : "";
  }

  function mappingFile() {
    const text = (byId("mapping-text") || {}).value || "";
    return new File([text], "agent_identities.csv", { type: "text/csv" });
  }

  async function upload(dryRun) {
    const form = new FormData();
    form.append("file", mappingFile());
    const query = datasetName() ? "?dataset=" + encodeURIComponent(datasetName()) : "";
    const suffix = dryRun ? (query ? "&" : "?") + "dry_run=true" : query;
    status(dryRun ? "校验中…" : "重算并发布中…");
    buttons(true);
    try {
      const response = await fetch("/api/agents/mapping" + suffix, {
        method: "PUT",
        body: form
      });
      const payload = await response.json().catch(function () { return {}; });
      if (!response.ok) {
        throw new Error((payload.message || payload.detail || response.statusText) +
          "（HTTP " + response.status + "）");
      }
      if (dryRun) {
        const mapping = payload.mapping || {};
        status("校验通过：" + mapping.mapped_count + " 条有工号、" +
          mapping.name_only_count + " 条仅姓名、" + mapping.conflict_count +
          " 条冲突。未保存。", "good");
        buttons(false);
        return;
      }
      status("已发布 v" + (payload.run ? payload.run.version : "?") +
        "，未计入 " + (payload.uncounted ? payload.uncounted.rows : "?") + " 行，正在刷新…",
        "good");
      const runId = payload.run ? payload.run.run_id : "";
      window.location.assign("/agents?run_id=" + encodeURIComponent(runId));
    } catch (error) {
      status(error.message, "bad");
      buttons(false);
    }
  }

  function bind() {
    const check = byId("mapping-check");
    const save = byId("mapping-save");
    if (check) check.addEventListener("click", function () { upload(true); });
    if (save) save.addEventListener("click", function () { upload(false); });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
