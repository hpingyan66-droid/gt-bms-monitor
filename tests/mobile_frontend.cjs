/* Browser-state regression tests without a browser package or network access.
 * Run: node --test tests/mobile_frontend.cjs
 * Keys and samples below are public synthetic fixtures, never local settings.
 */
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const test = require("node:test");

const staticRoot = path.join(__dirname, "..", "src", "gtbms", "static");
const source = fs.readFileSync(path.join(staticRoot, "mobile.js"), "utf8");
const html = fs.readFileSync(path.join(staticRoot, "mobile.html"), "utf8");
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
const TEST_KEY = "A".repeat(43);
const STORAGE_KEY = "gtbms.viewer.key.v1";
const fixture = {
  schema: 1, mode: "ble", state: "connected", fresh: true,
  received_at: "2026-01-01T03:00:00Z", age_seconds: 0,
  stale_after_seconds: 6, poll_seconds: 2, alerts: [],
  measurement: {
    voltage_v: 64, soc_pct: 70, remaining_ah: 35, raw_current_a: 2,
    current_a: null, current_direction: "unconfirmed", power_w: null,
    temperatures_c: [24, 23, 22], cell_delta_v: 0.010,
    cell_voltages_v: Array(16).fill(4), quality_flags: [], alarms: [],
    charge_mos: true, discharge_mos: true
  },
  prediction: null
};

function browser({ fragment = `#key=${TEST_KEY}`, storedKey = null, storageDisabled = false } = {}) {
  const downloads = [], blobs = new Map();
  class Element {
    constructor() {
      this.textContent = ""; this.dataset = {}; this.style = {}; this.hidden = false;
      this.value = ""; this.events = {}; this.children = []; this.classes = new Set();
      this.attributes = new Map();
      this.classList = {
        remove: name => this.classes.delete(name),
        toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name)
      };
    }
    addEventListener(name, fn) { this.events[name] = fn; }
    replaceChildren(...children) { this.children = children; }
    append(...children) { this.children.push(...children); }
    closest() { return this; }
    setAttribute(name, value) { this.attributes.set(name, value); }
    removeAttribute(name) { this.attributes.delete(name); }
    click() { downloads.push({ name: this.download, blob: blobs.get(this.href) }); }
    remove() {}
  }
  const elements = new Map(ids.map(id => [id, new Element()]));
  const events = {}, documentEvents = {}, timers = new Map(), intervals = [], requests = [];
  const stored = new Map(storedKey ? [[STORAGE_KEY, storedKey]] : []);
  let timerId = 0, performanceTime = 0, wallTime = 100000;
  const window = {
    location: { hash: fragment, pathname: "/", search: "", replace() { throw new Error("Unexpected navigation"); } },
    addEventListener: (name, fn) => { events[name] = fn; },
    setTimeout: (fn, milliseconds) => { timers.set(++timerId, { fn, milliseconds }); return timerId; },
    clearTimeout: id => timers.delete(id),
    setInterval: fn => { intervals.push(fn); }
  };
  const document = {
    hidden: false,
    body: new Element(),
    getElementById: id => elements.get(id) || null,
    createElement: () => new Element(),
    addEventListener: (name, fn) => { documentEvents[name] = fn; }
  };
  class Clock extends Date { static now() { return wallTime; } }
  const context = {
    window, document,
    history: { replaceState(_state, _title, url) { assert.equal(url, "/"); window.location.hash = ""; } },
    sessionStorage: {
      getItem(name) { if (storageDisabled) throw new Error("disabled"); return stored.get(name) || null; },
      setItem(name, value) { if (storageDisabled) throw new Error("disabled"); stored.set(name, value); },
      removeItem(name) { if (storageDisabled) throw new Error("disabled"); stored.delete(name); }
    },
    performance: { now: () => performanceTime }, Date: Clock,
    Intl, URLSearchParams, AbortController, Blob,
    URL: { createObjectURL(blob) { const url = `blob:synthetic-${blobs.size}`; blobs.set(url, blob); return url; }, revokeObjectURL(url) { blobs.delete(url); } },
    fetch(url, options) {
      assert.equal(window.location.hash, "", "erase fragment before API access");
      assert.equal(url, "/api/view", "no credential-bearing URLs or other endpoints");
      assert.equal(options.method, "GET", "viewer never mutates");
      assert.equal(options.headers.Authorization, `Bearer ${TEST_KEY}`);
      assert.equal(options.cache, "no-store");
      assert.equal(options.credentials, "omit");
      assert.equal(options.redirect, "error");
      assert.equal(options.referrerPolicy, "no-referrer");
      return new Promise((resolve, reject) => requests.push({ resolve, reject, options }));
    }
  };
  vm.runInNewContext(source, context, { filename: "mobile.js" });
  return {
    element: id => elements.get(id), events, documentEvents, document, requests, stored, downloads,
    timerDelays() { return [...timers.values()].map(timer => timer.milliseconds); },
    timerCallback(milliseconds) { return [...timers.values()].find(timer => timer.milliseconds === milliseconds)?.fn; },
    advance(performanceMs, wallMs = performanceMs) { performanceTime += performanceMs; wallTime += wallMs; },
    watchdog() { intervals.forEach(fn => fn()); },
    resolve(index, data = fixture, status = 200) {
      requests[index].resolve({ status, ok: status === 200, json: async () => data });
    },
    fireTimer(milliseconds) {
      const entry = [...timers.entries()].find(([, timer]) => timer.milliseconds === milliseconds);
      assert.ok(entry, `scheduled ${milliseconds} ms timer`);
      timers.delete(entry[0]); entry[1].fn();
    }
  };
}
const flush = () => new Promise(resolve => setImmediate(resolve));
const value = (b, id) => b.element(id).textContent;

