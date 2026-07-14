"use strict";

const state = {
  loading: false,
  names: {},
};

const numberFormat = new Intl.NumberFormat("zh-TW", { maximumFractionDigits: 0 });
const priceFormat = new Intl.NumberFormat("zh-TW", { maximumFractionDigits: 2 });

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function finiteNumber(value, fallback = null) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function money(value) {
  const number = finiteNumber(value);
  return number === null ? "-" : `${numberFormat.format(Math.round(number))} 元`;
}

function signedMoney(value) {
  const number = finiteNumber(value);
  if (number === null) return "-";
  const prefix = number > 0 ? "+" : "";
  return `${prefix}${numberFormat.format(Math.round(number))} 元`;
}

function percent(value, digits = 2) {
  const number = finiteNumber(value);
  if (number === null) return "-";
  const prefix = number > 0 ? "+" : "";
  return `${prefix}${number.toFixed(digits)}%`;
}

function price(value) {
  const number = finiteNumber(value);
  return number === null ? "-" : priceFormat.format(number);
}

function compactDateTime(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  const normalized = text.replace("T", " ").replace(/\+08:00$/, "");
  return normalized.length >= 16 ? normalized.slice(0, 16) : normalized;
}

function quoteSource(value) {
  const text = String(value || "");
  const sources = [];
  if (/Shioaji|Sinopac/i.test(text)) sources.push("永豐");
  if (/Capital|群益/i.test(text)) sources.push("群益");
  return sources.length ? [...new Set(sources)].join("＋") : (text || "來源未確認");
}

function tone(value) {
  const number = finiteNumber(value, 0);
  if (number > 0) return "up";
  if (number < 0) return "down";
  return "flat";
}

async function fetchJson(url) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(url, {
      cache: "no-store",
      credentials: "same-origin",
      signal: controller.signal,
    });
    const payload = await response.json();
    if (!response.ok || payload?.ok === false) {
      throw new Error(payload?.error || `HTTP ${response.status}`);
    }
    return payload;
  } finally {
    window.clearTimeout(timer);
  }
}

function setConnectionStatus(message, error = false) {
  const element = document.getElementById("connectionStatus");
  element.textContent = message;
  element.classList.toggle("error", error);
}

async function loadNames(codes) {
  const missing = codes.filter((code) => !state.names[code]);
  if (!missing.length) return;
  const payload = await fetchJson(`/api/stock-info?codes=${encodeURIComponent(missing.join(","))}`);
  Object.entries(payload.stocks || {}).forEach(([code, item]) => {
    state.names[code] = item?.name || "";
  });
}

