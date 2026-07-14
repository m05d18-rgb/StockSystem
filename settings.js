const stocks = ["2330", "2317", "2454", "2603", "2881", "2303", "3711", "3037"];

let latestBackendStatus = null;

function applyTheme(theme = localStorage.getItem("stock-vibe-theme-v1") || "dark") {
  const dark = theme !== "light";
  document.body.classList.toggle("dark", dark);
  localStorage.setItem("stock-vibe-theme-v1", dark ? "dark" : "light");
  const button = document.getElementById("themeToggle");
  if (button) {
    button.textContent = dark ? "☀" : "◐";
    button.title = dark ? "切換淺色模式" : "切換深色模式";
  }
}

function initSidebarToggle() {
  const sidebar = document.getElementById("appSidebar");
  const toggle = document.getElementById("sidebarToggle");
  const backdrop = document.getElementById("sidebarBackdrop");
  if (!sidebar || !toggle || !backdrop) return;
  const setOpen = (open) => {
    document.body.classList.toggle("sidebar-open", open);
    sidebar.setAttribute("aria-hidden", open ? "false" : "true");
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    toggle.textContent = open ? "收起" : "選單";
    backdrop.hidden = !open;
  };
  toggle.addEventListener("click", () => setOpen(!document.body.classList.contains("sidebar-open")));
  backdrop.addEventListener("click", () => setOpen(false));
  sidebar.querySelectorAll("a").forEach((link) => {
    link.addEventListener("click", () => setOpen(false));
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setOpen(false);
  });
  setOpen(false);
}

function friendlyError(error) {
  const message = error?.message || String(error);
  if (message === "Failed to fetch" || message.includes("NetworkError")) {
    return "本機伺服器沒有回應，請確認 http://127.0.0.1:8008/ 的 Python server 正在執行後重新整理。";
  }
  return message;
}

