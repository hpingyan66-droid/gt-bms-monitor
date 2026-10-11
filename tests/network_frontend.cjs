/* Offline browser-state checks for the localhost connection manager.
 * No Windows/Bluetooth/network configuration is touched. */
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const test = require("node:test");

const root = path.join(__dirname, "..", "src", "gtbms", "static");
const source = fs.readFileSync(path.join(root, "network.js"), "utf8");
const html = fs.readFileSync(path.join(root, "network.html"), "utf8");
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
const TOKEN = "public-test-csrf-token";
const fixture = {
  profiles: { mobile_router: { wlan_profile: "Travel", interface_id: "adapter-a" }, home_router: { wlan_profile: "Home", interface_id: "adapter-a" }, local_area: {} },
  inventory: {
    wifi_profiles: [{ name: "Travel", interface_id: "adapter-a", interface_name: "Wi-Fi A" },
      { name: "Home", interface_id: "adapter-a", interface_name: "Wi-Fi A" },
      { name: "Shared", interface_id: "adapter-a", interface_name: "Wi-Fi A" },
      { name: "Shared", interface_id: "adapter-b", interface_name: "Wi-Fi B" }],
    addresses: [{ ip_address: "192.0.2.20", prefix_length: 24, interface_index: 12, interface_alias: "Wi-Fi A", network_category: "Private", adapter_status: "Up", candidate_kind: "router" }],
    hotspot: { state: "off", ssid: "Surface-local" }, errors: []
  },
  inventory_checked_at: "2026-10-11T01:02:03+00:00",
  active_mode: "mobile_router", viewer: { state: "running", url: "http://192.0.2.20:8766/#key=DO_NOT_USE" },
  operation: { state: "idle", phase: "", message: "待機中", mode: null }, last_error: null
};
const flush = () => new Promise(resolve => setImmediate(resolve));
const clone = value => JSON.parse(JSON.stringify(value));

function browser(hostname = "127.0.0.1") {
  class Element {
    constructor() { this.textContent = ""; this.value = ""; this.disabled = false; this.hidden = false; this.dataset = {}; this.children = []; this.attributes = new Map(); this.events = {}; }
    addEventListener(name, fn) { this.events[name] = fn; }
    replaceChildren(...children) { this.children = children; }
    setAttribute(name, value) { this.attributes.set(name, value); }
    removeAttribute(name) { this.attributes.delete(name); }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
  }
  const elements = new Map(ids.map(id => [id, new Element()]));
  const timers = new Map(), requests = [];
  let clock = 0, timerId = 0, active = 0, maxActive = 0;
  const context = {
    window: { location: { hostname }, setTimeout(fn, ms) { timers.set(++timerId, { fn, ms }); return timerId; }, clearTimeout(id) { timers.delete(id); } },
    document: { hidden: false, getElementById(id) { return elements.get(id); }, createElement() { return new Element(); } },
    performance: { now: () => clock }, Date, Intl, AbortController,
    fetch(url, options) {
      assert.ok(["/api/session", "/api/state", "/api/profile", "/api/switch", "/api/refresh", "/api/hotspot-settings"].includes(url));
      assert.equal(options.cache, "no-store"); assert.equal(options.credentials, "omit");
      assert.equal(options.redirect, "error"); assert.equal(options.referrerPolicy, "no-referrer");
      assert.ok(["GET", "POST"].includes(options.method));
      if (options.method === "POST") {
        assert.equal(options.headers["X-GTBMS-Token"], TOKEN);
        assert.equal(options.headers["Content-Type"], "application/json");
      }
      active++; maxActive = Math.max(active, maxActive);
      let done = false;
      const finish = () => { if (!done) { done = true; active--; } };
      return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject, finish }));
    }
  };
  vm.runInNewContext(source, context, { filename: "network.js" });
  return {
    element: id => elements.get(id), context, requests, maxActive: () => maxActive,
    click(id) { elements.get(id).events.click({ preventDefault() {} }); },
    advance(ms) { clock += ms; },
    text() { return [...elements.values()].map(el => el.textContent + el.children.map(c => c.textContent).join(" ")).join(" "); },
    timerDelays() { return [...timers.values()].map(t => t.ms); },
    fire(ms) { const item = [...timers.entries()].find(([, t]) => t.ms === ms); assert.ok(item, `timer ${ms}`); timers.delete(item[0]); item[1].fn(); },
    resolve(index, data, status = 200) {
      const request = requests[index];
      if (status >= 400 || status === 204) request.finish();
      request.resolve({ status, ok: status >= 200 && status < 300, json: async () => { request.finish(); return clone(data); } });
    },
    fail(index) { requests[index].finish(); requests[index].reject(new Error("private raw error must not display")); }
  };
}

