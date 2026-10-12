"use strict";

// This panel owns the saved report independently of the selected intake template.
window.ValidationTracking = (() => {
  const STORAGE_KEY = "validation-demo.latest-report";
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const byId = (id) => document.getElementById(id);
  const statuses = { pending: "待执行", completed: "已完成", skipped: "已跳过" };
  const snapshotFields = {
    title: "样例名称", industry: "所属行业", offering: "产品或服务", target_customer: "目标客群",
    business_problem: "经营问题", current_metrics: "当前指标", budget_range: "测试预算", goal: "验证目标", time_limit: "验证时限",
  };
  const errorCopy = {
    record_access_denied: "报告恢复凭证无效或已失效，无法读取或保存。请重新完成一次虚构资料分析。",
    record_not_found: "未找到这份已保存报告，请稍后重新读取。",
    record_not_completed: "这份报告尚未完成，暂时无法记录执行情况。",
    invalid_input: "日志未通过检查：完成时需填写指标与结论，每项最多 400 字，请勿包含联系方式或密钥。",
    rate_limited: "共享调用限额已用尽：分析/重试/恢复/保存合计每IP每10分钟5次。请等待限额窗口恢复（最长约 10 分钟）后重试；未保存的输入与恢复凭证仍保留。",
    demo_disabled: "记录服务当前未开放，请稍后重新读取。",
    feishu_update_failed: "执行日志暂时无法保存，请稍后重试。",
    service_unavailable: "记录服务暂时不可用，请稍后重试；未保存的输入仍保留在页面中。",
  };

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
  }

  function create({ renderAnalysis }) {
    let latest = null;
    let generation = 0;
    let reading = false;
    let saving = null;
    let record = null;
    const rows = new Map();

    function status(message, tone = "info") {
      byId("tracker-status").textContent = message;
      byId("tracker-status").dataset.tone = tone;
    }

    function controls() {
      const executionsKnown = Array.isArray(record?.executions);
      byId("tracker-refresh").disabled = !latest || reading || saving !== null;
      byId("copy-recovery-code").hidden = !latest;
      byId("restore-recovery-code").disabled = reading || saving !== null;
      byId("recovery-code-input").disabled = reading || saving !== null;
      for (const [day, row] of rows) {
        row.fields.disabled = !executionsKnown || reading || saving === day;
        row.button.disabled = !executionsKnown || reading || saving !== null;
      }
    }

    function identify() {
      byId("tracker-title").textContent = latest.title || "已保存报告（资料快照不可用）";
      byId("tracker-request-id").textContent = `报告 UUID：${latest.requestId}`;
      byId("tracker-request-id").hidden = false;
    }

    function persist() {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(latest));
        return true;
      } catch (_) {
        return false;
      }
    }

    async function post(path, extra = {}) {
      const credentials = latest;
      const response = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: credentials.requestId, access_token: credentials.accessToken, ...extra }),
      });
      let payload;
      try { payload = await response.json(); } catch (_) { payload = {}; }
      if (!response.ok) {
        const code = response.status === 429 ? "rate_limited" : response.status === 422 ? "invalid_input"
          : payload.detail?.code || "service_unavailable";
        throw Object.assign(new Error(errorCopy[code] || errorCopy.service_unavailable), { code });
      }
      if (payload.request_id !== credentials.requestId || !payload.analysis || !Array.isArray(payload.executions)) {
        throw new Error(errorCopy.service_unavailable);
      }
      return payload;
    }

    function progress() {
      if (!Array.isArray(record?.executions)) {
        byId("tracker-progress").textContent = "执行进度待恢复";
        return;
      }
      const completed = new Set((record?.executions || [])
        .filter((execution) => execution.status === "completed" && execution.day >= 1 && execution.day <= 7)
        .map((execution) => execution.day)).size;
      byId("tracker-progress").textContent = `${completed} / 7 天已完成`;
    }

    function setEvidenceRequired(row) {
      row.metric.required = row.selection.value === "completed";
      row.conclusion.required = row.selection.value === "completed";
    }

    function values(row) {
      return { status: row.selection.value, observed_metric: row.metric.value.trim(), conclusion: row.conclusion.value.trim() };
    }

    function fillRow(row, execution) {
      const executionsKnown = Array.isArray(record?.executions);
      const saved = execution || { status: executionsKnown ? "pending" : "", observed_metric: "", conclusion: "" };
      row.selection.value = saved.status;
      row.metric.value = saved.observed_metric;
      row.conclusion.value = saved.conclusion;
      row.baseline = JSON.stringify(values(row));
      setEvidenceRequired(row);
      row.feedback.textContent = !executionsKnown ? "执行状态未知；日志尚未读回，请重新读取后保存。"
        : execution?.updated_at ? `已保存 · ${formatTime(execution.updated_at)}` : "尚未保存执行记录";
      row.feedback.dataset.tone = execution ? "success" : "info";
    }

    function formatTime(value) {
      const time = new Date(value);
      return Number.isNaN(time.getTime()) ? "时间暂不可用" : time.toLocaleString("zh-CN", { hour12: false });
    }

    function textareaField(day, suffix, labelText) {
      const field = node("div", "form-field");
      const id = `day-${day}-${suffix}`;
      const label = node("label", "", labelText);
      label.setAttribute("for", id);
      const input = node("textarea", "");
      input.id = id;
      input.name = suffix === "metric" ? "observed_metric" : "conclusion";
      input.rows = 2;
      input.maxLength = 400;
      input.placeholder = "仅填写虚构演示内容，最多 400 字";
      input.setAttribute("aria-describedby", `day-${day}-feedback`);
      field.append(label, input);
      return { field, input };
    }

    function buildRows() {
      rows.clear();
      byId("tracker-list").replaceChildren();
      for (let day = 1; day <= 7; day++) {
        const action = record.analysis.seven_day_actions?.find((item) => item.day === day);
        const form = node("form", "execution-row");
        form.id = `day-${day}-form`;
        const fields = node("fieldset", "");
        const legend = node("legend", "", `第 ${day} 天`);
        fields.append(legend);
        fields.append(node("p", "execution-action", action?.action || "记录这一天的虚构实验"));
        if (action) fields.append(node("p", "execution-rule", `计划指标：${action.metric}；判断规则：${action.decision_rule}`));
        const grid = node("div", "execution-grid");
        const statusField = node("div", "form-field execution-status");
        const label = node("label", "", "执行状态");
        label.setAttribute("for", `day-${day}-status`);
        const selection = node("select", "");
        selection.id = `day-${day}-status`;
        selection.name = "status";
        const unknown = node("option", "", "未知（待读取）");
        unknown.value = "";
        unknown.disabled = true;
        unknown.hidden = true;
        selection.append(unknown);
        for (const [value, text] of Object.entries(statuses)) {
          const option = node("option", "", text);
          option.value = value;
          selection.append(option);
        }
        statusField.append(label, selection);
        const metric = textareaField(day, "metric", "观察指标（完成时必填）");
        const conclusion = textareaField(day, "conclusion", "结论（完成时必填）");
        grid.append(statusField, metric.field, conclusion.field);
        fields.append(grid);
        const footer = node("div", "execution-footer");
        const button = node("button", "secondary-button", `保存第 ${day} 天`);
        button.type = "submit";
        const feedback = node("p", "form-feedback");
        feedback.id = `day-${day}-feedback`;
        feedback.setAttribute("role", "status");
        feedback.setAttribute("aria-live", "polite");
        footer.append(button, feedback);
        form.append(fields, footer);
        const row = { fields, selection, metric: metric.input, conclusion: conclusion.input, button, feedback, baseline: "" };
        rows.set(day, row);
        selection.addEventListener("change", () => setEvidenceRequired(row));
        for (const input of [selection, row.metric, row.conclusion]) {
          input.addEventListener("input", () => {
            row.feedback.textContent = "有未保存的修改";
            row.feedback.dataset.tone = "info";
          });
        }
        form.addEventListener("submit", (event) => { event.preventDefault(); return saveDay(day); });
        fillRow(row, (record.executions || []).find((item) => item.day === day));
        byId("tracker-list").append(form);
      }
    }

    function renderSnapshot(scenario, source) {
      const content = byId("saved-snapshot-content");
      content.replaceChildren();
      byId("saved-snapshot").hidden = false;
      if (!scenario) {
        byId("saved-snapshot-source").textContent = "这份旧版报告未保存资料快照，无法展示原始输入。";
        return;
      }
      byId("saved-snapshot-source").textContent = source === "submission"
        ? "本次提交资料（已确认保存）" : "已从演示表读回资料快照";
      for (const [field, label] of Object.entries(snapshotFields)) {
        const pair = node("div", "");
        pair.append(node("dt", "", label), node("dd", "", typeof scenario[field] === "string" ? scenario[field] : "—"));
        content.append(pair);
      }
    }

    function applyRecord(details, savedDay = null, reset = false, source = "readback") {
      const previousExecutionsKnown = Array.isArray(record?.executions);
      record = details;
      if (typeof details.scenario?.title === "string") latest = { ...latest, title: details.scenario.title };
      identify();
      renderAnalysis(details.analysis, byId("saved-report-content"));
      byId("saved-report").hidden = false;
      renderSnapshot(details.scenario, source);
      if (reset || rows.size === 0) buildRows();
      else {
        for (const [day, row] of rows) {
          if (!previousExecutionsKnown || day === savedDay || JSON.stringify(values(row)) === row.baseline) {
            fillRow(row, (details.executions || []).find((item) => item.day === day));
          }
        }
      }
      progress();
    }

    async function refresh() {
      if (!latest || reading || saving !== null) return;
      const current = generation;
      reading = true;
      controls();
      status("正在读取已保存报告与七天记录…");
      try {
        const details = await post("/api/record");
        if (current !== generation) return;
        applyRecord(details);
        const stored = persist();
        status(stored ? "已恢复这份报告。可按天保存虚构执行记录，未保存的修改会保留。"
          : "报告已读取，但浏览器无法保存恢复凭证。请复制恢复码并妥善保存。", stored ? "success" : "error");
      } catch (error) {
        if (current === generation) status(errorCopy[error.code] || errorCopy.service_unavailable, "error");
      } finally {
        if (current === generation) { reading = false; controls(); }
      }
    }

    async function saveDay(day) {
      if (!latest || !Array.isArray(record?.executions) || reading || saving !== null) return;
      const row = rows.get(day);
      const execution = { day, ...values(row) };
      if (!Object.hasOwn(statuses, execution.status)
        || execution.observed_metric.length > 400 || execution.conclusion.length > 400
        || (execution.status === "completed" && (!execution.observed_metric || !execution.conclusion))) {
        row.feedback.textContent = errorCopy.invalid_input;
        row.feedback.dataset.tone = "error";
        return;
      }
      const current = generation;
      saving = day;
      controls();
      row.feedback.textContent = "正在保存这一天的虚构记录…";
      try {
        const details = await post("/api/actions", { execution });
        if (current !== generation) return;
        applyRecord(details, day);
        status(`第 ${day} 天已保存；这是虚构演示日志。`, "success");
      } catch (error) {
        if (current === generation) {
          row.feedback.textContent = errorCopy[error.code] || errorCopy.service_unavailable;
          row.feedback.dataset.tone = "error";
        }
      } finally {
        if (current === generation) { saving = null; controls(); }
      }
    }

    function acceptSuccess(result, scenario, { readExistingLogs = false } = {}) {
      generation++;
      reading = false;
      saving = null;
      latest = {
        requestId: result.request_id, accessToken: result.access_token,
        scenarioId: scenario?.id || null, title: scenario?.title || "已保存报告",
      };
      const stored = persist();
      // Only a genuinely new analysis has known empty logs. A completed retry
      // may already have saved logs, so null keeps every save locked until readback.
      applyRecord({ request_id: result.request_id, scenario, analysis: result.analysis, executions: readExistingLogs ? null : [] }, null, true, "submission");
      controls();
      status(stored ? "报告已保存。刷新可恢复报告与七天日志；浏览器仅保留最近一份。"
        : "报告已完成，但浏览器无法保存恢复凭证。请复制恢复码并妥善保存。", stored ? "success" : "error");
      // All four POST routes share five calls per IP per ten minutes. Seed new
      // reports locally; only recovery/retry reads existing authoritative logs.
      if (readExistingLogs) return refresh();
    }

    async function restore() {
      try {
        const stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
        if (!stored) return;
        if (!uuid.test(stored.requestId) || typeof stored.accessToken !== "string" || !/^[0-9a-f]{64}$/i.test(stored.accessToken)) {
          status("浏览器中的报告恢复凭证不完整，请重新完成一次虚构资料分析。", "error");
          return;
        }
        latest = {
          requestId: stored.requestId, accessToken: stored.accessToken,
          scenarioId: typeof stored.scenarioId === "string" ? stored.scenarioId : null,
          title: typeof stored.title === "string" ? stored.title : "已保存报告",
        };
        identify();
        controls();
        await refresh();
      } catch (_) {
        status("浏览器无法读取报告恢复凭证；完成新分析后可在本页记录。", "error");
      }
    }

    async function importRecoveryCode() {
      if (reading || saving !== null) return;
      const input = byId("recovery-code-input");
      const feedback = byId("recovery-code-feedback");
      let code;
      try {
        if (input.value.length > 512) throw new Error("invalid");
        code = JSON.parse(input.value);
        if (!code || typeof code !== "object" || Array.isArray(code) || Object.keys(code).length !== 2
          || typeof code.request_id !== "string" || !uuid.test(code.request_id)
          || typeof code.access_token !== "string" || !/^[0-9a-f]{64}$/i.test(code.access_token)) {
          throw new Error("invalid");
        }
      } catch (_) {
        input.setAttribute("aria-invalid", "true");
        feedback.textContent = "恢复码格式不正确，请粘贴从「复制报告恢复码」保存的完整内容。";
        feedback.dataset.tone = "error";
        return;
      }
      input.removeAttribute("aria-invalid");
      feedback.textContent = "恢复码格式已通过，正在读取这份报告；结果见上方状态。";
      feedback.dataset.tone = "info";
      generation++;
      latest = {
        requestId: code.request_id.toLowerCase(), accessToken: code.access_token,
        scenarioId: null, title: "已保存报告（资料快照不可用）",
      };
      // Clear the previous report before relabeling so a failed import can never
      // attach old analysis or unsaved logs to this different bearer code.
      record = null;
      rows.clear();
      byId("tracker-list").replaceChildren();
      byId("saved-report-content").replaceChildren();
      byId("saved-report").hidden = true;
      byId("saved-snapshot-content").replaceChildren();
      byId("saved-snapshot-source").textContent = "";
      byId("saved-snapshot").hidden = true;
      progress();
      identify();
      persist();
      controls();
      await refresh();
      feedback.textContent = "读取结果见上方状态。恢复码保留在输入框中，读取失败时可稍后重试。";
    }

    byId("tracker-refresh").addEventListener("click", refresh);
    byId("restore-recovery-code").addEventListener("click", importRecoveryCode);
    byId("copy-recovery-code").addEventListener("click", async () => {
      if (!latest) return;
      try {
        await navigator.clipboard.writeText(JSON.stringify({ request_id: latest.requestId, access_token: latest.accessToken }));
        status("报告恢复码已复制。持有码者可以读取和编辑这份报告，请妥善保存。", "success");
      } catch (_) {
        status("浏览器不允许复制，请保留本页或允许剪贴板访问后重试。", "error");
      }
    });
    return { restore, acceptSuccess };
  }
  return { create };
})();