function fmt(value, digits = 2) {
  return Number(value).toLocaleString("zh-TW", { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

function pct(value) {
  return `${value >= 0 ? "+" : ""}${fmt(value, 2)}%`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function backendResult(type, title, message = "") {
  const result = document.getElementById("backendUpdateResult");
  if (!result) return;
  result.className = `explanation-card ${type === "error" ? "down" : type === "success" ? "up" : ""}`.trim();
  result.innerHTML = `<strong>${title}</strong>${message}`;
}

function setBackendBusy(active) {
  const refreshButton = document.getElementById("refreshMlStatus");
  const updateButton = document.getElementById("runMlUpdate");
  const busy = Boolean(active);
  if (refreshButton) {
    refreshButton.disabled = busy;
    refreshButton.textContent = active === "refresh" ? "讀取中" : "重新讀取狀態";
  }
  if (updateButton) {
    updateButton.disabled = busy;
    updateButton.textContent = active === "update" ? "正式更新中" : "現在更新一次";
  }
}

function progressMetricText(stage) {
  if (!stage) return "N/A";
  const value = Number(stage.current);
  if (!Number.isFinite(value)) return "N/A";
  if (stage.metric === "profitFactor") return fmt(value, 2);
  if (stage.metric === "precision") return `${fmt(value * 100, 2)}%`;
  return pct(value * 100);
}

function recentHitRateText(status) {
  if (status?.recentHitRate != null) {
    return `${fmt(status.recentHitRate * 100, 1)}%（${status.recentHitSampleCount || 0} 筆已結算）`;
  }
  const pending = Number(status?.predictionPendingRows || 0);
  const completed = Number(status?.predictionCompletedRows || 0);
  return `等待結算（已結算 ${completed} 筆 / 等待 ${pending} 筆）`;
}

function backendActionText(action) {
  const value = String(action || "").toUpperCase();
  if (value === "BUY_CANDIDATE") return "買進候選";
  if (value === "WAIT_MARKET_RISK") return "大盤風險等待";
  if (value === "WAIT") return "等待";
  return action || "等待";
}

function modelProgressHtml(progress) {
  if (!progress?.stages?.length) {
    return `<div class="stage-list"><div class="stage-pill"><span>模型進步階段</span><strong>尚未訓練</strong></div></div>`;
  }
  const stageRows = progress.stages.map((stage) => `
    <div class="stage-pill ${stage.passed ? "passed" : "pending"}" title="${escapeHtml(`${stage.title}｜${stage.label}：${progressMetricText(stage)}`)}">
      <span>${stage.stage}</span>
      <strong>${stage.passed ? "已達標" : "未達標"}</strong>
      <small>${escapeHtml(stage.title)}</small>
    </div>
  `).join("");
  return `
    <div class="stage-list">${stageRows}</div>
  `;
}

function backendStatusHtml(status, model, extraModelNames) {
  const metrics = model?.metrics || {};
  const modelNames = extraModelNames || "尚未訓練";
  const modelCount = modelNames === "尚未訓練" ? 0 : modelNames.split("、").filter(Boolean).length;
  const modelSummary = modelCount ? `${modelCount} 個模型已啟用` : modelNames;
  const targetLabel = model?.targetType === "short-profit-net-v1"
    ? "3／5／10 日短期淨獲利"
    : (model?.targetType || "等待重新訓練");
  return `
    <div class="backend-status-dashboard">
      <section class="status-card status-card-wide">
        <h3>模型狀態</h3>
        <div class="status-inline-list">
          <span>模型檔<strong>${status.modelExists ? "已保存" : "尚未建立"}</strong></span>
          <span>最近學習<strong>${model?.trainedAt || "尚未訓練"}</strong></span>
          <span>買進門檻<strong>${status.adaptiveThreshold == null ? "N/A" : fmt(status.adaptiveThreshold * 100, 1) + "%"}</strong></span>
          <span>學習目標<strong>${escapeHtml(targetLabel)}</strong></span>
          <span>資料<strong>${status.priceRows ?? 0} 筆價量 / ${status.predictionRows ?? 0} 筆判斷 / ${status.tradeRows ?? 0} 筆交易</strong></span>
        </div>
        <p class="status-note compact-line" title="${escapeHtml(modelNames)}">啟用模型：${modelSummary}${model?.policyHash ? `｜策略 ${escapeHtml(model.policyHash)}` : ""}</p>
      </section>
      <section class="status-card">
        <h3>訊號結算</h3>
        <div class="status-inline-list">
          <span>命中率<strong>${recentHitRateText(status)}</strong></span>
          <span>結算<strong>${status.predictionCompletedRows || 0} / ${status.predictionPendingRows || 0}</strong></span>
        </div>
        <p class="status-note">${status.recentHitRule || "3／5／10 日短期淨報酬成熟後回補結果"}</p>
      </section>
      <section class="status-card">
        <h3>績效指標</h3>
        <div class="status-inline-list">
          <span>Precision<strong>${metrics.precision == null ? "N/A" : fmt(metrics.precision * 100, 1) + "%"}</strong></span>
          <span>平均<strong>${metrics.averageTradeReturn == null ? "N/A" : pct(metrics.averageTradeReturn * 100)}</strong></span>
          <span>PF<strong>${metrics.profitFactor == null ? "N/A" : fmt(metrics.profitFactor, 2)}</strong></span>
          <span>回撤<strong>${metrics.maxDrawdown == null ? "N/A" : pct(metrics.maxDrawdown * 100)}</strong></span>
        </div>
      </section>
      <section class="status-card status-card-stage">
        <h3>階段目標</h3>
        <div class="status-inline-list">
          <span>目前<strong>第 ${status.modelProgress?.currentStage || model?.progress?.currentStage || 1} 階段</strong></span>
          <span>下一目標<strong>${status.modelProgress?.nextGoal?.title || model?.progress?.nextGoal?.title || "持續觀察"}</strong></span>
        </div>
        ${modelProgressHtml(status.modelProgress || model?.progress)}
      </section>
    </div>
  `;
}

function systemHealthHtml(health) {
  const packages = Object.values(health?.packages || {});
  const prediction = health?.predictionTest?.prediction;
  const probability = Number(prediction?.probability);
  const packageRows = packages.length
    ? packages.map((item) => `
      <tr>
        <td>${escapeHtml(item.package)}</td>
        <td>${escapeHtml(item.installed || "缺少")}</td>
        <td>${escapeHtml(item.expected || "")}</td>
        <td class="${item.ok ? "up" : "down"}">${item.ok ? "正常" : item.importOk ? "版本不符" : "不可用"}</td>
      </tr>
    `).join("")
    : `<tr><td colspan="4">沒有套件檢查結果</td></tr>`;
  const errors = (health?.errors || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  return `
    <strong>${health?.ok ? "獨立模型可用" : "獨立模型不可用；正式規則分析不受影響"}</strong>
    <div class="backend-status system-health-grid">
      <div class="plan-item"><span>檢查時間</span><strong>${escapeHtml(health?.checkedAt || "")}</strong></div>
      <div class="plan-item"><span>Python 路徑</span><strong>${escapeHtml(health?.python?.executable || "未知")}</strong></div>
      <div class="plan-item"><span>Python 版本</span><strong>${escapeHtml(health?.python?.version || "未知")}</strong></div>
      <div class="plan-item"><span>model.pkl</span><strong>${health?.model?.loadOk ? "可載入" : "不可載入"}</strong></div>
      <div class="plan-item"><span>模型版本</span><strong>${escapeHtml(health?.model?.version || "無")}</strong></div>
      <div class="plan-item"><span>正式預測測試</span><strong>${health?.predictionTest?.ok ? `${escapeHtml(health.predictionTest.symbol)} / ${fmt(probability * 100, 1)}%` : "失敗"}</strong></div>
    </div>
    ${errors ? `<div class="explanation-card down"><strong>失敗原因</strong><ul>${errors}</ul></div>` : ""}
    <div class="feature-table-wrap system-health-table-wrap">
      <table>
        <thead><tr><th>套件</th><th>目前版本</th><th>要求版本</th><th>狀態</th></tr></thead>
        <tbody>${packageRows}</tbody>
      </table>
    </div>
  `;
}

const DAILY_SYSTEM_HEALTH_CHECK_KEY = "stockSystemDailyHealthCheckDate";

function todayDateKey() {
  // 明確用台北時區，不依賴執行環境系統時區——這是本機桌面應用，目前系統
  // 時區本來就是台灣，但跟專案其他業務日期判斷(server.py taipei_localtime/
  // ml_backend.py today_key)一樣明確寫死，不留隱含假設。
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Taipei", year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(new Date());
  const lookup = Object.fromEntries(parts.map((p) => [p.type, p.value]));
  return `${lookup.year}-${lookup.month}-${lookup.day}`;
}

function runDailyFirstSystemHealthCheck() {
  const today = todayDateKey();
  const markDone = () => {
    try { localStorage.setItem(DAILY_SYSTEM_HEALTH_CHECK_KEY, today); } catch {}
  };
  try {
    if (localStorage.getItem(DAILY_SYSTEM_HEALTH_CHECK_KEY) === today) return;
  } catch {
    // localStorage 不可用(隱私模式等)：仍然每次都跑一次檢查，不記錄狀態。
  }
  // 標記「今天已檢查」延後到檢查真的執行完(成功或失敗都算)才寫入：舊版
  // 在排程 setTimeout 之前就先寫入，若使用者在 1.2 秒內透過側邊欄連結
  // (非SPA、直接卸載頁面)離開，setTimeout 永遠不會執行，但標記已永久
  // 寫入，導致當天不會再自動觸發——這是靜默失效。改成 finally 才標記，
  // 沒跑完的話下次進來這頁還會再試一次。
  setTimeout(() => {
    Promise.resolve(runSystemHealthCheck({ dailyStartup: true, skipBackendReload: true })).finally(markDone);
  }, 1200);
}

async function runSystemHealthCheck(options = {}) {
  const button = document.getElementById("systemHealthCheck");
  const result = document.getElementById("systemHealthResult");
  if (button) {
    button.disabled = true;
    button.textContent = options.dailyStartup ? "每日首次檢查中" : "檢查中";
  }
  if (result) {
    result.className = "explanation-card";
    result.innerHTML = "<strong>系統健康檢查</strong>正在檢查正式模型、Python 與套件版本。";
  }
  try {
    const response = await fetch("/api/system/health");
    const payload = await response.json();
    if (result) {
      result.className = `explanation-card ${payload.ok ? "up" : "down"}`;
      result.innerHTML = systemHealthHtml(payload);
    }
    if (!options.skipBackendReload) await loadBackendStatus({ quiet: true });
  } catch (error) {
    if (result) {
      result.className = "explanation-card down";
      result.innerHTML = `<strong>獨立模型檢查失敗；正式規則分析不受影響</strong>${escapeHtml(friendlyError(error))}`;
    }
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "系統健康檢查";
    }
  }
}

function setSelectValue(selectId, value) {
  const select = document.getElementById(selectId);
  if (!select) return;
  const next = value || select.options[0]?.value || "";
  if (![...select.options].some((option) => option.value === next)) {
    const option = document.createElement("option");
    option.value = next;
    option.textContent = `${next}（目前自訂）`;
    select.appendChild(option);
  }
  select.value = next;
}

function renderFinmindPlanStatus(plan) {
  const box = document.getElementById("finmindPlanStatus");
  if (!box) return;
  if (!plan) {
    box.innerHTML = `<strong>FinMind Sponsor 會員</strong>等待後端額度資料。`;
    return;
  }
  const numberOrZero = (value) => Number.isFinite(Number(value)) ? Number(value) : 0;
  const hardLimit = numberOrZero(plan.hourlyLimit || plan.hardLimit || 6000);
  const safeLimit = numberOrZero(plan.safeLimit || 5000);
  const reserved = numberOrZero(plan.reserved || Math.max(hardLimit - safeLimit, 0));
  const localCalls = numberOrZero(plan.calls);
  const officialCalls = Number.isFinite(Number(plan.officialCalls)) ? Number(plan.officialCalls) : null;
  const usageBase = safeLimit || hardLimit || 1;
  const usagePct = Math.max(0, Math.min(100, (localCalls / usageBase) * 100));
  const official = plan.official || {};
  const officialText = official.ok
    ? `官方 user_info 已讀取${official.cached ? "（快取）" : ""}`
    : `官方 user_info 未讀取${official.error ? `：${official.error}` : ""}`;
  const blockedText = plan.blocked ? `已暫停：${plan.lastError || "FinMind 額度保護"}` : "額度正常";
  box.innerHTML = `
    <strong>FinMind Sponsor 會員</strong>
    <div class="finmind-quota-grid">
      <span>官方上限</span><b>${fmt(hardLimit, 0)} 次/小時</b>
      <span>系統安全上限</span><b>${fmt(safeLimit, 0)} 次/小時</b>
      <span>預留額度</span><b>${fmt(reserved, 0)} 次</b>
      <span>本地已用</span><b>${fmt(localCalls, 0)} 次</b>
      <span>官方已用</span><b>${officialCalls == null ? "未讀取" : `${fmt(officialCalls, 0)} 次`}</b>
      <span>狀態</span><b>${escapeHtml(blockedText)}</b>
    </div>
    <div class="finmind-quota-bar"><span style="width:${usagePct.toFixed(1)}%"></span></div>
    <small>${escapeHtml(officialText)}；${escapeHtml(plan.note || "Sponsor 會員額度保護中")}</small>
  `;
}

async function loadTokenStatus() {
  const badge = document.getElementById("tokenStatus");
  try {
    const response = await fetch("/api/settings/finmind-token");
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "Token 狀態讀取失敗");
    badge.textContent = payload.configured ? `已設定 ${payload.masked}` : "尚未設定";
    badge.style.background = payload.configured ? "#e7f8ef" : "#fff4e6";
    badge.style.color = payload.configured ? "#16a34a" : "#d97706";
    renderFinmindPlanStatus(payload.membership);
  } catch (error) {
    badge.textContent = friendlyError(error);
    badge.style.background = "#fff4e6";
    badge.style.color = "#d97706";
    renderFinmindPlanStatus(null);
  }
}

async function saveToken() {
  const input = document.getElementById("finmindTokenInput");
  const badge = document.getElementById("tokenStatus");
  const token = input.value.trim();
  badge.textContent = "儲存中";
  badge.style.background = "#eef4fb";
  badge.style.color = "#2563eb";
  try {
    const response = await fetch("/api/settings/finmind-token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token })
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "儲存失敗");
    input.value = "";
    await loadTokenStatus();
  } catch (error) {
    badge.textContent = friendlyError(error);
    badge.style.background = "#fff4e6";
    badge.style.color = "#d97706";
  }
}

async function clearToken() {
  const badge = document.getElementById("tokenStatus");
  const input = document.getElementById("finmindTokenInput");
  badge.textContent = "清除中";
  badge.style.background = "#eef4fb";
  badge.style.color = "#2563eb";
  try {
    await fetch("/api/settings/finmind-token", { method: "DELETE" });
    // 清除前若使用者手動打了字還沒儲存，欄位裡的機密字串要一併清掉，
    // 否則畫面上仍留著明文(截圖分享/交接電腦時的洩漏風險)。
    if (input) input.value = "";
    // Token都清掉了，畫面上不該還留著清除前「測試成功/讀到N筆」的舊
    // 測試結果文字，會誤導使用者以為FinMind功能仍正常。比照
    // clearSinopacSettings()/clearAiSettings()都會清測試結果的作法。
    const testResult = document.getElementById("finmindTestResult");
    if (testResult) testResult.innerHTML = "";
    await loadTokenStatus();
  } catch (error) {
    badge.textContent = friendlyError(error);
    badge.style.background = "#fff4e6";
    badge.style.color = "#d97706";
  }
}

async function testFinmindToken() {
  const result = document.getElementById("finmindTestResult");
  result.innerHTML = `<strong>FinMind 測試</strong>連線測試中...`;
  try {
    const response = await fetch("/api/settings/finmind-token/test");
    const payload = await response.json();
    if (!response.ok || !payload.ok || !payload.usable) {
      throw new Error(payload.error || "FinMind Token 無法使用");
    }
    result.innerHTML = `
      <strong>FinMind 測試成功</strong>
      ${payload.source || "FinMind"} / ${payload.symbol || "2330.TW"}，
      讀到 ${payload.rows || 0} 筆，最新日期 ${payload.latestDate || "N/A"}，
      最新收盤 ${payload.latestClose == null ? "N/A" : fmt(payload.latestClose, 2)}。
    `;
    renderFinmindPlanStatus(payload.membership);
  } catch (error) {
    result.innerHTML = `<strong>FinMind 測試失敗</strong>${escapeHtml(friendlyError(error))}`;
  }
}
async function loadSinopacStatus() {
  const badge = document.getElementById("sinopacStatus");
  try {
    const response = await fetch("/api/sinopac/status");
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "永豐 API 狀態讀取失敗");
    badge.textContent = payload.configured
      ? `已設定 ${payload.apiKeyMasked}${payload.caConfigured ? " / CA 已設定" : " / CA 未設定"}`
      : "尚未設定";
    badge.style.background = payload.configured ? "#e7f8ef" : "#fff4e6";
    badge.style.color = payload.configured ? "#16a34a" : "#d97706";
    document.getElementById("sinopacSimulationInput").checked = Boolean(payload.simulation);
    const caPathInput = document.getElementById("sinopacCaPathInput");
    if (caPathInput) caPathInput.placeholder = payload.caPathLabel ? `已設定：${payload.caPathLabel}` : "例如 C:\\Sinopac\\Sinopac.pfx";
  } catch (error) {
    badge.textContent = friendlyError(error);
    badge.style.background = "#fff4e6";
    badge.style.color = "#d97706";
  }
}

async function saveSinopacSettings() {
  const badge = document.getElementById("sinopacStatus");
  badge.textContent = "儲存中";
  badge.style.background = "#eef4fb";
  badge.style.color = "#2563eb";
  try {
    const response = await fetch("/api/sinopac/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        apiKey: document.getElementById("sinopacApiKeyInput").value.trim(),
        secretKey: document.getElementById("sinopacSecretKeyInput").value.trim(),
        simulation: document.getElementById("sinopacSimulationInput").checked,
        caPath: document.getElementById("sinopacCaPathInput").value.trim(),
        caPassword: document.getElementById("sinopacCaPasswordInput").value.trim(),
        personId: document.getElementById("sinopacPersonIdInput").value.trim()
      })
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "永豐 API 儲存失敗");
    document.getElementById("sinopacApiKeyInput").value = "";
    document.getElementById("sinopacSecretKeyInput").value = "";
    document.getElementById("sinopacCaPathInput").value = "";
    document.getElementById("sinopacCaPasswordInput").value = "";
    document.getElementById("sinopacPersonIdInput").value = "";
    // 儲存新憑證後，畫面上不該還留著舊憑證的「測試通過」徽章——使用者可能
    // 誤以為新設定已經驗證過，實際上新憑證還沒被 runSinopacFullTest 驗證。
    // 比照 clearSinopacSettings() 的重置寫法。
    const testStatus = document.getElementById("sinopacTestStatus");
    if (testStatus) testStatus.textContent = "尚未測試";
    await loadSinopacStatus();
  } catch (error) {
    badge.textContent = friendlyError(error);
    badge.style.background = "#fff4e6";
    badge.style.color = "#d97706";
  }
}