async function ready(b = browser(), state = fixture, session = { token: TOKEN }) {
  await flush();
  assert.equal(b.requests[0].url, "/api/session");
  b.resolve(0, session); await flush();
  assert.equal(b.requests[1].url, "/api/state");
  b.resolve(1, state); await flush();
  return b;
}

test("localhost startup only reads session/state and exposes connection guide without viewer keys", async () => {
  const b = await ready();
  assert.equal(b.requests.length, 2);
  assert.ok(b.requests.every(request => request.options.method === "GET"));
  assert.equal(b.element("active-mode").textContent, "モバイルルーターモード");
  assert.match(b.element("viewer-state").textContent, /スマホ到達は未確認/);
  assert.equal(b.element("connection-link").getAttribute("href"), "/connection");
  assert.equal(b.element("connection-link").getAttribute("rel"), "noopener noreferrer");
  assert.equal(b.element("hotspot-ssid").textContent, "Surface-local");
  for (const secret of [TOKEN, "DO_NOT_USE"]) assert.equal(b.text().includes(secret), false);
  assert.equal(b.element("switch-home_router").disabled, false);
});

test("non-localhost hosts cannot bootstrap requests or perform management actions", async () => {
  const b = browser("192.0.2.20"); await flush();
  b.click("switch-local_area"); b.click("refresh"); b.click("hotspot-settings"); await flush();
  assert.equal(b.requests.length, 0);
  assert.match(b.element("error").textContent, /localhost/);
  assert.equal(b.element("connection-link").getAttribute("href"), null);
});

test("Wi-Fi profile selection preserves its interface and profile save never switches automatically", async () => {
  const b = await ready();
  const select = b.element("profile-mobile_router");
  select.value = JSON.stringify(["Shared", "adapter-b"]); select.events.change();
  assert.equal(b.element("save-mobile_router").disabled, false);
  assert.equal(b.element("switch-mobile_router").disabled, true);
  b.click("save-mobile_router"); b.click("save-mobile_router"); await flush();
  assert.equal(b.requests.length, 3);
  assert.equal(b.requests[2].url, "/api/profile");
  assert.deepEqual(JSON.parse(b.requests[2].options.body), { mode: "mobile_router", wlan_profile: "Shared", interface_id: "adapter-b" });
  b.resolve(2, {}); await flush();
  const updated = clone(fixture); updated.profiles.mobile_router = { wlan_profile: "Shared", interface_id: "adapter-b" };
  b.resolve(3, updated); await flush();
  assert.equal(b.element("save-mobile_router").disabled, true);
  assert.equal(b.element("switch-mobile_router").disabled, false);
  assert.equal(b.requests.filter(request => request.url === "/api/switch").length, 0);
});

test("switch is single submission and never optimistically adopts the requested mode", async () => {
  const b = await ready();
  b.click("switch-home_router"); b.click("switch-home_router"); b.click("refresh"); await flush();
  assert.equal(b.requests.length, 3);
  assert.deepEqual(JSON.parse(b.requests[2].options.body), { mode: "home_router" });
  assert.match(b.element("notice").textContent, /スマホのWi-Fi再接続/);
  assert.equal(b.element("active-mode").textContent, "モバイルルーターモード");
  assert.equal(b.element("connection-link").getAttribute("href"), null);
  b.resolve(2, { operation: { state: "running" } }, 202); await flush();
  const running = clone(fixture); running.operation = { state: "running", phase: "connect", message: "接続中", mode: "home_router" };
  b.resolve(3, running); await flush();
  assert.equal(b.element("active-mode").textContent, "モバイルルーターモード");
  assert.equal(b.element("switch-local_area").disabled, true);
  assert.equal(b.element("profile-home_router").disabled, true);
  b.fire(1000); await flush();
  const done = clone(fixture); done.active_mode = "home_router"; done.operation.state = "succeeded";
  b.resolve(4, done); await flush();
  assert.equal(b.element("active-mode").textContent, "ホームルーターモード");
  assert.equal(b.element("status-heading").textContent, "Surface側の切替を確認しました");
  assert.equal(b.element("connection-link").getAttribute("href"), "/connection");
});

