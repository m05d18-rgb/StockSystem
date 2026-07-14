// 交易複盤獨立頁(2026-07-09):只做一件事——顯示「真實成交複盤」卡(永豐已實現損益 × 雷達推薦),
// 並提供手動「重新抓取」(force 匯入,不受主頁 once/day 閘門限制,收盤後/當天剛賣也能立刻拉最新成交)。
// 刻意不載入主頁 app.js(避免整套背景監控/庫存同步機器在這頁重跑)。本頁絕不下單；
// 唯一寫入操作是使用者明確確認後，替舊持股一次性補鎖成交策略週期。

let showAllRealizedTrades = false;
let radarScoreTrackState = { loading: false, error: "", payload: null };

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
  sidebar.querySelectorAll("a").forEach((link) => link.addEventListener("click", () => setOpen(false)));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setOpen(false); });
  setOpen(false);
}

function friendlyError(error) {
  const message = error?.message || String(error);
  if (message === "Failed to fetch" || message.includes("NetworkError")) {
    return "本機伺服器沒有回應，請確認 http://127.0.0.1:8008/ 的 Python server 正在執行後重新整理。";
  }
  return message;
}

function fmtMoney(v) {
  if (v === null || v === undefined || v === "") return "-";
  const n = Number(v);
  return Number.isFinite(n) ? `${n >= 0 ? "+" : ""}${Math.round(n).toLocaleString("en-US")}` : "-";
}

function fmtAmount(v) {
  if (v === null || v === undefined || v === "") return "-";
  const n = Number(v);
  return Number.isFinite(n) ? Math.round(n).toLocaleString("en-US") : "-";
}

function fmtPrice(v) {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n.toFixed(2) : "-";
}

function profitClass(v) {
  const n = Number(v);
  if (!Number.isFinite(n) || n === 0) return "";
  return n > 0 ? "rrv-pos" : "rrv-neg";
}

function fmtPctRatio(v, digits = 1) {
  if (v === null || v === undefined || v === "") return "-";
  const n = Number(v);
  return Number.isFinite(n) ? `${n >= 0 ? "+" : ""}${(n * 100).toFixed(digits)}%` : "-";
}