async function clearSinopacSettings() {
  const badge = document.getElementById("sinopacStatus");
  badge.textContent = "清除中";
  badge.style.background = "#eef4fb";
  badge.style.color = "#2563eb";
  try {
    await fetch("/api/sinopac/settings", { method: "DELETE" });
    document.getElementById("sinopacHoldingsResult").innerHTML = "";
    // 清乾淨要包含：測試結果摘要(不清的話畫面殘留清除前『測試通過/CA已設定』
    // 的舊文字，讓人誤以為憑證仍生效)，以及使用者清除前若手動打字但沒儲存
    // 的機密輸入框(明文殘留在DOM的洩漏風險)。
    const fullTestResult = document.getElementById("sinopacFullTestResult");
    if (fullTestResult) fullTestResult.innerHTML = "";
    const testStatus = document.getElementById("sinopacTestStatus");
    if (testStatus) testStatus.textContent = "尚未測試";
    ["sinopacApiKeyInput", "sinopacSecretKeyInput", "sinopacCaPathInput", "sinopacCaPasswordInput", "sinopacPersonIdInput"]
      .forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.value = "";
      });
    await loadSinopacStatus();
  } catch (error) {
    badge.textContent = friendlyError(error);
    badge.style.background = "#fff4e6";
    badge.style.color = "#d97706";
  }
}