test("failed switch keeps server-reported previous mode and recovery-required is clearly shown", async () => {
  const b = await ready(); b.click("switch-local_area"); await flush();
  b.resolve(2, {}, 202); await flush();
  const failed = clone(fixture); failed.operation = { state: "recovery_required", phase: "rollback", message: "設定を確認してください", mode: "local_area" }; failed.last_error = "復旧が必要です"; failed.viewer.state = "stopped";
  b.resolve(3, failed); await flush();
  assert.equal(b.element("active-mode").textContent, "モバイルルーターモード");
  assert.equal(b.element("status-heading").textContent, "接続の復旧が必要です");
  assert.equal(b.element("error").textContent, "復旧が必要です");
  assert.equal(b.element("connection-link").getAttribute("href"), null);
});

test("polls and user actions use one serialized request channel with no catch-up burst", async () => {
  const b = await ready();
  b.fire(1000); await flush(); assert.equal(b.requests[2].url, "/api/state");
  b.click("switch-home_router"); await flush();
  assert.equal(b.requests.length, 3, "write waits for outstanding GET");
  b.advance(1400); b.resolve(2, fixture); await flush();
  assert.equal(b.requests[3].url, "/api/switch");
  b.resolve(3, {}, 202); await flush();
  b.resolve(4, fixture); await flush();
  assert.equal(b.maxActive(), 1);
  assert.equal(b.timerDelays().filter(ms => ms <= 1000).length, 1);
  assert.ok(b.timerDelays().includes(1000));
});

test("slow polling is start-based and schedules one minimum pause", async () => {
  const b = await ready(); b.fire(1000); await flush();
  b.advance(1200); b.resolve(2, fixture); await flush();
  assert.deepEqual(b.timerDelays(), [100]);
  assert.equal(b.maxActive(), 1);
});

test("lost state request marks last confirmed mode and disables writes until state recovers", async () => {
  const b = await ready(); b.fire(1000); await flush(); b.fail(2); await flush();
  assert.match(b.element("active-mode").textContent, /最終確認/);
  assert.equal(b.element("switch-home_router").disabled, true);
  assert.equal(b.element("badge-mobile_router").hidden, true);
  assert.match(b.element("viewer-state").textContent, /現在は未確認/);
  assert.equal(b.text().includes("private raw error"), false);
  b.fire(1000); await flush(); b.resolve(3, fixture); await flush();
  assert.equal(b.element("active-mode").textContent, "モバイルルーターモード");
  assert.equal(b.element("switch-home_router").disabled, false);
  assert.equal(b.element("badge-mobile_router").hidden, false);
});

test("uncertain POST is never retried and cannot set active mode", async () => {
  const b = await ready(); b.click("switch-home_router"); await flush(); b.fail(2); await flush();
  assert.match(b.element("active-mode").textContent, /モバイルルーターモード.*最終確認/);
  assert.match(b.element("error").textContent, /再送せず/);
  b.fire(1000); await flush(); b.resolve(3, fixture); await flush();
  assert.equal(b.requests.filter(request => request.options.method === "POST").length, 1);
});

test("auth failure disables writes even after later state reads succeed", async () => {
  const b = await ready(); b.click("refresh"); await flush(); b.resolve(2, {}, 403); await flush();
  assert.equal(b.element("refresh").disabled, true);
  b.fire(1000); await flush(); b.resolve(3, fixture); await flush();
  assert.match(b.element("status-heading").textContent, /管理セッション/);
  assert.equal(b.element("switch-home_router").disabled, true);
  assert.equal(b.element("connection-link").getAttribute("href"), null);
});

test("hotspot settings and inventory refresh happen only after their explicit button action", async () => {
  const b = await ready(); b.click("hotspot-settings"); await flush();
  assert.equal(b.requests[2].url, "/api/hotspot-settings");
  assert.deepEqual(JSON.parse(b.requests[2].options.body), {});
  b.resolve(2, {}, 204); await flush(); b.resolve(3, fixture); await flush();
  b.click("refresh"); await flush(); assert.equal(b.requests[4].url, "/api/refresh");
  assert.deepEqual(JSON.parse(b.requests[4].options.body), {});
});

test("null router profiles, unknown mode and absent inventory remain safe", async () => {
  const initial = clone(fixture); initial.active_mode = "unknown";
  initial.profiles.mobile_router = null; initial.profiles.home_router = null;
  initial.inventory.wifi_profiles = []; initial.inventory.addresses = []; initial.inventory.hotspot = { state: "unknown", ssid: null };
  initial.viewer.state = "stopped";
  const b = await ready(browser(), initial);
  assert.equal(b.element("active-mode").textContent, "未確認");
  assert.equal(b.element("switch-mobile_router").disabled, true);
  assert.equal(b.element("save-home_router").disabled, true);
  assert.equal(b.element("switch-local_area").disabled, false);
  assert.equal(b.element("hotspot-ssid").textContent, "未確認");
  assert.equal(b.element("connection-link").getAttribute("href"), null);
});