function fmtPrecision(v) {
  if (v === null || v === undefined || v === "") return "-";
  const n = Number(v);
  return Number.isFinite(n) ? `${Math.round(n * 100)}%` : "-";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function radarTrackRate(value, digits = 1) {
  if (value === null || value === undefined || value === "") return "-";
  return Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(digits)}%` : "-";
}

function renderRadarScoreTrackRecord() {
  const box = document.getElementById("radarScoreTrackRecord");
  if (!box) return;
  const payload = radarScoreTrackState.payload || {};
  const buckets = Array.isArray(payload.scoreBuckets) ? payload.scoreBuckets : [];
  if (radarScoreTrackState.loading && !buckets.length) {
    box.innerHTML = '<p class="radar-score-record-status">正在結算分數區間戰績...</p>';
    return;
  }
  if (radarScoreTrackState.error && !buckets.length) {
    box.innerHTML = `<p class="radar-score-record-status">${escapeHtml(radarScoreTrackState.error)}</p>`;
    return;
  }
  if (!buckets.length) {
    box.innerHTML = '<p class="radar-score-record-status">尚無可結算的雷達候選；累積後會依相同成交口徑顯示。</p>';
    return;
  }

  const overall = payload.overall || {};
  const deployment = payload.deploymentReadiness || {};
  const live = deployment.live || {};
  const proxy = deployment.proxy || {};
  const walkForward = deployment.walkForward || {};
  const readinessReady = deployment.formalReady === true;
  const readinessLabel = deployment.readinessDate
    ? (readinessReady ? "可上線" : "只觀察")
    : "尚未驗證";
  const readinessDetail = deployment.readinessDate
    ? `${deployment.readinessDate}｜盤中可成交報價確認 n=${Number(live.settled || 0)}、命中 ${radarTrackRate(live.targetHitRate)}、淨報酬 ${radarTrackRate(live.avgNetReturn, 2)}、PF ${live.profitFactor == null ? "-" : Number(live.profitFactor).toFixed(2)}｜隔日開盤代理 n=${Number(proxy.settled || 0)}、淨報酬 ${radarTrackRate(proxy.avgNetReturn, 2)}、PF ${proxy.profitFactor == null ? "-" : Number(proxy.profitFactor).toFixed(2)}｜日線代理 walk-forward 淨報酬 ${radarTrackRate(walkForward.avgNetReturn, 2)}、PF ${walkForward.profitFactor == null ? "-" : Number(walkForward.profitFactor).toFixed(2)}、命中提升 ${radarTrackRate(walkForward.precisionLift, 2)}｜連續通過 ${Number(deployment.consecutivePassDays || 0)}/${Number(deployment.requiredPassDays || 5)} 日`
    : "等待交易日收盤或盤前驗證建立正式戰績紀錄";

  const ruleConfig = payload.ruleConfig || {};
  const configText = ruleConfig.source === "walk_forward_approved"
    ? "walk-forward 權重已通過並套用"
    : ruleConfig.source === "walk_forward_observation"
      ? "walk-forward 觀察模式，正式分數維持原權重"
      : "目前使用內建規則權重";
  const guardrail = ruleConfig.entryGuardrailCalibration || {};
  const guardrailApproved = guardrail.approved === true;
  const guardrailRecent = guardrail.recentOos || {};
  const guardrailPressure = guardrail.pressureTestOos || {};
  const guardrailRecentResult = guardrailRecent.recommended || {};
  const guardrailPressureResult = guardrailPressure.recommended || {};
  const guardrailChecks = Object.values(guardrail.adoptionChecks || {});
  const guardrailHasCalibration = Boolean(guardrail.recommendedKey || guardrailChecks.length);
  const guardrailStatus = guardrailApproved
    ? "已採用"
    : guardrailHasCalibration ? "觀察中" : "尚未校準";
  const guardrailLabel = guardrail.recommendedLabel || "基準（不限制）";
  const guardrailDetail = guardrailHasCalibration
    ? `建議：${guardrailLabel}｜近期 OOS ${Number(guardrailRecentResult.trades || 0)} 筆、命中提升 ${radarTrackRate(guardrailRecent.precisionLift, 2)}、淨報酬 ${radarTrackRate(guardrailRecentResult.avgNetReturn, 2)}、PF ${guardrailRecentResult.profitFactor == null ? "-" : Number(guardrailRecentResult.profitFactor).toFixed(2)}｜壓力測試 ${Number(guardrailPressureResult.trades || 0)} 筆、淨報酬 ${radarTrackRate(guardrailPressureResult.avgNetReturn, 2)}、PF ${guardrailPressureResult.profitFactor == null ? "-" : Number(guardrailPressureResult.profitFactor).toFixed(2)}｜穩定度 ${radarTrackRate(guardrail.ruleStability, 1)}｜門檻 ${guardrailChecks.filter(Boolean).length}/${guardrailChecks.length} 通過`
    : "等待規則 walk-forward 產生觀察結果；未核准前不影響正式買賣";

  const entryModes = payload.entryModes || {};
  const confirmedEligible = payload.entryModePerformance?.intradayConfirmed?.eligible || {};
  const policy = payload.policy || {};
  const diagnostics = payload.diagnostics || {};
  const bestBucket = diagnostics.bestEligibleBucket || null;
  const regimeGroups = Array.isArray(diagnostics.regimeGroups) ? diagnostics.regimeGroups : [];
  const calibratedRegimes = ruleConfig.regimeThresholdCalibration?.regimes || {};
  const observationExperiments = payload.observationExperiments || {};
  const liveExperiments = observationExperiments.live || {};
  const proxyExperiments = observationExperiments.proxy || {};
  const liveBaseline = liveExperiments.baseline || {};
  const experimentCohort = Number(liveBaseline.settled || 0) > 0
    ? liveExperiments
    : proxyExperiments;
  const experimentBasisLabel = experimentCohort.label || "規則觀察";
  const experimentRows = (Array.isArray(experimentCohort.experiments) ? experimentCohort.experiments : [])
    .filter((item) => item.key !== "baseline" && Number(item.settled || 0) > 0)
    .sort((left, right) => {
      const leftReturn = left.avgNetReturn == null ? -999 : Number(left.avgNetReturn);
      const rightReturn = right.avgNetReturn == null ? -999 : Number(right.avgNetReturn);
      return rightReturn - leftReturn || Number(right.settled || 0) - Number(left.settled || 0);
    })
    .slice(0, 5)
    .map((item) => {
      const status = item.adoptionCandidate === true
        ? "可進下一階段"
        : item.researchQualified === true
          ? "代理通過，不套用"
          : item.samplePass === false ? "樣本不足" : "未通過";
      return `
        <tr>
          <td>${escapeHtml(item.label || item.key || "-")}</td>
          <td>${Number(item.settled || 0)}</td>
          <td>${radarTrackRate(item.targetHitLift, 2)}</td>
          <td class="${Number(item.avgNetReturn) >= 0 ? "up" : "down"}">${radarTrackRate(item.avgNetReturn, 2)}</td>
          <td>${item.profitFactor == null ? "-" : Number(item.profitFactor).toFixed(2)}</td>
          <td>${escapeHtml(status)}</td>
        </tr>`;
    }).join("");
  const lossGroups = Array.isArray(diagnostics.underperformingGroups)
    ? diagnostics.underperformingGroups.slice(0, 3)
    : [];
  const lossGroupText = lossGroups.length
    ? `目前主要虧損分層：${lossGroups.map((item) => `${item.label} n=${Number(item.settled || 0)}、淨損益 ${radarTrackRate(item.avgNetReturn, 2)}`).join("；")}`
    : "目前沒有達 20 筆且平均淨損益為負的可識別分層。";
  const confirmedEntries = Object.entries(entryModes)
    .filter(([key]) => key.startsWith("intraday_confirmed") || key === "intraday_execution_analysis")
    .reduce((sum, [, count]) => sum + Number(count || 0), 0);

  const bucketRows = buckets.map((bucket) => {
    const ci = Array.isArray(bucket.targetHitConfidence95) ? bucket.targetHitConfidence95 : [];
    const eligible = bucket.eligible || {};
    const lowSample = Number(bucket.settled || 0) < 10;
    return `
      <tr class="${lowSample ? "low-sample" : ""}">
        <td>${escapeHtml(bucket.label || "-")}${lowSample ? "（樣本少）" : ""}</td>
        <td>${Number(bucket.settled || 0)}</td>
        <td>${radarTrackRate(bucket.targetHitRate)}</td>
        <td>${radarTrackRate(ci[0])}～${radarTrackRate(ci[1])}</td>
        <td class="${Number(bucket.avgNetReturn) >= 0 ? "up" : "down"}">${radarTrackRate(bucket.avgNetReturn, 2)}</td>
        <td>${radarTrackRate(bucket.stopRate)}</td>
        <td>${Number(eligible.settled || 0)} / ${radarTrackRate(eligible.targetHitRate)}</td>
      </tr>`;
  }).join("");

  const regimeRows = regimeGroups.map((group) => {
    const calibration = calibratedRegimes[group.key] || {};
    const threshold = Number(calibration.effectiveThreshold ?? policy.regimeThresholds?.[group.key] ?? policy.minimumFormalScore ?? 60);
    return `
      <tr>
        <td>${escapeHtml(group.label || group.key || "-")}</td>
        <td>${Number(group.settled || 0)}</td>
        <td>${radarTrackRate(group.targetHitRate)}</td>
        <td class="${Number(group.avgNetReturn) >= 0 ? "up" : "down"}">${radarTrackRate(group.avgNetReturn, 2)}</td>
        <td>${group.profitFactor == null ? "-" : Number(group.profitFactor).toFixed(2)}</td>
        <td>${threshold.toFixed(0)} 分</td>
        <td>${calibration.approved === true ? "已採用" : "觀察"}</td>
      </tr>`;
  }).join("");

  const diagnosticText = diagnostics.scoreMonotonic === false
    ? `目前分數與命中率尚未呈單調關係，分數只作規則排序、不能當上漲機率。${bestBucket ? `現有可買樣本中，淨損益最佳區間為 ${bestBucket.label}（${radarTrackRate(bestBucket.avgNetReturn, 2)}）。` : ""}`
    : diagnostics.scoreMonotonic === true
      ? "目前已有足夠樣本的分數區間呈單調關係，仍須持續觀察。"
      : "分數區間樣本仍不足以判斷單調性。";

  box.innerHTML = `
    <div class="radar-deployment-readiness ${readinessReady ? "ready" : "observe"}">
      <strong>正式上線門檻｜${escapeHtml(readinessLabel)}</strong>
      <span>${escapeHtml(readinessDetail)}</span>
    </div>
    <div class="radar-entry-guardrail ${guardrailApproved ? "ready" : "observe"}">
      <strong>不追價進場防線｜${escapeHtml(guardrailStatus)}</strong>
      <span>${escapeHtml(guardrailDetail)}</span>
    </div>
    <div class="radar-score-metrics">
      <div class="radar-score-metric"><span>已結算</span><strong>${Number(overall.settled || 0)} 筆</strong></div>
      <div class="radar-score-metric"><span>10日先達 +10%</span><strong>${radarTrackRate(overall.targetHitRate)}</strong></div>
      <div class="radar-score-metric"><span>平均淨損益</span><strong class="${Number(overall.avgNetReturn) >= 0 ? "up" : "down"}">${radarTrackRate(overall.avgNetReturn, 2)}</strong></div>
      <div class="radar-score-metric"><span>盤中報價確認</span><strong>${Number(confirmedEligible.settled || 0)} / ${radarTrackRate(confirmedEligible.targetHitRate)}</strong></div>
    </div>
    <div class="radar-score-table-wrap">
      <table class="radar-score-table">
        <thead><tr><th>分數區間</th><th>已結算</th><th>命中率</th><th>95%區間</th><th>平均淨損益</th><th>停損率</th><th>可買樣本／命中</th></tr></thead>
        <tbody>${bucketRows}</tbody>
      </table>
    </div>
    ${regimeRows ? `
      <h4 class="radar-score-subtitle">市場狀態戰績</h4>
      <div class="radar-score-table-wrap">
        <table class="radar-score-table radar-regime-table">
          <thead><tr><th>市場狀態</th><th>已結算</th><th>命中率</th><th>平均淨損益</th><th>獲利因子</th><th>正式門檻</th><th>狀態</th></tr></thead>
          <tbody>${regimeRows}</tbody>
        </table>
       </div>` : ""}
    <h4 class="radar-score-subtitle">規則觀察實驗</h4>
    ${experimentRows ? `
      <div class="radar-score-table-wrap">
        <table class="radar-score-table">
          <thead><tr><th>${escapeHtml(experimentBasisLabel)}</th><th>已結算</th><th>命中提升</th><th>平均淨損益</th><th>獲利因子</th><th>狀態</th></tr></thead>
          <tbody>${experimentRows}</tbody>
        </table>
      </div>` : '<p class="radar-score-record-note">規則實驗尚無可結算樣本。</p>'}
    <p class="radar-score-record-note">${escapeHtml(lossGroupText)} 正式規則未變更；只有盤中可成交報價確認達 50 筆、淨損益轉正、獲利因子大於 1 且命中提升至少 2 個百分點，才可進入採用評估。</p>
    <p class="radar-score-record-note">${escapeHtml(diagnosticText)}</p>
    <p class="radar-score-record-note">${escapeHtml(configText)}。市場狀態只調整正式門檻，不改寫原始分數；只有淨損益為正、獲利因子大於 1、命中率至少提升 2 個百分點且通過歷史壓力測試才採用。基礎門檻 ${Number(policy.minimumFormalScore || 60).toFixed(0)} 分；盤中可成交報價確認採第一次通過完整 shadow 閘門的 ask／成本後成交估計，僅作紙上績效，不代表券商實際成交；舊資料才用隔日開盤加 0.1% 滑價。10個交易日內先達 +10% 算命中、先到 -7% 算停損；同日兩者都碰到時保守算停損，損益包含買賣手續費、證交稅與雙邊滑價。盤中報價確認訊號 ${confirmedEntries} 筆、已結算 ${Number(confirmedEligible.settled || 0)} 筆；隔日開盤代理只供回測，不可解鎖正式買賣。</p>`;
}

async function loadRadarScoreTrackRecord() {
  radarScoreTrackState = { ...radarScoreTrackState, loading: true, error: "" };
  renderRadarScoreTrackRecord();
  try {
    const response = await fetch("/api/radar/score-track-record?days=365");
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "分數區間戰績讀取失敗");
    radarScoreTrackState = { loading: false, error: "", payload };
  } catch (error) {
    radarScoreTrackState = { loading: false, error: friendlyError(error), payload: null };
  }
  renderRadarScoreTrackRecord();
}

function modelSignalSideLabel(side) {
  const text = String(side || "").toUpperCase();
  if (text.includes("SELL") || text.includes("EXIT")) return "賣/出場";
  if (text.includes("BUY")) return "買";
  return side || "-";
}

function tradeHorizonText(item) {
  const label = item?.tradeHorizonLabel || item?.entryTradeHorizonLabel || item?.exitTradeHorizonLabel || "";
  const days = item?.tradeHorizonDays || item?.entryTradeHorizonDays || item?.exitTradeHorizonDays || "";
  if (!label && !days) return "-";
  return `${label || "-"}${days ? ` ${days}` : ""}`;
}

function signalSessionText(item) {
  const label = item?.signalSessionLabel || item?.entrySessionLabel || item?.exitSessionLabel || "";
  const time = item?.signalTime || item?.entrySignalTime || item?.exitSignalTime || "";
  if (!label && !time) return "-";
  return `${label || "-"}${time ? ` ${time}` : ""}`;
}

function paperFillText(trade) {
  const mode = {
    signal_price: "訊號價",
    touch_buy_point: "觸價",
    same_day_signal_price: "盤中訊號價",
    next_open: "隔日開盤",
    pending_next_open: "等隔日",
    not_touched: "未觸價",
    missing_market_data: "缺官方日K",
    missing_volume: "缺成交量",
    insufficient_liquidity: "成交量不足",
    locked_limit_up: "鎖漲停未成交",
    locked_limit_down: "鎖跌停未成交",
  }[trade?.entryFillMode] || trade?.entryFillMode || "";
  const date = trade?.entryFillDate && trade.entryFillDate !== trade.entryDate ? trade.entryFillDate : "";
  return `${mode || "-"}${date ? ` ${date}` : ""}`;
}

function paperExitReasonText(trade) {
  const reason = {
    model_exit: "模型出場",
    stop_loss: "停損",
    take_profit: "停利",
    time_exit: "週期到期",
    model_exit_without_position: "無持倉賣訊",
  }[trade?.exitReason] || trade?.exitReason || "-";
  const fill = {
    next_open: "隔日開盤",
    same_day_signal_price: "盤中訊號價",
    gap_open: "跳空開盤",
    horizon_next_open: "到期隔日開盤",
    stop_touch: "觸價",
    target_touch: "觸價",
  }[trade?.exitFillMode] || "";
  const blocked = Number(trade?.blockedExitDays || 0);
  return `${reason}${fill ? ` · ${fill}` : ""}${blocked ? ` · 延後${blocked}日` : ""}`;
}

function paperUnfilledReason(mode) {
  return {
    missing_market_data: "缺少官方日K",
    pending_next_open: "等待下一交易日",
    not_touched: "價格未觸及",
    missing_volume: "缺少成交量",
    insufficient_liquidity: "一張超過日量5%",
    locked_limit_up: "鎖漲停買不到",
    locked_limit_down: "鎖跌停賣不掉",
    insufficient_paper_cash: "紙上帳現金不足",
    portfolio_position_limit: "已達持股檔數上限",
  }[mode] || mode || "未分類";
}

function exitReasonLabel(reason) {
  return {
    model_exit: "模型出場",
    stop_loss: "停損",
    take_profit: "停利",
    time_exit: "週期到期",
    unknown: "未分類",
  }[reason] || reason || "-";
}

function paperSnapshotStatusClass(status) {
  const key = String(status || "");
  if (key === "done") return "snapshot-status-done";
  if (key === "failed" || key === "missed") return "snapshot-status-bad";
  if (key === "running_window") return "snapshot-status-active";
  return "snapshot-status-muted";
}

async function rerunPaperSnapshotSession(sessionKey, button) {
  const prev = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = "補跑中";
  }
  try {
    const response = await fetch("/api/paper-signals/snapshot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session: sessionKey, maxSymbols: 180, includeHoldings: true }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || !payload.ok) throw new Error(payload.error || "補跑失敗");
    await renderModelPaperReview();
  } catch (error) {
    window.alert(`模型紙上快照補跑失敗：${friendlyError(error)}`);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = prev || "補跑";
    }
  }
}

function appendPaperSnapshotSchedule(box, schedule) {
  if (!box || !Array.isArray(schedule) || !schedule.length) return;
  const title = document.createElement("div");
  title.className = "rrv-note";
  title.textContent = "今日模型紙上快照排程";
  box.append(title);

  const table = document.createElement("table");
  table.className = "rrv-table snapshot-schedule-table";
  const thead = document.createElement("thead");
  const tr = document.createElement("tr");
  ["時段", "時間窗", "狀態", "已存", "檢查", "錯誤", "執行時間", "操作"].forEach((text) => {
    const th = document.createElement("th");
    th.textContent = text;
    tr.append(th);
  });
  thead.append(tr);
  table.append(thead);

  const tbody = document.createElement("tbody");
  schedule.forEach((item) => {
    const row = document.createElement("tr");
    if (item.message) row.title = item.message;
    const values = [
      `${item.label || item.key || "-"}${item.time ? ` ${item.time}` : ""}`,
      item.window?.label || "-",
      item.statusLabel || item.status || "-",
      item.saved ?? 0,
      item.checked ?? 0,
      item.errors ?? 0,
      item.ranAt || "-",
    ];
    values.forEach((value, index) => {
      const td = document.createElement("td");
      if (index === 2) {
        const badge = document.createElement("span");
        badge.className = `snapshot-status ${paperSnapshotStatusClass(item.status)}`;
        badge.textContent = value;
        td.append(badge);
      } else {
        td.textContent = value;
      }
      if (index === 5 && Number(item.errors) > 0) td.className = "rrv-neg";
      row.append(td);
    });
    const actionTd = document.createElement("td");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "mini-action-button";
    btn.textContent = item.status === "done" ? "重跑" : "補跑";
    btn.addEventListener("click", () => rerunPaperSnapshotSession(item.key, btn));
    actionTd.append(btn);
    row.append(actionTd);
    tbody.append(row);
  });
  table.append(tbody);
  const wrap = document.createElement("div");
  wrap.className = "rrv-table-wrap";
  wrap.append(table);
  box.append(wrap);
}

async function renderModelAccuracyReview() {
  const box = document.getElementById("modelAccuracyReview");
  const summary = document.getElementById("modelAccuracySummary");
  if (!box || !summary) return;

  let payload;
  try {
    const response = await fetch("/api/model-experiments/tcn/status", { cache: "no-store" });
    payload = await response.json().catch(() => ({}));
    if (!response.ok || !payload.ok) throw new Error(payload.error || "讀取失敗");
  } catch (error) {
    summary.replaceChildren();
    const status = document.createElement("strong");
    status.textContent = "目前無法比較模型";
    summary.append(status);
    box.replaceChildren();
    const note = document.createElement("div");
    note.className = "rrv-note rrv-caveat";
    note.textContent = `模型準確率讀取失敗：${friendlyError(error)}`;
    box.append(note);
    return;
  }

  const latest = payload.latestRun;
  const names = { tcn: "日線 TCN", xgboost: "XGBoost", lightgbm: "LightGBM" };
  const rows = Object.entries(names)
    .map(([key, name]) => {
      const metric = latest?.metrics?.[key] || {};
      const accuracy = Number(metric.dailyTop5Precision);
      const samples = Number(metric.dailyTop5Trades);
      return {
        key,
        name,
        accuracy: Number.isFinite(accuracy) ? accuracy : null,
        samples: Number.isFinite(samples) ? samples : 0,
      };
    })
    .filter((item) => item.accuracy !== null)
    .sort((left, right) => right.accuracy - left.accuracy);

  if (!latest || latest.status !== "completed" || !rows.length) {
    summary.replaceChildren();
    const status = document.createElement("strong");
    status.textContent = latest?.status === "running" ? "模型測試執行中" : "尚無可比較結果";
    summary.append(status);
    box.replaceChildren();
    const note = document.createElement("div");
    note.className = "rrv-note rrv-caveat";
    note.textContent = "等待 TCN、XGBoost、LightGBM 完成同一批外推測試。";
    box.append(note);
    return;
  }

  const leader = rows[0];
  const runnerUp = rows[1];
  const leadPoints = runnerUp ? (leader.accuracy - runnerUp.accuracy) * 100 : null;
  summary.replaceChildren();
  const headline = document.createElement("strong");
  headline.textContent = `目前最高：${leader.name} ${fmtPrecision(leader.accuracy)}`;
  const detail = document.createElement("span");
  detail.textContent = leadPoints === null
    ? `資料日 ${latest.dataMaxDate || "-"}`
    : `領先第 2 名 ${leadPoints.toFixed(2)} 個百分點，差距仍小；資料日 ${latest.dataMaxDate || "-"}`;
  summary.append(headline, detail);

  const table = document.createElement("table");
  table.className = "rrv-table model-accuracy-table";
  const thead = document.createElement("thead");
  const heading = document.createElement("tr");
  ["模型", "已結算樣本", "準確率"].forEach((label) => {
    const th = document.createElement("th");
    th.textContent = label;
    heading.append(th);
  });
  thead.append(heading);
  table.append(thead);

  const tbody = document.createElement("tbody");
  rows.forEach((item, index) => {
    const row = document.createElement("tr");
    if (index === 0) row.className = "model-accuracy-leading";
    const model = document.createElement("td");
    model.textContent = item.name;
    const samples = document.createElement("td");
    samples.textContent = fmtAmount(item.samples);
    const accuracy = document.createElement("td");
    accuracy.className = "model-accuracy-value";
    accuracy.textContent = fmtPrecision(item.accuracy);
    row.append(model, samples, accuracy);
    tbody.append(row);
  });
  table.append(tbody);

  const wrap = document.createElement("div");
  wrap.className = "rrv-table-wrap";
  wrap.append(table);
  const definition = document.createElement("div");
  definition.className = "rrv-note model-accuracy-definition";
  definition.textContent = "準確率＝每日前 5 名在 10 日內先漲 +10% 的比例；先跌 -7% 算失敗。三個模型使用同一外推測試，結果不會自動接入正式買賣。";
  box.replaceChildren(wrap, definition);
}

async function renderRadarDiscoveryAccuracy() {
  const box = document.getElementById("radarDiscoveryAccuracyReview");
  const summary = document.getElementById("radarDiscoveryAccuracySummary");
  if (!box || !summary) return;
  let payload;
  try {
    const response = await fetch("/api/radar/discovery-recall?days=30&refresh=1", {
      cache: "no-store",
    });
    payload = await response.json().catch(() => ({}));
    if (!response.ok || !payload.ok) throw new Error(payload.error || "讀取失敗");
  } catch (error) {
    summary.replaceChildren();
    const status = document.createElement("strong");
    status.textContent = "目前無法計算雷達找到率";
    summary.append(status);
    box.replaceChildren();
    const note = document.createElement("div");
    note.className = "rrv-note rrv-caveat";
    note.textContent = `雷達找到率讀取失敗：${friendlyError(error)}`;
    box.append(note);
    return;
  }

  const days = Array.isArray(payload.days) ? payload.days : [];
  const latest = payload.latest;
  const tradable = payload.tradableAccuracy || {};
  const newCandidate = tradable.newIntradayCandidates || {};
  const existingConfirmed = tradable.existingRadarIntradayConfirmed || {};
  if (!latest || !days.length) {
    summary.replaceChildren();
    const status = document.createElement("strong");
    status.textContent = "尚無收盤後結算資料";
    summary.append(status);
    box.replaceChildren();
    const pendingMetrics = document.createElement("div");
    pendingMetrics.className = "radar-recall-metrics";
    [
      ["盤中新候選訊號", fmtAmount(newCandidate.signals || 0)],
      ["已結算", fmtAmount(newCandidate.settled || 0)],
      ["可買準確率", fmtPrecision(newCandidate.targetHitRate)],
    ].forEach(([label, value]) => {
      const item = document.createElement("div");
      const name = document.createElement("span");
      name.textContent = label;
      const number = document.createElement("strong");
      number.textContent = value;
      item.append(name, number);
      pendingMetrics.append(item);
    });
    const note = document.createElement("div");
    note.className = "rrv-note rrv-caveat";
    note.textContent = payload?.settlement?.reason
      || "下一個完整交易日收盤資料同步後，系統會自動產生找到率。";
    box.append(pendingMetrics, note);
    return;
  }

  summary.replaceChildren();
  const headline = document.createElement("strong");
  headline.textContent = `最新找到率 ${fmtPrecision(latest.recall)}`;
  const detail = document.createElement("span");
  detail.textContent = `${latest.date}｜實際強勢 ${latest.actualMovers}、找到 ${latest.detectedMovers}、提早 ${latest.earlyDetected || 0}、仍可交易 ${latest.actionableDetected || 0}、過晚 ${latest.lateDetected || 0}、漏掉 ${latest.missedMovers}`;
  summary.append(headline, detail);

  const aggregate = payload.aggregate || {};
  const metrics = document.createElement("div");
  metrics.className = "radar-recall-metrics";
  [
    ["找到率", fmtPrecision(aggregate.recall)],
    ["提早找到率", fmtPrecision(aggregate.earlyRecall)],
    ["盤中新候選可買準確率", fmtPrecision(newCandidate.targetHitRate)],
    ["原雷達盤中確認準確率", fmtPrecision(existingConfirmed.targetHitRate)],
    ["平均最大不利幅度", fmtPrecision(newCandidate.avgMaxAdverse)],
    ["平均扣成本損益", fmtPrecision(newCandidate.avgNetReturn)],
    ["盤中新候選獲利因子", newCandidate.profitFactor == null ? "-" : fmt(newCandidate.profitFactor, 2)],
    ["盤中新候選已結算", fmtAmount(newCandidate.settled || 0)],
    ["累計實際強勢", fmtAmount(aggregate.actualMovers || 0)],
  ].forEach(([label, value]) => {
    const item = document.createElement("div");
    const name = document.createElement("span");
    name.textContent = label;
    const number = document.createElement("strong");
    number.textContent = value;
    item.append(name, number);
    metrics.append(item);
  });

  const table = document.createElement("table");
  table.className = "rrv-table model-accuracy-table radar-recall-table";
  const thead = document.createElement("thead");
  const heading = document.createElement("tr");
  ["日期", "實際強勢", "找到", "仍可交易", "過晚", "漏掉", "找到率", "提早找到"].forEach((label) => {
    const th = document.createElement("th");
    th.textContent = label;
    heading.append(th);
  });
  thead.append(heading);
  table.append(thead);
  const tbody = document.createElement("tbody");
  days.slice(0, 10).forEach((item) => {
    const row = document.createElement("tr");
    [
      item.date || "-",
      fmtAmount(item.actualMovers || 0),
      fmtAmount(item.detectedMovers || 0),
      fmtAmount(item.actionableDetected || 0),
      fmtAmount(item.lateDetected || 0),
      fmtAmount(item.missedMovers || 0),
      fmtPrecision(item.recall),
      fmtPrecision(item.earlyRecall),
    ].forEach((value, index) => {
      const td = document.createElement("td");
      td.textContent = value;
      if (index >= 6) td.className = "model-accuracy-value radar-recall-value";
      row.append(td);
    });
    tbody.append(row);
  });
  table.append(tbody);
  const wrap = document.createElement("div");
  wrap.className = "rrv-table-wrap";
  wrap.append(table);

  const note = document.createElement("div");
  note.className = "rrv-note model-accuracy-definition";
  const misses = Array.isArray(latest.missed) ? latest.missed.slice(0, 5) : [];
  const readiness = payload.readiness || {};
  const readinessText = readiness.paperSimulationEligible
    ? "已達盤中新發現模擬研究門檻；正式交易仍停用。"
    : `尚未達模擬研究門檻：${(readiness.reasons || []).join("；") || "等待完整交易日資料"}。`;
  note.textContent = `${readinessText}${misses.length
    ? ` 最新漏股：${misses.map((item) => `${item.symbol} ${item.reason}`).join("；")}`
    : " 最新交易日沒有漏掉符合定義的強勢股。"}`;
  box.replaceChildren(metrics, wrap, note);
}

function renderModelTestOverview(payload, error = null) {
  const box = document.getElementById("modelTestOverview");
  if (!box) return;
  box.replaceChildren();
  if (error) {
    const msg = document.createElement("div");
    msg.className = "rrv-note";
    msg.textContent = `讀取模型測試失敗：${friendlyError(error)}`;
    box.append(msg);
    return;
  }
  const overall = payload?.overall || {};
  const horizons = overall.horizons || {};
  const groups = overall.horizonGroups || {};
  const paperTrades = payload?.paperTrades || {};
  if (!overall.signals) {
    const empty = document.createElement("div");
    empty.className = "rrv-note rrv-caveat";
    empty.textContent = "目前還沒有模型測試資料。每日獨立模型循環產生訊號後，這裡會累積短期、中期、長期驗證結果。";
    box.append(empty);
    return;
  }

  const grid = document.createElement("div");
  grid.className = "model-test-grid";
  const addTile = (label, value, sub, clsValue = null) => {
    const tile = document.createElement("div");
    tile.className = "model-test-tile";
    const small = document.createElement("span");
    small.textContent = label;
    const main = document.createElement("strong");
    main.textContent = value;
    if (clsValue !== null) main.className = profitClass(clsValue);
    const note = document.createElement("em");
    note.textContent = sub;
    tile.append(small, main, note);
    grid.append(tile);
  };
  const groupMetric = (key, fallbackHorizon) => (groups[key]?.metrics || horizons[fallbackHorizon] || {});
  const short = groupMetric("short", "5d");
  const mid = groupMetric("mid", "20d");
  const long = groupMetric("long", "60d");
  addTile("總模型訊號", `${overall.signals || 0} 筆`, `買賣訊號 ${overall.actionableSignals || 0} 筆`);
  addTile("短期測試", fmtPrecision(short.precision), `1/3/5日，均報酬 ${fmtPctRatio(short.averageReturn)}`, short.averageReturn);
  addTile("中期測試", fmtPrecision(mid.precision), `10/20日，均報酬 ${fmtPctRatio(mid.averageReturn)}`, mid.averageReturn);
  addTile("長期測試", fmtPrecision(long.precision), `60日，均報酬 ${fmtPctRatio(long.averageReturn)}`, long.averageReturn);
  const winRange = paperTrades.winRateConfidence95 || {};
  const winRangeText = winRange.low != null && winRange.high != null
    && Number.isFinite(Number(winRange.low)) && Number.isFinite(Number(winRange.high))
    ? `95% ${fmtPrecision(winRange.low)}–${fmtPrecision(winRange.high)}`
    : "95%區間待累積";
  const h5 = horizons["5d"] || {};
  addTile(
    "模型虛擬交易",
    fmtPrecision(paperTrades.winRate),
    `已結算 n=${winRange.samples || 0}，5日待驗證 ${h5.pending || 0} 筆；${winRangeText}`,
    paperTrades.avgReturn,
  );
  box.append(grid);

  const note = document.createElement("div");
  note.className = "rrv-note";
  note.textContent = "模型紙上測試，不代表真實下單。";
  box.append(note);
}

async function renderTcnExperimentReview() {
  const box = document.getElementById("tcnExperimentReview");
  if (!box) return;
  let payload;
  try {
    const response = await fetch("/api/model-experiments/tcn/status");
    payload = await response.json().catch(() => ({}));
    if (!response.ok || !payload.ok) throw new Error(payload.error || "讀取失敗");
  } catch (error) {
    box.replaceChildren();
    const note = document.createElement("div");
    note.className = "rrv-note rrv-caveat";
    note.textContent = `讀取日線 TCN 實驗失敗：${friendlyError(error)}`;
    box.append(note);
    return;
  }

  box.replaceChildren();
  const latest = payload.latestRun;
  const isolation = document.createElement("div");
  isolation.className = "rrv-note rrv-attribution";
  isolation.textContent = "獨立觀察模式：不參與妖股雷達候選、分數或正式買賣判斷。";
  box.append(isolation);
  if (!latest) {
    const empty = document.createElement("div");
    empty.className = "rrv-note rrv-caveat";
    empty.textContent = "日線 TCN 尚未完成第一輪基線，盤中分支維持鎖定。";
    box.append(empty);
    return;
  }

  const run = document.createElement("div");
  run.className = "rrv-summary";
  [
    `狀態 ${latest.status === "completed" ? "已完成" : latest.status === "running" ? "執行中" : "失敗"}`,
    `資料日 ${latest.dataMaxDate || "-"}`,
    `樣本 ${fmtAmount(latest.sampleCount)}`,
    `訓練/驗證/測試 ${fmtAmount(latest.trainCount)}/${fmtAmount(latest.validationCount)}/${fmtAmount(latest.testCount)}`,
  ].forEach((text) => {
    const item = document.createElement("span");
    item.textContent = text;
    run.append(item);
  });
  box.append(run);
  if (latest.error) {
    const error = document.createElement("div");
    error.className = "rrv-note rrv-caveat";
    error.textContent = latest.error;
    box.append(error);
  }

  const metrics = latest.metrics || {};
  if (Object.keys(metrics).length) {
    const table = document.createElement("table");
    table.className = "rrv-table tcn-comparison-table";
    const thead = document.createElement("thead");
    const heading = document.createElement("tr");
    ["模型", "AUC", "精準率", "召回率", "每日前5命中", "每日前5淨報酬", "淨報酬MAE"].forEach((label) => {
      const th = document.createElement("th");
      th.textContent = label;
      heading.append(th);
    });
    thead.append(heading);
    table.append(thead);
    const tbody = document.createElement("tbody");
    const names = { tcn: "日線 TCN", xgboost: "XGBoost", lightgbm: "LightGBM" };
    ["tcn", "xgboost", "lightgbm"].forEach((key) => {
      const metric = metrics[key];
      if (!metric) return;
      const row = document.createElement("tr");
      [
        names[key],
        metric.auc == null ? "-" : Number(metric.auc).toFixed(3),
        fmtPrecision(metric.precision),
        fmtPrecision(metric.recall),
        `${fmtPrecision(metric.dailyTop5Precision)} (${fmtAmount(metric.dailyTop5Trades)}筆)`,
        fmtPctRatio(metric.dailyTop5AvgNetReturn, 2),
        fmtPctRatio(metric.netReturnMae, 2),
      ].forEach((value, index) => {
        const td = document.createElement("td");
        td.textContent = value;
        if (index === 5) td.className = profitClass(metric.dailyTop5AvgNetReturn);
        row.append(td);
      });
      tbody.append(row);
    });
    table.append(tbody);
    const wrap = document.createElement("div");
    wrap.className = "rrv-table-wrap";
    wrap.append(table);
    box.append(wrap);
  }

  const gate = latest.gate || {};
  const intraday = payload.intradayData || {};
  const orderBook = payload.orderBookData || {};
  const schedule = payload.schedule || {};
  const gateGrid = document.createElement("div");
  gateGrid.className = "model-test-grid tcn-gate-grid";
  const addGate = (label, value, detail, positive = false) => {
    const tile = document.createElement("div");
    tile.className = "model-test-tile";
    const title = document.createElement("span");
    title.textContent = label;
    const main = document.createElement("strong");
    main.textContent = value;
    main.className = positive ? "rrv-pos" : "";
    const note = document.createElement("em");
    note.textContent = detail;
    tile.append(title, main, note);
    gateGrid.append(tile);
  };
  addGate(
    "日線基線閘門",
    gate.dailyTcnQualified ? "通過" : "觀察中",
    gate.dailyTcnQualified
      ? "TCN 已在外推集勝過兩個樹模型"
      : `尚未同時勝過 XGBoost 與 LightGBM；下次 ${schedule.dailyTcn?.nextRunAt || "週五 18:40"}`,
    Boolean(gate.dailyTcnQualified),
  );
  addGate(
    "盤中資料",
    `${fmtAmount(intraday.sessions)} 日 / ${fmtAmount(intraday.bars)} 根`,
    `${fmtAmount(intraday.symbols)} 檔；門檻 60 日、100,000 根、100 檔`,
    Boolean(intraday.ready),
  );
  addGate(
    "盤中分支",
    payload.intradayBranch?.eligible ? "可進觀察訓練" : "資料累積中",
    payload.intradayBranch?.eligible
      ? "即使放行仍不接正式雷達"
      : `尚差 ${fmtAmount(intraday.remaining?.sessions)} 個交易日；每天開盤自動收集`,
    Boolean(payload.intradayBranch?.eligible),
  );
  addGate(
    "TFT",
    payload.tftGate?.eligible ? "可另案評估" : "暫不啟動",
    `共用真實盤中資料；${fmtAmount(intraday.sessions)} / ${fmtAmount(payload.tftGate?.minimumIntradaySessions || 120)} 日`,
    Boolean(payload.tftGate?.eligible),
  );
  addGate(
    "委託簿模型",
    payload.orderBookGate?.eligible ? "可進觀察訓練" : "五檔資料累積中",
    `${fmtAmount(orderBook.sessions)} 日 / ${fmtAmount(orderBook.featureRows)} 根；真實五檔觀測 ${fmtAmount(orderBook.observations)} 筆`,
    Boolean(payload.orderBookGate?.eligible),
  );
  box.append(gateGrid);

  const target = latest.target || {};
  const targetNote = document.createElement("div");
  targetNote.className = "rrv-note";
  targetNote.textContent = "同口徑：次日開盤含滑價進場、10 日內先 +10% 才算命中、-7% 停損、同日停利停損採保守停損優先；另輸出 5/20/60 日與 MFE/MAE 多目標。";
  if (target.primary) targetNote.dataset.target = target.primary;
  box.append(targetNote);
}

// 讀本機 /api/portfolio/realized-review(只讀 DB,不碰永豐),把「真實成交複盤」卡渲染出來。
// 誠實三態、台股漲紅跌綠、XSS-safe(全 textContent)。跟主頁移除前的 loadRealizedRadarReview 同邏輯,
// 差別:這是整頁主角,無資料時顯示提示而非隱藏,並標資料截止日。
async function renderRealizedReview() {
  const box = document.getElementById("realizedRadarReview");
  if (!box) return;
  let payload;
  try {
    const response = await fetch("/api/portfolio/realized-review");
    payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "讀取失敗");
  } catch (error) {
    box.replaceChildren();
    const msg = document.createElement("div");
    msg.className = "rrv-note";
    msg.textContent = `讀取交易紀錄失敗：${friendlyError(error)}`;
    box.append(msg);
    return;
  }
  const trades = payload.trades || [];
  const s = payload.summary || {};
  box.replaceChildren();

  if (!trades.length) {
    const empty = document.createElement("div");
    empty.className = "rrv-note rrv-caveat";
    empty.textContent = payload.note || "還沒有已實現損益資料。按上方「更新已實現損益」從永豐匯入（唯讀、不下單）。";
    box.append(empty);
    return;
  }

  const money = (v) => { const n = Number(v); return Number.isFinite(n) ? `${n >= 0 ? "+" : ""}${Math.round(n).toLocaleString("en-US")}` : "-"; };
  const pct = (v) => (Number.isFinite(Number(v)) ? `${Number(v) >= 0 ? "+" : ""}${Number(v).toFixed(1)}%` : "-");
  const cls = (v) => (Number(v) >= 0 ? "rrv-pos" : "rrv-neg"); // 台股漲紅跌綠

  // 資料截止日(最新一筆賣出日;trades 已依賣出日新到舊排序)——讓你知道要不要按重新抓取補今天的
  const latestSell = trades[0] && trades[0].sellDate ? trades[0].sellDate : null;
  if (latestSell) {
    const asOf = document.createElement("div");
    asOf.className = "rrv-note";
    asOf.textContent = `資料截至最新賣出日：${latestSell}（更新最新成交請按「更新已實現損益」）`;
    box.append(asOf);
  }

  // 摘要:真實勝率/總損益(這兩個是真的)
  const summary = document.createElement("div");
  summary.className = "rrv-summary";
  const wr = Number.isFinite(Number(s.winRate)) ? `${Math.round(s.winRate * 100)}%` : "-";
  const seg1 = document.createElement("span");
  seg1.textContent = `你自己的 ${s.count} 筆真實成交，勝率 ${wr}，總損益 `;
  const seg2 = document.createElement("strong");
  seg2.className = cls(s.totalPnl);
  seg2.textContent = `${money(s.totalPnl)} 元`;
  summary.append(seg1, seg2);
  box.append(summary);

  // 誠實歸因:這行勝率/總損益是「你自己」的整體交易成績,不是雷達績效。
  const methodNotes = [];
  const attribution = document.createElement("div");
  attribution.className = "rrv-note rrv-attribution";
  const brokerPnlCount = Number((s.pnlBasisCounts || {}).sinopac_realized || 0);
  attribution.textContent = payload.source === "local_trades_table"
    ? `↑ 這是你整體交易的成績，不是雷達績效；其中 ${brokerPnlCount} 筆採永豐已實現淨損益，沒有券商損益對帳的交易才使用成交價毛損益。`
    : "↑ 這是你整體交易的成績，不是雷達績效；雷達當時對每筆的判斷看下表「雷達當時」欄。";
  methodNotes.push(attribution);

  // 誠實 caveat(後端算好的 note:涵蓋窗/樣本不足/會累積)
  if (payload.note) {
    const note1 = document.createElement("div");
    note1.className = "rrv-note rrv-caveat";
    note1.textContent = payload.note;
    methodNotes.push(note1);
  }

  // 逐筆表格
  const stateMeta = {
    recommended: { label: "曾判可買", clsName: "rrv-st-rec" },
    candidate_only: { label: "候選未買", clsName: "rrv-st-cand" },
    not_scanned: { label: "未掃到", clsName: "rrv-st-none" },
    no_history: { label: "未上線", clsName: "rrv-st-nohist" },
  };
  const table = document.createElement("table");
  table.className = "rrv-table";
  const thead = document.createElement("thead");
  const htr = document.createElement("tr");
  ["股票", "賣出日", "損益", "報酬率", "雷達當時"].forEach((t) => {
    const th = document.createElement("th"); th.textContent = t; htr.append(th);
  });
  thead.append(htr); table.append(thead);
  const tbody = document.createElement("tbody");
  const visibleTrades = showAllRealizedTrades ? trades : trades.slice(0, 6);
  visibleTrades.forEach((t) => {
    const tr = document.createElement("tr");
    const tdStock = document.createElement("td"); tdStock.textContent = `${t.code}${t.name ? " " + t.name : ""}`;
    const tdDate = document.createElement("td"); tdDate.textContent = t.sellDate || "-";
    const tdPnl = document.createElement("td"); tdPnl.textContent = money(t.pnl); tdPnl.className = cls(t.pnl);
    const tdPct = document.createElement("td"); tdPct.textContent = pct(t.pnlPct); tdPct.className = cls(t.pnlPct);
    const tdRadar = document.createElement("td");
    const meta = stateMeta[t.radarState] || stateMeta.no_history;
    const badge = document.createElement("span");
    badge.className = `rrv-badge ${meta.clsName}`;
    let label = meta.label;
    if (t.radarState === "recommended" && t.radarScore != null) label += `（分 ${t.radarScore}）`;
    badge.textContent = label; tdRadar.append(badge);
    tr.append(tdStock, tdDate, tdPnl, tdPct, tdRadar);
    tbody.append(tr);
  });
  table.append(tbody);

  // 近似對齊警語(表格正上方,白話)
  const approxNote = document.createElement("div");
  approxNote.className = "rrv-note rrv-approx";
  approxNote.textContent = payload.source === "local_trades_table"
    ? "「雷達當時」欄已用本地 trades 的買進日往前找最近一次掃描；未匹配/手動舊資料仍可能缺買進日。"
    : "⚠️「雷達當時」欄是用賣出當天往前找最近一次掃描推估的，只能當參考、非精準對帳（永豐沒給買進日）。";
  methodNotes.push(approxNote);

  const wrap = document.createElement("div");
  wrap.className = "rrv-table-wrap";
  wrap.append(table);
  box.append(wrap);

  if (trades.length > 6) {
    const toggleRows = document.createElement("button");
    toggleRows.type = "button";
    toggleRows.className = "mini-action-button review-row-toggle";
    toggleRows.textContent = showAllRealizedTrades
      ? "只看最近 6 筆"
      : `顯示全部 ${trades.length} 筆`;
    toggleRows.addEventListener("click", () => {
      showAllRealizedTrades = !showAllRealizedTrades;
      renderRealizedReview();
    });
    box.append(toggleRows);
  }

  // 底部:三態分佈
  const st = s.byState || {};
  const cnt = (k) => ((st[k] || {}).count || 0);
  const footer = document.createElement("div");
  footer.className = "rrv-note";
  footer.textContent =
    `雷達對齊：可買 ${cnt("recommended")}／候選 ${cnt("candidateOnly")}` +
    `／未掃到 ${cnt("notScanned")}／未上線 ${cnt("noHistory")}。`;
  box.append(footer);

  const method = document.createElement("details");
  method.className = "inline-review-disclosure";
  const methodSummary = document.createElement("summary");
  methodSummary.textContent = "計算方式";
  method.append(methodSummary, ...methodNotes);
  box.append(method);
}

function appendModelCalibration(box, calibration, error = "") {
  const calTitle = document.createElement("div");
  calTitle.className = "rrv-note";
  calTitle.textContent = "策略校準建議（觀察模式，尚未套用正式判斷）";
  box.append(calTitle);

  if (error) {
    const warning = document.createElement("div");
    warning.className = "rrv-note rrv-caveat";
    warning.textContent = `策略校準讀取失敗：${error}`;
    box.append(warning);
    return;
  }

  const calibrationRows = (calibration?.records || []).filter((row) =>
    String(row?.strategy || "").startsWith("model_")
  );
  if (!calibrationRows.length) {
    const empty = document.createElement("div");
    empty.className = "rrv-note rrv-caveat";
    empty.textContent = "目前尚無模型策略校準紀錄；收盤後完成模型績效結算才會產生。";
    box.append(empty);
    return;
  }

  const weakRows = calibrationRows.filter((row) =>
    Number(row.sample_count || 0) >= 20
    && (Number(row.precision_5d) < 0.45 || Number(row.average_return_5d) < 0)
  );
  const calSummary = document.createElement("div");
  calSummary.className = "rrv-summary";
  calSummary.textContent =
    `觀察中 ${calibrationRows.length} 策略；需降權/提高門檻 ${weakRows.length} 策略；` +
    "目前只產生建議，不套用正式買賣。";
  box.append(calSummary);
  const table = document.createElement("table");
  table.className = "rrv-table";
  const thead = document.createElement("thead");
  const tr = document.createElement("tr");
  ["日期", "策略", "樣本", "5日勝率", "5日均報酬", "建議", "觀察天數", "原因"].forEach((text) => {
    const th = document.createElement("th");
    th.textContent = text;
    tr.append(th);
  });
  thead.append(tr);
  table.append(thead);
  const tbody = document.createElement("tbody");
  calibrationRows.slice(0, 12).forEach((row) => {
    const tr = document.createElement("tr");
    [
      row.calibration_date || "-",
      row.strategy || "-",
      row.sample_count || 0,
      fmtPrecision(row.precision_5d),
      fmtPctRatio(row.average_return_5d),
      row.suggested_action || "-",
      row.observation_days || 0,
      row.reason || "-",
    ].forEach((value, index) => {
      const td = document.createElement("td");
      td.textContent = value;
      if (index === 4) td.className = profitClass(row.average_return_5d);
      if (index === 5 && String(value).includes("lower")) td.className = "rrv-neg";
      if (index === 5 && String(value).includes("raise")) td.className = "rrv-neg";
      tr.append(td);
    });
    tbody.append(tr);
  });
  table.append(tbody);
  const wrap = document.createElement("div");
  wrap.className = "rrv-table-wrap";
  wrap.append(table);
  box.append(wrap);
}

async function renderModelPaperReview(options = {}) {
  const summaryOnly = options.summaryOnly === true;
  const box = document.getElementById("modelPaperReview");
  if (!box) return;
  if (!summaryOnly) {
    box.replaceChildren();
    const loading = document.createElement("div");
    loading.className = "rrv-note";
    loading.textContent = "正在讀取模型逐筆與校準資料...";
    box.append(loading);
  }

  let payload = null;
  let calibration = null;
  let statsError = "";
  let calibrationError = "";
  const refresh = options.refreshOutcomes === true ? "1" : "0";
  const statsPromise = fetch(`/api/paper-signals/stats?refresh=${refresh}&scope=model`)
    .then(async (response) => {
      const data = await response.json().catch(() => null);
      if (!response.ok || !data?.ok) throw new Error(data?.error || "讀取失敗");
      return data;
    });
  const calibrationPromise = summaryOnly
    ? Promise.resolve(null)
    : fetch("/api/strategy-calibration?limit=40").then(async (response) => {
      const data = await response.json().catch(() => null);
      if (!response.ok || !data?.ok) throw new Error(data?.error || "讀取失敗");
      return data;
    });

  try {
    payload = await statsPromise;
  } catch (error) {
    statsError = friendlyError(error);
    renderModelTestOverview(null, error);
  }

  try {
    calibration = await calibrationPromise;
  } catch (error) {
    calibrationError = friendlyError(error);
  }

  if (payload) renderModelTestOverview(payload);
  if (summaryOnly) return;

  box.replaceChildren();
  if (statsError) {
    const msg = document.createElement("div");
    msg.className = "rrv-note rrv-caveat";
    msg.textContent = `模型逐筆讀取失敗：${statsError}`;
    box.append(msg);
    appendModelCalibration(box, calibration, calibrationError);
    return;
  }

  const overall = payload.overall || {};
  const horizons = overall.horizons || {};
  const horizonGroups = overall.horizonGroups || {};
  const sessionGroups = payload.sessionGroups || overall.sessionGroups || [];
  const recent = payload.recentSignals || [];
  if (!overall.signals) {
    const empty = document.createElement("div");
    empty.className = "rrv-note rrv-caveat";
    empty.textContent = "目前還沒有獨立模型紙上買賣訊號。每日模型循環產生 BUY_CANDIDATE 後，會在這裡累積驗證。";
    box.append(empty);
    appendModelCalibration(box, calibration, calibrationError);
    return;
  }

  const head = document.createElement("div");
  head.className = "rrv-head";
  const title = document.createElement("strong");
  title.textContent = "模型紙上買賣複盤（不混入真實成交）";
  head.append(title);
  box.append(head);

  const summary = document.createElement("div");
  summary.className = "rrv-summary";
  const h5 = horizons["5d"] || {};
  const parts = [
    `總訊號 ${overall.signals || 0} 筆`,
    `買賣訊號 ${overall.actionableSignals || 0} 筆`,
    `5日已驗證 ${h5.actionableSamples || 0} 筆`,
    `5日待驗證 ${h5.pending || 0} 筆`,
    `5日精準度 ${fmtPrecision(h5.precision)}`,
    `5日平均方向報酬 ${fmtPctRatio(h5.averageReturn)}`,
  ];
  parts.forEach((text, index) => {
    const span = document.createElement(index >= 4 ? "strong" : "span");
    if (index === 5) span.className = profitClass(h5.averageReturn);
    span.textContent = text;
    summary.append(span);
  });
  box.append(summary);

  const note = document.createElement("div");
  note.className = "rrv-note";
  note.textContent = "買進訊號：後續上漲算命中；賣出/出場訊號：後續下跌算命中。今天剛產生的訊號會列為待驗證，不會算進精準度。";
  box.append(note);

  const groupSummary = document.createElement("div");
  groupSummary.className = "rrv-summary";
  [
    ["短期", horizonGroups.short?.metrics],
    ["中期", horizonGroups.mid?.metrics],
    ["長期", horizonGroups.long?.metrics],
  ].forEach(([label, metrics]) => {
    const span = document.createElement("strong");
    span.className = profitClass(metrics?.averageReturn);
    span.textContent = `${label} ${fmtPrecision(metrics?.precision)} / ${fmtPctRatio(metrics?.averageReturn)}`;
    groupSummary.append(span);
  });
  box.append(groupSummary);

  const horizonTable = document.createElement("table");
  horizonTable.className = "rrv-table";
  const hthead = document.createElement("thead");
  const htr = document.createElement("tr");
  ["分類", "天期", "已驗證", "待驗證", "命中", "精準度", "平均方向報酬", "獲利因子", "最大回撤"].forEach((text) => {
    const th = document.createElement("th");
    th.textContent = text;
    htr.append(th);
  });
  hthead.append(htr);
  horizonTable.append(hthead);
  const htbody = document.createElement("tbody");
  [
    ["短期", "1d"],
    ["短期", "3d"],
    ["短期", "5d"],
    ["中期", "10d"],
    ["中期", "20d"],
    ["長期", "60d"],
  ].forEach(([group, key]) => {
    const row = horizons[key] || {};
    const tr = document.createElement("tr");
    [
      group,
      key.toUpperCase(),
      row.actionableSamples || 0,
      row.pending || 0,
      row.hits || 0,
      fmtPrecision(row.precision),
      fmtPctRatio(row.averageReturn),
      Number.isFinite(Number(row.profitFactor)) ? Number(row.profitFactor).toFixed(2) : "-",
      fmtPctRatio(row.maxDrawdown),
    ].forEach((value, index) => {
      const td = document.createElement("td");
      td.textContent = value;
      if (index === 6) td.className = profitClass(row.averageReturn);
      if (index === 8) td.className = profitClass(row.maxDrawdown);
      tr.append(td);
    });
    htbody.append(tr);
  });
  horizonTable.append(htbody);
  const horizonWrap = document.createElement("div");
  horizonWrap.className = "rrv-table-wrap";
  horizonWrap.append(horizonTable);
  box.append(horizonWrap);

  if (sessionGroups.length) {
    const sessionTitle = document.createElement("div");
    sessionTitle.className = "rrv-note";
    sessionTitle.textContent = "分時段模型測試（看開盤、盤中、收盤哪個時段比較準）";
    box.append(sessionTitle);
    const sessionTable = document.createElement("table");
    sessionTable.className = "rrv-table";
    const sthead = document.createElement("thead");
    const str = document.createElement("tr");
    ["時段", "訊號", "買賣", "主天期", "主天期精準", "1日", "5日", "20日", "60日", "待驗證"].forEach((text) => {
      const th = document.createElement("th");
      th.textContent = text;
      str.append(th);
    });
    sthead.append(str);
    sessionTable.append(sthead);
    const stbody = document.createElement("tbody");
    sessionGroups.forEach((item) => {
      const primary = item.primaryMetrics || {};
      const h = item.horizons || {};
      const row = document.createElement("tr");
      const sessionName = `${item.label || item.key || "-"}${item.time ? ` ${item.time}` : ""}`;
      [
        sessionName,
        item.signals || 0,
        item.actionableSignals || 0,
        String(item.primary || "-").toUpperCase(),
        fmtPrecision(primary.precision),
        fmtPrecision(h["1d"]?.precision),
        fmtPrecision(h["5d"]?.precision),
        fmtPrecision(h["20d"]?.precision),
        fmtPrecision(h["60d"]?.precision),
        primary.pending || 0,
      ].forEach((value, index) => {
        const td = document.createElement("td");
        td.textContent = value;
        if ([4, 5, 6, 7, 8].includes(index)) {
          const metricMap = { 4: primary, 5: h["1d"], 6: h["5d"], 7: h["20d"], 8: h["60d"] };
          td.className = profitClass(metricMap[index]?.averageReturn);
        }
        row.append(td);
      });
      stbody.append(row);
    });
    sessionTable.append(stbody);
    const sessionWrap = document.createElement("div");
    sessionWrap.className = "rrv-table-wrap";
    sessionWrap.append(sessionTable);
    box.append(sessionWrap);
  }

  const paperTrades = payload.paperTrades || {};
  const paperTitle = document.createElement("div");
  paperTitle.className = "rrv-note";
  paperTitle.textContent = "模型虛擬交易帳（只依模型當下 BUY / SELL / EXIT 判斷買賣；後續行情只用來驗證損益）";
  box.append(paperTitle);

  const simulation = paperTrades.simulation || {};
  const shares = Number(simulation.shares || 1000);
  const buyFee = Number(simulation.buyCommissionRate);
  const sellFee = Number(simulation.sellCommissionRate);
  const sellTax = Number(simulation.sellTaxRate);
  const slippage = Number(simulation.slippageRatePerSide);
  const maxParticipation = Number(simulation.maxVolumeParticipation);
  const initialCapital = Number(simulation.initialCapital);
  const maxOpenPositions = Number(simulation.maxOpenPositions);
  const simulationNote = document.createElement("div");
  simulationNote.className = "rrv-note rrv-caveat";
  simulationNote.textContent = `模擬帳務：起始資金 ${Number.isFinite(initialCapital) ? fmtAmount(initialCapital) : "2,500,000"}，最多同時持有 ${Number.isFinite(maxOpenPositions) ? maxOpenPositions : 20} 檔；每筆 ${Number.isFinite(shares) ? shares.toLocaleString("en-US") : "1,000"} 股；買手續費 ${Number.isFinite(buyFee) ? (buyFee * 100).toFixed(4) : "0.1425"}%、賣手續費 ${Number.isFinite(sellFee) ? (sellFee * 100).toFixed(4) : "0.1425"}%、證交稅 ${Number.isFinite(sellTax) ? (sellTax * 100).toFixed(1) : "0.3"}%、每邊滑價 ${Number.isFinite(slippage) ? (slippage * 100).toFixed(2) : "0.10"}%；單筆最多占日量 ${Number.isFinite(maxParticipation) ? (maxParticipation * 100).toFixed(0) : "5"}%。${simulation.note || "未套用券商折扣或當沖稅率"}`;
  box.append(simulationNote);

  const paperSummary = document.createElement("div");
  paperSummary.className = "rrv-summary";
  const confidence = paperTrades.winRateConfidence95 || {};
  const confidenceText = confidence.low != null && confidence.high != null
    && Number.isFinite(Number(confidence.low)) && Number.isFinite(Number(confidence.high))
    ? `${fmtPrecision(confidence.low)}–${fmtPrecision(confidence.high)} (n=${confidence.samples || 0})`
    : `待累積 (n=${confidence.samples || 0})`;
  [
    `已平倉 ${paperTrades.closedCount || 0} 筆`,
    `未平倉 ${paperTrades.openCount || 0} 筆`,
    `未成交買訊 ${paperTrades.unfilledBuySignals || 0} 筆`,
    `未成交賣訊 ${paperTrades.unfilledSellSignals || 0} 筆`,
    `資金/持倉上限擋單 ${paperTrades.capitalRejectedBuySignals || 0} 筆`,
    `紙上帳權益 ${fmtAmount(paperTrades.accountEquity)}`,
    `可用現金 ${fmtAmount(paperTrades.cashBalance)}`,
    `真實持股出場 ${paperTrades.externalHoldingExitSymbols || 0} 檔/${paperTrades.externalHoldingExitSignals || 0} 筆`,
    `真模型 orphan ${paperTrades.modelOrphanSellSignals || 0} 筆`,
    `扣成本勝率 ${fmtPrecision(paperTrades.winRate)}`,
    `勝率95%區間 ${confidenceText}`,
    `平均淨報酬 ${fmtPctRatio(paperTrades.avgReturn)}`,
    `已實現淨損益 ${fmtMoney(paperTrades.closedNetPnlPerLot)}`,
    `未實現淨損益 ${fmtMoney(paperTrades.openNetPnlPerLot)}`,
    `模擬總淨損益 ${fmtMoney(paperTrades.totalNetPnlPerLot)}`,
  ].forEach((text, index) => {
    const span = document.createElement(index >= 5 ? "strong" : "span");
    if (index === 11) span.className = profitClass(paperTrades.avgReturn);
    if ([12, 13, 14].includes(index)) span.className = profitClass([paperTrades.closedNetPnlPerLot, paperTrades.openNetPnlPerLot, paperTrades.totalNetPnlPerLot][index - 12]);
    span.textContent = text;
    paperSummary.append(span);
  });
  box.append(paperSummary);

  const exitCounts = paperTrades.exitReasonCounts || {};
  const orphanBreakdown = paperTrades.orphanSellBreakdown || [];
  const unfilledBuyReasons = paperTrades.unfilledBuyReasonCounts || {};
  const unfilledSellReasons = paperTrades.unfilledSellReasonCounts || {};
  if (Object.keys(exitCounts).length || orphanBreakdown.length || Object.keys(unfilledBuyReasons).length || Object.keys(unfilledSellReasons).length) {
    const diagTitle = document.createElement("div");
    diagTitle.className = "rrv-note";
    diagTitle.textContent = "紙上交易診斷（拆出停利、停損、模型賣訊與真實持股出場）";
    box.append(diagTitle);
    const table = document.createElement("table");
    table.className = "rrv-table";
    const thead = document.createElement("thead");
    const tr = document.createElement("tr");
    ["分類", "原因", "筆數", "真模型問題"].forEach((text) => {
      const th = document.createElement("th");
      th.textContent = text;
      tr.append(th);
    });
    thead.append(tr);
    table.append(thead);
    const tbody = document.createElement("tbody");
    Object.entries(exitCounts).forEach(([reason, count]) => {
      const row = document.createElement("tr");
      ["已平倉", exitReasonLabel(reason), count || 0, "-"].forEach((value) => {
        const td = document.createElement("td");
        td.textContent = value;
        row.append(td);
      });
      tbody.append(row);
    });
    [["未成交買訊", unfilledBuyReasons], ["未成交賣訊", unfilledSellReasons]].forEach(([group, reasons]) => {
      Object.entries(reasons).forEach(([reason, count]) => {
        const row = document.createElement("tr");
        [group, paperUnfilledReason(reason), count || 0, count || 0].forEach((value, index) => {
          const td = document.createElement("td");
          td.textContent = value;
          if (index === 3 && Number(value) > 0) td.className = "rrv-neg";
          row.append(td);
        });
        tbody.append(row);
      });
    });
    orphanBreakdown.forEach((item) => {
      const row = document.createElement("tr");
      ["未配對賣訊", item.label || item.reason || "-", item.count || 0, item.modelCount || 0].forEach((value, index) => {
        const td = document.createElement("td");
        td.textContent = value;
        if (index === 3 && Number(value) > 0) td.className = "rrv-neg";
        row.append(td);
      });
      tbody.append(row);
    });
    table.append(tbody);
    const wrap = document.createElement("div");
    wrap.className = "rrv-table-wrap";
    wrap.append(table);
    box.append(wrap);
  }

  const alignment = paperTrades.realTradeAlignment || payload.realTradeAlignment || {};
  if (alignment.realTrades) {
    const alignTitle = document.createElement("div");
    alignTitle.className = "rrv-note";
    alignTitle.textContent = `真實買賣 × 模型判斷（交易日前 ${alignment.windowDays || 3} 天內有同向模型訊號算對齊）`;
    box.append(alignTitle);
    const alignSummary = document.createElement("div");
    alignSummary.className = "rrv-summary";
    [
      `真實買進 ${alignment.buyTrades || 0} 筆`,
      `買進對齊 ${fmtPrecision(alignment.buyAlignmentRate)}`,
      `真實賣出 ${alignment.sellTrades || 0} 筆`,
      `賣出對齊 ${fmtPrecision(alignment.sellAlignmentRate)}`,
      `已平倉 ${alignment.roundTrips || 0} 筆`,
      `真實損益 ${fmtMoney(alignment.roundTripTotalPnl)}`,
      `未對齊 ${((alignment.buyMissed || 0) + (alignment.sellMissed || 0))} 筆`,
    ].forEach((text, index) => {
      const span = document.createElement(index === 1 || index === 3 ? "strong" : "span");
      if (index === 5) span.className = profitClass(alignment.roundTripTotalPnl);
      span.textContent = text;
      alignSummary.append(span);
    });
    box.append(alignSummary);

    const roundTrips = alignment.roundTripRows || [];
    if (roundTrips.length) {
      const rtTable = document.createElement("table");
      rtTable.className = "rrv-table local-trade-table";
      const rtHead = document.createElement("thead");
      const rtTr = document.createElement("tr");
      ["股票", "買進日", "賣出日", "買價", "賣價", "股數", "損益", "報酬", "模型買", "模型賣"].forEach((text) => {
        const th = document.createElement("th");
        th.textContent = text;
        rtTr.append(th);
      });
      rtHead.append(rtTr);
      rtTable.append(rtHead);
      const rtBody = document.createElement("tbody");
      roundTrips.slice(0, 40).forEach((item) => {
        const row = document.createElement("tr");
        [
          item.symbol || "-",
          item.buyDate || "-",
          item.sellDate || "-",
          fmtPrice(item.buyPrice),
          fmtPrice(item.sellPrice),
          item.shares || "-",
          fmtMoney(item.pnl),
          fmtPctRatio(item.returnPct),
          item.buyModelMatched ? `${item.buyModelSignalDate || "有"} ${item.buyLagDays ?? ""}` : "沒有",
          item.sellModelMatched ? `${item.sellModelSignalDate || "有"} ${item.sellLagDays ?? ""}` : "沒有",
        ].forEach((value, index) => {
          const td = document.createElement("td");
          td.textContent = value;
          if (index === 6) td.className = profitClass(item.pnl);
          if (index === 7) td.className = profitClass(item.returnPct);
          if ((index === 8 && !item.buyModelMatched) || (index === 9 && !item.sellModelMatched)) td.className = "rrv-neg";
          row.append(td);
        });
        rtBody.append(row);
      });
      rtTable.append(rtBody);
      const rtWrap = document.createElement("div");
      rtWrap.className = "rrv-table-wrap";
      rtWrap.append(rtTable);
      box.append(rtWrap);
    }

    const rows = alignment.rows || [];
    const table = document.createElement("table");
    table.className = "rrv-table local-trade-table";
    const thead = document.createElement("thead");
    const tr = document.createElement("tr");
    ["真實日", "股票", "買賣", "真實價", "股數", "模型對齊", "模型日", "模型策略", "落差"].forEach((text) => {
      const th = document.createElement("th");
      th.textContent = text;
      tr.append(th);
    });
    thead.append(tr);
    table.append(thead);
    const tbody = document.createElement("tbody");
    rows.slice(0, 40).forEach((item) => {
      const row = document.createElement("tr");
      [
        item.tradeDate || "-",
        item.symbol || "-",
        item.side === "BUY" ? "買" : "賣",
        fmtPrice(item.price),
        item.shares || "-",
        item.modelMatched ? "有" : "沒有",
        item.modelSignalDate || "-",
        item.modelStrategy || "-",
        Number.isFinite(Number(item.lagDays)) ? `${item.lagDays} 天` : "-",
      ].forEach((value, index) => {
        const td = document.createElement("td");
        td.textContent = value;
        if (index === 5) td.className = item.modelMatched ? "rrv-pos" : "rrv-neg";
        row.append(td);
      });
      tbody.append(row);
    });
    table.append(tbody);
    const wrap = document.createElement("div");
    wrap.className = "rrv-table-wrap";
    wrap.append(table);
    box.append(wrap);
  }

  appendPaperSnapshotSchedule(box, payload?.snapshotSchedule || []);

  appendModelCalibration(box, calibration, calibrationError);

  const closedTrades = paperTrades.closed || [];
  if (closedTrades.length) {
    const table = document.createElement("table");
    table.className = "rrv-table local-trade-table";
    const thead = document.createElement("thead");
    const tr = document.createElement("tr");
    ["股票", "策略", "時段", "週期", "成交", "買進日", "賣出日", "出場", "買價", "賣價", "毛報酬", "毛損益", "成本", "淨報酬", "淨損益", "天數"].forEach((text) => {
      const th = document.createElement("th");
      th.textContent = text;
      tr.append(th);
    });
    thead.append(tr);
    table.append(thead);
    const tbody = document.createElement("tbody");
    closedTrades.slice(0, 40).forEach((trade) => {
      const row = document.createElement("tr");
      [
        `${trade.symbol || "-"}${trade.name ? ` ${trade.name}` : ""}`,
        `${trade.entryStrategy || "-"} → ${trade.exitStrategy || "-"}`,
        signalSessionText(trade),
        tradeHorizonText(trade),
        paperFillText(trade),
        trade.entryDate || "-",
        trade.exitDate || "-",
        paperExitReasonText(trade),
        fmtPrice(trade.entryPrice),
        fmtPrice(trade.exitPrice),
        fmtPctRatio(trade.returnPct),
        fmtMoney(trade.pnlPerLot),
        fmtMoney(trade.totalCosts),
        fmtPctRatio(trade.netReturnPct),
        fmtMoney(trade.netPnlPerLot),
        Number.isFinite(Number(trade.holdDays)) ? trade.holdDays : "-",
      ].forEach((value, index) => {
        const td = document.createElement("td");
        td.textContent = value;
        if (index === 10) td.className = profitClass(trade.returnPct);
        if (index === 11) td.className = profitClass(trade.pnlPerLot);
        if (index === 13) td.className = profitClass(trade.netReturnPct);
        if (index === 14) td.className = profitClass(trade.netPnlPerLot);
        row.append(td);
      });
      tbody.append(row);
    });
    table.append(tbody);
    const wrap = document.createElement("div");
    wrap.className = "rrv-table-wrap";
    wrap.append(table);
    box.append(wrap);
  } else {
    const noClosed = document.createElement("div");
    noClosed.className = "rrv-note rrv-caveat";
    noClosed.textContent = "模型目前還沒有可配對的 BUY→SELL/EXIT round-trip；有買進後再出現賣出/出場訊號才會形成模型虛擬已平倉。";
    box.append(noClosed);
  }

  const openTrades = paperTrades.open || [];
  if (openTrades.length) {
    const openTitle = document.createElement("div");
    openTitle.className = "rrv-note";
    openTitle.textContent = "模型未平倉";
    box.append(openTitle);
    const table = document.createElement("table");
    table.className = "rrv-table local-trade-table";
    const thead = document.createElement("thead");
    const tr = document.createElement("tr");
    ["股票", "策略", "時段", "週期", "成交", "買進日", "買價", "最新日", "最新價", "未實現毛報酬", "未實現毛損益", "估計成本", "未實現淨報酬", "未實現淨損益"].forEach((text) => {
      const th = document.createElement("th");
      th.textContent = text;
      tr.append(th);
    });
    thead.append(tr);
    table.append(thead);
    const tbody = document.createElement("tbody");
    openTrades.slice(0, 30).forEach((trade) => {
      const row = document.createElement("tr");
      [
        `${trade.symbol || "-"}${trade.name ? ` ${trade.name}` : ""}`,
        trade.entryStrategy || "-",
        signalSessionText(trade),
        tradeHorizonText(trade),
        paperFillText(trade),
        trade.entryDate || "-",
        fmtPrice(trade.entryPrice),
        trade.latestDate || "-",
        fmtPrice(trade.latestPrice),
        fmtPctRatio(trade.unrealizedReturnPct),
        fmtMoney(trade.unrealizedPnlPerLot),
        fmtMoney(trade.totalCosts),
        fmtPctRatio(trade.unrealizedNetReturnPct),
        fmtMoney(trade.unrealizedNetPnlPerLot),
      ].forEach((value, index) => {
        const td = document.createElement("td");
        td.textContent = value;
        if (index === 9) td.className = profitClass(trade.unrealizedReturnPct);
        if (index === 10) td.className = profitClass(trade.unrealizedPnlPerLot);
        if (index === 12) td.className = profitClass(trade.unrealizedNetReturnPct);
        if (index === 13) td.className = profitClass(trade.unrealizedNetPnlPerLot);
        row.append(td);
      });
      tbody.append(row);
    });
    table.append(tbody);
    const wrap = document.createElement("div");
    wrap.className = "rrv-table-wrap";
    wrap.append(table);
    box.append(wrap);
  }

  const unfilledOrders = [
    ...(paperTrades.unfilledBuys || []).map((trade) => ({ ...trade, direction: "買", date: trade.entryDate, mode: trade.entryFillMode, note: trade.entryFillNote, participation: trade.entryVolumeParticipation })),
    ...(paperTrades.unfilledSells || []).map((trade) => ({ ...trade, direction: "賣", date: trade.exitDate, mode: trade.exitFillMode, note: trade.exitFillNote, participation: trade.exitVolumeParticipation })),
  ];
  if (unfilledOrders.length) {
    const unfilledTitle = document.createElement("div");
    unfilledTitle.className = "rrv-note";
    unfilledTitle.textContent = "模型未成交訊號（不列入持倉與損益）";
    box.append(unfilledTitle);
    const table = document.createElement("table");
    table.className = "rrv-table local-trade-table";
    const thead = document.createElement("thead");
    const tr = document.createElement("tr");
    ["方向", "股票", "訊號日", "訊號價", "原因", "占日量", "說明"].forEach((text) => {
      const th = document.createElement("th");
      th.textContent = text;
      tr.append(th);
    });
    thead.append(tr);
    table.append(thead);
    const tbody = document.createElement("tbody");
    unfilledOrders.slice(0, 50).forEach((trade) => {
      const row = document.createElement("tr");
      const participation = Number(trade.participation);
      [
        trade.direction,
        `${trade.symbol || "-"}${trade.name ? ` ${trade.name}` : ""}`,
        trade.date || "-",
        fmtPrice(trade.signalPrice),
        paperUnfilledReason(trade.mode),
        Number.isFinite(participation) ? `${(participation * 100).toFixed(2)}%` : "-",
        trade.note || "-",
      ].forEach((value, index) => {
        const td = document.createElement("td");
        td.textContent = value;
        if (index === 4) td.className = "rrv-neg";
        row.append(td);
      });
      tbody.append(row);
    });
    table.append(tbody);
    const wrap = document.createElement("div");
    wrap.className = "rrv-table-wrap";
    wrap.append(table);
    box.append(wrap);
  }

  const orphanGroups = paperTrades.orphanSellGroups || [];
  const externalStates = paperTrades.externalHoldingExitStates || [];
  if (externalStates.length) {
    const externalTitle = document.createElement("div");
    externalTitle.className = "rrv-note";
    externalTitle.textContent = "真實持股出場狀態（每檔只看最新，不把每天提醒當模型錯誤）";
    box.append(externalTitle);
    const table = document.createElement("table");
    table.className = "rrv-table local-trade-table";
    const thead = document.createElement("thead");
    const tr = document.createElement("tr");
    ["股票", "最新日期", "時段", "重複日", "累計筆", "最新價", "最新判斷"].forEach((text) => {
      const th = document.createElement("th");
      th.textContent = text;
      tr.append(th);
    });
    thead.append(tr);
    table.append(thead);
    const tbody = document.createElement("tbody");
    externalStates.slice(0, 30).forEach((item) => {
      const row = document.createElement("tr");
      [
        `${item.symbol || "-"}${item.name ? ` ${item.name}` : ""}`,
        item.latestDate || "-",
        `${item.latestSession || "-"}${item.latestSignalTime ? ` ${item.latestSignalTime}` : ""}`,
        item.repeatDays || 0,
        item.count || 0,
        fmtPrice(item.latestPrice),
        item.latestDecision || "-",
      ].forEach((value, index) => {
        const td = document.createElement("td");
        td.textContent = value;
        if (index === 4 && Number(value) >= 5) td.className = "rrv-neg";
        row.append(td);
      });
      tbody.append(row);
    });
    table.append(tbody);
    const wrap = document.createElement("div");
    wrap.className = "rrv-table-wrap";
    wrap.append(table);
    box.append(wrap);
  }

  const modelOrphanGroups = orphanGroups.filter((item) => Number(item.modelCount || 0) > 0);
  if (modelOrphanGroups.length) {
    const orphanTitle = document.createElement("div");
    orphanTitle.className = "rrv-note";
    orphanTitle.textContent = "真模型 orphan 賣訊最多的股票";
    box.append(orphanTitle);
    const table = document.createElement("table");
    table.className = "rrv-table";
    const thead = document.createElement("thead");
    const tr = document.createElement("tr");
    ["股票", "總筆數", "真模型 orphan", "最後日期", "主要原因"].forEach((text) => {
      const th = document.createElement("th");
      th.textContent = text;
      tr.append(th);
    });
    thead.append(tr);
    table.append(thead);
    const tbody = document.createElement("tbody");
    modelOrphanGroups.slice(0, 20).forEach((item) => {
      const row = document.createElement("tr");
      [
        `${item.symbol || "-"}${item.name ? ` ${item.name}` : ""}`,
        item.count || 0,
        item.modelCount || 0,
        item.latestDate || "-",
        item.reasonLabel || item.reason || "-",
      ].forEach((value, index) => {
        const td = document.createElement("td");
        td.textContent = value;
        if (index === 2 && Number(value) > 0) td.className = "rrv-neg";
        row.append(td);
      });
      tbody.append(row);
    });
    table.append(tbody);
    const wrap = document.createElement("div");
    wrap.className = "rrv-table-wrap";
    wrap.append(table);
    box.append(wrap);
  }

  const strategies = (payload.strategies || [])
    .slice()
    .sort((a, b) => (b.samples || 0) - (a.samples || 0))
    .slice(0, 8);
  if (strategies.length) {
    const stTitle = document.createElement("div");
    stTitle.className = "rrv-note";
    stTitle.textContent = "策略分組（以 5 日結果排序顯示前 8 組）";
    box.append(stTitle);
    const table = document.createElement("table");
    table.className = "rrv-table";
    const thead = document.createElement("thead");
    const tr = document.createElement("tr");
    ["策略", "訊號", "已驗證", "命中率", "平均方向報酬", "待驗證"].forEach((text) => {
      const th = document.createElement("th");
      th.textContent = text;
      tr.append(th);
    });
    thead.append(tr);
    table.append(thead);
    const tbody = document.createElement("tbody");
    strategies.forEach((item) => {
      const row = document.createElement("tr");
      [
        item.strategy || "-",
        item.signals || 0,
        item.samples || 0,
        fmtPrecision(item.precision5d),
        fmtPctRatio(item.averageReturn5d),
        item.pending5d || 0,
      ].forEach((value, index) => {
        const td = document.createElement("td");
        td.textContent = value;
        if (index === 4) td.className = profitClass(item.averageReturn5d);
        row.append(td);
      });
      tbody.append(row);
    });
    table.append(tbody);
    const wrap = document.createElement("div");
    wrap.className = "rrv-table-wrap";
    wrap.append(table);
    box.append(wrap);
  }

  const recentTitle = document.createElement("div");
  recentTitle.className = "rrv-note";
  recentTitle.textContent = "最近模型買賣訊號";
  box.append(recentTitle);

  const recentTable = document.createElement("table");
  recentTable.className = "rrv-table local-trade-table";
  const rthead = document.createElement("thead");
  const rtr = document.createElement("tr");
  ["日期", "時段", "股票", "方向", "策略", "週期", "判斷", "價格", "1日", "3日", "5日", "10日", "20日", "60日"].forEach((text) => {
    const th = document.createElement("th");
    th.textContent = text;
    rtr.append(th);
  });
  rthead.append(rtr);
  recentTable.append(rthead);
  const rtbody = document.createElement("tbody");
  recent.slice(0, 40).forEach((signal) => {
    const tr = document.createElement("tr");
    const horizonText = (key) => {
      const h = (signal.horizons || {})[key] || {};
      if (!Number.isFinite(Number(h.adjustedReturn))) return "待";
      return `${h.hit ? "中" : "失"} ${fmtPctRatio(h.adjustedReturn)}`;
    };
    [
      signal.signalDate || "-",
      signalSessionText(signal),
      `${signal.symbol || "-"}${signal.name ? ` ${signal.name}` : ""}`,
      modelSignalSideLabel(signal.side),
      signal.strategy || "-",
      tradeHorizonText(signal),
      signal.decision || "-",
      fmtPrice(signal.price),
      horizonText("1d"),
      horizonText("3d"),
      horizonText("5d"),
      horizonText("10d"),
      horizonText("20d"),
      horizonText("60d"),
    ].forEach((value, index) => {
      const td = document.createElement("td");
      td.textContent = value;
      if (index >= 8 && String(value).startsWith("中")) td.className = "rrv-pos";
      if (index >= 8 && String(value).startsWith("失")) td.className = "rrv-neg";
      tr.append(td);
    });
    rtbody.append(tr);
  });
  recentTable.append(rtbody);
  const recentWrap = document.createElement("div");
  recentWrap.className = "rrv-table-wrap";
  recentWrap.append(recentTable);
  box.append(recentWrap);
}

async function renderLocalTradeLedger() {
  const box = document.getElementById("localTradeLedger");
  if (!box) return;
  let payload;
  try {
    const response = await fetch("/api/trades?limit=120");
    payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "讀取失敗");
  } catch (error) {
    box.replaceChildren();
    const msg = document.createElement("div");
    msg.className = "rrv-note";
    msg.textContent = `讀取本地交易紀錄失敗：${friendlyError(error)}`;
    box.append(msg);
    return;
  }
  const rows = payload.trades || [];
  box.replaceChildren();
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "rrv-note rrv-caveat";
    empty.textContent = "目前沒有真實委託/成交紀錄。紙上訊號不列在這裡；同步永豐成交回報或系統下單後會出現在這裡。";
    box.append(empty);
    return;
  }

  const head = document.createElement("div");
  head.className = "rrv-head";
  const title = document.createElement("strong");
  title.textContent = "本地 trades 表（原始成交 / 券商成本 / round-trip）";
  head.append(title);
  box.append(head);

  const note = document.createElement("div");
  note.className = "rrv-note";
  note.textContent = "這裡只看真實成交，紙上訊號已排除。原始成交價用於停損停利，券商成本用於持倉淨損益；兩者因手續費、除息或成本調整可能不同。策略週期仍只接受成交時或人工確認，不由持有天數猜測。";
  box.append(note);

  const dedupeActions = document.createElement("div");
  dedupeActions.className = "inline-actions";
  const checkBtn = document.createElement("button");
  checkBtn.type = "button";
  checkBtn.className = "mini-action-button";
  checkBtn.textContent = "檢查重複";
  const cleanBtn = document.createElement("button");
  cleanBtn.type = "button";
  cleanBtn.className = "mini-action-button danger-action";
  cleanBtn.textContent = "清理 exact duplicate";
  dedupeActions.append(checkBtn, cleanBtn);
  box.append(dedupeActions);

  const duplicateBox = document.createElement("div");
  duplicateBox.className = "rrv-note";
  box.append(duplicateBox);
  const showDuplicateResult = (payload) => {
    if (!payload?.ok) {
      duplicateBox.textContent = `重複檢查失敗：${payload?.error || "未知錯誤"}`;
      return;
    }
    duplicateBox.textContent =
      `重複群組 ${payload.groups || 0} 組，重複列 ${payload.duplicateRows || 0} 筆` +
      (payload.deleted ? `，已刪 ${payload.deleted} 筆` : "");
  };
  checkBtn.addEventListener("click", async () => {
    const payload = await fetch("/api/trades/duplicates").then((r) => r.json()).catch((error) => ({ ok: false, error: friendlyError(error) }));
    showDuplicateResult(payload);
  });
  cleanBtn.addEventListener("click", async () => {
    const preview = await fetch("/api/trades/duplicates").then((r) => r.json()).catch((error) => ({ ok: false, error: friendlyError(error) }));
    if (!preview.ok || !preview.duplicateRows) {
      showDuplicateResult(preview);
      return;
    }
    if (!window.confirm(`確定刪除 ${preview.duplicateRows} 筆 exact duplicate？會保留每組最早那筆。`)) return;
    const payload = await fetch("/api/trades/deduplicate", { method: "POST", body: "{}" }).then((r) => r.json()).catch((error) => ({ ok: false, error: friendlyError(error) }));
    showDuplicateResult(payload);
    await renderLocalTradeLedger();
  });

  const table = document.createElement("table");
  table.className = "rrv-table local-trade-table";
  const thead = document.createElement("thead");
  const htr = document.createElement("tr");
  ["股票", "買賣", "狀態", "成交策略", "成交時間", "賣出日", "原始成交價", "券商成本/股", "股數", "損益", "Broker"].forEach((text) => {
    const th = document.createElement("th");
    th.textContent = text;
    htr.append(th);
  });
  thead.append(htr);
  table.append(thead);

  const statusLabels = {
    paper: "紙上",
    submitted: "已委託",
    partial: "部分成交",
    filled: "已成交",
    closed: "已平倉",
  };
  const tbody = document.createElement("tbody");
  rows.forEach((trade) => {
    const tr = document.createElement("tr");
    const dealAt = String(trade.filled_at || trade.buy_at || trade.buyDate || "").replace("T", " ") || "-";
    const sellDate = String(trade.exit_at || "").slice(0, 10) || "-";
    const refs = [
      trade.broker_dseq,
      trade.broker_seqno,
      trade.broker_ordno,
      trade.broker_order_id,
    ].filter((value, index, values) => value && values.indexOf(value) === index).join(" / ") || "-";
    const evidence = Number(trade.executionEvidenceCount || 0);
    const brokerText = `${refs}${evidence ? `｜證據 ${evidence}` : ""}`;
    const pnlText = trade.pnl == null
      ? "-"
      : `${fmtMoney(trade.pnl)}${trade.pnl_basis === "sinopac_realized" ? "（永豐）" : "（毛）"}`;
    const cells = [
      trade.symbol || "-",
      trade.side || "-",
      statusLabels[trade.status] || trade.status || "-",
      trade.strategyHorizon === "short_trade" ? "短期" : trade.strategyHorizon === "mid_swing" ? "中期" : trade.strategyHorizon === "long_trend" ? "長期" : "未知",
      dealAt,
      sellDate,
      fmtPrice(trade.executionPrice || trade.execution_price || trade.price),
      trade.side === "BUY" ? fmtPrice(trade.costBasisPrice) : "-",
      trade.filled_shares || trade.shares || "-",
      pnlText,
      brokerText,
    ];
    cells.forEach((value, index) => {
      const td = document.createElement("td");
      td.textContent = value;
      if (index === 9) td.className = profitClass(trade.pnl);
      tr.append(td);
    });
    tbody.append(tr);
  });
  table.append(tbody);
  const wrap = document.createElement("div");
  wrap.className = "rrv-table-wrap";
  wrap.append(table);
  box.append(wrap);
}

function openLegacyLotImport(item) {
  document.querySelector(".legacy-lot-dialog")?.remove();
  const expectedShares = Number(item.migratableShares || 0);
  if (!Number.isFinite(expectedShares) || expectedShares <= 0) return;

  const dialog = document.createElement("dialog");
  dialog.className = "legacy-lot-dialog";
  const form = document.createElement("form");
  form.className = "legacy-lot-form";
  const head = document.createElement("div");
  head.className = "legacy-lot-head";
  const heading = document.createElement("strong");
  heading.textContent = `${item.symbol} ${item.name && item.name !== item.symbol ? item.name : ""}｜分批 lot`;
  const closeButton = document.createElement("button");
  closeButton.type = "button";
  closeButton.className = "icon-action-button";
  closeButton.textContent = "×";
  closeButton.title = "關閉";
  closeButton.setAttribute("aria-label", "關閉分批 lot 視窗");
  head.append(heading, closeButton);

  const summary = document.createElement("div");
  summary.className = "legacy-lot-summary";
  summary.textContent = `待補 ${expectedShares.toLocaleString("en-US")} 股｜券商均價 ${fmtPrice(item.brokerAveragePrice)}` +
    (Number(item.legacyPlaceholderShares) > 0 ? `｜含占位 ${Number(item.legacyPlaceholderShares).toLocaleString("en-US")} 股` : "");

  const labels = document.createElement("div");
  labels.className = "legacy-lot-labels";
  ["買進日", "每股成本", "股數", "策略", ""].forEach((text) => {
    const span = document.createElement("span");
    span.textContent = text;
    labels.append(span);
  });
  const rowsBox = document.createElement("div");
  rowsBox.className = "legacy-lot-rows";
  const totals = document.createElement("div");
  totals.className = "legacy-lot-totals";
  totals.setAttribute("aria-live", "polite");
  const status = document.createElement("div");
  status.className = "legacy-lot-status";
  status.setAttribute("aria-live", "polite");

  const actions = document.createElement("div");
  actions.className = "legacy-lot-dialog-actions";
  const addButton = document.createElement("button");
  addButton.type = "button";
  addButton.textContent = "新增批次";
  const cancelButton = document.createElement("button");
  cancelButton.type = "button";
  cancelButton.textContent = "取消";
  const submitButton = document.createElement("button");
  submitButton.type = "submit";
  submitButton.className = "primary-action";
  submitButton.textContent = "確認匯入";
  submitButton.disabled = true;
  actions.append(addButton, cancelButton, submitButton);

  const today = new Intl.DateTimeFormat("sv-SE", {
    timeZone: "Asia/Taipei", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(new Date());

  const readLots = () => Array.from(rowsBox.querySelectorAll(".legacy-lot-row")).map((row) => ({
    buyDate: row.querySelector("[data-lot-date]")?.value || "",
    price: Number(row.querySelector("[data-lot-price]")?.value || 0),
    shares: Number(row.querySelector("[data-lot-shares]")?.value || 0),
    strategyHorizon: row.querySelector("[data-lot-horizon]")?.value || "",
  }));

  const updateTotals = () => {
    const lots = readLots();
    const shares = lots.reduce((sum, lot) => sum + (Number.isFinite(lot.shares) ? lot.shares : 0), 0);
    const remaining = expectedShares - shares;
    totals.textContent = `已填 ${shares.toLocaleString("en-US")} 股｜${remaining === 0 ? "股數吻合" : `尚差 ${remaining.toLocaleString("en-US")} 股`}`;
    totals.classList.toggle("valid", remaining === 0);
    const complete = lots.length > 0 && lots.every((lot) =>
      lot.buyDate && lot.price > 0 && Number.isInteger(lot.shares) && lot.shares > 0 && lot.strategyHorizon
    );
    submitButton.disabled = !(complete && remaining === 0);
  };

  const addRow = (suggestedShares = "") => {
    const row = document.createElement("div");
    row.className = "legacy-lot-row";
    const dateInput = document.createElement("input");
    dateInput.type = "date";
    dateInput.max = today;
    dateInput.required = true;
    dateInput.dataset.lotDate = "1";
    dateInput.setAttribute("aria-label", "買進日");
    const priceInput = document.createElement("input");
    priceInput.type = "number";
    priceInput.min = "0.01";
    priceInput.step = "0.01";
    priceInput.required = true;
    priceInput.dataset.lotPrice = "1";
    priceInput.setAttribute("aria-label", "每股成本");
    const sharesInput = document.createElement("input");
    sharesInput.type = "number";
    sharesInput.min = "1";
    sharesInput.step = "1";
    sharesInput.required = true;
    sharesInput.value = suggestedShares;
    sharesInput.dataset.lotShares = "1";
    sharesInput.setAttribute("aria-label", "股數");
    const horizonSelect = document.createElement("select");
    horizonSelect.required = true;
    horizonSelect.dataset.lotHorizon = "1";
    horizonSelect.setAttribute("aria-label", "策略週期");
    [["", "選擇週期"], ["short_trade", "短期"], ["mid_swing", "中期"], ["long_trend", "長期"]]
      .forEach(([value, label]) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        if (!value) {
          option.disabled = true;
          option.selected = true;
        }
        horizonSelect.append(option);
      });
    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "icon-action-button";
    removeButton.textContent = "×";
    removeButton.title = "移除批次";
    removeButton.setAttribute("aria-label", "移除這批 lot");
    removeButton.addEventListener("click", () => {
      if (rowsBox.children.length <= 1) return;
      row.remove();
      updateTotals();
    });
    [dateInput, priceInput, sharesInput, horizonSelect].forEach((control) => {
      control.addEventListener("input", updateTotals);
      control.addEventListener("change", updateTotals);
    });
    row.append(dateInput, priceInput, sharesInput, horizonSelect, removeButton);
    rowsBox.append(row);
    updateTotals();
  };

  addButton.addEventListener("click", () => {
    const used = readLots().reduce((sum, lot) => sum + (lot.shares || 0), 0);
    addRow(Math.max(0, expectedShares - used) || "");
  });
  closeButton.addEventListener("click", () => dialog.close());
  cancelButton.addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const lots = readLots();
    if (submitButton.disabled) return;
    if (!window.confirm(`確定匯入 ${item.symbol} 的 ${lots.length} 批、合計 ${expectedShares} 股？買進日、成本與策略週期將寫入本地 trades 並保留稽核紀錄。`)) return;
    submitButton.disabled = true;
    addButton.disabled = true;
    status.textContent = "寫入中...";
    try {
      const response = await fetch("/api/portfolio/legacy-lots", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol: item.symbol, lots }),
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || "匯入失敗");
      dialog.close();
      await Promise.all([renderPortfolioExitAnalysis(), renderLocalTradeLedger()]);
    } catch (error) {
      status.textContent = `失敗：${friendlyError(error)}`;
      addButton.disabled = false;
      updateTotals();
    }
  });

  form.append(head, summary, labels, rowsBox, totals, status, actions);
  dialog.append(form);
  document.body.append(dialog);
  addRow(expectedShares);
  dialog.showModal();
}

function openBatchHorizonLock(items) {
  document.querySelector(".batch-horizon-dialog")?.remove();
  const unknownItems = (items || []).filter((item) =>
    item?.hasUnknownHorizon === true ||
    item?.strategyHorizon === "unknown" ||
    (item?.lots || []).some((lot) => lot?.strategyHorizon === "unknown")
  );
  if (!unknownItems.length) return;

  const dialog = document.createElement("dialog");
  dialog.className = "legacy-lot-dialog batch-horizon-dialog";
  const form = document.createElement("form");
  form.className = "legacy-lot-form";
  const head = document.createElement("div");
  head.className = "legacy-lot-head";
  const heading = document.createElement("strong");
  heading.textContent = "批次鎖定持股策略週期";
  const closeButton = document.createElement("button");
  closeButton.type = "button";
  closeButton.className = "icon-action-button";
  closeButton.textContent = "×";
  closeButton.title = "關閉";
  closeButton.setAttribute("aria-label", "關閉批次週期視窗");
  head.append(heading, closeButton);

  const summary = document.createElement("div");
  summary.className = "legacy-lot-summary";
  summary.textContent = `${unknownItems.length} 檔待確認。系統不預設、不推測；全部選完且通過持股股數驗證才會一次寫入。`;
  const labels = document.createElement("div");
  labels.className = "batch-horizon-labels";
  ["股票", "持股", "買進日", "策略週期"].forEach((text) => {
    const span = document.createElement("span");
    span.textContent = text;
    labels.append(span);
  });
  const rowsBox = document.createElement("div");
  rowsBox.className = "batch-horizon-rows";
  unknownItems.forEach((item) => {
    const row = document.createElement("div");
    row.className = "batch-horizon-row";
    row.dataset.symbol = item.symbol || "";
    const stock = document.createElement("strong");
    stock.textContent = `${item.symbol || "-"}${item.name && item.name !== item.symbol ? ` ${item.name}` : ""}`;
    const shares = document.createElement("span");
    shares.textContent = `${Number(item.positionShares || item.brokerShares || 0).toLocaleString("en-US")} 股`;
    const buyDate = document.createElement("span");
    buyDate.textContent = item.positionBuyDateKnown ? "完整" : "部分／全部未知";
    const select = document.createElement("select");
    select.dataset.batchHorizon = "1";
    select.setAttribute("aria-label", `${item.symbol} 策略週期`);
    [["", "請選擇"], ["short_trade", "短期｜10 日"], ["mid_swing", "中期｜20～60 日"], ["long_trend", "長期｜60 日以上"]]
      .forEach(([value, label]) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        if (!value) {
          option.disabled = true;
          option.selected = true;
        }
        select.append(option);
      });
    row.append(stock, shares, buyDate, select);
    rowsBox.append(row);
  });

  const status = document.createElement("div");
  status.className = "legacy-lot-status";
  status.setAttribute("aria-live", "polite");
  const actions = document.createElement("div");
  actions.className = "legacy-lot-dialog-actions";
  const cancelButton = document.createElement("button");
  cancelButton.type = "button";
  cancelButton.textContent = "取消";
  const submitButton = document.createElement("button");
  submitButton.type = "submit";
  submitButton.className = "primary-action";
  submitButton.textContent = "驗證全部並鎖定";
  submitButton.disabled = true;
  actions.append(cancelButton, submitButton);

  const readAssignments = () => Array.from(rowsBox.querySelectorAll(".batch-horizon-row")).map((row) => ({
    symbol: row.dataset.symbol || "",
    strategyHorizon: row.querySelector("[data-batch-horizon]")?.value || "",
  }));
  const updateReady = () => {
    const assignments = readAssignments();
    submitButton.disabled = !assignments.length || assignments.some((item) => !item.strategyHorizon);
  };
  rowsBox.querySelectorAll("select").forEach((select) => select.addEventListener("change", updateReady));
  closeButton.addEventListener("click", () => dialog.close());
  cancelButton.addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => dialog.remove(), { once: true });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitButton.disabled) return;
    const assignments = readAssignments();
    submitButton.disabled = true;
    rowsBox.querySelectorAll("select").forEach((select) => { select.disabled = true; });
    status.textContent = "驗證券商股數與本地 FIFO lot...";
    try {
      const previewResponse = await fetch("/api/portfolio/strategy-horizons/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: "preview", assignments }),
      });
      const preview = await previewResponse.json();
      if (!previewResponse.ok || !preview.ok) throw new Error(preview.error || "批次預覽失敗");
      const unknownDates = (preview.batch?.items || []).filter((item) => !item.buyDateKnown).length;
      const confirmed = window.confirm(
        `已驗證 ${preview.batch?.assignmentCount || assignments.length} 檔持股。\n` +
        `${unknownDates} 檔含未知買進日，這些部位仍不啟用時間出場。\n` +
        "確定一次鎖定？完成後不能每日重新分類或覆寫。"
      );
      if (!confirmed) {
        status.textContent = "已取消，尚未寫入任何資料。";
        rowsBox.querySelectorAll("select").forEach((select) => { select.disabled = false; });
        updateReady();
        return;
      }
      status.textContent = "整批寫入中...";
      const applyResponse = await fetch("/api/portfolio/strategy-horizons/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: "apply", confirmAll: true, assignments }),
      });
      const result = await applyResponse.json();
      if (!applyResponse.ok || !result.ok) throw new Error(result.error || "批次鎖定失敗");
      dialog.close();
      await Promise.all([renderPortfolioExitAnalysis(), renderLocalTradeLedger()]);
    } catch (error) {
      status.textContent = `失敗：${friendlyError(error)}`;
      rowsBox.querySelectorAll("select").forEach((select) => { select.disabled = false; });
      updateReady();
    }
  });

  form.append(head, summary, labels, rowsBox, status, actions);
  dialog.append(form);
  document.body.append(dialog);
  dialog.showModal();
}

async function renderMarketSessionValidation() {
  const box = document.getElementById("marketSessionValidationReview");
  if (!box) return;
  let payload;
  try {
    const response = await fetch("/api/market/session-validations?limit=20", { cache: "no-store" });
    payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "讀取失敗");
  } catch (error) {
    box.replaceChildren();
    const message = document.createElement("div");
    message.className = "rrv-note";
    message.textContent = `交易日驗收讀取失敗：${friendlyError(error)}`;
    box.append(message);
    return;
  }

  const acceptance = payload.acceptance || {};
  const stages = acceptance.stages || {};
  box.replaceChildren();
  const head = document.createElement("div");
  head.className = "rrv-head";
  const title = document.createElement("strong");
  title.textContent = "交易日三階段驗收";
  const date = document.createElement("span");
  date.textContent = acceptance.sessionDate || "-";
  head.append(title, date);
  box.append(head);

  const state = document.createElement("div");
  state.className = "rrv-note";
  state.textContent = acceptance.fullDayReady
    ? "開盤、盤中、收盤均已通過"
    : acceptance.entryGuardReady
      ? "開盤閘門已通過，等待其餘階段"
      : "開盤閘門未通過，不產生盤中買進放行";
  box.append(state);

  const table = document.createElement("table");
  table.className = "rrv-table session-validation-table";
  const thead = document.createElement("thead");
  const headerRow = document.createElement("tr");
  ["階段", "狀態", "檢查時間", "結果"].forEach((text) => {
    const th = document.createElement("th");
    th.textContent = text;
    headerRow.append(th);
  });
  thead.append(headerRow);
  table.append(thead);
  const tbody = document.createElement("tbody");
  [
    ["open", "09:05 開盤"],
    ["intraday", "09:50 盤中"],
    ["close", "18:00 收盤"],
  ].forEach(([key, label]) => {
    const stage = stages[key];
    const row = document.createElement("tr");
    const stageCell = document.createElement("td");
    stageCell.textContent = label;
    const statusCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `rrv-badge ${stage ? (stage.ok ? "rrv-st-rec" : "rrv-st-cand") : "rrv-st-none"}`;
    badge.textContent = stage ? (stage.ok ? "通過" : "失敗") : "未執行";
    statusCell.append(badge);
    const checkedCell = document.createElement("td");
    checkedCell.textContent = stage?.checkedAt || "-";
    const detailCell = document.createElement("td");
    const failures = stage?.failures || [];
    const warnings = stage?.warnings || [];
    detailCell.textContent = failures.length
      ? failures.join("、")
      : warnings.length
        ? `警告：${warnings.join("、")}`
        : stage ? "必要檢查通過" : "-";
    row.append(stageCell, statusCell, checkedCell, detailCell);
    tbody.append(row);
  });
  table.append(tbody);
  box.append(table);
}

async function renderPortfolioExitAnalysis() {
  const box = document.getElementById("portfolioExitAnalysisReview");
  if (!box) return;
  let payload;
  try {
    const response = await fetch("/api/portfolio/exit-analysis", { cache: "no-store" });
    payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "讀取失敗");
  } catch (error) {
    box.replaceChildren();
    const message = document.createElement("div");
    message.className = "rrv-note";
    message.textContent = `讀取後端統一出場計算失敗：${friendlyError(error)}`;
    box.append(message);
    return;
  }

  const items = payload.items || [];
  box.replaceChildren();
  const head = document.createElement("div");
  head.className = "rrv-head";
  const title = document.createElement("strong");
  title.textContent = `後端統一出場計算（${payload.policy?.version || "portfolio-exit"}）`;
  const meta = document.createElement("span");
  meta.textContent = `更新 ${payload.generatedAt || "最新快照"}`;
  head.append(title, meta);
  box.append(head);

  const note = document.createElement("div");
  note.className = "rrv-note";
  note.textContent = "主頁、通知與本頁共用這份後端結果；策略週期取自買進成交紀錄，買進日取自 FIFO 成交回報。未知買進日不啟用時間出場。";
  box.append(note);

  const unknownItems = items.filter((item) =>
    item?.hasUnknownHorizon === true || item?.strategyHorizon === "unknown" ||
    (item?.lots || []).some((lot) => lot?.strategyHorizon === "unknown")
  );
  if (unknownItems.length) {
    const batchActions = document.createElement("div");
    batchActions.className = "batch-horizon-actions";
    const batchButton = document.createElement("button");
    batchButton.type = "button";
    batchButton.textContent = `批次確認 ${unknownItems.length} 檔週期`;
    batchButton.title = "逐檔選擇短期、中期或長期；驗證全部通過後以單一交易鎖定";
    batchButton.addEventListener("click", () => openBatchHorizonLock(unknownItems));
    batchActions.append(batchButton);
    box.append(batchActions);
  }

  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "rrv-note rrv-caveat";
    empty.textContent = "目前沒有可分析的券商持股快照。請先回主頁同步永豐庫存。";
    box.append(empty);
    return;
  }

  const table = document.createElement("table");
  table.className = "rrv-table local-trade-table";
  const thead = document.createElement("thead");
  const htr = document.createElement("tr");
  ["股票", "成交策略", "FIFO 買進日", "持有日", "成本", "現價", "成本後損益", "後端判斷", "計算驗證", "建議股數"].forEach((text) => {
    const th = document.createElement("th");
    th.textContent = text;
    htr.append(th);
  });
  thead.append(htr);
  table.append(thead);
  const tbody = document.createElement("tbody");
  items.forEach((item) => {
    const tr = document.createElement("tr");
    const rate = Number(item.netPnlRate);
    const rateText = Number.isFinite(rate) ? `${rate >= 0 ? "+" : ""}${rate.toFixed(2)}%` : "-";
    const stockCell = document.createElement("td");
    stockCell.textContent = `${item.symbol || "-"}${item.name && item.name !== item.symbol ? ` ${item.name}` : ""}`;
    tr.append(stockCell);

    const strategyCell = document.createElement("td");
    const strategyLabel = document.createElement("div");
    strategyLabel.textContent = `${item.strategyHorizonLabel || "週期未知"}${item.mixedHorizons ? "（混合部位）" : ""}`;
    strategyCell.append(strategyLabel);
    const unknownLots = (item.lots || []).filter((lot) => lot?.strategyHorizon === "unknown");
    if (item.hasUnknownHorizon === true || unknownLots.length > 0) {
      const controls = document.createElement("div");
      controls.className = "legacy-horizon-lock";
      const select = document.createElement("select");
      select.setAttribute("aria-label", `${item.symbol} 舊持股策略週期`);
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "選擇週期";
      placeholder.disabled = true;
      placeholder.selected = true;
      select.append(placeholder);
      [
        ["short_trade", "短期｜10 日"],
        ["mid_swing", "中期｜20～60 日"],
        ["long_trend", "長期｜60 日以上"],
      ].forEach(([value, label]) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        select.append(option);
      });
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "鎖定週期";
      button.title = "只補鎖未知週期的既有持股；鎖定後不可每日重新分類";
      button.disabled = true;
      select.addEventListener("change", () => {
        button.disabled = !select.value;
      });
      const status = document.createElement("span");
      status.className = "legacy-horizon-status";
      status.setAttribute("aria-live", "polite");
      button.addEventListener("click", async () => {
        const selectedLabel = select.options[select.selectedIndex]?.textContent || select.value;
        const buyDateNote = item.positionBuyDateKnown
          ? "FIFO 買進日已知。"
          : "部分或全部買進日未知，鎖定後仍不會啟用時間停損。";
        const confirmed = window.confirm(
          `確定把 ${item.symbol} 未分類的既有持股鎖定為「${selectedLabel}」？\n` +
          `${buyDateNote}\n此操作只補未知週期，完成後不能改成其他週期。`
        );
        if (!confirmed) return;
        button.disabled = true;
        select.disabled = true;
        status.textContent = "寫入中...";
        try {
          const response = await fetch("/api/portfolio/strategy-horizon", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ symbol: item.symbol, strategyHorizon: select.value }),
          });
          const result = await response.json();
          if (!response.ok || !result.ok) throw new Error(result.error || "鎖定失敗");
          status.textContent = `已鎖定 ${selectedLabel}`;
          await Promise.all([renderPortfolioExitAnalysis(), renderLocalTradeLedger()]);
        } catch (error) {
          status.textContent = `失敗：${friendlyError(error)}`;
          button.disabled = !select.value;
          select.disabled = false;
        }
      });
      controls.append(select, button, status);
      strategyCell.append(controls);
    }
    if (Number(item.migratableShares) > 0) {
      const importActions = document.createElement("div");
      importActions.className = "legacy-lot-import-action";
      const importButton = document.createElement("button");
      importButton.type = "button";
      importButton.textContent = `分批匯入 ${Number(item.migratableShares).toLocaleString("en-US")} 股`;
      importButton.title = "輸入未被真實成交覆蓋之分批買進日、成本、股數與策略週期";
      importButton.addEventListener("click", () => openLegacyLotImport(item));
      importActions.append(importButton);
      strategyCell.append(importActions);
    }
    tr.append(strategyCell);

    const fifoBuyDate = item.buyDateKnown
      ? `${item.buyDate}${item.positionBuyDateKnown === false ? "（部分未知）" : ""}`
      : "未知（時間出場停用）";
    const cells = [
      fifoBuyDate,
      item.tradingDaysHeld === null || item.tradingDaysHeld === undefined ? "-" : `${item.tradingDaysHeld} 日`,
      fmtPrice(item.buyPrice),
      fmtPrice(item.currentPrice),
      `${fmtMoney(item.estimatedNetPnl)} / ${rateText}`,
      item.status || "-",
      item.decisionVerified ? `通過：${(item.evidence || []).join("、")}` : "未通過／只觀察",
      Number(item.sellShares) > 0 ? `${item.sellShares} 股` : "0",
    ];
    cells.forEach((value, index) => {
      const td = document.createElement("td");
      td.textContent = value;
      if (index === 4) td.className = profitClass(item.estimatedNetPnl);
      if (index === 6) td.className = item.decisionVerified ? "rrv-neg" : "";
      tr.append(td);
    });
    tbody.append(tr);
  });
  table.append(tbody);
  const wrap = document.createElement("div");
  wrap.className = "rrv-table-wrap";
  wrap.append(table);
  box.append(wrap);
}

async function renderExitDecisionLogs() {
  const box = document.getElementById("exitDecisionLogs");
  if (!box) return;
  let payload;
  try {
    const response = await fetch("/api/portfolio/exit-decision-logs?limit=80");
    payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "讀取失敗");
  } catch (error) {
    box.replaceChildren();
    const msg = document.createElement("div");
    msg.className = "rrv-note";
    msg.textContent = `讀取買賣提醒留痕失敗：${friendlyError(error)}`;
    box.append(msg);
    return;
  }
  const logs = payload.logs || [];
  box.replaceChildren();
  if (!logs.length) {
    const empty = document.createElement("div");
    empty.className = "rrv-note rrv-caveat";
    empty.textContent = "目前還沒有停利/停損提醒留痕；守門員真正發出提醒後會留下當時價格、停損線、確認賣出價與通知通道。";
    box.append(empty);
    return;
  }
  const head = document.createElement("div");
  head.className = "rrv-head";
  const title = document.createElement("strong");
  title.textContent = "停利停損提醒決策紀錄";
  head.append(title);
  box.append(head);

  const table = document.createElement("table");
  table.className = "rrv-table local-trade-table";
  const thead = document.createElement("thead");
  const htr = document.createElement("tr");
  ["時間", "股票", "現價", "防守價", "確認賣出", "計算驗證", "通道", "原因"].forEach((text) => {
    const th = document.createElement("th");
    th.textContent = text;
    htr.append(th);
  });
  thead.append(htr);
  table.append(thead);
  const tbody = document.createElement("tbody");
  logs.forEach((log) => {
    const tr = document.createElement("tr");
    let decisionReasons = [];
    try {
      decisionReasons = Array.isArray(log.decision_reasons)
        ? log.decision_reasons
        : JSON.parse(log.decision_reasons || "[]");
    } catch {
      decisionReasons = [];
    }
    const decisionText = log.decision_verified
      ? `${log.decision_type || "賣出"}｜${decisionReasons.join("、") || "已通過"}`
      : "舊紀錄／未留證據";
    [
      log.created_at || "-",
      `${log.symbol || "-"}${log.name ? ` ${log.name}` : ""}`,
      fmtPrice(log.current_price),
      fmtPrice(log.stop_price),
      fmtPrice(log.confirm_sell_price),
      decisionText,
      log.channel || "-",
      log.reason || "-",
    ].forEach((value, index) => {
      const td = document.createElement("td");
      td.textContent = value;
      if (index === 2 || index === 4) td.className = "rrv-neg";
      tr.append(td);
    });
    tbody.append(tr);
  });
  table.append(tbody);
  const wrap = document.createElement("div");
  wrap.className = "rrv-table-wrap";
  wrap.append(table);
  box.append(wrap);
}

async function renderPortfolioExitPerformance() {
  const box = document.getElementById("portfolioExitPerformanceReview");
  if (!box) return;
  let payload;
  try {
    const response = await fetch("/api/portfolio/exit-analysis/performance", { cache: "no-store" });
    payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "讀取失敗");
  } catch (error) {
    box.replaceChildren();
    const message = document.createElement("div");
    message.className = "rrv-note";
    message.textContent = `讀取出場判斷驗證失敗：${friendlyError(error)}`;
    box.append(message);
    return;
  }

  box.replaceChildren();
  const head = document.createElement("div");
  head.className = "rrv-head";
  const title = document.createElement("strong");
  title.textContent = "出場判斷驗證";
  const meta = document.createElement("span");
  meta.textContent = `歷史 ${payload.historyCount || 0} 筆｜正式賣出 ${payload.verifiedEventCount || 0} 筆｜已結算 ${payload.outcomeCount || 0} 個視窗`;
  head.append(title, meta);
  box.append(head);

  const groups = payload.groups || [];
  if (!groups.length) {
    const empty = document.createElement("div");
    empty.className = "rrv-note rrv-caveat";
    empty.textContent = `已保留 ${payload.historyCount || 0} 筆判斷歷史；尚無正式賣出事件可結算，未來事件會依 1／3／5／10／20／60 個交易日逐步驗證。`;
    box.append(empty);
    return;
  }

  const table = document.createElement("table");
  table.className = "rrv-table local-trade-table";
  const thead = document.createElement("thead");
  const htr = document.createElement("tr");
  ["策略", "未來交易日", "樣本", "判斷正確率", "淨效果", "平均效果", "獲利因子", "提早賣出率"].forEach((text) => {
    const th = document.createElement("th");
    th.textContent = text;
    htr.append(th);
  });
  thead.append(htr);
  table.append(thead);
  const labels = { short_trade: "短期", mid_swing: "中期", long_trend: "長期", unknown: "未知" };
  const tbody = document.createElement("tbody");
  groups.forEach((group) => {
    const tr = document.createElement("tr");
    [
      labels[group.strategyHorizon] || group.strategyHorizon || "-",
      `${group.horizonDays} 日`,
      group.samples ?? 0,
      fmtPctRatio(group.precision, 1),
      fmtMoney(group.netPnl),
      `${fmtMoney(group.averageNetPnl)} / ${Number.isFinite(Number(group.averageDecisionNetPct)) ? `${Number(group.averageDecisionNetPct) >= 0 ? "+" : ""}${Number(group.averageDecisionNetPct).toFixed(2)}%` : "-"}`,
      group.profitFactor === null || group.profitFactor === undefined ? "尚無虧損樣本" : Number(group.profitFactor).toFixed(2),
      fmtPctRatio(group.prematureSellRate, 1),
    ].forEach((value, index) => {
      const td = document.createElement("td");
      td.textContent = value;
      if (index === 4 || index === 5) td.className = profitClass(group.netPnl);
      tr.append(td);
    });
    tbody.append(tr);
  });
  table.append(tbody);
  const wrap = document.createElement("div");
  wrap.className = "rrv-table-wrap";
  wrap.append(table);
  box.append(wrap);
  const note = document.createElement("div");
  note.className = "rrv-note";
  note.textContent = "淨效果比較當時賣出與繼續持有至官方收盤的淨變化；正值代表當時賣出較有利，負值列入提早賣出。";
  box.append(note);
}

function renderOrderFillDiagnostics(payload) {
  const box = document.getElementById("orderFillDiagnostics");
  if (!box) return;
  const sync = payload?.sync || {};
  const details = sync.details || [];
  box.hidden = false;
  box.replaceChildren();

  const head = document.createElement("div");
  head.className = "rrv-head";
  const title = document.createElement("strong");
  title.textContent = "成交回報同步診斷";
  head.append(title);
  box.append(head);

  const summary = document.createElement("div");
  summary.className = "rrv-note";
  summary.textContent =
    `匯入 ${sync.imported || 0} 筆，新建 ${sync.createdTrades || 0} 筆，更新 ${sync.updatedTrades || 0} 筆，` +
    `關閉 round-trip ${sync.closedTrades || 0} 筆，拆分 ${sync.splitTrades || 0} 筆，` +
    `未匹配 ${sync.unmatched || 0} 筆。`;
  box.append(summary);

  if (!details.length) {
    const empty = document.createElement("div");
    empty.className = "rrv-note rrv-caveat";
    empty.textContent = "這次沒有可顯示的成交明細。若永豐有成交但這裡空白，表示 Shioaji 回傳 shape 需要再實機調整。";
    box.append(empty);
    return;
  }

  const reasonText = {
    missing_code: "缺股票代號",
    invalid_fill_payload: "成交資料不完整",
    missing_broker_refs: "缺 broker refs",
    no_matching_local_order: "找不到本地下單紀錄",
    not_enough_open_buy_shares: "本地可關閉 BUY 股數不足",
  };
  const table = document.createElement("table");
  table.className = "rrv-table local-trade-table";
  const thead = document.createElement("thead");
  const htr = document.createElement("tr");
  ["股票", "買賣", "股數", "成交價", "時間", "結果", "關閉/拆分"].forEach((text) => {
    const th = document.createElement("th");
    th.textContent = text;
    htr.append(th);
  });
  thead.append(htr);
  table.append(thead);
  const tbody = document.createElement("tbody");
  details.forEach((item) => {
    const tr = document.createElement("tr");
    const closed = (item.closedTradeIds || []).join(",") || "-";
    const split = (item.splitTradeIds || []).join(",");
    const outcome = item.matched
      ? `${item.createdLocalTrade ? "新建；" : ""}${item.status || "matched"}${item.unclosedShares ? `；未關 ${item.unclosedShares} 股` : ""}`
      : (reasonText[item.reason] || item.reason || "未匹配");
    [
      item.code || "-",
      item.action || "-",
      item.shares || "-",
      fmtPrice(item.price),
      item.dealAt || "-",
      outcome,
      split ? `${closed} / split ${split}` : closed,
    ].forEach((value, index) => {
      const td = document.createElement("td");
      td.textContent = value;
      if (index === 5 && !item.matched) td.className = "rrv-neg";
      tr.append(td);
    });
    tbody.append(tr);
  });
  table.append(tbody);
  const wrap = document.createElement("div");
  wrap.className = "rrv-table-wrap";
  wrap.append(table);
  box.append(wrap);
}

// 手動「重新抓取」:呼叫探測端點強制從永豐匯入(不受主頁 once/day 閘門限制),抓完重繪。
// 永豐短時間重複登入會被拒(400),所以剛更新過庫存的話這裡可能失敗,提示稍等再試。唯讀、不下單。
function bindRefresh() {
  const btn = document.getElementById("refreshRealized");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const status = document.getElementById("realizedStatus");
    const prev = btn.textContent;
    btn.disabled = true; btn.textContent = "抓取中…";
    if (status) status.textContent = "🔄 從永豐抓已實現損益中…(需登入永豐,約 10–30 秒,唯讀不下單)";
    try {
      const response = await fetch("/api/sinopac/realized-pnl?days=180");
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.ok) {
        if (status) status.textContent = `❌ 抓取失敗：${payload.error || "未知錯誤"}（若剛更新過庫存,永豐會拒絕短時間重複登入,稍等 1 分鐘再試）`;
      } else {
        if (status) status.textContent = `✅ 已從永豐更新：抓到 ${payload.count} 筆${payload.saved != null ? `，存入 ${payload.saved} 筆` : ""}`;
        await renderRealizedReview();
      }
    } catch (error) {
      if (status) status.textContent = `❌ 抓取錯誤：${friendlyError(error)}`;
    } finally {
      btn.disabled = false; btn.textContent = prev;
    }
  });
}

function bindOrderFillSync() {
  const btn = document.getElementById("syncOrderFills");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const status = document.getElementById("realizedStatus");
    const prev = btn.textContent;
    btn.disabled = true;
    btn.textContent = "同步中...";
    if (status) status.textContent = "從永豐抓委託成交回報中...(更新本地 trades 買進日/成交價，唯讀不下單)";
    try {
      const response = await fetch("/api/sinopac/order-fills/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.ok) {
        if (status) status.textContent = `委託成交回報同步失敗：${payload.error || "未知錯誤"}`;
        return;
      }
      const sync = payload.sync || {};
      if (status) {
        status.textContent =
          `委託成交回報同步完成：抓到 ${payload.count || 0} 筆成交摘要，` +
          `匯入 ${sync.imported || 0} 筆，新建本地交易 ${sync.createdTrades || 0} 筆，更新 ${sync.updatedTrades || 0} 筆` +
          (sync.closedTrades ? `，關閉回合 ${sync.closedTrades} 筆` : "") +
          (sync.splitTrades ? `，拆分 ${sync.splitTrades} 筆` : "") +
          (sync.unmatched ? `，未匹配 ${sync.unmatched} 筆` : "");
      }
      renderOrderFillDiagnostics(payload);
      await renderRealizedReview();
      await renderLocalTradeLedger();
      await renderPortfolioExitAnalysis();
    } catch (error) {
      if (status) status.textContent = `委託成交回報同步錯誤：${friendlyError(error)}`;
    } finally {
      btn.disabled = false;
      btn.textContent = prev;
    }
  });
}

const reviewDisclosureLoaders = new Map([
  ["modelPaperSection", () => renderModelPaperReview()],
  ["radarScoreRecordSection", () => loadRadarScoreTrackRecord()],
  ["reviewEvidenceSection", () => Promise.all([
    renderMarketSessionValidation(),
    renderPortfolioExitAnalysis(),
    renderPortfolioExitPerformance(),
    renderExitDecisionLogs(),
  ])],
  ["tcnExperimentSection", () => renderTcnExperimentReview()],
  ["localLedgerSection", () => renderLocalTradeLedger()],
]);

async function loadReviewDisclosure(details) {
  if (!details || details.dataset.loaded === "1" || details.dataset.loading === "1") return;
  const loader = reviewDisclosureLoaders.get(details.id);
  if (!loader) return;
  const toggle = details.querySelector(".review-disclosure-toggle");
  details.dataset.loading = "1";
  if (toggle) toggle.textContent = "載入中";
  try {
    await loader();
    details.dataset.loaded = "1";
  } finally {
    delete details.dataset.loading;
    if (toggle) toggle.textContent = details.open ? "收合" : "展開";
  }
}

function bindReviewDisclosures() {
  document.querySelectorAll(".review-disclosure").forEach((details) => {
    details.addEventListener("toggle", () => {
      const toggle = details.querySelector(".review-disclosure-toggle");
      if (toggle && details.dataset.loading !== "1") {
        toggle.textContent = details.open ? "收合" : "展開";
      }
      if (details.open) loadReviewDisclosure(details);
    });
  });

  const openModel = document.getElementById("openModelPaperReview");
  if (openModel) {
    openModel.addEventListener("click", () => {
      const details = document.getElementById("modelPaperSection");
      if (!details) return;
      details.open = true;
      loadReviewDisclosure(details);
      details.scrollIntoView({ block: "start", behavior: "smooth" });
    });
  }
}

applyTheme();
initSidebarToggle();
renderModelAccuracyReview();
renderRadarDiscoveryAccuracy();
const _themeBtn = document.getElementById("themeToggle");
if (_themeBtn) {
  _themeBtn.addEventListener("click", () => applyTheme(document.body.classList.contains("dark") ? "light" : "dark"));
}