function setCapitalBadge(badge, text, tone = "warn") {
  badge.textContent = text;
  badge.style.background = tone === "ok" ? "#e7f8ef" : tone === "busy" ? "#eef4fb" : "#fff4e6";
  badge.style.color = tone === "ok" ? "#16a34a" : tone === "busy" ? "#2563eb" : "#d97706";
}

async function loadCapitalStatus() {
  const badge = document.getElementById("capitalStatus");
  try {
    const response = await fetch("/api/capital/status");
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "群益狀態讀取失敗");
    if (!payload.comReady) {
      setCapitalBadge(badge, "COM 元件不可用");
    } else if (!payload.configured) {
      setCapitalBadge(badge, "COM 可用 / 尚未設定");
    } else if (payload.readyForFailover) {
      setCapitalBadge(badge, "報價備援已驗證", "ok");
    } else if (payload.quoteVerified) {
      setCapitalBadge(badge, "實際報價已驗證 / 待啟用", "ok");
    } else {
      setCapitalBadge(badge, `已設定 ${payload.userIdMasked || ""} / ${payload.accountNoMasked || ""} / 待測試`);
    }
  } catch (error) {
    setCapitalBadge(badge, "讀取失敗");
  }
}

async function saveCapitalSettings() {
  const badge = document.getElementById("capitalStatus");
  setCapitalBadge(badge, "儲存中", "busy");
  try {
    const response = await fetch("/api/capital/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        userId: document.getElementById("capitalUserIdInput").value.trim(),
        password: document.getElementById("capitalPasswordInput").value.trim(),
        accountNo: document.getElementById("capitalAccountNoInput").value.trim(),
      }),
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "群益設定儲存失敗");
    document.getElementById("capitalUserIdInput").value = "";
    document.getElementById("capitalPasswordInput").value = "";
    document.getElementById("capitalAccountNoInput").value = "";
    document.getElementById("capitalTestResult").innerHTML = "";
    await loadCapitalStatus();
  } catch (error) {
    setCapitalBadge(badge, friendlyError(error));
  }
}

async function clearCapitalSettings() {
  const badge = document.getElementById("capitalStatus");
  setCapitalBadge(badge, "清除中", "busy");
  try {
    const response = await fetch("/api/capital/settings", { method: "DELETE" });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "群益設定清除失敗");
    ["capitalUserIdInput", "capitalPasswordInput", "capitalAccountNoInput"].forEach((id) => { document.getElementById(id).value = ""; });
    document.getElementById("capitalTestResult").innerHTML = "";
    await loadCapitalStatus();
  } catch (error) {
    setCapitalBadge(badge, friendlyError(error));
  }
}

async function testCapitalConnection() {
  const result = document.getElementById("capitalTestResult");
  result.className = "explanation-card";
  result.innerHTML = "<strong>群益報價測試</strong>正在登入、訂閱並驗證 2330 實際報價...";
  try {
    const response = await fetch("/api/capital/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const payload = await response.json();
    if (!response.ok || !payload.ok || !payload.usable) throw new Error(payload.error || payload.loginMessage || payload.quoteConnectionMessage || "群益報價不可用");
    result.className = "explanation-card up";
    const quoteName = payload.stockName ? `${escapeHtml(payload.stockName)} ` : "";
    result.innerHTML = `<strong>群益實際報價測試通過</strong>證券帳號 ${escapeHtml(payload.accountNoMasked)}｜${quoteName}${escapeHtml(payload.symbol)}｜價格 ${escapeHtml(payload.price)}｜成交量 ${escapeHtml(payload.totalVolume)}｜報價時間 ${escapeHtml(payload.quoteTimestamp)}。永豐報價失敗或缺漏時，系統會自動使用通過新鮮度檢查的群益報價。`;
    await loadCapitalStatus();
  } catch (error) {
    result.className = "explanation-card down";
    result.innerHTML = `<strong>群益報價測試失敗</strong>${escapeHtml(friendlyError(error))}`;
    await loadCapitalStatus();
  }
}

async function loadAiSettingsStatus() {
  const badge = document.getElementById("aiApiStatus");
  try {
    const response = await fetch("/api/settings/ai");
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "AI 設定讀取失敗");
    const openaiText = payload.openaiConfigured ? `OpenAI ${payload.openaiMasked}` : "OpenAI 未設定";
    const perplexityText = payload.perplexityConfigured ? `Perplexity ${payload.perplexityMasked}` : "Perplexity 未設定";
    badge.textContent = `${openaiText} / ${perplexityText}`;
    badge.style.background = payload.openaiConfigured || payload.perplexityConfigured ? "#e7f8ef" : "#fff4e6";
    badge.style.color = payload.openaiConfigured || payload.perplexityConfigured ? "#16a34a" : "#d97706";
    document.getElementById("enableAiAnalysisInput").checked = Boolean(payload.enableAi);
    document.getElementById("enableNewsSummaryInput").checked = Boolean(payload.enableNews);
    setSelectValue("openaiModelInput", payload.openaiModel || "gpt-5.4-mini");
    setSelectValue("perplexityModelInput", payload.perplexityModel || "sonar");
  } catch (error) {
    badge.textContent = friendlyError(error);
    badge.style.background = "#fff4e6";
    badge.style.color = "#d97706";
  }
}