test("fragment bootstrap uses session storage and authenticated GET; unconfirmed power stays hidden", async () => {
  const b = browser();
  assert.equal(b.requests.length, 1);
  assert.equal(b.stored.get(STORAGE_KEY), TEST_KEY);
  b.resolve(0, { ...fixture, measurement: { ...fixture.measurement, power_w: 128 } });
  await flush();
  assert.equal(value(b, "soc"), "70");
  assert.equal(value(b, "current"), "2.00");
  assert.equal(value(b, "power"), "—");
  assert.match(value(b, "last-received"), /12:00:00/);
  assert.equal(b.element("cells").children.length, 16);
  assert.equal(b.element("connection").dataset.state, "connected");
});

test("independent freshness watchdog removes all current values and predictions", async () => {
  const b = browser();
  b.resolve(0, { ...fixture, prediction: { safe_remaining_range_km: 20, remaining_range_km: 25, full_range_km: 40, reliability: "低い" } });
  await flush();
  assert.equal(value(b, "safe-range"), "20.0");
  b.advance(6100); b.watchdog();
  for (const id of ["soc", "current", "voltage", "power", "temperature", "cell-delta", "safe-range", "normal-range", "full-range", "remaining-ah", "raw-current", "mos"]) {
    assert.equal(value(b, id), "—", `${id} must not remain stale`);
  }
  assert.equal(b.element("connection").dataset.state, "stale");
  assert.equal(b.element("cells").children.length, 1);
});

test("network transit time consumes freshness budget", async () => {
  const b = browser(); b.advance(3500); b.resolve(0, { ...fixture, age_seconds: 3 });
  await flush(); assert.equal(value(b, "soc"), "—");
});

test("wall clock expiry clears values when mobile performance clock pauses", async () => {
  const b = browser(); b.resolve(0); await flush();
  b.advance(0, 6100); b.watchdog(); assert.equal(value(b, "soc"), "—");
});

test("clock rollback invalidates the sample", async () => {
  const b = browser(); b.resolve(0); await flush();
  b.advance(0, -1); b.watchdog(); assert.equal(value(b, "soc"), "—");
});

test("failed fetch immediately clears the previous measurements", async () => {
  const b = browser(); b.resolve(0); await flush();
  b.fireTimer(500); b.requests[1].reject(new Error("offline")); await flush();
  assert.equal(value(b, "soc"), "—");
  assert.equal(b.element("connection").dataset.state, "surface_offline");
});

test("four-second request timer aborts; offline event immediately hides data", async () => {
  const b = browser(); b.fireTimer(4000);
  assert.equal(b.requests[0].options.signal.aborted, true);
  b.requests[0].reject(new Error("aborted")); await flush();
  assert.equal(b.element("connection").dataset.state, "surface_offline");
  b.fireTimer(500); b.resolve(1); await flush();
  b.events.offline(); assert.equal(value(b, "soc"), "—");
});