function renderHoldings(payload) {
  const summary = payload.summary || {};
  const rows = Object.values(payload.holdings || {})
    .sort((a, b) => String(a.code).localeCompare(String(b.code), "zh-TW"));

  document.getElementById("portfolioValue").textContent = money(summary.currentValue);
  const pnlElement = document.getElementById("portfolioPnl");
  pnlElement.textContent = signedMoney(summary.totalPnl);
  pnlElement.className = tone(summary.totalPnl);
  const returnElement = document.getElementById("portfolioReturn");
  returnElement.textContent = percent(summary.returnRate);
  returnElement.className = tone(summary.returnRate);
  document.getElementById("availableCash").textContent = money(summary.availableAfterSettlement);
  document.getElementById("holdingsCount").textContent = `${rows.length} 檔`;
  const inventoryAt = compactDateTime(summary.updatedAt);
  const quoteAt = compactDateTime(summary.quoteUpdatedAt || summary.quoteCheckedAt);
  const coverage = summary.quoteCoverage || {};
  const quoteStatus = summary.quoteFresh === true
    ? "報價完整"
    : summary.quoteCheckedAt
      ? `報價缺漏 ${Number(coverage.fresh || 0)} / ${Number(coverage.requested || rows.length)}`
      : "尚未由後端更新報價";
  document.getElementById("holdingsUpdatedAt").textContent = [
    inventoryAt ? `庫存同步 ${inventoryAt}` : "庫存時間未確認",
    quoteAt ? `行情 ${quoteAt}` : "行情時間未確認",
    quoteStatus,
  ].join("｜");

  const list = document.getElementById("holdingsList");
  if (!rows.length) {
    list.innerHTML = '<div class="empty-state">目前沒有持股資料</div>';
    return;
  }
  list.innerHTML = rows.map((item) => {
    const code = String(item.code || "");
    const shares = finiteNumber(item.shares, 0);
    const currentPrice = finiteNumber(item.currentPrice);
    const marketValue = currentPrice === null ? null : currentPrice * shares;
    const pnl = finiteNumber(item.pnl);
    const costPrice = finiteNumber(item.price);
    const totalReturnRate = currentPrice !== null && costPrice !== null && costPrice > 0
      ? ((currentPrice - costPrice) / costPrice) * 100
      : null;
    const dayChangeRate = finiteNumber(item.changeRate);
    const itemQuoteAt = compactDateTime(item.quoteAt || item.receivedAt);
    const source = quoteSource(item.quoteSource || item.marketDataSource || summary.quoteSource);
    const freshness = item.quoteFresh === true
      ? "報價已驗證"
      : item.quoteFresh === false
        ? "報價缺漏或過期"
        : "舊快取";
    return `
      <article class="stock-row">
        <div class="stock-title">
          <strong>${escapeHtml(code)}</strong><span>${escapeHtml(state.names[code] || "")}</span>
          <div class="stock-detail">${numberFormat.format(shares)} 股｜成本 ${price(item.price)}｜現價 ${price(currentPrice)}</div>
          <div class="quote-meta ${item.quoteFresh === false ? "stale" : ""}">${escapeHtml(itemQuoteAt ? `報價 ${itemQuoteAt}｜${source}｜${freshness}` : `${source}｜${freshness}｜時間未確認`)}</div>
        </div>
        <div class="stock-value">
          <strong>${money(marketValue)}</strong>
          <span class="${tone(pnl)}">未實現 ${signedMoney(pnl)}</span>
          <span class="${tone(totalReturnRate)}">總漲幅 ${percent(totalReturnRate)}</span>
          <span class="${tone(dayChangeRate)}">今日漲跌 ${percent(dayChangeRate)}</span>
        </div>
      </article>`;
  }).join("");
}

function formalBuyAllowed(candidate, intraday, payload) {
  const hasDanger = (candidate.riskFlags || []).some((flag) => flag?.severity === "danger");
  return payload.decisionValidity?.validForTrading === true
    && payload.deploymentReadiness?.formalReady === true
    && candidate.buyAllowed === true
    && candidate.riskVetoed !== true
    && candidate.performanceVetoed !== true
    && !hasDanger
    && intraday?.canBuy === true;
}