async function saveAiSettings() {
  const badge = document.getElementById("aiApiStatus");
  badge.textContent = "儲存中";
  badge.style.background = "#eef4fb";
  badge.style.color = "#2563eb";
  try {
    const response = await fetch("/api/settings/ai", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        openaiApiKey: document.getElementById("openaiApiKeyInput").value.trim(),
        perplexityApiKey: document.getElementById("perplexityApiKeyInput").value.trim(),
        enableAi: document.getElementById("enableAiAnalysisInput").checked,
        enableNews: document.getElementById("enableNewsSummaryInput").checked,
        openaiModel: document.getElementById("openaiModelInput").value.trim(),
        perplexityModel: document.getElementById("perplexityModelInput").value.trim()
      })
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "AI 設定儲存失敗");
    document.getElementById("openaiApiKeyInput").value = "";
    document.getElementById("perplexityApiKeyInput").value = "";
    await loadAiSettingsStatus();
  } catch (error) {
    badge.textContent = friendlyError(error);
    badge.style.background = "#fff4e6";
    badge.style.color = "#d97706";
  }
}

async function clearAiSettings() {
  const badge = document.getElementById("aiApiStatus");
  badge.textContent = "清除中";
  badge.style.background = "#eef4fb";
  badge.style.color = "#2563eb";
  try {
    await fetch("/api/settings/ai", { method: "DELETE" });
    document.getElementById("openaiApiKeyInput").value = "";
    document.getElementById("perplexityApiKeyInput").value = "";
    // 清乾淨要包含測試結果摘要，不清的話畫面殘留清除前『OpenAI 可用』的
    // 舊文字，讓人誤以為 Key 仍生效。
    const testResult = document.getElementById("aiTestResult");
    if (testResult) testResult.innerHTML = "";
    await loadAiSettingsStatus();
  } catch (error) {
    badge.textContent = friendlyError(error);
    badge.style.background = "#fff4e6";
    badge.style.color = "#d97706";
  }
}

function renderAiTestLine(label, item) {
  const state = item?.usable ? "可用" : item?.enabled === false ? "已停用" : "不可用";
  // item.message/error 是 OpenAI/Perplexity 回覆的自由文字(間接外部可控)，
  // 跟同檔案其他插入外部/伺服器文字的地方(systemHealthHtml/
  // sinopacTestResultHtml等)一樣要 escapeHtml，避免理論上的 DOM XSS。
  const detail = escapeHtml(item?.message || item?.error || "沒有回傳測試結果");
  const model = item?.model ? ` / ${escapeHtml(item.model)}` : "";
  return `<div class="plan-item"><span>${escapeHtml(label)}${model}</span><strong>${state}</strong><small>${detail}</small></div>`;
}

async function testAiSettings() {
  const result = document.getElementById("aiTestResult");
  result.innerHTML = `<strong>AI 連線測試</strong>測試中，會各送出一次最小請求...`;
  try {
    const response = await fetch("/api/settings/ai/test");
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "AI 連線測試失敗");
    result.innerHTML = `
      <strong>${payload.usable ? "AI 連線測試完成" : "AI 連線目前不可用"}</strong>
      ${renderAiTestLine("OpenAI", payload.openai)}
      ${renderAiTestLine("Perplexity", payload.perplexity)}
      <small>測試時間：${payload.checkedAt || "N/A"}</small>
    `;
  } catch (error) {
    result.innerHTML = `<strong>AI 連線測試失敗</strong>${escapeHtml(friendlyError(error))}`;
  }
}

async function loadLineSettingsStatus() {
  const badge = document.getElementById("lineApiStatus");
  try {
    const response = await fetch("/api/settings/line");
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "LINE 設定讀取失敗");
    const consecutiveFailures = Number(payload.consecutiveFailures || 0);
    // LINE 推播是無狀態的：連續失敗(網路不穩/API限流)之前完全沒有跨呼叫
    // 記憶，使用者不在電腦前時不會知道通知其實一直送不出去。這裡把後端
    // 持久化的連續失敗次數顯示出來，下次來設定頁至少能看到。
    if (payload.configured && consecutiveFailures >= 3) {
      // 徽章(.signal-badge)是設計給「已設定 abcd...wxyz」這種短文字用的藥丸
      // 樣式，原始英文技術錯誤訊息(最長500字元)直接塞進去會撐版、非技術
      // 使用者也看不懂。徽章只顯示簡短摘要，完整錯誤訊息放 title 屬性，
      // 滑鼠移過去才看得到，不擠壓版面。
      badge.textContent = `連續失敗 ${consecutiveFailures} 次`;
      badge.title = payload.lastFailureError || "未知錯誤";
      badge.style.background = "#fff4e6";
      badge.style.color = "#d97706";
    } else {
      // LINE免費方案一個月只有200則推播額度，把本月已用量直接顯示在徽章
      // 上(這是本系統自己送出的計數，若同一channel有其他來源會偏低)。
      // 用到警示門檻(160則)就轉成橘色提醒，留緩衝給賣出提醒等關鍵通知。
      const quota = payload.quota || null;
      const quotaText = quota ? `｜本月 ${quota.sent}/${quota.limit} 則` : "";
      badge.textContent = payload.configured ? `已設定 ${payload.targetMasked}${quotaText}` : "尚未設定";
      badge.removeAttribute("title");
      if (payload.configured && quota && quota.warn) {
        badge.title = `本月LINE推播已用 ${quota.sent} 則(上限 ${quota.limit})，接近上限，建議保留額度給賣出提醒`;
        badge.style.background = "#fff4e6";
        badge.style.color = "#d97706";
      } else {
        badge.style.background = payload.configured ? "#e7f8ef" : "#fff4e6";
        badge.style.color = payload.configured ? "#16a34a" : "#d97706";
      }
    }
    document.getElementById("enableLineAlertsInput").checked = Boolean(payload.enabled);
  } catch (error) {
    badge.textContent = friendlyError(error);
    badge.style.background = "#fff4e6";
    badge.style.color = "#d97706";
  }
}

async function saveLineSettings() {
  const badge = document.getElementById("lineApiStatus");
  badge.textContent = "儲存中";
  badge.style.background = "#eef4fb";
  badge.style.color = "#2563eb";
  try {
    const response = await fetch("/api/settings/line", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        channelAccessToken: document.getElementById("lineChannelTokenInput").value.trim(),
        targetId: document.getElementById("lineTargetIdInput").value.trim(),
        enabled: document.getElementById("enableLineAlertsInput").checked
      })
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "LINE 設定儲存失敗");
    document.getElementById("lineChannelTokenInput").value = "";
    document.getElementById("lineTargetIdInput").value = "";
    await loadLineSettingsStatus();
  } catch (error) {
    badge.textContent = friendlyError(error);
    badge.style.background = "#fff4e6";
    badge.style.color = "#d97706";
  }
}

async function clearLineSettings() {
  const badge = document.getElementById("lineApiStatus");
  badge.textContent = "清除中";
  badge.style.background = "#eef4fb";
  badge.style.color = "#2563eb";
  try {
    await fetch("/api/settings/line", { method: "DELETE" });
    document.getElementById("lineChannelTokenInput").value = "";
    document.getElementById("lineTargetIdInput").value = "";
    document.getElementById("lineTestResult").innerHTML = "";
    await loadLineSettingsStatus();
  } catch (error) {
    badge.textContent = friendlyError(error);
    badge.style.background = "#fff4e6";
    badge.style.color = "#d97706";
  }
}

