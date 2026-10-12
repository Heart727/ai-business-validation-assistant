const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const scenarios = JSON.parse(fs.readFileSync(path.join(root, "data/scenarios.json"), "utf8"));
const analysis = JSON.parse(fs.readFileSync(path.join(root, "data/example_analysis.json"), "utf8")).analysis;
const requestId = "0e9dbe92-0fc0-4cc2-9b5e-d7baccc04853";
const requestToken = "019dbe92-0fc0-4cc2-9b5e-d7baccc04853";
const accessToken = "a".repeat(64);
const pendingKey = "validation-demo.pending-request";
const latestKey = "validation-demo.latest-report";

// A small DOM boundary double: run the actual scripts and HTML, while the
// parent verifies layout and native browser behavior separately.
class Element {
  constructor(tagName = "div") {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this._text = "";
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  get childElementCount() { return this.children.length; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ""; this.children = children; }
  setAttribute(name, value) { this.attributes[name] = String(value); if (name === "id") this.id = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  async fire(name) { for (const callback of this.listeners[name] || []) await callback({ preventDefault() {} }); }
  reportValidity() { return true; }
  focus() { this.focused = true; }
}

function storage(seed = {}) {
  const items = new Map(Object.entries(seed));
  const writes = [];
  return { writes, getItem: (key) => items.get(key) ?? null,
    setItem: (key, value) => { writes.push({ key, value }); items.set(key, value); },
    removeItem: (key) => items.delete(key) };
}

async function boot({ session = {}, local = {}, route = () => null, live = true } = {}) {
  const elements = new Map();
  const html = fs.readFileSync(path.join(root, "static/index.html"), "utf8");
  for (const match of html.matchAll(/<([a-z][\w-]*)\b([^>]*\bid="([^"]+)"[^>]*)>/gi)) {
    const element = new Element(match[1]);
    element.id = match[3];
    element.hidden = /\bhidden\b/.test(match[2]);
    element.disabled = /\bdisabled\b/.test(match[2]);
    elements.set(element.id, element);
  }
  function find(id, element) {
    if (element.id === id) return element;
    for (const child of element.children) { const result = find(id, child); if (result) return result; }
    return null;
  }
  const document = {
    getElementById(id) {
      if (elements.has(id)) return elements.get(id);
      for (const element of elements.values()) { const found = find(id, element); if (found) return found; }
      return null;
    },
    createElement: (tag) => new Element(tag),
    createTextNode: (text) => { const node = new Element("#text"); node.textContent = text; return node; },
  };
  const requests = [];
  const sessionStorage = storage(session);
  const localStorage = storage(local);
  const clipboardCopies = [];
  let uuidCount = 0;
  const context = vm.createContext({
    document, sessionStorage, localStorage, console,
    crypto: { randomUUID: () => ++uuidCount === 1 ? requestId : requestToken },
    navigator: { clipboard: { writeText: async (value) => { clipboardCopies.push(value); } } },
    fetch: async (url, options) => {
      const body = options?.body ? JSON.parse(options.body) : null;
      requests.push({ url, body });
      const custom = await route(url, body);
      if (custom) return { ok: custom.status < 400, status: custom.status, json: async () => custom.payload };
      const submission = requests.findLast((item) => ["/api/analyze", "/api/retry"].includes(item.url));
      const submittedScenario = submission?.body?.draft ? { id: submission.body.scenario_id, ...submission.body.draft } : scenarios[0];
      const payload = url === "/api/health" ? { live_demo_enabled: live }
        : url === "/api/scenarios" ? scenarios : url === "/api/preview" ? analysis
        : url === "/api/record" ? record([], submittedScenario) : {};
      return { ok: true, status: 200, json: async () => payload };
    },
  });
  context.window = context;
  for (const filename of ["observations.js", "tracking.js", "app.js"]) {
    const file = path.join(root, "static", filename);
    if (fs.existsSync(file)) vm.runInContext(fs.readFileSync(file, "utf8"), context, { filename });
  }
  await settle();
  return { context, requests, sessionStorage, localStorage, clipboardCopies, byId: document.getElementById,
    allText: () => [...elements.values()].map((element) => element.textContent).join("\n"),
    eval: (code) => vm.runInContext(code, context) };
}
const settle = () => new Promise((resolve) => setImmediate(resolve));
const completed = { status: 200, payload: { request_id: requestId, status: "completed", analysis, access_token: accessToken } };
const record = (executions = [], scenario = scenarios[0]) => ({ request_id: requestId, scenario, analysis, executions });
const saved = JSON.stringify({ requestId, accessToken, scenarioId: scenarios[0].id, title: scenarios[0].title });

test("edited intake requires confirmation and submits a private immutable snapshot", async () => {
  let resolveRequest;
  const app = await boot({ route: (url) => url === "/api/analyze" ? new Promise((resolve) => { resolveRequest = resolve; }) : null });
  assert.ok(app.byId("draft-title"), "an editable intake must be rendered");
  app.byId("draft-title").value = "虚构：修改后的早餐实验";
  await app.byId("run-button").fire("click");
  assert.equal(app.requests.filter((item) => item.url === "/api/analyze").length, 0);
  app.byId("fictional-only").checked = true;
  const submission = app.byId("run-button").fire("click");
  await settle();
  const sent = app.requests.find((item) => item.url === "/api/analyze").body;
  assert.equal(sent.draft.title, "虚构：修改后的早餐实验");
  assert.equal(sent.draft.fictional_only, true);
  assert.equal(sent.request_id, requestId);
  assert.equal(sent.request_token, requestToken);
  assert.equal(app.byId("intake-fields").disabled, true);
  assert.equal(app.eval("Object.isFrozen(state.pending) && Object.isFrozen(state.pending.draft)"), true);
  app.byId("draft-title").value = "虚构：请求途中改动";
  assert.equal(JSON.parse(app.sessionStorage.getItem(pendingKey)).draft.title, "虚构：修改后的早餐实验");
  resolveRequest(completed);
  await submission;
  assert.equal(JSON.parse(app.localStorage.getItem(latestKey)).accessToken, accessToken);
  assert.equal(app.sessionStorage.getItem(pendingKey), null);
  assert.equal(app.byId("result-title").textContent, "虚构：修改后的早餐实验");
  assert.ok(app.byId("result-request-id").textContent.includes(requestId));
  assert.equal(app.allText().includes(requestToken), false);
  assert.equal(app.allText().includes(accessToken), false);
  assert.equal(app.requests.some((item) => item.url === "/api/record"), false, "a new successful analysis must not spend another request reading empty logs");
  assert.equal(app.byId("tracker-list").childElementCount, 7);
});

test("reload and retry reuse the edited draft and request token instead of the template", async () => {
  const draft = { ...scenarios[0], title: "虚构：刷新前草稿", fictional_only: true };
  delete draft.id;
  const pending = { scenarioId: scenarios[0].id, requestId, requestToken, draft };
  const app = await boot({ session: { [pendingKey]: JSON.stringify(pending) }, route: (url) => url === "/api/retry" ? completed : null });
  assert.ok(app.byId("draft-title"), "restored intake must remain available");
  assert.equal(app.byId("draft-title").value, draft.title);
  assert.equal(app.byId("intake-fields").disabled, true);
  app.byId("draft-title").value = "虚构：篡改表单";
  await app.byId("retry-button").fire("click");
  const retried = app.requests.find((item) => item.url === "/api/retry").body;
  assert.deepEqual(retried, { scenario_id: scenarios[0].id, request_id: requestId, request_token: requestToken, draft });
});

test("protected pending records without a valid token or UUID are not restored", async () => {
  const draft = { ...scenarios[0], fictional_only: true }; delete draft.id;
  for (const corrupt of [{ requestId, requestToken: "broken" }, { requestId: "broken", requestToken }, { requestId }]) {
    const app = await boot({ session: { [pendingKey]: JSON.stringify({ ...corrupt, scenarioId: scenarios[0].id, draft }) } });
    assert.equal(app.eval("state.pending === null"), true);
    assert.equal(app.requests.some((item) => item.url === "/api/retry"), false);
  }
});

test("validation errors identify fields safely and permit correcting the form with a new request", async () => {
  const app = await boot({ route: (url) => url === "/api/analyze" ? { status: 422, payload: { detail: { code: "invalid_input", fields: ["body.draft.goal"] } } } : null });
  assert.ok(app.byId("fictional-only"), "intake confirmation must be rendered");
  app.byId("fictional-only").checked = true;
  await app.byId("run-button").fire("click");
  assert.match(app.byId("status-message").textContent, /验证目标/);
  assert.equal(app.byId("draft-goal").attributes["aria-invalid"], "true");
  assert.equal(app.byId("intake-fields").disabled, false);
  assert.equal(app.sessionStorage.getItem(pendingKey), null);
});

test("429 and content conflicts preserve the pending request and offer useful feedback", async () => {
  for (const [status, code, copy] of [[429, "unrecognized", /限额.*10\s*分钟/], [409, "request_content_conflict", /冲突|不一致/]]) {
    const app = await boot({ route: (url) => url === "/api/analyze" ? { status, payload: { detail: { code } } } : null });
    assert.ok(app.byId("fictional-only"));
    app.byId("fictional-only").checked = true;
    await app.byId("run-button").fire("click");
    assert.match(app.byId("status-message").textContent, copy);
    assert.equal(JSON.parse(app.sessionStorage.getItem(pendingKey)).requestToken, requestToken);
    assert.equal(app.byId("intake-fields").disabled, true);
  }
});

test("saved report restores in its own panel and never adopts the selected template title", async () => {
  const app = await boot({ local: { [latestKey]: saved }, route: (url) => url === "/api/record" ? { status: 200, payload: record() } : null });
  const loaded = app.requests.find((item) => item.url === "/api/record");
  assert.ok(loaded, "reload should fetch the saved record");
  assert.deepEqual(loaded.body, { request_id: requestId, access_token: accessToken });
  assert.equal(app.byId("tracker-list").childElementCount, 7);
  assert.equal(app.byId("tracker-title").textContent, scenarios[0].title);
  assert.ok(app.byId("saved-report-content").textContent.includes(analysis.executive_summary));
  app.eval(`selectScenario(${JSON.stringify(scenarios[1].id)})`);
  assert.equal(app.byId("selected-title").textContent, scenarios[1].title);
  assert.equal(app.byId("tracker-title").textContent, scenarios[0].title);
  assert.equal(app.byId("result-content").textContent.includes(analysis.executive_summary), false);
});

test("day save validates completed evidence, updates progress and preserves another day's unsaved draft", async () => {
  const dayOne = { day: 1, status: "completed", observed_metric: "虚构：12 次", conclusion: "虚构：继续实验", updated_at: "2026-10-12T08:00:00Z" };
  const app = await boot({ local: { [latestKey]: saved }, route: (url) => url === "/api/record" ? { status: 200, payload: record() } : url === "/api/actions" ? { status: 200, payload: record([dayOne]) } : null });
  assert.ok(app.byId("day-1-status"), "seven daily forms must be available");
  app.byId("day-1-status").value = "completed";
  await app.byId("day-1-form").fire("submit");
  assert.equal(app.requests.some((item) => item.url === "/api/actions"), false);
  assert.match(app.byId("day-1-feedback").textContent, /指标.*结论/);
  app.byId("day-1-metric").value = dayOne.observed_metric;
  app.byId("day-1-conclusion").value = dayOne.conclusion;
  app.byId("day-2-metric").value = "尚未保存的虚构指标";
  await app.byId("day-1-form").fire("submit");
  assert.deepEqual(app.requests.find((item) => item.url === "/api/actions").body, {
    request_id: requestId, access_token: accessToken,
    execution: { day: 1, status: "completed", observed_metric: dayOne.observed_metric, conclusion: dayOne.conclusion },
  });
  assert.match(app.byId("tracker-progress").textContent, /1\s*\/\s*7/);
  assert.equal(app.byId("day-2-metric").value, "尚未保存的虚构指标");
  assert.equal(app.requests.some((item) => ["/api/analyze", "/api/retry"].includes(item.url)), false);
});

test("a failed record read offers recovery without issuing an AI request", async () => {
  const app = await boot({ local: { [latestKey]: saved }, route: (url) => url === "/api/record" ? { status: 403, payload: { detail: { code: "record_access_denied" } } } : null });
  assert.ok(app.byId("tracker-status"));
  assert.match(app.byId("tracker-status").textContent, /凭证|令牌/);
  assert.equal(app.byId("tracker-refresh").disabled, false);
  assert.equal(app.byId("tracker-list").childElementCount, 0);
  assert.equal(app.requests.some((item) => ["/api/analyze", "/api/retry"].includes(item.url)), false);
});

test("a slow previous report recovery cannot erase a newly completed analysis", async () => {
  let releaseOldRecord;
  let recordReads = 0;
  const app = await boot({ local: { [latestKey]: saved }, route: (url) => url === "/api/record"
    ? ++recordReads === 1 ? new Promise((resolve) => { releaseOldRecord = resolve; }) : { status: 200, payload: record([], scenarios[1]) }
    : url === "/api/analyze" ? completed : null });
  app.eval(`selectScenario(${JSON.stringify(scenarios[1].id)})`);
  app.byId("fictional-only").checked = true;
  await app.byId("run-button").fire("click");
  assert.equal(app.byId("result-title").textContent, scenarios[1].title);
  releaseOldRecord({ status: 200, payload: record() });
  await settle();
  assert.equal(app.byId("result-title").textContent, scenarios[1].title);
  assert.equal(app.byId("tracker-title").textContent, scenarios[1].title);
});

test("recovering a completed request reads existing execution logs rather than resetting all seven days", async () => {
  const draft = { ...scenarios[0], fictional_only: true }; delete draft.id;
  const app = await boot({ session: { [pendingKey]: JSON.stringify({ scenarioId: scenarios[0].id, requestId, requestToken, draft }) }, route: (url) => url === "/api/retry" ? completed
    : url === "/api/record" ? { status: 200, payload: record([{ day: 3, status: "completed", observed_metric: "虚构：8 单", conclusion: "虚构：继续验证", updated_at: "2026-10-12T08:00:00Z" }]) } : null });
  await app.byId("retry-button").fire("click");
  assert.equal(app.byId("day-3-status").value, "completed");
  assert.equal(app.byId("day-3-metric").value, "虚构：8 单");
  assert.match(app.byId("tracker-progress").textContent, /1\s*\/\s*7/);
});

test("a protected request denial explains the missing recovery credential and keeps its snapshot", async () => {
  const app = await boot({ route: (url) => url === "/api/analyze"
    ? { status: 403, payload: { detail: { code: "record_access_denied" } } } : null });
  app.byId("fictional-only").checked = true;
  await app.byId("run-button").fire("click");
  assert.match(app.byId("status-message").textContent, /凭证/);
  assert.equal(JSON.parse(app.sessionStorage.getItem(pendingKey)).requestToken, requestToken);
});

test("legacy pending requests retry with the original scenario-only payload", async () => {
  const app = await boot({ session: { [pendingKey]: JSON.stringify({ requestId, scenarioId: scenarios[0].id }) },
    route: (url) => url === "/api/retry" ? { status: 200, payload: { ...completed.payload, access_token: null } } : null });
  await app.byId("retry-button").fire("click");
  assert.deepEqual(app.requests.find((item) => item.url === "/api/retry").body, { request_id: requestId, scenario_id: scenarios[0].id });
  assert.equal(app.byId("result-title").textContent, scenarios[0].title);
  assert.ok(app.byId("result-content").textContent.includes(analysis.executive_summary));
  assert.ok(app.byId("result-request-id").textContent.includes(requestId));
  assert.match(app.byId("status-message").textContent, /无法保存.*执行记录/);
  assert.equal(app.byId("retry-button").hidden, true);
  assert.equal(app.sessionStorage.getItem(pendingKey), null);
  assert.equal(app.localStorage.getItem(latestKey), null);
  assert.equal(app.requests.some((item) => ["/api/record", "/api/actions"].includes(item.url)), false);
  assert.equal(app.byId("tracker-list").childElementCount, 0);
});

test("a token-null legacy report cannot replace a previously saved writable report", async () => {
  const previousId = "219dbe92-0fc0-4cc2-9b5e-d7baccc04853";
  const previous = JSON.stringify({ requestId: previousId, accessToken, scenarioId: scenarios[1].id, title: scenarios[1].title });
  const app = await boot({ session: { [pendingKey]: JSON.stringify({ requestId, scenarioId: scenarios[0].id }) }, local: { [latestKey]: previous },
    route: (url) => url === "/api/retry" ? { status: 200, payload: { ...completed.payload, access_token: null } }
      : url === "/api/record" ? { status: 200, payload: { ...record([], scenarios[1]), request_id: previousId } } : null });
  await app.byId("retry-button").fire("click");
  assert.equal(app.byId("result-title").textContent, scenarios[0].title);
  assert.equal(app.byId("tracker-title").textContent, scenarios[1].title);
  assert.equal(JSON.parse(app.localStorage.getItem(latestKey)).requestId, previousId);
  assert.equal(app.requests.filter((item) => item.url === "/api/record").length, 1);
  assert.match(app.byId("status-message").textContent, /无法保存.*执行记录/);
});

test("edited protected requests still require a valid report token on completion", async () => {
  const draft = { ...scenarios[0], fictional_only: true }; delete draft.id;
  const app = await boot({ session: { [pendingKey]: JSON.stringify({ requestId, requestToken, draft, scenarioId: scenarios[0].id }) },
    route: (url) => url === "/api/retry" ? { status: 200, payload: { ...completed.payload, access_token: null } } : null });
  await app.byId("retry-button").fire("click");
  assert.notEqual(app.sessionStorage.getItem(pendingKey), null);
  assert.equal(app.byId("intake-fields").disabled, true);
  assert.equal(app.localStorage.getItem(latestKey), null);
});

test("empty, oversized and unmarked intake fields never reach the analysis API", async () => {
  for (const [field, value] of [["goal", "   "], ["goal", "甲".repeat(401)], ["title", "真实店铺"], ["industry", "甲".repeat(61)]]) {
    const app = await boot();
    app.byId("fictional-only").checked = true;
    app.byId(`draft-${field}`).value = value;
    await app.byId("run-button").fire("click");
    assert.equal(app.requests.some((item) => item.url === "/api/analyze"), false);
    assert.equal(app.byId(`draft-${field}`).attributes["aria-invalid"], "true");
    assert.equal(app.sessionStorage.getItem(pendingKey), null);
  }
});

test("skipped logs accept blank evidence and oversized logs remain unsent", async () => {
  const app = await boot({ local: { [latestKey]: saved }, route: (url) => url === "/api/record"
    ? { status: 200, payload: record([], null) } : url === "/api/actions" ? { status: 200, payload: record([{ day: 2, status: "skipped", observed_metric: "", conclusion: "", updated_at: "2026-10-12T08:00:00Z" }], null) } : null });
  assert.equal(app.byId("tracker-title").textContent, scenarios[0].title);
  app.byId("day-2-status").value = "skipped";
  app.byId("day-2-metric").value = "甲".repeat(401);
  await app.byId("day-2-form").fire("submit");
  assert.equal(app.requests.some((item) => item.url === "/api/actions"), false);
  app.byId("day-2-metric").value = "";
  await app.byId("day-2-form").fire("submit");
  assert.deepEqual(app.requests.find((item) => item.url === "/api/actions").body.execution, { day: 2, status: "skipped", observed_metric: "", conclusion: "" });
  assert.match(app.byId("tracker-progress").textContent, /0\s*\/\s*7/);
});

test("server validation of a log preserves the user's metric and the saved completion count", async () => {
  const app = await boot({ local: { [latestKey]: saved }, route: (url) => url === "/api/record"
    ? { status: 200, payload: record() } : url === "/api/actions" ? { status: 422, payload: { detail: { code: "invalid_input", fields: ["body.execution.observed_metric"] } } } : null });
  app.byId("day-1-status").value = "completed";
  app.byId("day-1-metric").value = "虚构：待检查的指标";
  app.byId("day-1-conclusion").value = "虚构：待检查的结论";
  await app.byId("day-1-form").fire("submit");
  assert.equal(app.byId("day-1-metric").value, "虚构：待检查的指标");
  assert.match(app.byId("day-1-feedback").textContent, /日志未通过检查/);
  assert.match(app.byId("tracker-progress").textContent, /0\s*\/\s*7/);
});

test("offline selection keeps the fixed preview labeled with its own scenario", async () => {
  const app = await boot({ live: false });
  app.eval(`selectScenario(${JSON.stringify(scenarios[1].id)})`);
  assert.equal(app.byId("draft-title").value, scenarios[1].title);
  assert.equal(app.byId("result-title").textContent, scenarios[0].title);
  assert.match(app.byId("status-message").textContent, /未针对当前选择重新生成/);
  assert.match(app.byId("mode-badge").textContent, /静态/);
});

test("ending a conflicted request preserves the edited fictional input for correction", async () => {
  const app = await boot({ route: (url) => url === "/api/analyze"
    ? { status: 409, payload: { detail: { code: "request_content_conflict" } } } : null });
  app.eval(`selectScenario(${JSON.stringify(scenarios[1].id)})`);
  app.byId("draft-goal").value = "虚构：保留这个修改后的目标";
  app.byId("fictional-only").checked = true;
  await app.byId("run-button").fire("click");
  await app.byId("new-sample-button").fire("click");
  assert.equal(app.byId("draft-title").value, scenarios[1].title);
  assert.equal(app.byId("draft-goal").value, "虚构：保留这个修改后的目标");
  assert.equal(app.byId("intake-fields").disabled, false);
  assert.equal(app.byId("fictional-only").checked, false);
  assert.equal(app.sessionStorage.getItem(pendingKey), null);
});

test("tracker rate limiting explains the ten-minute wait and preserves unsaved input and credentials", async () => {
  const app = await boot({ local: { [latestKey]: saved }, route: (url) => url === "/api/record"
    ? { status: 200, payload: record() } : url === "/api/actions" ? { status: 429, payload: {} } : null });
  app.byId("day-1-status").value = "completed";
  app.byId("day-1-metric").value = "虚构：未保存的 10 单";
  app.byId("day-1-conclusion").value = "虚构：等待窗口后保存";
  await app.byId("day-1-form").fire("submit");
  assert.match(app.byId("day-1-feedback").textContent, /10\s*分钟/);
  assert.equal(app.byId("day-1-metric").value, "虚构：未保存的 10 单");
  assert.equal(app.byId("day-1-conclusion").value, "虚构：等待窗口后保存");
  assert.equal(JSON.parse(app.localStorage.getItem(latestKey)).accessToken, accessToken);
  assert.equal(app.byId("tracker-refresh").disabled, false);
  assert.equal(app.requests.filter((item) => item.url === "/api/record").length, 1);
});

test("a failed template read does not destroy the private pending snapshot needed for the next refresh", async () => {
  const draft = { ...scenarios[0], fictional_only: true }; delete draft.id;
  const pending = JSON.stringify({ scenarioId: scenarios[0].id, requestId, requestToken, draft });
  const app = await boot({ session: { [pendingKey]: pending }, route: (url) => url === "/api/scenarios"
    ? { status: 503, payload: {} } : null });
  assert.equal(app.sessionStorage.getItem(pendingKey), pending);
  assert.equal(app.requests.some((item) => item.url === "/api/retry"), false);
  assert.equal(app.byId("run-button").disabled, true);
});

test("invalid manually pasted recovery codes never fetch or replace saved credentials", async () => {
  for (const code of ["not JSON", "null", "[]", "{}", JSON.stringify({ request_id: "broken", access_token: accessToken }),
    JSON.stringify({ request_id: requestId, access_token: "broken" }),
    JSON.stringify({ request_id: requestId, access_token: accessToken, extra: "unwanted" }), " ".repeat(513)]) {
    const app = await boot({ local: { [latestKey]: saved } });
    assert.ok(app.byId("recovery-code-input"), "the pasted recovery-code field must exist");
    app.byId("recovery-code-input").value = code;
    const before = app.requests.length;
    const stored = app.localStorage.getItem(latestKey);
    const writes = app.localStorage.writes.length;
    await app.byId("restore-recovery-code").fire("click");
    assert.equal(app.requests.length, before);
    assert.equal(app.localStorage.getItem(latestKey), stored);
    assert.equal(app.localStorage.writes.length, writes);
    assert.equal(app.byId("tracker-title").textContent, scenarios[0].title);
    assert.equal(app.byId("recovery-code-input").value, code);
    assert.equal(app.byId("recovery-code-input").attributes["aria-invalid"], "true");
  }
});

test("manual recovery reads one authoritative record without changing the selected intake", async () => {
  const app = await boot({ route: (url) => url === "/api/record" ? { status: 200, payload: record() } : null });
  app.eval(`selectScenario(${JSON.stringify(scenarios[1].id)})`);
  assert.ok(app.byId("recovery-code-input"), "manual recovery must be available in a fresh browser");
  app.byId("recovery-code-input").value = JSON.stringify({ request_id: requestId.toUpperCase(), access_token: accessToken });
  await app.byId("restore-recovery-code").fire("click");
  assert.deepEqual(app.requests.filter((item) => item.url === "/api/record"), [{ url: "/api/record", body: { request_id: requestId, access_token: accessToken } }]);
  assert.equal(app.requests.some((item) => ["/api/analyze", "/api/retry", "/api/actions"].includes(item.url)), false);
  assert.equal(app.byId("tracker-title").textContent, scenarios[0].title);
  assert.equal(app.byId("tracker-list").childElementCount, 7);
  assert.equal(app.byId("selected-title").textContent, scenarios[1].title);
  assert.equal(app.byId("draft-title").value, scenarios[1].title);
  const credentials = JSON.parse(app.localStorage.getItem(latestKey));
  assert.equal(credentials.requestId, requestId);
  assert.equal(credentials.accessToken, accessToken);
});

test("failed manual recovery keeps the pasted code available and removes stale report labels", async () => {
  for (const status of [403, 429]) {
    let reads = 0;
    const newId = "219dbe92-0fc0-4cc2-9b5e-d7baccc04853";
    const code = JSON.stringify({ request_id: newId, access_token: accessToken });
    const app = await boot({ local: { [latestKey]: saved }, route: (url) => url === "/api/record"
      ? ++reads === 1 ? { status: 200, payload: record() }
        : { status, payload: { detail: { code: "record_access_denied" } } } : null });
    assert.ok(app.byId("recovery-code-input"));
    app.byId("recovery-code-input").value = code;
    await app.byId("restore-recovery-code").fire("click");
    assert.equal(reads, 2, "manual recovery must issue exactly one new record read");
    assert.equal(app.byId("recovery-code-input").value, code);
    assert.equal(app.byId("restore-recovery-code").disabled, false);
    assert.equal(JSON.parse(app.localStorage.getItem(latestKey)).requestId, newId);
    assert.equal(app.byId("tracker-list").childElementCount, 0);
    assert.equal(app.byId("saved-report").hidden, true);
    assert.equal(app.byId("saved-snapshot").hidden, true);
    assert.equal(app.byId("saved-snapshot-content").textContent, "");
    assert.equal(app.byId("tracker-title").textContent.includes(scenarios[0].title), false);
    assert.match(app.byId("tracker-status").textContent, status === 429 ? /10\s*分钟/ : /凭证/);
  }
});

test("copied bearer recovery code contains only the report ID and record token and can be imported", async () => {
  const source = await boot({ local: { [latestKey]: saved }, route: (url) => url === "/api/record" ? { status: 200, payload: record() } : null });
  await source.byId("copy-recovery-code").fire("click");
  assert.equal(source.clipboardCopies.length, 1);
  assert.deepEqual(JSON.parse(source.clipboardCopies[0]), { request_id: requestId, access_token: accessToken });
  const destination = await boot({ route: (url) => url === "/api/record" ? { status: 200, payload: record() } : null });
  assert.ok(destination.byId("recovery-code-input"));
  destination.byId("recovery-code-input").value = source.clipboardCopies[0];
  await destination.byId("restore-recovery-code").fire("click");
  assert.equal(destination.byId("tracker-title").textContent, scenarios[0].title);
  assert.equal(JSON.parse(destination.localStorage.getItem(latestKey)).accessToken, accessToken);
});

test("restored snapshot shows nine known saved fields and excludes private data despite intake changes", async () => {
  const scenario = { ...scenarios[0], current_metrics: "虚构读回：每天 23 单", budget_range: "虚构读回：每周 250 元",
    request_token: requestToken, private_nonce: "private-snapshot-nonce", request_token_digest: "private-snapshot-digest",
    access_token: accessToken, accessToken };
  const app = await boot({ local: { [latestKey]: saved }, route: (url) => url === "/api/record"
    ? { status: 200, payload: record([], scenario) } : null });
  const snapshot = app.byId("saved-snapshot-content");
  assert.ok(snapshot, "the persisted input snapshot must be available beside the saved report");
  assert.equal(snapshot.tagName, "DL");
  assert.deepEqual(snapshot.children.map((pair) => pair.children.map((element) => element.textContent)), [
    ["样例名称", scenarios[0].title], ["所属行业", scenarios[0].industry],
    ["产品或服务", scenarios[0].offering], ["目标客群", scenarios[0].target_customer],
    ["经营问题", scenarios[0].business_problem], ["当前指标", "虚构读回：每天 23 单"],
    ["测试预算", "虚构读回：每周 250 元"], ["验证目标", scenarios[0].goal], ["验证时限", scenarios[0].time_limit],
  ]);
  for (const secret of [requestToken, accessToken, "private-snapshot-nonce", "private-snapshot-digest", scenarios[0].id]) {
    assert.equal(snapshot.textContent.includes(secret), false);
  }
  assert.equal(app.byId("saved-snapshot-source").textContent, "已从演示表读回资料快照");
  const original = snapshot.textContent;
  app.eval(`selectScenario(${JSON.stringify(scenarios[1].id)})`);
  assert.equal(app.byId("draft-current_metrics").value, scenarios[1].current_metrics);
  assert.equal(snapshot.textContent, original);
  assert.equal(app.byId("saved-snapshot").hidden, false);
});

test("new snapshot distinguishes submitted input from API readback without spending an extra read", async () => {
  const savedScenario = { ...scenarios[0], current_metrics: "虚构读回：每天 27 单" };
  const app = await boot({ route: (url) => url === "/api/analyze" ? completed : url === "/api/actions"
    ? { status: 200, payload: record([{ day: 1, status: "pending", observed_metric: "", conclusion: "", updated_at: "2026-10-12T08:00:00Z" }], savedScenario) } : null });
  app.byId("draft-current_metrics").value = "虚构提交：每天 25 单";
  app.byId("fictional-only").checked = true;
  await app.byId("run-button").fire("click");
  assert.ok(app.byId("saved-snapshot-source"));
  assert.equal(app.byId("saved-snapshot-source").textContent, "本次提交资料（已确认保存）");
  assert.ok(app.byId("saved-snapshot-content").textContent.includes("虚构提交：每天 25 单"));
  await app.byId("day-1-form").fire("submit");
  assert.equal(app.byId("saved-snapshot-source").textContent, "已从演示表读回资料快照");
  assert.ok(app.byId("saved-snapshot-content").textContent.includes("虚构读回：每天 27 单"));
  assert.equal(app.requests.some((item) => item.url === "/api/record"), false);
});

test("a legacy null snapshot reports its absence instead of borrowing the selected template", async () => {
  const app = await boot({ local: { [latestKey]: saved }, route: (url) => url === "/api/record"
    ? { status: 200, payload: record([], null) } : null });
  assert.ok(app.byId("saved-snapshot-source"));
  assert.match(app.byId("saved-snapshot-source").textContent, /没有|未保存|不可用|暂无/);
  assert.equal(app.byId("saved-snapshot-content").childElementCount, 0);
  assert.equal(app.byId("saved-snapshot-content").textContent.includes(scenarios[0].current_metrics), false);
  assert.equal(app.byId("saved-snapshot").hidden, false);
});

test("a completed retry cannot save unknown logs after a 429 read and unlocks only after authoritative refresh", async () => {
  const draft = { ...scenarios[0], fictional_only: true }; delete draft.id;
  const original = { day: 3, status: "completed", observed_metric: "虚构：已有 8 单", conclusion: "虚构：保留原结论", updated_at: "2026-10-12T08:00:00Z" };
  let persistedLog = { ...original };
  let reads = 0;
  let writes = 0;
  const app = await boot({ session: { [pendingKey]: JSON.stringify({ scenarioId: scenarios[0].id, requestId, requestToken, draft }) }, route: (url, body) => {
    if (url === "/api/retry") return completed;
    if (url === "/api/record") return ++reads === 1 ? { status: 429, payload: {} } : { status: 200, payload: record([{ ...persistedLog }]) };
    if (url === "/api/actions") {
      writes++;
      persistedLog = { ...body.execution, updated_at: original.updated_at };
      return { status: 200, payload: record([{ ...persistedLog }]) };
    }
    return null;
  } });
  await app.byId("retry-button").fire("click");
  await app.byId("day-3-form").fire("submit");
  assert.equal(writes, 0, "unknown recovery rows must never send a destructive default save");
  assert.deepEqual(persistedLog, original);
  for (let day = 1; day <= 7; day++) {
    const form = app.byId(`day-${day}-form`);
    assert.equal(form.children[0].disabled, true);
    assert.equal(form.children[1].children[0].disabled, true);
    assert.notEqual(app.byId(`day-${day}-status`).value, "pending");
    assert.match(app.byId(`day-${day}-feedback`).textContent, /未知|未读取|尚未读回/);
  }
  assert.match(app.byId("tracker-progress").textContent, /未知|待/);
  assert.equal(app.byId("tracker-refresh").disabled, false);
  assert.equal(JSON.parse(app.localStorage.getItem(latestKey)).accessToken, accessToken);
  await app.byId("tracker-refresh").fire("click");
  assert.equal(reads, 2);
  assert.equal(app.byId("day-3-status").value, "completed");
  assert.equal(app.byId("day-3-metric").value, original.observed_metric);
  assert.equal(app.byId("day-3-conclusion").value, original.conclusion);
  assert.equal(app.byId("day-3-form").children[0].disabled, false);
  assert.equal(app.byId("day-3-form").children[1].children[0].disabled, false);
  assert.match(app.byId("tracker-progress").textContent, /1\s*\/\s*7/);
  assert.equal(writes, 0);
  assert.deepEqual(persistedLog, original);
});

test("an old retry read finishing after a new analysis cannot lock or replace the new report", async () => {
  for (const status of [200, 429]) {
    const draft = { ...scenarios[0], fictional_only: true }; delete draft.id;
    let releaseOldRecord;
    const newId = "319dbe92-0fc0-4cc2-9b5e-d7baccc04853";
    const app = await boot({ session: { [pendingKey]: JSON.stringify({ scenarioId: scenarios[0].id, requestId, requestToken, draft }) }, route: (url) =>
      url === "/api/retry" ? completed : url === "/api/record" ? new Promise((resolve) => { releaseOldRecord = resolve; })
        : url === "/api/analyze" ? { status: 200, payload: { ...completed.payload, request_id: newId } } : null });
    const retry = app.byId("retry-button").fire("click");
    await settle();
    let uuidCount = 0;
    app.context.crypto.randomUUID = () => ++uuidCount === 1 ? newId : requestToken;
    app.eval(`selectScenario(${JSON.stringify(scenarios[1].id)})`);
    app.byId("fictional-only").checked = true;
    await app.byId("run-button").fire("click");
    releaseOldRecord({ status, payload: status === 200 ? record([{ day: 3, status: "completed", observed_metric: "旧记录", conclusion: "旧结论", updated_at: "2026-10-12T08:00:00Z" }]) : {} });
    await retry;
    assert.equal(app.byId("tracker-title").textContent, scenarios[1].title);
    assert.equal(JSON.parse(app.localStorage.getItem(latestKey)).requestId, newId);
    assert.equal(app.byId("day-3-status").value, "pending");
    assert.equal(app.byId("day-3-metric").value, "");
    assert.equal(app.byId("day-3-form").children[0].disabled, false);
    assert.equal(app.byId("day-3-form").children[1].children[0].disabled, false);
    assert.match(app.byId("tracker-progress").textContent, /0\s*\/\s*7/);
    assert.equal(app.requests.filter((item) => item.url === "/api/record").length, 1);
  }
});
