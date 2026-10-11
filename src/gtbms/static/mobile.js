"use strict";

(() => {
  const STORAGE_KEY = "gtbms.viewer.key.v1";
  const KEY_PATTERN = /^[A-Za-z0-9_-]{43}$/;
  const POLL_MS = 500;
  const MIN_WAIT_MS = 50;
  const TIMEOUT_MS = 4000;
  const RECORD_LIMIT = 600;
  const RECORD_DURATION_MS = 120000;
  const element = id => document.getElementById(id);
  const text = (id, value) => { element(id).textContent = value; };
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const number = (value, digits = 1) => finite(value) ? value.toFixed(digits) : "—";
  const unit = (value, suffix) => finite(value) ? `${number(value)} ${suffix}` : "—";
  const jst = new Intl.DateTimeFormat("ja-JP", {
    timeZone: "Asia/Tokyo", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"
  });
  let key = "";
  let generation = 0;
  let controller = null;
  let restartPending = false;
  let nextPoll = null;
  let freshness = null;
  let pageActive = !document.hidden;
  let comparison = null;
  const labelChoices = {
    device: ["unknown", "android", "iphone", "surface"],
    adapter: ["unknown", "internal", "external"],
    network: ["unknown", "mobile_router", "home_router", "local_area", "router", "hotspot", "bluetooth"],
    motor: ["unknown", "stopped", "running"]
  };
  const metricNames = ["http_rtt_ms", "request_duration_ms", "dom_apply_ms", "measurement_age_upper_ms", "relay_age_ms",
    "upstream_rtt_ms", "refresh_interval_ms", "request_interval_ms", "measurement_update_interval_ms"];

  function clearDiagnostics() {
    for (const id of ["diag-rtt", "diag-dom", "diag-age", "diag-relay", "diag-upstream", "diag-refresh"]) text(id, "—");
  }

  function updateComparisonControls() {
    const active = Boolean(comparison && comparison.active);
    for (const name of Object.keys(labelChoices)) element(`compare-${name}`).disabled = active;
    element("compare-start").disabled = active || !key;
    element("compare-stop").disabled = !active;
    element("compare-save").disabled = active || !comparison || !comparison.observations.length;
    if (comparison) {
      const seconds = Math.ceil(Math.max(0, comparison.target_duration_ms - Math.max(0, performance.now() - comparison.startedPerf)) / 1000);
      const reasons = { manual: "手動", duration: "時間到達", hidden: "画面が非表示", labels_changed: "条件変更", access_lost: "閲覧キー失効", clock_changed: "時計変更" };
      text("compare-status", `${active ? `記録中 · 残り約 ${seconds}秒` : `記録終了 · ${reasons[comparison.stop_reason] || "終了"}`} · 保持 ${comparison.observations.length}件 / 除外 ${comparison.dropped}件`);
      updateComparisonSummary();
    }
  }

  function stopComparison(reason = "manual", record = comparison) {
    if (record && comparison === record && record.active) {
      record.active = false;
      record.ended_at = new Date(Date.now()).toISOString();
      record.duration_ms = Math.max(0, performance.now() - record.startedPerf);
      record.stop_reason = typeof reason === "string" ? reason : "manual";
      if (record.timer !== null) window.clearTimeout(record.timer);
      record.timer = null;
    }
    updateComparisonControls();
  }

  function comparisonExpired(record) {
    if (!record || record !== comparison || !record.active) return true;
    const nowWall = Date.now();
    if (nowWall < record.lastWall) stopComparison("clock_changed", record);
    else if (performance.now() - record.startedPerf >= record.target_duration_ms || nowWall - record.startedWall >= record.target_duration_ms) stopComparison("duration", record);
    record.lastWall = nowWall;
    return !record.active;
  }

  function startComparison() {
    if (!key || !pageActive || document.hidden || (comparison && comparison.active)) return;
    const labels = { source: "manual" };
    for (const [name, choices] of Object.entries(labelChoices)) {
      const value = element(`compare-${name}`).value;
      labels[name] = choices.includes(value) ? value : "unknown";
    }
    comparison = {
      active: true, labels, started_at: new Date(Date.now()).toISOString(), ended_at: null,
      startedPerf: performance.now(), startedWall: Date.now(), lastWall: Date.now(), duration_ms: null, observations: [], dropped: 0,
      target_duration_ms: RECORD_DURATION_MS, stop_reason: null, timer: null,
      sourceModes: new Set(), seenMeasurements: new Set(), lastNewPerf: null, lastRequestPerf: null
    };
    const record = comparison;
    record.timer = window.setTimeout(() => {
      if (comparison === record && record.active) stopComparison("duration", record);
    }, record.target_duration_ms);
    updateComparisonControls();
  }

  function metric(value) {
    return finite(value) && value >= 0 && value <= 1e12 ? value : null;
  }

  function diagnostics(data, rtt, dom, elapsed, success, record, startedPerf, httpSuccess, jsonSuccess) {
    const valid = data && data.schema === 1 && ["ble", "mock", "unknown"].includes(data.mode);
    const receivedMs = valid && typeof data.received_at === "string" ? Date.parse(data.received_at) : NaN;
    const receivedAt = Number.isFinite(receivedMs) ? new Date(receivedMs).toISOString() : null;
    const delivery = valid && data.delivery && typeof data.delivery === "object" ? data.delivery : {};
    const age = valid ? metric(data.age_seconds) : null;
    const point = {
      observed_at: new Date(Date.now()).toISOString(), received_at: receivedAt,
      elapsed_ms: record ? metric(performance.now() - record.startedPerf) : null,
      source_mode: valid ? data.mode : "unknown",
      source_fresh: valid && typeof data.fresh === "boolean" ? data.fresh : null,
      source_state: valid && ["connected", "connecting", "surface_offline", "stale", "reconnecting", "stopped", "invalid"].includes(data.state) ? data.state : "unknown",
      http_success: httpSuccess === true, json_success: jsonSuccess === true,
      poll_seconds: valid ? metric(data.poll_seconds) : null,
      stale_after_seconds: valid ? metric(data.stale_after_seconds) : null,
      success, new_measurement: false, http_rtt_ms: metric(rtt), request_duration_ms: metric(performance.now() - startedPerf), dom_apply_ms: metric(dom),
      measurement_age_upper_ms: receivedAt && age !== null ? metric(age * 1000 + elapsed + (dom || 0)) : null,
      relay_age_ms: metric(delivery.relay_age_ms), upstream_rtt_ms: metric(delivery.upstream_rtt_ms),
      refresh_interval_ms: metric(delivery.refresh_interval_ms),
      request_interval_ms: null, measurement_update_interval_ms: null
    };
    // Whitelist numeric timings only. Never retain the response, request headers or exception text.
    for (const [id, name] of [["diag-rtt", "http_rtt_ms"], ["diag-dom", "dom_apply_ms"],
      ["diag-age", "measurement_age_upper_ms"], ["diag-relay", "relay_age_ms"],
      ["diag-upstream", "upstream_rtt_ms"], ["diag-refresh", "refresh_interval_ms"]]) text(id, unit(point[name], "ms"));
    if (comparisonExpired(record)) return;
    point.request_interval_ms = record.lastRequestPerf === null ? null : metric(startedPerf - record.lastRequestPerf);
    record.lastRequestPerf = startedPerf;
    record.sourceModes.add(point.source_mode);
    const sampleId = `${point.source_mode}:${receivedAt}`;
    if (success && receivedAt && !record.seenMeasurements.has(sampleId)) {
      point.new_measurement = true;
      point.measurement_update_interval_ms = record.lastNewPerf === null ? null : metric(performance.now() - record.lastNewPerf);
      record.lastNewPerf = performance.now();
      record.seenMeasurements.add(sampleId);
    }
    record.observations.push(point);
    if (record.observations.length > RECORD_LIMIT) {
      record.observations.shift();
      record.dropped += 1;
    }
    updateComparisonControls();
  }

  function summarizeMetrics(rows) {
    const metrics = {};
    for (const name of metricNames) {
      const values = rows.map(row => row[name]).filter(finite).sort((a, b) => a - b);
      const percentile = p => values.length ? values[Math.ceil(values.length * p) - 1] : null;
      metrics[name] = { count: values.length, p50: percentile(.5), p95: percentile(.95), max: values.length ? values[values.length - 1] : null };
    }
    return metrics;
  }

  function comparisonCounts(rows) {
    return { observations: rows.length, success: rows.filter(row => row.success).length,
      failure: rows.filter(row => !row.success).length, new_measurements: rows.filter(row => row.new_measurement).length,
      http_success: rows.filter(row => row.http_success).length, http_failure: rows.filter(row => !row.http_success).length,
      stale_or_invalid: rows.filter(row => row.http_success && !row.success).length };
  }

  function updateComparisonSummary() {
    const rows = comparison.observations;
    const counts = comparisonCounts(rows);
    for (const [id, name] of [["compare-http-success", "http_success"], ["compare-fresh-success", "success"],
      ["compare-new-count", "new_measurements"], ["compare-failure-count", "http_failure"], ["compare-stale-count", "stale_or_invalid"]]) text(id, String(counts[name]));
    const metrics = summarizeMetrics(rows.filter(row => row.success));
    for (const [id, name] of [["compare-rtt", "http_rtt_ms"], ["compare-updates", "measurement_update_interval_ms"], ["compare-age", "measurement_age_upper_ms"]]) {
      const summary = metrics[name];
      text(id, summary.count ? `${number(summary.p50, 0)} / ${number(summary.p95, 0)} ms (${summary.count}件)` : "—");
    }
    const mixed = comparison.sourceModes.has("ble") && comparison.sourceModes.has("mock");
    text("compare-source", mixed ? "実機・模擬データ混在：比較対象から除外" : comparison.sourceModes.has("mock") ? "模擬データ：実機との比較不可" : comparison.sourceModes.has("ble") ? "実機データ" : "未確認");
  }

  function comparisonExport() {
    const rows = comparison.observations;
    return {
      schema: 2, labels: comparison.labels,
      source_modes: [...new Set(rows.map(row => row.source_mode))].sort(),
      session_source_modes: [...comparison.sourceModes].sort(),
      interval: { started_at: comparison.started_at, ended_at: comparison.ended_at, duration_ms: comparison.duration_ms,
        target_duration_ms: comparison.target_duration_ms, stop_reason: comparison.stop_reason,
        retained_from_at: rows[0]?.observed_at || null, retained_to_at: rows[rows.length - 1]?.observed_at || null,
        dropped_observations: comparison.dropped, observation_limit: RECORD_LIMIT },
      counts: comparisonCounts(rows),
      percentile_method: "nearest_rank", metrics_scope: "fresh_display_success",
      metrics: summarizeMetrics(rows.filter(row => row.success)), attempt_metrics: summarizeMetrics(rows), observations: rows
    };
  }

  function saveComparison() {
    if (!comparison || comparison.active || !comparison.observations.length) return;
    let url;
    try {
      const blob = new Blob([JSON.stringify(comparisonExport(), null, 2)], { type: "application/json" });
      url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      const stamp = comparison.started_at.replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z");
      link.download = `gtbms-display-${stamp}-${comparison.labels.device}-${comparison.labels.network}.json`;
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      text("compare-status", "JSON保存を開始しました。保存先はブラウザーで確認してください。");
    } catch {
      if (url) URL.revokeObjectURL(url);
      text("compare-status", "このブラウザーではJSON保存を開始できません。記録はこのタブに残っています。");
    }
  }

  const messages = {
    connected: ["最新データを受信中", "Surfaceで測定した値を表示しています。"],
    connecting: ["BMSへ接続中", "SurfaceでBluetooth接続を開始しています。現在値は非表示です。"],
    surface_offline: ["Surfaceに接続できません", "Wi-FiまたはSurfaceとの通信が切れています。現在値は非表示です。"],
    stale: ["最新の測定値がありません", "古い値を表示しないよう、測定値を非表示にしています。"],
    reconnecting: ["BMSへ再接続中", "SurfaceでBluetooth接続を確認しています。現在値は非表示です。"],
    stopped: ["Surfaceの監視は停止中", "監視を開始する操作は、停車中にSurfaceで行ってください。"],
    invalid: ["測定値を確認できません", "受信データを確認できないため、現在値は非表示です。"]
  };

  function status(state, detail) {
    const known = Object.prototype.hasOwnProperty.call(messages, state) ? state : "invalid";
    element("connection").dataset.state = known;
    text("status-title", messages[known][0]);
    text("status-detail", detail || messages[known][1]);
  }

  function clearMeasurements() {
    freshness = null;
    for (const id of ["soc", "remaining-ah", "safe-range", "voltage", "current", "power",
      "temperature", "cell-delta", "normal-range", "full-range", "raw-current", "mos"]) text(id, "—");
    element("soc-fill").style.width = "0%";
    text("range-note", "予測データなし");
    text("current-title", "電流");
    text("current-note", "最新の測定値なし");
    text("power-note", "最新の測定値なし");
    text("alarms", "最新の測定値なし");
    text("quality", "最新の測定値なし");
    text("sample-age", "最新の測定値なし");
    text("source-alerts", "");
    element("source-alerts").hidden = true;
    element("alarms").closest(".protection").classList.remove("alarm");
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "最新の測定値なし";
    element("cells").replaceChildren(empty);
  }

  function invalidate(state = "stale", detail) {
    clearMeasurements();
    status(state, detail);
  }

  function forgetKey(message) {
    key = "";
    try { sessionStorage.removeItem(STORAGE_KEY); } catch { /* Storage may be disabled. */ }
    stopRequests();
    stopComparison("access_lost");
    invalidate("stale", "停車中に閲覧キーを確認して、再度接続してください。");
    element("access-panel").hidden = false;
    element("access-key").value = "";
    text("access-error", message);
  }

  function stopRequests() {
    generation += 1;
    if (nextPoll !== null) window.clearTimeout(nextPoll);
    nextPoll = null;
    restartPending = false;
    if (controller) controller.abort();
    // Keep single-flight ownership until the aborted fetch/json actually settles.
  }

  function applyMeasurement(measurement, prediction) {
    text("soc", number(measurement.soc_pct, 0));
    const soc = measurement.soc_pct;
    element("soc-fill").style.width = finite(soc) ? `${Math.max(0, Math.min(100, soc))}%` : "0%";
    text("remaining-ah", number(measurement.remaining_ah, 2));
    text("voltage", number(measurement.voltage_v, 2));
    const confirmed = ["charge_positive", "discharge_positive"].includes(measurement.current_direction);
    text("current", number(confirmed ? measurement.current_a : measurement.raw_current_a, 2));
    text("current-title", confirmed ? "電流" : "電流 · BMS生値");
    text("current-note", confirmed ? "＋放電 / −充電" : "符号方向 未確認");
    text("raw-current", finite(measurement.raw_current_a) ? `${number(measurement.raw_current_a, 2)} A` : "—");
    text("power", number(confirmed ? measurement.power_w : null, 0));
    text("power-note", confirmed ? "＋放電 / −充電" : "符号確認後に表示");
    const temperatures = Array.isArray(measurement.temperatures_c) ? measurement.temperatures_c.filter(finite) : [];
    text("temperature", number(temperatures.length ? Math.max(...temperatures) : null));
    text("cell-delta", number(finite(measurement.cell_delta_v) ? measurement.cell_delta_v * 1000 : null, 0));
    const alarms = Array.isArray(measurement.alarms) ? measurement.alarms.filter(value => typeof value === "string") : null;
    text("alarms", alarms ? (alarms.length ? alarms.join(" / ") : "異常コードなし") : "未対応");
    element("alarms").closest(".protection").classList.toggle("alarm", Boolean(alarms && alarms.length));
    const flags = Array.isArray(measurement.quality_flags) ? measurement.quality_flags : null;
    text("quality", flags ? (flags.length ? flags.filter(value => typeof value === "string").join(" / ") : "形式検査 正常") : "未確認");
    const mos = value => value === true ? "ON" : value === false ? "OFF" : "未対応";
    text("mos", `${mos(measurement.charge_mos)} / ${mos(measurement.discharge_mos)}`);

    const cells = Array.isArray(measurement.cell_voltages_v) ? measurement.cell_voltages_v.slice(0, 32) : [];
    if (cells.length) {
      const nodes = cells.map((voltage, index) => {
        const cell = document.createElement("div");
        cell.className = "cell";
        const label = document.createElement("span");
        label.textContent = `セル ${index + 1}`;
        const value = document.createElement("strong");
        value.textContent = number(voltage, 3);
        cell.append(label, value);
        return cell;
      });
      element("cells").replaceChildren(...nodes);
    }
    if (prediction && typeof prediction === "object" && !Array.isArray(prediction)) {
      text("safe-range", number(prediction.safe_remaining_range_km));
      text("normal-range", unit(prediction.remaining_range_km, "km"));
      text("full-range", unit(prediction.full_range_km, "km"));
      text("range-note", typeof prediction.reliability === "string" ? `信頼性：${prediction.reliability}` : "予測データなし");
    }
  }

  function receive(data, startedPerf, startedWall) {
    clearMeasurements();
    if (!data || data.schema !== 1 || !["ble", "mock", "unknown"].includes(data.mode)) {
      status("invalid");
      return;
    }
    text("mode", data.mode === "mock" ? "模擬データ" : data.mode === "ble" ? "実機 · 閲覧専用" : "接続待ち");
    element("mode").classList.toggle("mock", data.mode === "mock");
    const receivedTime = typeof data.received_at === "string" ? Date.parse(data.received_at) : NaN;
    text("last-received", Number.isFinite(receivedTime) ? jst.format(receivedTime) : "—");
    const alerts = Array.isArray(data.alerts) ? data.alerts.filter(value => typeof value === "string").slice(0, 20) : [];
    if (alerts.length) {
      text("source-alerts", `最終受信の通知：${alerts.map(value => value.slice(0, 256)).join(" / ")}`);
      element("source-alerts").hidden = false;
    }
    if (data.fresh !== true || data.state !== "connected") {
      status(data.state === "connected" ? "stale" : data.state);
      return;
    }
    const age = data.age_seconds;
    const limit = data.stale_after_seconds;
    const acceptedPerf = performance.now();
    const acceptedWall = Date.now();
    const elapsed = Math.max(acceptedPerf - startedPerf, acceptedWall - startedWall, 0);
    // Subtract the complete request duration: network delay must never extend freshness.
    const budget = finite(age) && finite(limit) ? (limit - age) * 1000 - elapsed : 0;
    if (!Number.isFinite(receivedTime) || !finite(age) || age < 0 || !finite(limit) || limit <= 0 || limit > 120 || budget <= 0) {
      status("stale");
      return;
    }
    if (!data.measurement || typeof data.measurement !== "object" || Array.isArray(data.measurement)) {
      status("invalid");
      return;
    }
    applyMeasurement(data.measurement, data.prediction);
    freshness = {
      // Anchor before DOM work so rendering cannot extend the remaining lifetime.
      performanceDeadline: acceptedPerf + budget,
      wallDeadline: acceptedWall + budget,
      performanceStart: acceptedPerf,
      wallStart: acceptedWall,
      age: age + elapsed / 1000
    };
    status("connected", data.mode === "mock" ? "模擬BMSの値です。実機の測定値ではありません。" : undefined);
    watchdog();
    return Boolean(freshness);
  }

  function watchdog() {
    if (comparison && comparison.active) {
      if (!comparisonExpired(comparison)) updateComparisonControls();
    }
    if (!freshness) return;
    const nowPerf = performance.now();
    const nowWall = Date.now();
    // Both clocks are necessary: mobile browsers can pause timers and performance time.
    if (!pageActive || document.hidden || nowPerf >= freshness.performanceDeadline || nowWall >= freshness.wallDeadline || nowWall < freshness.wallStart) {
      invalidate("stale");
      return;
    }
    const age = freshness.age + Math.max(nowPerf - freshness.performanceStart, nowWall - freshness.wallStart, 0) / 1000;
    text("sample-age", `${Math.floor(age)}秒前の測定値`);
  }

  async function poll() {
    nextPoll = null;
    if (controller || !key || !pageActive || document.hidden) return;
    const requestGeneration = generation;
    const ownController = new AbortController();
    controller = ownController;
    const startedPerf = performance.now();
    const startedWall = Date.now();
    const requestRecord = comparison && !comparisonExpired(comparison) ? comparison : null;
    let httpSuccess = false;
    let jsonSuccess = false;
    const finishAttempt = (data = null, dom = null, success = false, bodyReadAt = null) => {
      if (requestGeneration !== generation || !pageActive || document.hidden) return;
      const rtt = bodyReadAt === null ? null : Math.max(0, bodyReadAt - startedPerf);
      const elapsed = Math.max(rtt || 0, Date.now() - startedWall - (dom || 0), 0);
      diagnostics(data, rtt, dom, elapsed, success, requestRecord, startedPerf, httpSuccess, jsonSuccess);
    };
    const timeout = window.setTimeout(() => ownController.abort(), TIMEOUT_MS);
    try {
      const response = await fetch("/api/view", {
        method: "GET", headers: { Authorization: `Bearer ${key}` },
        credentials: "omit", cache: "no-store", redirect: "error", referrerPolicy: "no-referrer",
        signal: ownController.signal
      });
      if (requestGeneration !== generation || !pageActive || document.hidden) return;
      if (ownController.signal.aborted) throw new Error("Request expired");
      httpSuccess = response.ok === true;
      if (response.status === 401 || response.status === 403) {
        finishAttempt();
        forgetKey("閲覧キーを確認できません。Surfaceで表示した新しいキーを使用してください。");
        return;
      }
      if (!response.ok) throw new Error("View unavailable");
      let data;
      try {
        data = await response.json();
        jsonSuccess = true;
      } catch {
        if (requestGeneration === generation && pageActive && !document.hidden) {
          invalidate("invalid");
          finishAttempt(null, null, false, performance.now());
        }
        return;
      }
      if (requestGeneration !== generation || !pageActive || document.hidden) return;
      if (ownController.signal.aborted) throw new Error("Request expired");
      const domStarted = performance.now();
      const success = receive(data, startedPerf, startedWall) === true;
      const domMs = Math.max(0, performance.now() - domStarted);
      finishAttempt(data, domMs, success, domStarted);
    } catch {
      if (requestGeneration === generation && pageActive && !document.hidden) {
        invalidate("surface_offline");
        finishAttempt();
      }
    } finally {
      window.clearTimeout(timeout);
      if (controller === ownController) controller = null;
      if (requestGeneration === generation && key && pageActive && !document.hidden) {
        nextPoll = window.setTimeout(poll, Math.max(MIN_WAIT_MS, POLL_MS - (performance.now() - startedPerf)));
      } else if (restartPending && key && pageActive && !document.hidden) {
        restartPending = false;
        nextPoll = window.setTimeout(poll, MIN_WAIT_MS);
      }
    }
  }

  function reconnect() {
    stopRequests();
    clearDiagnostics();
    invalidate("stale", key ? "Surfaceから最新データを取得しています。" : "停車中にSurfaceの接続リンクを開くか、閲覧キーを貼り付けてください。");
    if (key && pageActive && !document.hidden) {
      if (controller) restartPending = true;
      else void poll();
    }
  }

  function acceptKey(value) {
    if (!KEY_PATTERN.test(value)) return false;
    key = value;
    try { sessionStorage.setItem(STORAGE_KEY, key); } catch { /* In-memory access still works. */ }
    element("access-panel").hidden = true;
    element("access-key").value = "";
    element("access-key").removeAttribute("aria-invalid");
    text("access-error", "");
    text("last-received", "—");
    updateComparisonControls();
    reconnect();
    return true;
  }

  element("access-form").addEventListener("submit", event => {
    event.preventDefault();
    const pasted = element("access-key").value.trim();
    if (!acceptKey(pasted)) {
      element("access-key").setAttribute("aria-invalid", "true");
      text("access-error", "Surfaceに表示された43文字の閲覧キーを貼り付けてください。");
    }
  });
  document.addEventListener("visibilitychange", () => {
    pageActive = !document.hidden;
    if (!pageActive) stopComparison("hidden");
    reconnect();
  });
  window.addEventListener("pagehide", () => {
    pageActive = false;
    stopComparison("hidden");
    stopRequests();
    clearDiagnostics();
    invalidate("stale");
  });
  window.addEventListener("pageshow", () => {
    pageActive = !document.hidden;
    reconnect();
  });
  window.addEventListener("offline", () => {
    stopRequests();
    clearDiagnostics();
    invalidate("surface_offline");
    if (key && pageActive && !document.hidden) {
      if (controller) restartPending = true;
      else nextPoll = window.setTimeout(poll, POLL_MS);
    }
  });
  window.addEventListener("online", reconnect);
  element("compare-start").addEventListener("click", startComparison);
  element("compare-stop").addEventListener("click", stopComparison);
  element("compare-save").addEventListener("click", saveComparison);
  for (const name of Object.keys(labelChoices)) element(`compare-${name}`).addEventListener("change", () => {
    if (comparison && comparison.active) stopComparison("labels_changed");
  });
  window.setInterval(watchdog, 250);

  // A fragment is not sent in HTTP requests. Erase it before any authenticated request.
  let fragmentKey = "";
  const hadFragment = Boolean(window.location.hash);
  if (hadFragment) {
    fragmentKey = new URLSearchParams(window.location.hash.slice(1)).get("key") || "";
    try {
      history.replaceState(null, "", window.location.pathname + window.location.search);
    } catch {
      window.location.replace(window.location.pathname + window.location.search);
      return;
    }
  }
  let savedKey = "";
  try { savedKey = sessionStorage.getItem(STORAGE_KEY) || ""; } catch { /* Storage is optional. */ }
  clearMeasurements();
  clearDiagnostics();
  updateComparisonControls();
  if (hadFragment && !acceptKey(fragmentKey)) {
    forgetKey("接続リンクの閲覧キーを確認できません。キーを貼り付けてください。");
  } else if (!hadFragment && !acceptKey(savedKey)) {
    status("stale", "停車中にSurfaceの接続リンクを開くか、閲覧キーを貼り付けてください。");
  }
})();