test("draft selections survive unchanged inventory polls and unlisted profiles cannot be submitted", async () => {
  const b = await ready(); const select = b.element("profile-home_router");
  select.value = JSON.stringify(["Shared", "adapter-b"]); select.events.change();
  const firstOptions = select.children;
  b.fire(1000); await flush(); b.resolve(2, fixture); await flush();
  assert.equal(select.value, JSON.stringify(["Shared", "adapter-b"]));
  assert.equal(select.children, firstOptions);
  select.value = JSON.stringify(["unknown", "adapter-x"]); select.events.change();
  b.click("save-home_router"); await flush();
  assert.equal(b.requests.length, 3);
});

test("server-supplied display strings are text and malformed state cannot enable switching", async () => {
  const state = clone(fixture); state.inventory.hotspot.ssid = "<img src=x onerror=alert(1)>";
  state.inventory.wifi_profiles[0].name = "<script>unsafe</script>";
  const b = await ready(browser(), state);
  assert.equal(b.element("hotspot-ssid").textContent, "<img src=x onerror=alert(1)>");
  assert.equal(b.element("hotspot-ssid").children.length, 0);
  b.fire(1000); await flush(); b.resolve(2, { operation: { state: "idle" } }); await flush();
  assert.equal(b.element("switch-local_area").disabled, true);
  assert.match(b.element("active-mode").textContent, /最終確認/);
});

test("the request queue also waits for JSON parsing before sending an action", async () => {
  const b = await ready(); b.fire(1000); await flush();
  let finishJson;
  b.requests[2].resolve({ status: 200, ok: true,
    json: () => new Promise(resolve => { finishJson = () => { b.requests[2].finish(); resolve(clone(fixture)); }; }) });
  await flush(); b.click("switch-home_router"); await flush();
  assert.equal(b.requests.length, 3);
  finishJson(); await flush();
  assert.equal(b.requests.length, 4);
  assert.equal(b.requests[3].url, "/api/switch");
  assert.equal(b.maxActive(), 1);
});

test("a late result after timeout cannot promote the reported active mode", async () => {
  const b = await ready(); b.fire(1000); await flush();
  b.advance(15000); b.fire(15000);
  assert.equal(b.requests[2].options.signal.aborted, true);
  const late = clone(fixture); late.active_mode = "home_router";
  b.resolve(2, late); await flush();
  assert.match(b.element("active-mode").textContent, /モバイルルーターモード.*最終確認/);
  assert.equal(b.element("switch-local_area").disabled, true);
  assert.equal(b.requests.filter(request => request.options.method === "POST").length, 0);
});

test("an external viewer is not advertised as a managed connection", async () => {
  const external = clone(fixture); external.viewer.state = "external";
  const b = await ready(browser(), external);
  assert.equal(b.element("viewer-state").textContent, "管理外のプロセス");
  assert.equal(b.element("connection-link").getAttribute("href"), null);
});

test("Windows inventory time comes from its own timestamp and does not advance with API polling", async () => {
  const b = await ready();
  assert.match(b.element("inventory-checked-at").textContent, /2026\/10\/11.*10:02:03/);
  const oldInventoryTime = b.element("inventory-checked-at").textContent;
  b.advance(30000); b.fire(1000); await flush(); b.resolve(2, fixture); await flush();
  assert.equal(b.element("inventory-checked-at").textContent, oldInventoryTime);
  assert.match(html, /管理API応答確認/);
  assert.match(html, /Windows接続情報の調査/);
  const refreshed = clone(fixture); refreshed.inventory_checked_at = "2026-10-11T01:03:04Z";
  b.fire(1000); await flush(); b.resolve(3, refreshed); await flush();
  assert.match(b.element("inventory-checked-at").textContent, /10:03:04/);
});

test("missing or invalid inventory timestamps never fall back to the current browser time", async () => {
  const state = clone(fixture); state.inventory_checked_at = null;
  const b = await ready(browser(), state);
  assert.equal(b.element("inventory-checked-at").textContent, "未確認");
  for (const value of ["not-a-date", "2026-10-11T01:02:03", undefined]) {
    state.inventory_checked_at = value;
    b.fire(1000); await flush(); b.resolve(b.requests.length - 1, state); await flush();
    assert.equal(b.element("inventory-checked-at").textContent, "未確認");
    assert.equal(b.element("switch-home_router").disabled, false);
  }
});