function renderRadar(payload, intradayPayload) {
  const quotes = intradayPayload.quotes || {};
  const candidates = Array.isArray(payload.candidates) ? payload.candidates : [];
  const rows = candidates.map((candidate) => {
    const intraday = quotes[String(candidate.symbol)] || {};
    return {
      candidate,
      intraday,
      canBuy: formalBuyAllowed(candidate, intraday, payload),
    };
  }).sort((a, b) => {
    if (a.canBuy !== b.canBuy) return a.canBuy ? -1 : 1;
    return finiteNumber(b.candidate.score, 0) - finiteNumber(a.candidate.score, 0);
  }).slice(0, 10);

  const validity = payload.decisionValidity || {};
  const readiness = payload.deploymentReadiness || {};
  const validityElement = document.getElementById("radarValidity");
  let validityText = "目前只供觀察，不是買進清單";
  let ready = false;
  if (validity.validForTrading !== true) {
    validityText = validity.summary || "雷達資料目前不可作為交易依據";
  } else if (readiness.formalReady !== true) {
    validityText = "盤中成交戰績與 walk-forward 門檻尚未通過，目前只供觀察";
  } else {
    ready = true;
    validityText = "交易日資料有效，仍須依每檔盤中狀態判斷";
  }
  validityElement.textContent = validityText;
  validityElement.classList.toggle("ready", ready);
  validityElement.classList.toggle("observe", !ready);

  const buyable = rows.filter((row) => row.canBuy).length;
  const coverage = intradayPayload.quoteCoverage || {};
  document.getElementById("radarCount").textContent = `${rows.length} 檔`;
  document.getElementById("marketRegime").textContent = payload.marketRegime?.label || "-";
  document.getElementById("buyableCount").textContent = String(buyable);
  document.getElementById("quoteCoverage").textContent = `${Number(coverage.received || 0)} / ${Number(coverage.requested || 0)}`;
  const updatedParts = [];
  if (payload.scanDate) updatedParts.push(`掃描 ${payload.scanDate}`);
  if (intradayPayload.updatedAt) updatedParts.push(`盤中 ${intradayPayload.updatedAt}`);
  document.getElementById("radarUpdatedAt").textContent = updatedParts.join("｜") || "尚無更新時間";

  const list = document.getElementById("radarList");
  if (!rows.length) {
    list.innerHTML = '<div class="empty-state">目前沒有雷達候選</div>';
    return;
  }
  list.innerHTML = rows.map(({ candidate, intraday, canBuy }) => {
    const currentPrice = intraday.currentPrice ?? intraday.price ?? candidate.close;
    const changeRate = intraday.changeRate ?? intraday.changePct ?? candidate.change1;
    const risks = (candidate.riskFlags || []).map((flag) => flag?.label).filter(Boolean);
    const status = canBuy ? "可買" : "只觀察";
    return `
      <article class="radar-row">
        <div class="radar-row-head">
          <div class="radar-title">
            <strong>${escapeHtml(candidate.symbol || "-")}</strong><span>${escapeHtml(candidate.name || "")}</span>
            <div class="radar-detail">${escapeHtml(candidate.sector || "未分類")}｜量比 ${price(candidate.volume_ratio ?? candidate.volumeRatio)}｜5 日 ${percent(candidate.change5)}</div>
          </div>
          <div class="radar-score">${price(candidate.score)} 分</div>
        </div>
        <div class="radar-numbers">
          <div><span>現價 / 今日漲跌</span><strong class="${tone(changeRate)}">${price(currentPrice)} / ${percent(changeRate)}</strong></div>
          <div><span>觸發價</span><strong>${price(candidate.buy_trigger)}</strong></div>
          <div><span>停損 / 停利</span><strong>${price(candidate.stop_price)} / ${price(candidate.take_profit)}</strong></div>
        </div>
        <span class="status-pill ${canBuy ? "ready" : ""}">${status}</span>
        ${risks.length ? `<div class="risk-line">風險：${escapeHtml(risks.join("、"))}</div>` : ""}
      </article>`;
  }).join("");
}

async function loadHoldings() {
  const payload = await fetchJson("/api/portfolio/summary-cache");
  const codes = Object.keys(payload.holdings || {});
  await loadNames(codes);
  renderHoldings(payload);
}

async function loadRadar() {
  const [payload, intradayPayload] = await Promise.all([
    fetchJson("/api/monster-scores?limit=100"),
    fetchJson("/api/monster-intraday?cachedOnly=1"),
  ]);
  renderRadar(payload, intradayPayload);
}

async function refreshAll() {
  if (state.loading) return;
  state.loading = true;
  const button = document.getElementById("refreshButton");
  button.disabled = true;
  setConnectionStatus("正在讀取最新快取");
  const results = await Promise.allSettled([loadHoldings(), loadRadar()]);
  const failures = results.filter((result) => result.status === "rejected");
  if (failures.length) {
    const message = failures.map((result) => result.reason?.message || "讀取失敗").join("；");
    setConnectionStatus(`部分資料讀取失敗：${message}`, true);
  } else {
    setConnectionStatus(`畫面已重新讀取 ${new Date().toLocaleTimeString("zh-TW", { hour12: false })}`);
  }
  button.disabled = false;
  state.loading = false;
}

function activateModule(moduleName, updateHash = true) {
  const radar = moduleName === "radar";
  document.getElementById("holdingsPanel").hidden = radar;
  document.getElementById("radarPanel").hidden = !radar;
  document.querySelectorAll(".module-tab").forEach((button) => {
    const active = button.dataset.module === moduleName;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  if (updateHash) history.replaceState(null, "", radar ? "#radar" : "#holdings");
}

function moduleFromHash() {
  return location.hash === "#radar" ? "radar" : "holdings";
}

document.querySelectorAll(".module-tab").forEach((button) => {
  button.addEventListener("click", () => activateModule(button.dataset.module));
});
document.getElementById("refreshButton").addEventListener("click", refreshAll);
window.addEventListener("hashchange", () => activateModule(moduleFromHash(), false));
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshAll();
});

// 每次完整開啟手機版都以永豐存股為首頁，也會把舊主畫面捷徑保存的
// #radar 啟動網址遷移成 #holdings；頁面開啟後仍可正常切換兩個分頁。
activateModule("holdings");
refreshAll();
window.setInterval(() => {
  if (!document.hidden) refreshAll();
}, 30000);
