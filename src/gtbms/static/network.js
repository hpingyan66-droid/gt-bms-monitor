"use strict";

(() => {
  const modes = {
    mobile_router: "モバイルルーターモード",
    home_router: "ホームルーターモード",
    local_area: "ローカルエリアモード"
  };
  const routers = ["mobile_router", "home_router"];
  const operationStates = ["idle", "running", "succeeded", "failed", "recovery_required"];
  const phaseNames = {
    validate: "接続条件を確認中", check_main: "BMS監視の継続を確認中", stop_viewer: "スマホ閲覧を切替中",
    stop_hotspot: "ホットスポットを停止中", connect: "Wi-Fiへ接続中", start_hotspot: "ホットスポットを開始中",
    inspect: "ネットワークを確認中", firewall: "アクセス範囲を設定中", start_viewer: "スマホ閲覧を開始中",
    commit: "切替結果を確認中", rollback: "前の接続を復旧中"
  };
  const element = id => document.getElementById(id);
  const text = (id, value) => { if (element(id).textContent !== value) element(id).textContent = value; };
  const plain = (value, limit = 400) => typeof value === "string" ? value.slice(0, limit) : "";
  const profileKey = profile => profile && typeof profile.wlan_profile === "string" && typeof profile.interface_id === "string"
    ? JSON.stringify([profile.wlan_profile, profile.interface_id]) : "";
  const jst = new Intl.DateTimeFormat("ja-JP", { timeZone: "Asia/Tokyo", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
  const inventoryJst = new Intl.DateTimeFormat("ja-JP", { timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
  let token = "";
  let current = null;
  let freshState = false;
  let submitting = false;
  let polling = false;
  let timer = null;
  let requestTail = Promise.resolve();
  let inventoryKey = "";
  let availableProfiles = [];
  let edition = null;
  const editionLabels = { full_access: "フルアクセス版", corporate: "法人利用版", individual: "個人利用版" };
  const editionCapabilities = ["can_operate", "can_manage_network", "can_export", "can_view_gps", "can_maintain"];
  const drafts = { mobile_router: "", home_router: "" };
  const dirty = { mobile_router: false, home_router: false };

  function acceptEdition(value) {
    if (value === undefined) {
      if (edition !== null) return;
      edition = { id: "legacy", label: "既存版 · フルアクセス互換", can_manage_network: true };
    } else {
      const valid = value && typeof value === "object" && !Array.isArray(value) &&
        Object.hasOwn(editionLabels, value.id) && editionCapabilities.every(name => typeof value[name] === "boolean");
      edition = { id: valid ? value.id : "unknown", label: valid ? editionLabels[value.id] : "配布種別を確認できません",
        can_manage_network: Boolean(valid && value.id === "full_access" && value.can_manage_network) };
    }
    text("edition-label", edition.label);
    text("edition-note", edition.can_manage_network
      ? "この配布版では接続管理を利用できます。BMS通信は読み取り専用です。"
      : edition.id === "unknown" ? "配布種別を確認できないため、接続管理と接続案内を停止しています。サービスの配布版を確認してください。"
      : `${edition.label}では接続管理とQR・接続案内を利用できません。監視・走行記録・GPS・履歴出力は監視画面から利用してください。`);
  }

  function busy() {
    return !edition?.can_manage_network || submitting || !token || !freshState || !current || current.operation.state === "running";
  }

  function selectedProfile(mode) {
    return availableProfiles.find(item => item.key === drafts[mode]);
  }

  function controls() {
    const locked = busy();
    element("refresh").disabled = locked;
    element("hotspot-settings").disabled = locked;
    element("switch-local_area").disabled = locked;
    for (const mode of routers) {
      element(`profile-${mode}`).disabled = locked;
      const selected = selectedProfile(mode);
      const saved = current ? profileKey(current.profiles[mode]) : "";
      element(`save-${mode}`).disabled = locked || !selected || selected.key === saved;
      element(`switch-${mode}`).disabled = locked || !saved || dirty[mode] || !availableProfiles.some(item => item.key === saved);
    }
    const canConnect = !locked && current.viewer.state === "running";
    const link = element("connection-link");
    link.hidden = !edition?.can_manage_network;
    link.setAttribute("aria-disabled", canConnect ? "false" : "true");
    if (canConnect) {
      link.setAttribute("href", "/connection");
      link.setAttribute("target", "_blank");
      link.setAttribute("rel", "noopener noreferrer");
      link.removeAttribute("tabindex");
    } else {
      link.removeAttribute("href");
      link.setAttribute("tabindex", "-1");
    }
  }

  function showError(message) {
    text("error", message);
    element("error").hidden = !message;
  }

  function showNotice(message) {
    text("notice", message);
    element("notice").hidden = !message;
  }

  function inventoryTimestamp(value) {
    if (typeof value !== "string" || value.length > 80 || !/(Z|[+-]\d{2}:\d{2})$/.test(value)) return "未確認";
    const time = new Date(value);
    return Number.isFinite(time.getTime()) ? inventoryJst.format(time) : "未確認";
  }

  function stateFailure(message) {
    freshState = false;
    element("status-panel").dataset.state = "offline";
    text("status-heading", "接続管理の状態を確認できません");
    text("operation-message", "最終確認時の表示を保持しています。再取得できるまで切替操作は停止します。");
    text("active-mode", current && modes[current.active_mode] ? `${modes[current.active_mode]}（最終確認）` : "未確認");
    text("viewer-state", "現在は未確認 · 状態を再取得しています");
    for (const mode of Object.keys(modes)) {
      element(`card-${mode}`).dataset.active = "false";
      element(`badge-${mode}`).hidden = true;
    }
    showError(message);
    controls();
  }

  function normalState(value) {
    if (!value || typeof value !== "object" || !value.profiles || !value.inventory ||
      !Array.isArray(value.inventory.wifi_profiles) || !Array.isArray(value.inventory.addresses) ||
      !Array.isArray(value.inventory.errors) || !value.inventory.hotspot || !value.viewer || !value.operation ||
      !operationStates.includes(value.operation.state) || !["running", "stopped", "external", "error"].includes(value.viewer.state)) {
      throw new Error("接続管理サービスの応答形式を確認できません。");
    }
    return { ...value, active_mode: Object.keys(modes).includes(value.active_mode) ? value.active_mode : "unknown" };
  }

  function profileOptions() {
    const list = current.inventory.wifi_profiles.slice(0, 200).filter(item => item &&
      typeof item.name === "string" && item.name.length > 0 && item.name.length <= 512 &&
      typeof item.interface_id === "string" && item.interface_id.length > 0 && item.interface_id.length <= 128);
    availableProfiles = list.map(item => ({
      key: profileKey({ wlan_profile: item.name, interface_id: item.interface_id }),
      wlan_profile: item.name, interface_id: item.interface_id,
      label: `${item.name} · ${plain(item.interface_name, 120) || item.interface_id}`
    })).filter((item, index, all) => all.findIndex(other => other.key === item.key) === index);
    // Do not rebuild a focused select on every poll when the inventory is unchanged.
    const nextKey = JSON.stringify(availableProfiles);
    const changed = nextKey !== inventoryKey;
    inventoryKey = nextKey;
    for (const mode of routers) {
      const saved = profileKey(current.profiles[mode]);
      if (!dirty[mode] || drafts[mode] === saved) {
        drafts[mode] = saved;
        dirty[mode] = false;
      }
      const select = element(`profile-${mode}`);
      if (changed || select.dataset.saved !== saved) {
        const empty = document.createElement("option");
        empty.value = "";
        empty.textContent = availableProfiles.length ? "保存済みWi-Fiとアダプターを選択" : "保存済みWi-Fiが見つかりません";
        const options = availableProfiles.map(item => {
          const option = document.createElement("option");
          option.value = item.key;
          option.textContent = item.label;
          return option;
        });
        if (saved && !availableProfiles.some(item => item.key === saved)) {
          const unavailable = document.createElement("option");
          unavailable.value = saved;
          unavailable.textContent = "保存済みの接続先（現在の一覧にありません）";
          unavailable.disabled = true;
          options.push(unavailable);
        }
        select.replaceChildren(empty, ...options);
        select.dataset.saved = saved;
      }
      select.value = drafts[mode];
      const savedName = current.profiles[mode] && plain(current.profiles[mode].wlan_profile, 512);
      text(`saved-${mode}`, dirty[mode] ? "未保存の選択です。接続先を保存してから切り替えてください。" : `保存済みの接続先：${savedName || "未登録"}`);
    }
  }

  function render(value) {
    current = normalState(value);
    acceptEdition(current.edition);
    freshState = true;
    const operation = current.operation;
    const phaseTitle = Object.prototype.hasOwnProperty.call(phaseNames, operation.phase) ? phaseNames[operation.phase] : "接続先を切替中";
    const titles = { idle: "接続先を選択できます", running: phaseTitle,
      succeeded: "Surface側の切替を確認しました", failed: "切替を完了できませんでした", recovery_required: "接続の復旧が必要です" };
    element("status-panel").dataset.state = operation.state;
    text("status-heading", titles[operation.state]);
    text("operation-message", plain(operation.message) || "切替後はスマホで接続と表示を確認してください。");
    text("active-mode", modes[current.active_mode] || "未確認");
    text("viewer-state", { running: "稼働中 · スマホ到達は未確認", stopped: "停止中", external: "管理外のプロセス", error: "起動状態を確認してください" }[current.viewer.state]);
    text("checked-at", jst.format(new Date()));
    text("inventory-checked-at", inventoryTimestamp(current.inventory_checked_at));
    showError(plain(current.last_error));
    if (edition && !edition.can_manage_network) {
      text("status-heading", "この配布版では接続管理を利用できません");
      text("operation-message", "配布版の利用範囲により、接続先の変更・Windows設定の表示・接続案内を停止しています。");
    } else if (!token) {
      text("status-heading", "管理セッションの再確認が必要です");
      showError("Surfaceで画面を再読み込みし、管理セッションを取得してください。");
    }
    for (const mode of Object.keys(modes)) {
      const active = current.active_mode === mode;
      element(`card-${mode}`).dataset.active = String(active);
      element(`badge-${mode}`).hidden = !active;
    }
    profileOptions();
    const hotspot = current.inventory.hotspot;
    text("hotspot-state", { on: "有効", off: "無効", unknown: "未確認" }[hotspot.state] || "未確認");
    text("hotspot-ssid", plain(hotspot.ssid, 128) || "未確認");
    const warnings = Array.isArray(operation.warnings) ? operation.warnings : [];
    const hotspotUnknown = !["on", "off"].includes(hotspot.state);
    const warningMessage = warnings.includes("hotspot_state_unconfirmed")
      ? "ホットスポットの停止状態は未確認です。ルーター側の切替結果とは別に、Windows設定で確認してください。"
      : hotspotUnknown ? "ホットスポットの状態は未確認です。ルーターの情報が正常なら登録・切替は可能です。ローカルエリアの完了には有効状態の確認が必要です。" : "";
    text("operation-warnings", warningMessage);
    element("operation-warnings").hidden = !warningMessage;
    const addresses = current.inventory.addresses.filter(item => item && typeof item === "object").slice(0, 100).map(item => {
      const li = document.createElement("li");
      const prefix = Number.isInteger(item.prefix_length) ? `/${item.prefix_length}` : "";
      li.textContent = `${plain(item.interface_alias, 120) || "インターフェース"} · ${plain(item.ip_address, 64)}${prefix} · ${plain(item.network_category, 30) || "分類未確認"} · ${plain(item.adapter_status, 30) || "状態未確認"}`;
      return li;
    });
    if (!addresses.length) {
      const li = document.createElement("li");
      li.textContent = "接続先のアドレスはまだ確認できません。";
      addresses.push(li);
    }
    element("address-list").replaceChildren(...addresses);
    const errors = current.inventory.errors.filter(item => typeof item === "string").slice(0, 10).map(item => {
      const li = document.createElement("li");
      li.textContent = plain(item);
      return li;
    });
    element("inventory-errors").replaceChildren(...errors);
    element("inventory-errors").hidden = !errors.length;
    controls();
  }

  function request(path, body) {
    // Serialize both reads and user actions. A slow poll cannot overlap a write.
    const task = requestTail.then(async () => {
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 15000);
      try {
        const options = { method: body === undefined ? "GET" : "POST", credentials: "omit",
          cache: "no-store", redirect: "error", referrerPolicy: "no-referrer", signal: controller.signal, headers: {} };
        if (body !== undefined) {
          if (!edition?.can_manage_network) throw new Error("管理セッションの配布版では接続管理を利用できません。");
          if (!token) throw new Error("管理セッションを確認できません。画面を再読み込みしてください。");
          options.headers = { "Content-Type": "application/json", "X-GTBMS-Token": token };
          options.body = JSON.stringify(body);
        }
        const response = await fetch(path, options);
        if (controller.signal.aborted) throw new Error("timeout");
        if (response.status === 401 || response.status === 403) {
          token = "";
          throw new Error("管理セッションの認証を確認できません。Surfaceで画面を再読み込みしてください。");
        }
        if (!response.ok) throw new Error(response.status === 409 ? "別の切替処理が進行中です。状態を確認してください。" : `接続管理の要求を完了できませんでした（HTTP ${response.status}）。`);
        const result = response.status === 204 ? {} : await response.json();
        if (controller.signal.aborted) throw new Error("timeout");
        return result;
      } finally {
        window.clearTimeout(timeout);
      }
    });
    requestTail = task.catch(() => {});
    return task;
  }

  function schedule(delay = 1000) {
    if (timer !== null) window.clearTimeout(timer);
    timer = window.setTimeout(poll, delay);
  }

  async function getState() {
    render(await request("/api/state"));
  }

  async function poll() {
    if (polling) return;
    timer = null;
    polling = true;
    const started = performance.now();
    try {
      if (!submitting && !document.hidden) await getState();
    } catch {
      stateFailure("接続管理サービスから最新状態を取得できません。自動で再確認します。BMS保存の状態は監視画面で確認できます。");
    } finally {
      polling = false;
      schedule(Math.max(100, 1000 - (performance.now() - started)));
    }
  }

  async function act(path, body, notice) {
    if (busy()) return;
    submitting = true;
    if (timer !== null) window.clearTimeout(timer);
    timer = null;
    showNotice(notice);
    showError("");
    controls();
    try {
      await request(path, body);
      await getState();
    } catch (error) {
      // No optimistic mode changes or automatic POST retries after an uncertain result.
      stateFailure(error && /^管理セッション|^別の切替処理|^接続管理の要求/.test(error.message)
        ? error.message : "要求の結果を確認できません。再送せず、サービスの状態を確認します。");
    } finally {
      submitting = false;
      controls();
      schedule();
    }
  }

  for (const mode of routers) {
    element(`profile-${mode}`).addEventListener("change", () => {
      if (busy()) return;
      drafts[mode] = element(`profile-${mode}`).value;
      dirty[mode] = drafts[mode] !== profileKey(current.profiles[mode]);
      text(`saved-${mode}`, dirty[mode] ? "未保存の選択です。接続先を保存してから切り替えてください。" : `保存済みの接続先：${plain(current.profiles[mode]?.wlan_profile, 512) || "未登録"}`);
      controls();
    });
    element(`save-${mode}`).addEventListener("click", () => {
      const profile = selectedProfile(mode);
      if (!profile || element(`save-${mode}`).disabled) return;
      void act("/api/profile", { mode, wlan_profile: profile.wlan_profile, interface_id: profile.interface_id }, "接続先の保存を確認しています。Wi-Fiの切替は別のボタンで行います。");
    });
  }
  for (const mode of Object.keys(modes)) element(`switch-${mode}`).addEventListener("click", () => {
    if (element(`switch-${mode}`).disabled) return;
    void act("/api/switch", { mode }, `${modes[mode]}への切替を要求しました。スマホのWi-Fi再接続が必要です。BMS監視と保存は継続します。`);
  });
  element("refresh").addEventListener("click", () => { void act("/api/refresh", {}, "Windowsの接続先一覧を再取得しています。"); });
  element("hotspot-settings").addEventListener("click", () => { void act("/api/hotspot-settings", {}, "Windowsのホットスポット設定を開きます。ネットワーク名とパスワードはWindows設定で確認してください。"); });
  element("connection-link").addEventListener("click", event => {
    if (element("connection-link").getAttribute("aria-disabled") === "true") event.preventDefault();
  });

  async function start() {
    controls();
    if (!["localhost", "127.0.0.1"].includes(window.location.hostname)) {
      stateFailure("この管理画面はSurfaceのlocalhostから開いてください。外部端末からの操作はできません。");
      return;
    }
    try {
      const session = await request("/api/session");
      if (!session || typeof session !== "object") throw new Error("session");
      acceptEdition(session.edition);
      const validToken = typeof session.token === "string" && session.token.length >= 1 && session.token.length <= 256;
      if (edition.can_manage_network && !validToken) throw new Error("session");
      token = validToken ? session.token : "";
      await getState();
    } catch {
      stateFailure("管理セッションまたは状態を取得できません。サービスの起動を確認し、画面を再読み込みしてください。");
    } finally {
      schedule();
    }
  }
  void start();
})();