async function testLineSettings() {
  const result = document.getElementById("lineTestResult");
  result.innerHTML = `<strong>LINE 測試</strong>正在送出測試訊息...`;
  try {
    const response = await fetch("/api/settings/line/test");
    const payload = await response.json();
    if (!response.ok || !payload.ok || !payload.sent) throw new Error(payload.error || "LINE 測試訊息未送出");
    result.innerHTML = `<strong>LINE 測試成功</strong>已送到 ${payload.targetMasked || "設定的 LINE 目標"}。`;
  } catch (error) {
    result.innerHTML = `<strong>LINE 測試失敗</strong>${escapeHtml(friendlyError(error))}`;
  }
}
async function testSinopacHoldings() {
  const result = document.getElementById("sinopacHoldingsResult");
  result.innerHTML = `<strong>永豐庫存</strong>讀取中...`;
  try {
    const response = await fetch("/api/sinopac/holdings");
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "永豐庫存讀取失敗");
    result.innerHTML = `
      <strong>永豐庫存</strong>
      帳號：${payload.accountMasked}，模式：${payload.simulation ? "模擬" : "正式"}，庫存 ${payload.count} 檔。<br>
      ${payload.holdings.length ? payload.holdings.map((item) => `${item.code} / ${item.quantity} 股`).join("、") : "目前沒有庫存。"}
    `;
  } catch (error) {
    result.innerHTML = `<strong>永豐庫存讀取失敗</strong>${escapeHtml(friendlyError(error))}`;
  }
}

function sinopacTestStateText(state) {
  if (state === "pass") return "通過";
  if (state === "warn") return "注意";
  if (state === "fail") return "失敗";
  if (state === "skip") return "略過";
  return "未知";
}