test("unknown hotspot is explicit while optional hotspot errors do not block router actions", async () => {
  const state = clone(fixture);
  state.inventory.hotspot = { state: "unknown", ssid: null };
  state.inventory.errors = ["hotspot_no_upstream_profile"];
  const b = await ready(browser(), state);
  assert.equal(b.element("hotspot-state").textContent, "未確認");
  assert.equal(b.element("operation-warnings").hidden, false);
  assert.match(b.element("operation-warnings").textContent, /状態は未確認/);
  assert.match(b.element("operation-warnings").textContent, /ルーターの情報が正常なら/);
  assert.equal(b.element("switch-home_router").disabled, false);
  b.click("switch-home_router"); await flush();
  assert.equal(b.requests[2].url, "/api/switch");
});

test("hotspot stop warning remains visible for a successful router switch and clears with a confirmed response", async () => {
  const state = clone(fixture); state.operation.state = "succeeded";
  state.operation.warnings = ["hotspot_state_unconfirmed"];
  const b = await ready(browser(), state);
  assert.equal(b.element("status-heading").textContent, "Surface側の切替を確認しました");
  assert.match(b.element("operation-warnings").textContent, /停止状態は未確認/);
  assert.equal(b.element("operation-warnings").hidden, false);
  b.fire(1000); await flush(); b.resolve(2, fixture); await flush();
  assert.equal(b.element("operation-warnings").hidden, true);
  assert.equal(b.element("operation-warnings").textContent, "");
});

const edition = id => ({ id, label: id, can_operate: true, can_export: true, can_view_gps: true,
  can_manage_network: id === "full_access", can_maintain: id === "full_access" });

test("corporate and individual editions explain unavailable network operations and never send actions", async () => {
  for (const id of ["corporate", "individual"]) {
    const b = await ready(browser(), { ...fixture, edition: edition(id) }, { token: TOKEN, edition: edition(id) });
    assert.match(b.element("edition-label").textContent, /法人利用版|個人利用版/);
    assert.match(b.element("edition-note").textContent, /接続管理とQR・接続案内を利用できません/);
    for (const control of ["refresh", "hotspot-settings", "switch-local_area", "switch-mobile_router", "switch-home_router", "save-mobile_router", "save-home_router"]) {
      assert.equal(b.element(control).disabled, true, control); b.click(control);
    }
    assert.equal(b.element("connection-link").hidden, true);
    assert.equal(b.element("connection-link").getAttribute("href"), null);
    await flush(); assert.equal(b.requests.length, 2, "only read-only session and state requests");
  }
});

test("explicit restricted edition cannot be elevated by a missing edition in later legacy-shaped state", async () => {
  const b = await ready(browser(), fixture, { token: TOKEN, edition: edition("individual") });
  assert.equal(b.element("switch-local_area").disabled, true);
  b.fire(1000); await flush(); b.resolve(2, fixture); await flush();
  assert.equal(b.element("switch-local_area").disabled, true);
  assert.equal(b.element("edition-label").textContent, "個人利用版");
});

test("full access edition retains management and legacy responses preserve current operational UI", async () => {
  const full = await ready(browser(), { ...fixture, edition: edition("full_access") }, { token: TOKEN, edition: edition("full_access") });
  assert.equal(full.element("edition-label").textContent, "フルアクセス版");
  assert.equal(full.element("switch-local_area").disabled, false);
  assert.equal(full.element("connection-link").getAttribute("href"), "/connection");
  const legacy = await ready();
  assert.match(legacy.element("edition-label").textContent, /旧|既存版/);
  assert.equal(legacy.element("switch-local_area").disabled, false);
});

test("unknown, malformed, or contradictory edition metadata cannot enable network controls", async () => {
  for (const value of [null, { ...edition("full_access"), id: "owner" },
    { ...edition("full_access"), can_manage_network: "true" }, { ...edition("corporate"), can_manage_network: true }]) {
    const b = await ready(browser(), { ...fixture, edition: value }, { token: TOKEN, edition: value });
    assert.equal(b.element("switch-local_area").disabled, true);
    assert.equal(b.element("connection-link").getAttribute("href"), null);
    b.click("hotspot-settings"); await flush(); assert.equal(b.requests.length, 2);
  }
});