test("hide and restore clear values until a new response arrives", async () => {
  const b = browser(); b.resolve(0); await flush();
  b.document.hidden = true; b.documentEvents.visibilitychange();
  assert.equal(value(b, "soc"), "—");
  assert.equal(b.requests.length, 1);
  b.document.hidden = false; b.documentEvents.visibilitychange();
  assert.equal(value(b, "soc"), "—");
  b.resolve(1); await flush(); assert.equal(value(b, "soc"), "70");
});

test("a response from the old page generation cannot restore values", async () => {
  const b = browser(); b.events.pageshow();
  assert.equal(b.requests[0].options.signal.aborted, true);
  b.resolve(0); await flush(); assert.equal(value(b, "soc"), "—");
  b.fireTimer(50);
  b.resolve(1); await flush(); assert.equal(value(b, "soc"), "70");
  b.events.pagehide(); assert.equal(value(b, "soc"), "—");
});

test("401 clears credentials and requires a new key", async () => {
  const b = browser(); b.resolve(0, null, 401); await flush();
  assert.equal(b.stored.size, 0);
  assert.equal(b.element("access-panel").hidden, false);
  assert.equal(value(b, "soc"), "—");
  assert.match(value(b, "access-error"), /新しいキー/);
});

test("invalid fragment does not fall back to an older session key", () => {
  const b = browser({ fragment: "#key=invalid", storedKey: TEST_KEY });
  assert.equal(b.requests.length, 0); assert.equal(b.stored.size, 0);
  assert.equal(b.element("access-panel").hidden, false);
});