function sinopacTestResultHtml(payload) {
  const summary = payload?.summary || {};
  const hasWarn = Number(summary.warn || 0) > 0;
  const rows = (payload?.results || []).map((item) => `
    <tr>
      <td>${escapeHtml(item.label || item.key || "-")}</td>
      <td class="sinopac-test-${escapeHtml(item.state || "skip")}">${sinopacTestStateText(item.state)}</td>
      <td>${escapeHtml(item.message || "-")}</td>
    </tr>
  `).join("");
  const sampleSymbols = (payload?.sampleSymbols || []).join(", ") || "-";
  const status = payload?.status || {};
  return `
    <strong>${payload?.ok ? (hasWarn ? "永豐 API 測試有注意事項" : "永豐 API 完整測試完成") : "永豐 API 測試有異常"}</strong>
    <div class="backend-status system-health-grid sinopac-test-summary">
      <div class="plan-item"><span>測試時間</span><strong>${escapeHtml(payload?.checkedAt || "-")}</strong></div>
      <div class="plan-item"><span>API 設定</span><strong>${status.configured ? "已設定" : "未設定"}</strong></div>
      <div class="plan-item"><span>CA 憑證</span><strong>${status.caConfigured ? "已設定" : "未設定"}</strong></div>
      <div class="plan-item"><span>模式</span><strong>${status.simulation ? "模擬" : "正式"}</strong></div>
      <div class="plan-item"><span>測試股票</span><strong>${escapeHtml(sampleSymbols)}</strong></div>
      <div class="plan-item"><span>結果</span><strong>${summary.pass || 0} 通過 / ${summary.warn || 0} 注意 / ${summary.fail || 0} 失敗 / ${summary.skip || 0} 略過</strong></div>
    </div>
    <div class="feature-table-wrap system-health-table-wrap">
      <table>
        <thead><tr><th>測試項目</th><th>狀態</th><th>結果說明</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="3">尚無測試結果</td></tr>`}</tbody>
      </table>
    </div>
    <p class="settings-note">${escapeHtml(payload?.safetyNote || "此測試不會送出正式委託。")}</p>
  `;
}

async function runSinopacFullTest() {
  const result = document.getElementById("sinopacFullTestResult");
  const badge = document.getElementById("sinopacTestStatus");
  const button = document.getElementById("runSinopacFullTest");
  const symbols = (document.getElementById("sinopacTestSymbolsInput")?.value || "")
    .split(/[,\s，、]+/)
    .map((item) => item.trim())
    .filter(Boolean);
  result.innerHTML = `<strong>永豐 API 完整測試</strong>測試中，可能需要登入永豐 API，請稍候...`;
  badge.textContent = "測試中";
  badge.style.background = "#eef4fb";
  badge.style.color = "#2563eb";
  if (button) button.disabled = true;
  try {
    const response = await fetch("/api/sinopac/test-suite", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbols })
    });
    const payload = await response.json();
    if (!response.ok || !payload) throw new Error(payload?.error || "永豐 API 完整測試失敗");
    result.innerHTML = sinopacTestResultHtml(payload);
    const hasWarn = Number(payload.summary?.warn || 0) > 0;
    badge.textContent = payload.ok ? (hasWarn ? "有注意" : "測試通過") : "有異常";
    badge.style.background = payload.ok && !hasWarn ? "#e7f8ef" : "#fff4e6";
    badge.style.color = payload.ok && !hasWarn ? "#16a34a" : "#d97706";
  } catch (error) {
    result.innerHTML = `<strong>永豐 API 完整測試失敗</strong>${escapeHtml(friendlyError(error))}`;
    badge.textContent = "測試失敗";
    badge.style.background = "#fff4e6";
    badge.style.color = "#d97706";
  } finally {
    if (button) button.disabled = false;
  }
}

function setAdvancedFlowBusy(active, label = "") {
  ["refreshAdvancedFlowStatus", "refreshHoldingAdvancedFlow", "refreshCandidateAdvancedFlow", "startRealtimeFlow"].forEach((id) => {
    const button = document.getElementById(id);
    if (button) button.disabled = Boolean(active);
  });
  const badge = document.getElementById("advancedFlowStatus");
  if (badge && active) {
    badge.textContent = label || "處理中";
    badge.style.background = "#eef4fb";
    badge.style.color = "#2563eb";
  }
}

function advancedFlowResultHtml(payload) {
  const status = payload?.status || payload || {};
  const coverage = status.coverage || {};
  const staging = status.staging || {};
  const collector = status.realtimeCollector || {};
  const notes = (status.notes || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  const samples = (status.samples || []).slice(0, 8).map((item) => `
    <tr>
      <td>${escapeHtml(item.symbol)}</td>
      <td>${escapeHtml(item.date || "-")}</td>
      <td>${item.brokerBranch == null ? "無資料" : fmt(item.brokerBranch, 0)}</td>
      <td>${item.realtimeMoneyFlow == null ? "無資料" : fmt(item.realtimeMoneyFlow, 0)}</td>
    </tr>
  `).join("");
  return `
    <strong>${payload?.message || "進階資金流狀態"}</strong>
    <div class="backend-status system-health-grid">
      <div class="plan-item"><span>檢查檔數</span><strong>${fmt(status.scopeSymbols || 0, 0)} 檔</strong></div>
      <div class="plan-item"><span>主力分點</span><strong>${fmt(coverage.brokerBranch || 0, 0)} 檔</strong></div>
      <div class="plan-item"><span>即時資金流</span><strong>${fmt(coverage.realtimeFlow || 0, 0)} 檔</strong></div>
      <div class="plan-item"><span>Tick collector</span><strong>${collector.running ? `執行中 PID ${collector.pid}` : "未執行"}</strong></div>
      <div class="plan-item"><span>即時暫存</span><strong>${fmt(staging.symbols || 0, 0)} 檔 / ${fmt(staging.ticks || 0, 0)} ticks</strong></div>
      <div class="plan-item"><span>最後即時更新</span><strong>${escapeHtml(staging.updatedAt || "尚無")}</strong></div>
    </div>
    ${notes ? `<ul class="settings-note">${notes}</ul>` : ""}
    <div class="feature-table-wrap system-health-table-wrap">
      <table>
        <thead><tr><th>股票</th><th>日期</th><th>主力分點</th><th>即時資金流</th></tr></thead>
        <tbody>${samples || `<tr><td colspan="4">尚無進階資金流樣本</td></tr>`}</tbody>
      </table>
    </div>
  `;
}

async function loadAdvancedFlowStatus(options = {}) {
  const badge = document.getElementById("advancedFlowStatus");
  const result = document.getElementById("advancedFlowResult");
  if (!options.quiet) setAdvancedFlowBusy("status", "讀取中");
  try {
    const response = await fetch("/api/advanced-flow/status?scope=holdings&maxSymbols=24");
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "進階資金流狀態讀取失敗");
    if (badge) {
      const covered = (payload.coverage?.brokerBranch || 0) + (payload.coverage?.realtimeFlow || 0);
      badge.textContent = covered ? `已有資料 ${covered} 筆` : "尚未補齊";
      badge.style.background = covered ? "#e7f8ef" : "#fff4e6";
      badge.style.color = covered ? "#16a34a" : "#d97706";
    }
    if (result) {
      result.className = "explanation-card";
      result.innerHTML = advancedFlowResultHtml(payload);
    }
  } catch (error) {
    if (badge) {
      badge.textContent = "讀取失敗";
      badge.style.background = "#fff4e6";
      badge.style.color = "#d97706";
    }
    if (result) {
      result.className = "explanation-card down";
      result.innerHTML = `<strong>進階資金流讀取失敗</strong>${escapeHtml(friendlyError(error))}`;
    }
  } finally {
    // quiet 模式一開始就沒有上鎖(818行)，這裡不能無條件解鎖——如果使用者
    // 在這次背景查詢完成前手動點擊「補齊持股資金流」等按鈕觸發了
    // refreshAdvancedFlow(它有自己上鎖)，這裡的 finally 執行時會把真正
    // 還在執行中的請求誤解鎖，讓使用者能重複觸發同一個真實外部呼叫。
    if (!options.quiet) setAdvancedFlowBusy(false);
  }
}

async function refreshAdvancedFlow(scope) {
  const result = document.getElementById("advancedFlowResult");
  setAdvancedFlowBusy(scope, scope === "holdings" ? "補持股中" : "補候選中");
  if (result) {
    result.className = "explanation-card";
    result.innerHTML = `<strong>補齊進階資金流</strong>正在讀取 FinMind 會員資料與已暫存的 Shioaji 即時資金流，請勿重複點擊。`;
  }
  try {
    const response = await fetch("/api/advanced-flow/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        scope,
        maxSymbols: scope === "holdings" ? 24 : 60,
        forceRefresh: true
      })
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "補齊進階資金流失敗");
    if (result) {
      result.className = "explanation-card up";
      result.innerHTML = advancedFlowResultHtml(payload);
    }
    await loadAdvancedFlowStatus({ quiet: true });
  } catch (error) {
    if (result) {
      result.className = "explanation-card down";
      result.innerHTML = `<strong>補齊進階資金流失敗</strong>${escapeHtml(friendlyError(error))}`;
    }
  } finally {
    setAdvancedFlowBusy(false);
  }
}

async function startRealtimeFlow() {
  const result = document.getElementById("advancedFlowResult");
  setAdvancedFlowBusy("tick", "啟動中");
  if (result) result.innerHTML = `<strong>啟動即時資金流</strong>正在啟動 Shioaji tick 收集器。`;
  try {
    const response = await fetch("/api/advanced-flow/start-tick", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ trigger: "settings-button" })
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "即時資金流啟動失敗");
    if (result) {
      result.className = "explanation-card up";
      result.innerHTML = `<strong>${payload.started ? "即時資金流已啟動" : "即時資金流已在執行"}</strong>${escapeHtml(payload.message || "")}`;
    }
    await loadAdvancedFlowStatus({ quiet: true });
  } catch (error) {
    if (result) {
      result.className = "explanation-card down";
      result.innerHTML = `<strong>即時資金流啟動失敗</strong>${escapeHtml(friendlyError(error))}`;
    }
  } finally {
    setAdvancedFlowBusy(false);
  }
}

async function loadBackendStatus(options = {}) {
  const statusBox = document.getElementById("backendStatus");
  const predictionsBody = document.getElementById("backendPredictions");
  if (!options.quiet) {
    setBackendBusy("refresh");
    backendResult("info", "狀態讀取中", "正在讀取正式模型、資料列數與最近判斷。");
    if (predictionsBody) predictionsBody.innerHTML = `<tr><td colspan="6">正在讀取系統判斷紀錄...</td></tr>`;
  }
  try {
    const [statusResponse, predictionsResponse] = await Promise.all([
      fetch("/api/ml/status"),
      fetch("/api/ml/predictions?limit=12")
    ]);
    const status = await statusResponse.json();
    const predictions = await predictionsResponse.json();
    if (!status.ok) throw new Error(status.error || "資料更新狀態讀取失敗");
    if (!predictionsResponse.ok || predictions.ok === false) throw new Error(predictions.error || "系統判斷紀錄讀取失敗");
    latestBackendStatus = status;
    const model = status.model;
    const extraModels = model?.extraModels;
    const extraModelNames = extraModels?.models?.length ? extraModels.models.join("、") : "尚未訓練";
    statusBox.classList.add("backend-status-full");
    statusBox.innerHTML = backendStatusHtml(status, model, extraModelNames);
    const rows = predictions.predictions || [];
    predictionsBody.innerHTML = rows.length
      ? rows.map((row) => `
        <tr>
          <td>${row.price_date}</td>
          <td>${row.symbol}</td>
          <td>${fmt(row.probability * 100, 1)}%</td>
          <td>${fmt(row.threshold * 100, 1)}%</td>
          <td>${backendActionText(row.action)}</td>
          <td>${row.hit == null ? "等待" : row.hit ? `達標 ${pct(row.outcome_return * 100)}` : `未達 ${pct(row.outcome_return * 100)}`}</td>
        </tr>
      `).join("")
      : `<tr><td colspan="6">尚無系統判斷紀錄</td></tr>`;
    if (!options.quiet) {
      const symbolCount = Array.isArray(model?.symbols) ? model.symbols.length : 0;
      backendResult("success", "狀態讀取完成", `正式模型 ${symbolCount || "無"} 檔，最近學習 ${model?.trainedAt || "尚未訓練"}。`);
    }
  } catch (error) {
    statusBox.innerHTML = `<div class="plan-item"><span>資料更新狀態</span><strong>${escapeHtml(friendlyError(error))}</strong></div>`;
    predictionsBody.innerHTML = `<tr><td colspan="6">無法讀取系統判斷紀錄</td></tr>`;
    if (!options.quiet) backendResult("error", "狀態讀取失敗", escapeHtml(friendlyError(error)));
  } finally {
    if (!options.quiet) setBackendBusy(null);
  }
}

async function runBackendUpdate() {
  setBackendBusy("update");
  backendResult("info", "正式資料更新中", "使用目前正式模型股票清單更新資料與重新學習，完成前請不要重複點擊。");
  try {
    const modelSymbols = Array.isArray(latestBackendStatus?.model?.symbols)
      ? latestBackendStatus.model.symbols.map((symbol) => String(symbol).replace(".TW", "").trim()).filter(Boolean)
      : [];
    const body = {
      scope: "formal-model",
      useCurrentModelSymbols: true,
    };
    if (modelSymbols.length >= 20) body.symbols = modelSymbols;
    const response = await fetch("/api/ml/update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "更新失敗");
    await loadBackendStatus({ quiet: true });
    const updatedCount = payload.updatedSymbolCount || payload.model?.symbols?.length || modelSymbols.length || "正式模型";
    const requestedText = payload.requestedSymbolCount ? `，送出 ${payload.requestedSymbolCount} 檔` : "";
    backendResult("success", "正式更新完成", `實際更新 ${updatedCount} 檔${requestedText}，已重新讀取最新模型狀態。`);
  } catch (error) {
    document.getElementById("backendStatus").innerHTML = `<div class="plan-item"><span>更新失敗</span><strong>${escapeHtml(friendlyError(error))}</strong></div>`;
    backendResult("error", "正式更新失敗", escapeHtml(friendlyError(error)));
  } finally {
    setBackendBusy(null);
  }
}

// save/clear 這幾對操作的是同一份機密憑證，快速連點「儲存」又「清除」
// (或反過來)會讓兩個 fetch 並發送出，最終畫面顯示的狀態由哪個 response
// 較晚 resolve 決定，不保證反映使用者最後一次操作的真實意圖——在處理
// API Key/CA憑證/身分證字號的頁面上這種畫面-後端不同步會誤導判斷。
// 用同一把鎖把每組 save+clear 按鈕在操作進行中一起鎖住，跟同檔案
// runSinopacFullTest/setAdvancedFlowBusy 既有的 disabled 保護慣例一致。
function withButtonLock(buttonIds, handler) {
  return async (...args) => {
    const buttons = buttonIds.map((id) => document.getElementById(id)).filter(Boolean);
    if (buttons.some((btn) => btn.disabled)) return;
    buttons.forEach((btn) => { btn.disabled = true; });
    try {
      await handler(...args);
    } finally {
      buttons.forEach((btn) => { btn.disabled = false; });
    }
  };
}

function initSettings() {
  applyTheme();
  initSidebarToggle();
  // test按鈕原本各自用獨立的單元素鎖，跟對應的save/clear鎖組是不同陣列，
  // withButtonLock()只檢查傳入陣列裡的按鈕是否disabled、不會跨陣列生效——
  // 使用者可以在「測試」的fetch還在等待時同時按下「儲存」或「清除」，
  // 兩個非同步操作各自完成，最終畫面內容由哪個response較晚resolve決定，
  // 不保證反映使用者最後一次操作的真實意圖。改成save/clear/test共用同一組
  // 鎖陣列，讓三個按鈕互斥。
  const finmindButtons = ["saveFinmindToken", "clearFinmindToken", "testFinmindToken"];
  document.getElementById("saveFinmindToken").addEventListener("click", withButtonLock(finmindButtons, saveToken));
  document.getElementById("clearFinmindToken").addEventListener("click", withButtonLock(finmindButtons, clearToken));
  document.getElementById("testFinmindToken").addEventListener("click", withButtonLock(finmindButtons, testFinmindToken));
  const sinopacButtons = ["saveSinopacSettings", "clearSinopacSettings", "testSinopacHoldings"];
  document.getElementById("saveSinopacSettings").addEventListener("click", withButtonLock(sinopacButtons, saveSinopacSettings));
  document.getElementById("clearSinopacSettings").addEventListener("click", withButtonLock(sinopacButtons, clearSinopacSettings));
  document.getElementById("testSinopacHoldings").addEventListener("click", withButtonLock(sinopacButtons, testSinopacHoldings));
  document.getElementById("runSinopacFullTest").addEventListener("click", runSinopacFullTest);
  const capitalButtons = ["saveCapitalSettings", "clearCapitalSettings", "testCapitalConnection"];
  document.getElementById("saveCapitalSettings").addEventListener("click", withButtonLock(capitalButtons, saveCapitalSettings));
  document.getElementById("clearCapitalSettings").addEventListener("click", withButtonLock(capitalButtons, clearCapitalSettings));
  document.getElementById("testCapitalConnection").addEventListener("click", withButtonLock(capitalButtons, testCapitalConnection));
  document.getElementById("refreshAdvancedFlowStatus").addEventListener("click", () => loadAdvancedFlowStatus());
  document.getElementById("refreshHoldingAdvancedFlow").addEventListener("click", () => refreshAdvancedFlow("holdings"));
  document.getElementById("refreshCandidateAdvancedFlow").addEventListener("click", () => refreshAdvancedFlow("candidates"));
  document.getElementById("startRealtimeFlow").addEventListener("click", startRealtimeFlow);
  const aiButtons = ["saveAiSettings", "clearAiSettings", "testAiSettings"];
  document.getElementById("saveAiSettings").addEventListener("click", withButtonLock(aiButtons, saveAiSettings));
  document.getElementById("clearAiSettings").addEventListener("click", withButtonLock(aiButtons, clearAiSettings));
  document.getElementById("testAiSettings").addEventListener("click", withButtonLock(aiButtons, testAiSettings));
  const lineButtons = ["saveLineSettings", "clearLineSettings", "testLineSettings"];
  document.getElementById("saveLineSettings").addEventListener("click", withButtonLock(lineButtons, saveLineSettings));
  document.getElementById("clearLineSettings").addEventListener("click", withButtonLock(lineButtons, clearLineSettings));
  document.getElementById("testLineSettings").addEventListener("click", withButtonLock(lineButtons, testLineSettings));
  document.getElementById("systemHealthCheck").addEventListener("click", runSystemHealthCheck);
  document.getElementById("refreshMlStatus").addEventListener("click", () => loadBackendStatus());
  document.getElementById("runMlUpdate").addEventListener("click", runBackendUpdate);
  document.getElementById("themeToggle").addEventListener("click", () => {
    applyTheme(document.body.classList.contains("dark") ? "light" : "dark");
  });
  loadTokenStatus();
  loadSinopacStatus();
  loadCapitalStatus();
  loadAdvancedFlowStatus({ quiet: true });
  loadAiSettingsStatus();
  loadLineSettingsStatus();
  loadBackendStatus();
  runDailyFirstSystemHealthCheck();
}

initSettings();