test("manual key entry and disabled browser storage both work", () => {
  const b = browser({ fragment: "", storageDisabled: true });
  assert.equal(b.requests.length, 0);
  b.element("access-key").value = TEST_KEY;
  let prevented = false;
  b.element("access-form").events.submit({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true); assert.equal(b.requests.length, 1);
  assert.equal(b.element("access-key").value, "");
});

test("mock samples are visibly marked and unknown mode preserves offline diagnosis", async () => {
  let b = browser(); b.resolve(0, { ...fixture, mode: "mock" }); await flush();
  assert.equal(value(b, "mode"), "模擬データ"); assert.match(value(b, "status-detail"), /模擬BMS/);
  b = browser(); b.resolve(0, { ...fixture, mode: "unknown", state: "surface_offline", fresh: false, measurement: null }); await flush();
  assert.equal(value(b, "mode"), "接続待ち");
  assert.equal(b.element("connection").dataset.state, "surface_offline");
});

test("quality failures hide numbers while preserving explicitly labelled alerts", async () => {
  const b = browser(); b.resolve(0, { ...fixture, state: "invalid", fresh: false, measurement: null, alerts: ["測定品質要確認", "セル低電圧"] }); await flush();
  assert.equal(value(b, "soc"), "—");
  assert.equal(b.element("source-alerts").hidden, false);
  assert.equal(value(b, "source-alerts"), "最終受信の通知：測定品質要確認 / セル低電圧");
});

test("missing freshness budget, measurement, or schema cannot be displayed", async () => {
  for (const override of [{ age_seconds: null }, { stale_after_seconds: 0 }, { measurement: null }, { schema: 2 }]) {
    const b = browser(); b.resolve(0, { ...fixture, ...override }); await flush();
    assert.equal(value(b, "soc"), "—");
  }
});

test("malformed JSON is diagnosed as invalid data", async () => {
  const b = browser();
  b.requests[0].resolve({ status: 200, ok: true, json: async () => { throw new SyntaxError("bad JSON"); } });
  await flush(); assert.equal(b.element("connection").dataset.state, "invalid");
  assert.equal(value(b, "soc"), "—");
});

async function nextResponse(b, { requestMs = 0, data = fixture, error = null } = {}) {
  const delay = b.timerDelays().find(ms => ms >= 50 && ms <= 500);
  assert.notEqual(delay, undefined, "one scheduled viewer refresh");
  b.advance(delay); b.fireTimer(delay);
  const index = b.requests.length - 1;
  b.advance(requestMs);
  if (error) b.requests[index].reject(error);
  else b.resolve(index, data);
  await flush();
}

async function download(b) {
  b.element("compare-stop").events.click();
  b.element("compare-save").events.click();
  assert.equal(b.downloads.length, 1);
  assert.match(b.downloads[0].name, /^gtbms-display-\d{8}T\d{6}Z-(unknown|android|iphone|surface)-(unknown|mobile_router|home_router|local_area|router|hotspot|bluetooth)\.json$/);
  return JSON.parse(await b.downloads[0].blob.text());
}

test("half-second polling uses start time and leaves a fifty-ms wait after slow responses", async () => {
  const b = browser();
  assert.deepEqual(b.timerDelays(), [4000], "no second request timer while fetch is pending");
  b.advance(120); b.resolve(0); await flush();
  assert.deepEqual(b.timerDelays(), [380]);
  b.advance(380); b.fireTimer(380);
  assert.equal(b.requests.length, 2);
  b.advance(900); b.resolve(1); await flush();
  assert.deepEqual(b.timerDelays(), [50]);
});

test("repeated reconnects wait for the retired request and schedule only one successor", async () => {
  const b = browser();
  b.element("compare-start").events.click();
  b.events.online(); b.events.pageshow(); b.events.online();
  assert.equal(b.requests.length, 1);
  assert.equal(b.requests[0].options.signal.aborted, true);
  b.resolve(0); await flush();
  assert.equal(value(b, "soc"), "—");
  assert.equal(value(b, "diag-rtt"), "—");
  assert.deepEqual(b.timerDelays(), [120000, 50]);
  await nextResponse(b, { requestMs: 20 });
  const output = await download(b);
  assert.equal(output.counts.observations, 1, "retired response must not enter recording");
  assert.equal(output.metrics.http_rtt_ms.max, 20);
});

test("offline during an outstanding request cannot lose the reconnect reservation", async () => {
  const b = browser(); b.events.offline();
  assert.equal(b.requests.length, 1);
  b.requests[0].reject(new Error("aborted")); await flush();
  assert.deepEqual(b.timerDelays(), [50]);
  await nextResponse(b);
  assert.equal(value(b, "soc"), "70");
});

test("a response after the abort deadline is rejected even if fetch ignores abort", async () => {
  const b = browser(); b.advance(4000); b.fireTimer(4000);
  b.resolve(0); await flush();
  assert.equal(value(b, "soc"), "—");
  assert.equal(value(b, "diag-rtt"), "—");
  assert.deepEqual(b.timerDelays(), [50]);
});

test("diagnostics separate HTTP RTT, DOM work and age upper bound without cross-device clock subtraction", async () => {
  const b = browser();
  let soc = "";
  Object.defineProperty(b.element("soc"), "textContent", { get: () => soc, set(v) { soc = v; b.advance(2); } });
  b.advance(100);
  b.resolve(0, { ...fixture, age_seconds: 1,
    delivery: { relay_age_ms: 45, upstream_rtt_ms: 12, refresh_interval_ms: 500 } });
  await flush();
  assert.equal(value(b, "diag-rtt"), "100.0 ms");
  assert.equal(value(b, "diag-dom"), "4.0 ms");
  assert.equal(value(b, "diag-age"), "1104.0 ms");
  assert.equal(value(b, "diag-relay"), "45.0 ms");
  assert.equal(value(b, "diag-upstream"), "12.0 ms");
  assert.equal(value(b, "diag-refresh"), "500.0 ms");
});

test("DOM work that consumes the remaining lifetime cannot extend freshness or count as successful display", async () => {
  const b = browser(); b.resolve(0); await flush();
  b.element("compare-start").events.click();
  let soc = "";
  Object.defineProperty(b.element("soc"), "textContent", {
    get: () => soc, set(v) { soc = v; if (v === "70") b.advance(200); }
  });
  await nextResponse(b, { requestMs: 100, data: { ...fixture, age_seconds: 5.8 } });
  assert.equal(value(b, "soc"), "—");
  assert.equal(b.element("connection").dataset.state, "stale");
  const output = await download(b);
  assert.equal(output.counts.success, 0);
  assert.equal(output.counts.failure, 1);
  assert.equal(output.metrics.dom_apply_ms.max, null, "stale DOM work is excluded from fresh-success statistics");
  assert.equal(output.attempt_metrics.dom_apply_ms.max, 200);
});

test("comparison JSON contains allowlisted timing data, fixed enums and retained-window percentiles only", async () => {
  const b = browser(); b.resolve(0); await flush();
  b.element("compare-adapter").value = "external";
  b.element("compare-network").value = "router";
  b.element("compare-motor").value = "stopped";
  b.element("compare-start").events.click();
  assert.equal(b.element("compare-adapter").disabled, true);
  const injected = { ...fixture, secret_payload: TEST_KEY, ip: "192.0.2.55", gps: [35, 139],
    measurement: { ...fixture.measurement, private: "DO_NOT_EXPORT" },
    delivery: { relay_age_ms: 15, upstream_rtt_ms: 7, refresh_interval_ms: 500, token: TEST_KEY } };
  await nextResponse(b, { requestMs: 100, data: injected });
  await nextResponse(b, { requestMs: 200 });
  await nextResponse(b, { requestMs: 50, error: new Error(`DO_NOT_EXPORT ${TEST_KEY}`) });
  const output = await download(b);
  assert.deepEqual(output.labels, { source: "manual", device: "unknown", adapter: "external", network: "router", motor: "stopped" });
  assert.deepEqual(output.counts, { observations: 3, success: 2, failure: 1, new_measurements: 1,
    http_success: 2, http_failure: 1, stale_or_invalid: 0 });
  assert.deepEqual(output.metrics.http_rtt_ms, { count: 2, p50: 100, p95: 200, max: 200 });
  assert.equal(output.observations[2].http_rtt_ms, null);
  assert.equal(output.observations[2].request_duration_ms, 50);
  assert.equal(output.observations[1].new_measurement, false);
  assert.equal(output.observations[0].received_at, "2026-01-01T03:00:00.000Z");
  const raw = JSON.stringify(output);
  for (const forbidden of [TEST_KEY, "DO_NOT_EXPORT", "192.0.2.55", "voltage_v", "soc_pct", "gps", "secret_payload", "token"]) assert.equal(raw.includes(forbidden), false, forbidden);
  assert.equal(b.element("compare-adapter").disabled, false);
});

test("recording excludes a request begun before start and stops before label changes can mix conditions", async () => {
  const b = browser();
  b.element("compare-adapter").value = TEST_KEY;
  b.element("compare-start").events.click();
  b.resolve(0); await flush();
  assert.match(value(b, "compare-status"), /保持 0件/);
  await nextResponse(b);
  b.element("compare-adapter").value = "internal";
  b.element("compare-adapter").events.change();
  assert.equal(b.element("compare-start").disabled, false);
  await nextResponse(b);
  const output = await download(b);
  assert.equal(output.labels.adapter, "unknown", "untrusted label is never exported");
  assert.equal(output.counts.observations, 1);
});

test("hidden pages end comparison recording and an in-flight response cannot contaminate it", async () => {
  const b = browser(); b.resolve(0); await flush();
  b.element("compare-start").events.click(); await nextResponse(b);
  b.fireTimer(500);
  b.document.hidden = true; b.documentEvents.visibilitychange();
  assert.equal(b.element("compare-stop").disabled, true);
  b.resolve(2); await flush();
  const output = await download(b);
  assert.equal(output.counts.observations, 1);
  assert.equal(value(b, "diag-rtt"), "—");
});

test("comparison retains at most six hundred observations and summaries refer to retained rows", async () => {
  const b = browser(); b.resolve(0); await flush();
  b.element("compare-start").events.click();
  for (let i = 0; i < 605; i++) {
    // Deliberately fire callbacks faster than the requested delay to stress the
    // retention cap inside the 120-second window, without extending its deadline.
    const delay = b.timerDelays().find(ms => ms >= 50 && ms <= 500);
    b.advance(90); b.fireTimer(delay); b.advance(10);
    b.resolve(b.requests.length - 1, { ...fixture, mode: i === 0 ? "mock" : "ble",
      received_at: new Date(Date.UTC(2026, 0, 1, 3, 0, i)).toISOString() });
    await flush();
  }
  const output = await download(b);
  assert.equal(output.observations.length, 600);
  assert.equal(output.interval.dropped_observations, 5);
  assert.equal(output.counts.observations, 600);
  assert.equal(output.counts.new_measurements, 600);
  assert.deepEqual(output.source_modes, ["ble"], "retained modes describe retained observations");
  assert.deepEqual(output.session_source_modes, ["ble", "mock"], "dropped mock rows cannot hide mixed session sources");
  assert.deepEqual(output.metrics.measurement_update_interval_ms, { count: 600, p50: 100, p95: 100, max: 100 });
  assert.equal(output.metrics.http_rtt_ms.max, 10);
});

test("401 and network failure replace old diagnostics and optional invalid delivery fields are ignored", async () => {
  const b = browser(); b.advance(30); b.resolve(0); await flush();
  assert.equal(value(b, "diag-rtt"), "30.0 ms");
  await nextResponse(b, { requestMs: 40, error: new Error("offline") });
  assert.equal(value(b, "diag-rtt"), "—");
  assert.equal(value(b, "diag-age"), "—");
  await nextResponse(b, { data: { ...fixture, delivery: { relay_age_ms: -1, upstream_rtt_ms: Infinity, refresh_interval_ms: "500" } } });
  assert.equal(value(b, "diag-relay"), "—");
  assert.equal(value(b, "diag-upstream"), "—");
  assert.equal(value(b, "diag-refresh"), "—");
  b.fireTimer(500); b.advance(20); b.resolve(b.requests.length - 1, null, 401); await flush();
  assert.equal(value(b, "diag-rtt"), "—");
  assert.equal(b.element("compare-start").disabled, true);
});

test("schema two separates HTTP success, JSON validity and fresh display statistics", async () => {
  const b = browser(); b.resolve(0); await flush();
  b.element("compare-device").value = "iphone";
  b.element("compare-network").value = "local_area";
  b.element("compare-start").events.click();
  await nextResponse(b, { requestMs: 10 });
  await nextResponse(b, { requestMs: 80, data: { ...fixture, fresh: false, state: "stale", age_seconds: 9 } });
  b.advance(420); b.fireTimer(420); b.advance(50);
  b.requests[b.requests.length - 1].resolve({ status: 200, ok: true, json: async () => { throw new SyntaxError("private-error"); } });
  await flush();
  b.advance(450); b.fireTimer(450); b.advance(90);
  b.resolve(b.requests.length - 1, null, 503); await flush();
  assert.equal(value(b, "compare-http-success"), "3");
  assert.equal(value(b, "compare-fresh-success"), "1");
  assert.equal(value(b, "compare-new-count"), "1");
  assert.equal(value(b, "compare-failure-count"), "1");
  assert.equal(value(b, "compare-stale-count"), "2");
  assert.equal(value(b, "compare-rtt"), "10 / 10 ms (1件)");
  const output = await download(b);
  assert.equal(output.schema, 2);
  assert.equal(output.labels.device, "iphone");
  assert.equal(output.labels.network, "local_area");
  assert.equal(output.interval.target_duration_ms, 120000);
  assert.equal(output.interval.stop_reason, "manual");
  assert.deepEqual(output.source_modes, ["ble", "unknown"]);
  assert.deepEqual(output.session_source_modes, output.source_modes);
  assert.equal(output.observations[0].elapsed_ms, 510);
  assert.equal(output.observations[0].poll_seconds, 2);
  assert.equal(output.observations[0].stale_after_seconds, 6);
  assert.equal(output.observations[1].source_fresh, false);
  assert.equal(output.observations[1].source_state, "stale");
  assert.equal(output.observations[1].http_success, true);
  assert.equal(output.observations[1].json_success, true);
  assert.equal(output.observations[2].http_success, true);
  assert.equal(output.observations[2].json_success, false);
  assert.equal(output.observations[3].http_success, false);
  assert.equal(output.metrics_scope, "fresh_display_success");
  assert.equal(output.metrics.measurement_age_upper_ms.max, 10);
  assert.equal(output.attempt_metrics.measurement_age_upper_ms.max, 9080);
  assert.equal(JSON.stringify(output).includes("private-error"), false);
});

test("comparison expires automatically at two minutes and rejects its outstanding response", async () => {
  const b = browser(); b.resolve(0); await flush();
  b.element("compare-start").events.click();
  await nextResponse(b);
  b.fireTimer(500); // Request begins inside this comparison, but completes outside it.
  b.advance(119500); b.fireTimer(120000);
  assert.equal(b.element("compare-stop").disabled, true);
  assert.match(value(b, "compare-status"), /時間到達/);
  b.resolve(2); await flush();
  const output = await download(b);
  assert.equal(output.interval.duration_ms, 120000);
  assert.equal(output.interval.stop_reason, "duration");
  assert.equal(output.counts.observations, 1);
});

test("performance deadline expires comparison before a delayed timeout callback can run", async () => {
  const b = browser(); b.resolve(0); await flush();
  b.element("compare-start").events.click(); await nextResponse(b);
  b.advance(119500); b.watchdog();
  assert.equal(b.timerDelays().includes(120000), false);
  const output = await download(b);
  assert.equal(output.interval.stop_reason, "duration");
  assert.equal(output.interval.duration_ms, 120000);
});

test("a retired comparison timer and retired in-flight response cannot stop or enter a new comparison", async () => {
  const b = browser(); b.resolve(0); await flush();
  b.element("compare-start").events.click();
  const retiredTimer = b.timerCallback(120000);
  await nextResponse(b); b.fireTimer(500);
  b.element("compare-stop").events.click();
  b.element("compare-start").events.click();
  retiredTimer();
  assert.equal(b.element("compare-stop").disabled, false);
  b.resolve(2); await flush();
  assert.match(value(b, "compare-status"), /保持 0件/);
  await nextResponse(b);
  const output = await download(b);
  assert.equal(output.counts.observations, 1);
});

test("wall-clock pause and rollback end recording without accepting late observations", async () => {
  for (const wallAdvance of [120000, -1]) {
    const b = browser(); b.resolve(0); await flush();
    b.element("compare-start").events.click(); await nextResponse(b);
    b.advance(0, wallAdvance); b.watchdog();
    const output = await download(b);
    assert.equal(output.interval.stop_reason, wallAdvance < 0 ? "clock_changed" : "duration");
    assert.equal(output.counts.observations, 1);
  }
});

test("known samples returning out of order are not counted as new measurements", async () => {
  const b = browser(); b.resolve(0); await flush();
  b.element("compare-start").events.click();
  await nextResponse(b);
  await nextResponse(b, { data: { ...fixture, received_at: "2026-01-01T03:00:02Z" } });
  await nextResponse(b);
  const output = await download(b);
  assert.equal(output.counts.success, 3);
  assert.equal(output.counts.new_measurements, 2);
  assert.deepEqual(output.observations.map(row => row.new_measurement), [true, true, false]);
  assert.equal(output.metrics.measurement_update_interval_ms.count, 1);
});

test("real and mock source modes remain explicit and mixed records are labelled unsuitable", async () => {
  const b = browser(); b.resolve(0); await flush();
  b.element("compare-device").value = TEST_KEY;
  b.element("compare-network").value = "NOT_A_ROUTE";
  b.element("compare-start").events.click();
  await nextResponse(b);
  await nextResponse(b, { data: { ...fixture, mode: "mock" } });
  assert.match(value(b, "compare-source"), /混在.*除外/);
  const output = await download(b);
  assert.deepEqual(output.source_modes, ["ble", "mock"]);
  assert.equal(output.labels.device, "unknown");
  assert.equal(output.labels.network, "unknown");
  assert.equal(JSON.stringify(output).includes(TEST_KEY), false);
  assert.equal(JSON.stringify(output).includes("NOT_A_ROUTE"), false);
});

test("a response arriving after the recording deadline is excluded even when recording timers are delayed", async () => {
  const b = browser(); b.resolve(0); await flush();
  b.element("compare-start").events.click(); await nextResponse(b);
  b.advance(119200); b.fireTimer(500);
  b.advance(400); b.resolve(2); await flush();
  const output = await download(b);
  assert.equal(output.interval.stop_reason, "duration");
  assert.equal(output.counts.observations, 1);
  assert.equal(value(b, "soc"), "70", "normal read-only viewing continues after comparison expires");
});

test("all new device and network labels are preserved without automatic inference", async () => {
  for (const [device, network] of [["android", "mobile_router"], ["iphone", "home_router"], ["surface", "local_area"]]) {
    const b = browser(); b.resolve(0); await flush();
    b.element("compare-device").value = device;
    b.element("compare-network").value = network;
    b.element("compare-start").events.click(); await nextResponse(b);
    const output = await download(b);
    assert.equal(output.labels.device, device);
    assert.equal(output.labels.network, network);
    assert.equal(output.labels.source, "manual");
    assert.match(b.downloads[0].name, new RegExp(`-${device}-${network}\\.json$`));
  }
});
