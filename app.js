const defaultStocks = [
  { code: "2330", name: "台積電", base: 865, vol: 0.010, drift: 0.00018, sector: "半導體" },
  { code: "2317", name: "鴻海", base: 182, vol: 0.012, drift: 0.00012, sector: "電子代工" },
  { code: "2454", name: "聯發科", base: 1290, vol: 0.013, drift: 0.00013, sector: "IC 設計" },
  { code: "2603", name: "長榮", base: 205, vol: 0.015, drift: 0.00007, sector: "航運" },
  { code: "2881", name: "富邦金", base: 88, vol: 0.008, drift: 0.00008, sector: "金融" },
  { code: "2303", name: "聯電", base: 54, vol: 0.011, drift: 0.00007, sector: "半導體" },
  { code: "3711", name: "日月光投控", base: 158, vol: 0.012, drift: 0.00011, sector: "封測" },
  { code: "3037", name: "欣興", base: 176, vol: 0.016, drift: 0.00009, sector: "PCB" }
];

const knownStockProfiles = {
  "0050": { name: "元大台灣50", base: 150, vol: 0.008, drift: 0.0001, sector: "ETF" },
  "0056": { name: "元大高股息", base: 38, vol: 0.007, drift: 0.00008, sector: "ETF" },
  "00878": { name: "國泰永續高股息", base: 23, vol: 0.007, drift: 0.00008, sector: "ETF" },
  "00919": { name: "群益台灣精選高息", base: 24, vol: 0.008, drift: 0.00008, sector: "ETF" },
  "00929": { name: "復華台灣科技優息", base: 20, vol: 0.009, drift: 0.00008, sector: "ETF" },
  "2329": { name: "華泰", base: 54, vol: 0.018, drift: 0.00007, sector: "半導體業" },
  "2332": { name: "友訊", base: 18, vol: 0.015, drift: 0.00005, sector: "通信網路業" },
  "2610": { name: "華航", base: 22, vol: 0.014, drift: 0.00005, sector: "航運業" },
  "2614": { name: "東森", base: 18, vol: 0.014, drift: 0.00004, sector: "貿易百貨業" },
  "2913": { name: "農林", base: 11, vol: 0.013, drift: 0.00003, sector: "貿易百貨業" },
  "3051": { name: "力特", base: 28, vol: 0.018, drift: 0.00005, sector: "光電業" },
  "3056": { name: "富華新", base: 15, vol: 0.016, drift: 0.00004, sector: "營建業" },
  "3272": { name: "東碩", base: 16, vol: 0.018, drift: 0.00005, sector: "電腦及週邊設備業" },
  "3481": { name: "群創", base: 67, vol: 0.019, drift: 0.00006, sector: "光電業" },
  "4533": { name: "協易機", base: 29, vol: 0.017, drift: 0.00005, sector: "電機機械業" },
  "6244": { name: "茂迪", base: 28, vol: 0.017, drift: 0.00004, sector: "光電業" },
  "9934": { name: "成霖", base: 10, vol: 0.012, drift: 0.00003, sector: "其他業" },
  "2891": { name: "中信金", base: 36, vol: 0.008, drift: 0.00007, sector: "金融" },
  "2892": { name: "第一金", base: 28, vol: 0.007, drift: 0.00006, sector: "金融" },
  "5880": { name: "合庫金", base: 27, vol: 0.007, drift: 0.00006, sector: "金融" },
  "2412": { name: "中華電", base: 125, vol: 0.005, drift: 0.00004, sector: "電信" },
  "3045": { name: "台灣大", base: 110, vol: 0.006, drift: 0.00004, sector: "電信" }
};

const INVALID_STOCK_NAME_PLACEHOLDERS = new Set(["", "永豐存股", "台股", "股票", "一般股票", "無資料"]);

function normalizeStockCode(value) {
  return String(value || "").replace(/\.TW$/i, "").replace(/\.TWO$/i, "").replace(/[^\d]/g, "").trim();
}

function sanitizeStockName(value, code = "") {
  const name = String(value || "").trim();
  if (!name || name === code || INVALID_STOCK_NAME_PLACEHOLDERS.has(name)) return "";
  return name;
}

function isEtfLikeStock(stockOrCode) {
  const stock = typeof stockOrCode === "object" && stockOrCode ? stockOrCode : {};
  const code = normalizeStockCode(stock.code || stockOrCode);
  const text = [stock.name, stock.sector, stock.type, knownStockProfiles[code]?.name, knownStockProfiles[code]?.sector]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return code.startsWith("00") || text.includes("etf");
}

function stockDisplayName(stockOrCode, maybeName = "") {
  const code = normalizeStockCode(typeof stockOrCode === "object" ? stockOrCode.code : stockOrCode);
  const objectName = typeof stockOrCode === "object" ? stockOrCode.name : "";
  const rawName = sanitizeStockName(objectName, code) || sanitizeStockName(maybeName, code);
  const known = sanitizeStockName(knownStockProfiles[code]?.name, code) || sanitizeStockName(defaultStocks.find((stock) => stock.code === code)?.name, code);
  const name = rawName || known;
  return name && name !== code ? name : "";
}

function stockLabel(stockOrCode, maybeName = "") {
  const code = normalizeStockCode(typeof stockOrCode === "object" ? stockOrCode.code : stockOrCode);
  const name = stockDisplayName(stockOrCode, maybeName);
  return name ? `${code} ${name}` : code;
}

function portfolioStorageKey() {
  return "stock-vibe-yongfeng-portfolio-v1";
}

function portfolioHoldingsStorageKey() {
  return "stock-vibe-yongfeng-holdings-v1";
}

function portfolioSummaryStorageKey() {
  return "stock-vibe-yongfeng-summary-v1";
}

function portfolioExitAlertStorageKey() {
  return "stock-vibe-yongfeng-exit-alerts-v1";
}

function portfolioAlertHiddenStorageKey() {
  return "stock-vibe-yongfeng-alert-hidden-v1";
}

function portfolioAlertLastBuildStorageKey() {
  return "stock-vibe-yongfeng-alert-last-build-v1";
}

function portfolioAlertCacheStorageKey() {
  return "stock-vibe-yongfeng-alert-cache-v1";
}

let portfolioExitAnalysisState = {
  loading: false,
  error: "",
  payload: null,
  request: null,
};
let portfolioHorizonMigrationState = {
  previewKey: "",
  preview: null,
  busy: false,
};
let portfolioSummarySyncPromise = Promise.resolve(null);
let initialPortfolioExitReady = false;

function portfolioExitItemFor(code) {
  const normalized = normalizeStockCode(code);
  return (portfolioExitAnalysisState.payload?.items || []).find(
    (item) => normalizeStockCode(item?.symbol) === normalized
  ) || null;
}

async function loadPortfolioExitAnalysis(options = {}) {
  if (portfolioExitAnalysisState.request) return portfolioExitAnalysisState.request;
  portfolioExitAnalysisState.loading = true;
  portfolioExitAnalysisState.error = "";
  const request = (async () => {
    try {
      const response = await fetch("/api/portfolio/exit-analysis", { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "後端出場分析讀取失敗");
      portfolioExitAnalysisState.payload = payload;
      initialPortfolioExitReady = true;
      renderPortfolioHorizonMigration();
      if (options.render !== false && document.getElementById("watchlist")) renderScanner();
      return payload;
    } catch (error) {
      portfolioExitAnalysisState.error = friendlyError(error);
      renderPortfolioHorizonMigration();
      throw error;
    } finally {
      portfolioExitAnalysisState.loading = false;
      portfolioExitAnalysisState.request = null;
    }
  })();
  portfolioExitAnalysisState.request = request;
  return request;
}

function portfolioHorizonLabel(key) {
  return {
    short_trade: "短期｜10 日",
    mid_swing: "中期｜20～60 日",
    long_trend: "長期｜60 日以上",
    unknown: "週期未知",
  }[String(key || "unknown")] || "週期未知";
}

function portfolioHorizonSourceLabel(source) {
  return {
    order_entry: "買進成交鎖定",
    sinopac_manual_order: "永豐下單鎖定",
    sinopac_manual_order_unclassified: "永豐手動下單未分類",
    manual_batch_lot_lock: "lot 批次鎖定",
    manual_legacy_position_lock: "舊版股票鎖定",
    sinopac_position_detail_import: "永豐成交明細",
    external_fill_unknown: "外部成交待分類",
  }[String(source || "")] || String(source || "未記錄來源");
}

function portfolioHorizonLots() {
  const rows = [];
  (portfolioExitAnalysisState.payload?.items || []).forEach((item) => {
    (item?.lots || []).forEach((lot) => {
      rows.push({
        ...lot,
        symbol: normalizeStockCode(item?.symbol),
        name: stockDisplayName(item?.symbol, item?.name) || item?.symbol || "",
      });
    });
  });
  return rows.sort((a, b) => (
    String(a.symbol).localeCompare(String(b.symbol), "zh-Hant")
    || String(a.buyDate || "9999-99-99").localeCompare(String(b.buyDate || "9999-99-99"))
    || Number(a.tradeId || 0) - Number(b.tradeId || 0)
  ));
}

function portfolioHorizonAssignmentKey(assignments) {
  return JSON.stringify([...(assignments || [])].sort((a, b) => (
    String(a.symbol).localeCompare(String(b.symbol))
    || Number(a.tradeId || 0) - Number(b.tradeId || 0)
  )));
}

function portfolioHorizonCostText(lot) {
  const value = Number(lot?.costBasisPrice ?? lot?.buyPrice);
  return Number.isFinite(value)
    ? value.toLocaleString("zh-TW", { minimumFractionDigits: 2, maximumFractionDigits: 4 })
    : "-";
}

function collectPortfolioHorizonAssignments() {
  const section = document.getElementById("portfolioHorizonMigration");
  const unknownCount = Number(section?.dataset.unknownCount || 0);
  const selectors = [...document.querySelectorAll(".portfolio-horizon-select")];
  if (!unknownCount) throw new Error("目前沒有未知週期 lot");
  if (selectors.length !== unknownCount) throw new Error("仍有 lot 缺少 tradeId，請先同步成交明細");
  const assignments = selectors.map((select) => ({
    symbol: normalizeStockCode(select.dataset.symbol),
    tradeId: Number(select.dataset.tradeId || 0),
    strategyHorizon: select.value,
  }));
  if (assignments.some((row) => !row.symbol || !row.tradeId || !row.strategyHorizon)) {
    throw new Error("每個未知 lot 都必須選擇短期、中期或長期");
  }
  return assignments;
}

function resetPortfolioHorizonPreview(message = "") {
  portfolioHorizonMigrationState.previewKey = "";
  portfolioHorizonMigrationState.preview = null;
  const confirm = document.getElementById("confirmPortfolioHorizons");
  if (confirm) {
    confirm.checked = false;
    confirm.disabled = true;
  }
  if (message) {
    const box = document.getElementById("portfolioHorizonMessage");
    if (box) box.textContent = message;
  }
}

function updatePortfolioHorizonControls() {
  const previewButton = document.getElementById("previewPortfolioHorizons");
  const applyButton = document.getElementById("applyPortfolioHorizons");
  const confirm = document.getElementById("confirmPortfolioHorizons");
  let assignments = [];
  let complete = false;
  try {
    assignments = collectPortfolioHorizonAssignments();
    complete = true;
  } catch {
    complete = false;
  }
  const key = complete ? portfolioHorizonAssignmentKey(assignments) : "";
  const previewCurrent = Boolean(
    key && portfolioHorizonMigrationState.previewKey === key
  );
  if (previewButton) previewButton.disabled = portfolioHorizonMigrationState.busy || !complete;
  if (confirm) confirm.disabled = portfolioHorizonMigrationState.busy || !previewCurrent;
  if (applyButton) {
    applyButton.disabled = portfolioHorizonMigrationState.busy
      || !previewCurrent
      || confirm?.checked !== true;
  }
}

function renderPortfolioHorizonMigration() {
  const section = document.getElementById("portfolioHorizonMigration");
  const tbody = document.getElementById("portfolioHorizonRows");
  const badge = document.getElementById("portfolioHorizonStatus");
  const actions = document.getElementById("portfolioHorizonActions");
  const message = document.getElementById("portfolioHorizonMessage");
  if (!section || !tbody) return;
  const previousSelections = new Map(
    [...tbody.querySelectorAll(".portfolio-horizon-select")].map((select) => [
      `${select.dataset.symbol}:${select.dataset.tradeId}`,
      select.value,
    ])
  );
  const lots = portfolioHorizonLots();
  if (!lots.length) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  const unknownLots = lots.filter((lot) => ![
    "short_trade", "mid_swing", "long_trend",
  ].includes(String(lot?.strategyHorizon || "unknown")));
  section.dataset.unknownCount = String(unknownLots.length);
  if (badge) badge.textContent = unknownLots.length
    ? `待鎖定 ${unknownLots.length} 個 lot`
    : `已鎖定 ${lots.length} 個 lot`;
  if (actions) actions.hidden = unknownLots.length === 0;

  tbody.innerHTML = lots.map((lot) => {
    const symbol = normalizeStockCode(lot.symbol);
    const tradeId = Number(lot.tradeId || 0);
    const horizon = String(lot.strategyHorizon || "unknown");
    const locked = ["short_trade", "mid_swing", "long_trend"].includes(horizon);
    const selectionKey = `${symbol}:${tradeId}`;
    const selected = previousSelections.get(selectionKey) || "";
    const horizonCell = locked
      ? `<strong>${escapeHtml(portfolioHorizonLabel(horizon))}</strong>`
      : tradeId
        ? `<select class="portfolio-horizon-select" data-symbol="${escapeHtml(symbol)}" data-trade-id="${tradeId}" aria-label="${escapeHtml(`${symbol} lot ${tradeId} 策略週期`)}">
            <option value="">選擇週期</option>
            <option value="short_trade"${selected === "short_trade" ? " selected" : ""}>短期｜10 日</option>
            <option value="mid_swing"${selected === "mid_swing" ? " selected" : ""}>中期｜20～60 日</option>
            <option value="long_trend"${selected === "long_trend" ? " selected" : ""}>長期｜60 日以上</option>
          </select>`
        : `<span class="down">缺少 tradeId</span>`;
    const lockStatus = locked
      ? `${portfolioHorizonSourceLabel(lot.strategyHorizonSource)}${lot.strategyHorizonLockedAt ? `｜${lot.strategyHorizonLockedAt}` : ""}`
      : tradeId ? "尚未鎖定" : "先同步成交明細";
    return `
      <tr class="${locked ? "portfolio-horizon-locked" : "portfolio-horizon-pending"}">
        <td><strong>${escapeHtml(symbol)}</strong><small>${escapeHtml(lot.name || "")}</small></td>
        <td>#${tradeId || "-"}</td>
        <td>${escapeHtml(lot.buyDate || "未知")}</td>
        <td>${portfolioHorizonCostText(lot)}</td>
        <td>${Number(lot.shares || 0).toLocaleString("zh-TW")}</td>
        <td>${horizonCell}</td>
        <td><small>${escapeHtml(lockStatus)}</small></td>
      </tr>
    `;
  }).join("");

  if (!unknownLots.length) {
    resetPortfolioHorizonPreview();
    if (message) message.textContent = "所有持股 lot 已鎖定；既有週期不可覆寫。";
  } else if (message && !portfolioHorizonMigrationState.previewKey) {
    message.textContent = "逐批選擇週期後先預覽；確認寫入後不可覆寫。";
  }
  updatePortfolioHorizonControls();
}

async function postPortfolioHorizonBatch(body) {
  const response = await fetch("/api/portfolio/strategy-horizons/batch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok || !payload.ok) throw new Error(payload.error || "lot 週期鎖定失敗");
  return payload;
}

function bindPortfolioHorizonControls() {
  const section = document.getElementById("portfolioHorizonMigration");
  const previewButton = document.getElementById("previewPortfolioHorizons");
  const applyButton = document.getElementById("applyPortfolioHorizons");
  const confirm = document.getElementById("confirmPortfolioHorizons");
  const message = document.getElementById("portfolioHorizonMessage");
  if (!section || !previewButton || !applyButton || !confirm) return;

  section.addEventListener("change", (event) => {
    if (event.target?.classList?.contains("portfolio-horizon-select")) {
      resetPortfolioHorizonPreview("選項已變更，請重新預覽。");
    }
    updatePortfolioHorizonControls();
  });

  previewButton.addEventListener("click", async () => {
    try {
      const assignments = collectPortfolioHorizonAssignments();
      portfolioHorizonMigrationState.busy = true;
      if (message) message.textContent = "正在預覽 lot 鎖定結果...";
      updatePortfolioHorizonControls();
      const payload = await postPortfolioHorizonBatch({ mode: "preview", assignments });
      portfolioHorizonMigrationState.previewKey = portfolioHorizonAssignmentKey(assignments);
      portfolioHorizonMigrationState.preview = payload.batch;
      confirm.checked = false;
      if (message) {
        message.textContent = `預覽完成：${Number(payload.batch?.assignmentCount || 0)} 個 lot。核對後勾選確認。`;
      }
    } catch (error) {
      resetPortfolioHorizonPreview();
      if (message) message.textContent = friendlyError(error);
    } finally {
      portfolioHorizonMigrationState.busy = false;
      updatePortfolioHorizonControls();
    }
  });

  applyButton.addEventListener("click", async () => {
    try {
      const assignments = collectPortfolioHorizonAssignments();
      const key = portfolioHorizonAssignmentKey(assignments);
      if (key !== portfolioHorizonMigrationState.previewKey) {
        throw new Error("選項與預覽內容不同，請重新預覽");
      }
      if (!confirm.checked) throw new Error("必須先勾選已核對全部 lot");
      portfolioHorizonMigrationState.busy = true;
      if (message) message.textContent = "正在原子鎖定全部 lot...";
      updatePortfolioHorizonControls();
      const payload = await postPortfolioHorizonBatch({
        mode: "apply",
        confirmAll: true,
        assignments,
      });
      portfolioHorizonMigrationState.previewKey = "";
      portfolioHorizonMigrationState.preview = null;
      await loadPortfolioExitAnalysis({ render: false });
      renderScanner();
      const latestMessage = document.getElementById("portfolioHorizonMessage");
      if (latestMessage) {
        latestMessage.textContent = `已鎖定 ${Number(payload.batch?.assignmentCount || 0)} 個 lot，稽核批次 #${payload.batch?.auditId || "-"}。`;
      }
    } catch (error) {
      if (message) message.textContent = friendlyError(error);
    } finally {
      portfolioHorizonMigrationState.busy = false;
      updatePortfolioHorizonControls();
    }
  });
}

function sinopacAutoSyncStorageKey() {
  return "stock-vibe-yongfeng-auto-sync-v1";
}

function readPortfolioCodes() {
  try {
    const stored = JSON.parse(localStorage.getItem(portfolioStorageKey()) || "[]");
    const codes = stored.map(normalizeStockCode).filter((code) => code && !isEtfLikeStock(code));
    if (codes.length) return [...new Set(codes)];
  } catch (error) {
    console.warn("永豐存股清單讀取失敗", error);
  }
  return defaultStocks.map((stock) => stock.code);
}

function readPortfolioHoldings() {
  try {
    const stored = JSON.parse(localStorage.getItem(portfolioHoldingsStorageKey()) || "{}");
    return stored && typeof stored === "object" ? stored : {};
  } catch (error) {
    console.warn("永豐庫存明細讀取失敗", error);
    return {};
  }
}

function readCachedPortfolioCodes() {
  const codes = Object.values(readPortfolioHoldings())
    .map((item) => normalizeStockCode(item.code))
    .filter((code) => code && !isEtfLikeStock(code));
  return [...new Set(codes)];
}

function initialPortfolioCodes() {
  const cachedCodes = readCachedPortfolioCodes();
  if (cachedCodes.length) {
    safeSetItem(portfolioStorageKey(), JSON.stringify(cachedCodes));
    return cachedCodes;
  }
  return readPortfolioCodes();
}

function readPortfolioSummary() {
  try {
    const stored = JSON.parse(localStorage.getItem(portfolioSummaryStorageKey()) || "{}");
    return stored && typeof stored === "object" ? stored : {};
  } catch (error) {
    console.warn("永豐庫存總覽讀取失敗", error);
    return {};
  }
}

function sanitizeChangeRatePercent(rawChangeRate, currentPrice, referencePrice, code = "") {
  const raw = Number(rawChangeRate);
  const price = Number(currentPrice);
  const reference = Number(referencePrice);
  const hasReference = Number.isFinite(price) && Number.isFinite(reference) && reference > 0;
  const derived = hasReference ? ((price - reference) / reference) * 100 : null;
  if (!Number.isFinite(raw)) return derived;
  if (derived === null || Math.abs(derived) < 0.05) return raw;
  const ratio = Math.abs(raw) / Math.abs(derived);
  if (ratio < 0.2 || ratio > 5) {
    console.warn(`${code ? code + " " : ""}changeRate 尺度異常(raw=${raw}, 依現價/參考價推算=${derived.toFixed(2)})，改用推算值`);
    return derived;
  }
  return raw;
}

function savePortfolioHoldings(holdings) {
  const rows = Array.isArray(holdings) ? holdings : [];
  const mapped = Object.fromEntries(rows.filter((item) => !isEtfLikeStock(item)).map((item) => {
    const code = normalizeStockCode(item.code);
    const shares = Number.isFinite(Number(item.shares)) && Number(item.shares) > 0
      ? Number(item.shares)
      : Number(item.quantity || 0) * 1000;
    return [code, {
      code,
      name: sanitizeStockName(item.name || item.stockName || item.stock_name || item.stock_name_zh, code),
      quantity: shares / 1000,
      shares,
      quantitySource: item.quantitySource || "",
      price: Number(item.price || 0),
      pnl: item.pnl === null || item.pnl === undefined ? null : Number(item.pnl),
      currentPrice: item.currentPrice === null || item.currentPrice === undefined ? null : Number(item.currentPrice),
      referencePrice: item.referencePrice === null || item.referencePrice === undefined ? null : Number(item.referencePrice),
      changePrice: item.changePrice === null || item.changePrice === undefined ? null : Number(item.changePrice),
      changeRate: sanitizeChangeRatePercent(item.changeRate, item.currentPrice, item.referencePrice, code),
      openPrice: item.openPrice === null || item.openPrice === undefined ? null : Number(item.openPrice),
      highPrice: item.highPrice === null || item.highPrice === undefined ? null : Number(item.highPrice),
      lowPrice: item.lowPrice === null || item.lowPrice === undefined ? null : Number(item.lowPrice),
      totalVolume: item.totalVolume === null || item.totalVolume === undefined ? null : Number(item.totalVolume),
      snapshotAt: item.snapshotAt || "",
      direction: item.direction || ""
    }];
  }).filter(([code]) => code));
  safeSetItem(portfolioHoldingsStorageKey(), JSON.stringify(mapped));
  return mapped;
}

function holdingShares(holding) {
  const directShares = Number(holding?.shares);
  if (Number.isFinite(directShares) && directShares > 0) return directShares;
  return Number(holding?.quantity || 0) * 1000;
}

function firstFiniteNumber(values) {
  for (const value of values) {
    if (value === null || value === undefined || value === "") continue;
    const number = Number(value);
    if (Number.isFinite(number)) return number;
  }
  return null;
}

function savePortfolioSummary(payload, mappedHoldings = null) {
  const holdings = mappedHoldings || savePortfolioHoldings(payload?.holdings || []);
  const rows = Object.values(holdings || {});
  const totalCost = rows.reduce((sum, item) => {
    const shares = holdingShares(item);
    return sum + (shares && Number(item.price) ? shares * Number(item.price) : 0);
  }, 0);
  const fallbackValue = rows.reduce((sum, item) => {
    const shares = holdingShares(item);
    const price = Number(item.currentPrice || item.price || 0);
    return sum + (shares && price ? shares * price : 0);
  }, 0);
  let pnlKnown = false;
  const fallbackPnl = rows.reduce((sum, item) => {
    if (item.pnl === null || item.pnl === undefined || !Number.isFinite(Number(item.pnl))) return sum;
    pnlKnown = true;
    return sum + Number(item.pnl);
  }, 0);
  const totalPnl = pnlKnown ? fallbackPnl : fallbackValue - totalCost;
  const currentValue = pnlKnown ? totalCost + totalPnl : fallbackValue;
  const accountBalance = payload?.accountBalance || {};
  const settlements = payload?.settlements || {};
  // 只扣「未來還沒交割」的(pendingTotal);今天/過去已交割完成的那筆已反映在 acc_balance,
  // 用舊的 total(含今天)會把它重複扣一次→帳戶總值少一整筆(2026-07-06 修)。舊資料無 pendingTotal 才退回 total。
  const settlementTotal = settlements.ok
    ? (Number.isFinite(Number(settlements.pendingTotal)) ? Number(settlements.pendingTotal)
      : Number.isFinite(Number(settlements.total)) ? Number(settlements.total) : null)
    : null;
  const availableCash = firstFiniteNumber([
    accountBalance.availableCash,
    accountBalance.available_balance,
    accountBalance.available,
    accountBalance.acc_balance,
  ]);
  const availableAfterSettlement = firstFiniteNumber([
    accountBalance.availableAfterSettlement,
    accountBalance.afterSettlementAvailable,
    accountBalance.after_settlement_available,
    accountBalance.settlementAvailable,
    accountBalance.settlement_available,
  ]);
  const resolvedAvailableAfterSettlement = availableAfterSettlement !== null
    ? availableAfterSettlement
    : availableCash !== null && settlementTotal !== null
      ? availableCash + settlementTotal
      : availableCash;
  const summary = {
    count: rows.length,
    totalCost,
    currentValue,
    totalPnl,
    returnRate: totalCost ? (totalPnl / totalCost) * 100 : null,
    availableAfterSettlement: resolvedAvailableAfterSettlement,
    originalAvailable: availableCash,
    unsettledAmount: settlementTotal,
    settlementStatus: settlements.ok ? "ok" : (settlements.error || "無資料"),
    settlementError: settlements.rawError || settlements.error || "",
    availableCashSource: accountBalance.availableCashSource || "",
    updatedAt: payload?.updatedAt || accountBalance.updatedAt || new Date().toLocaleString("sv-SE").replace("T", " "),
    accountMasked: payload?.accountMasked || "",
  };
  safeSetItem(portfolioSummaryStorageKey(), JSON.stringify(summary));
  // 摘要+持股明細同步到伺服器：手機純閱讀不會自己跑永豐同步，靠這份
  // 伺服器快取顯示最新的總資產/持股現價/損益(fire-and-forget，失敗不影響本機)。
  portfolioSummarySyncPromise = fetch("/api/portfolio/summary-cache", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ summary, holdings }),
  }).then((response) => {
    if (!response.ok) return null;
    return loadPortfolioExitAnalysis({ render: true });
  }).catch(() => {});
  return summary;
}

async function loadPortfolioCacheFromServer() {
  // 手機端的即時同步入口：電腦每次同步永豐成功會把摘要+持股上傳伺服器，
  // 這裡把較新的那份拉下來寫進本機快取並重繪。updatedAt 比大小保證
  // 不會用舊資料蓋掉較新的本機資料(桌面自己剛同步完的情況)。
  try {
    const response = await fetch("/api/portfolio/summary-cache");
    const payload = await response.json();
    if (!response.ok || !payload.ok || !payload.summary) return;
    const localSummary = readPortfolioSummary();
    if (String(payload.summary.updatedAt || "") <= String(localSummary.updatedAt || "")) return;
    safeSetItem(portfolioSummaryStorageKey(), JSON.stringify(payload.summary));
    if (payload.holdings && typeof payload.holdings === "object" && Object.keys(payload.holdings).length) {
      safeSetItem(portfolioHoldingsStorageKey(), JSON.stringify(payload.holdings));
    }
    await loadPortfolioExitAnalysis({ render: false }).catch(() => null);
    renderScanner();
  } catch (error) { /* 讀不到就沿用本機快取 */ }
}
loadPortfolioCacheFromServer();
// 手機純閱讀：每45秒跟伺服器對時，電腦同步後手機一分鐘內自動跟上
if (window.matchMedia("(max-width: 720px)").matches) {
  setInterval(loadPortfolioCacheFromServer, 45000);
}

function makeStock(code, overrides = {}) {
  const existing = defaultStocks.find((stock) => stock.code === code);
  if (existing) return { ...existing, ...overrides };
  const profile = knownStockProfiles[code] || {};
  const displayName = sanitizeStockName(overrides.name, code) || sanitizeStockName(profile.name, code) || code;
  return {
    code,
    name: displayName,
    base: overrides.base || profile.base || 100,
    vol: overrides.vol || profile.vol || 0.012,
    drift: overrides.drift || profile.drift || 0.00007,
    sector: overrides.sector || profile.sector || "台股",
    market: overrides.market || profile.market || ""
  };
}

let stocks = initialPortfolioCodes().map(makeStock);

function analysisStockCodes() {
  return [...new Set(stocks.map((stock) => stock.code).map(normalizeStockCode).filter(Boolean))];
}

function analysisStocks() {
  const stockMap = new Map(stocks.map((stock) => [stock.code, stock]));
  return analysisStockCodes().map((code) => stockMap.get(code) || makeStock(code));
}

const state = {
  ticker: stocks[0]?.code || "2330",
  period: 5,
  strategy: "ai",
  overlay: "ma",
  maxRisk: 4,
  capital: 1000000,
  riskPct: 1,
  atrMultiple: 2,
  costPct: 0.3,
  alertLog: []
};

const RULE_BACKTEST_ENTRY_SCORE = 58;
const RULE_BACKTEST_EXIT_SCORE = 44;

let lastSnapshot = null;
const dataCache = new Map();
const dataErrors = new Map();
function readUiCache(key) {
  try {
    const payload = JSON.parse(localStorage.getItem(key) || "null");
    return payload && typeof payload === "object" ? payload : null;
  } catch {
    return null;
  }
}

function writeUiCache(key, payload) {
  try {
    localStorage.setItem(key, JSON.stringify(payload));
  } catch {
    // Ignore quota or private-mode storage failures; backend data remains authoritative.
  }
}

// localStorage 在無痕模式/第三方儲存被封鎖/配額爆滿時，getItem/setItem 都會
// 直接 throw(SecurityError/QuotaExceededError)。readUiCache/writeUiCache 已經
// 示範了正確做法，但既有程式碼裡還有十幾處 raw localStorage.setItem/getItem/
// removeItem 呼叫點沒有套用同一套保護——任何一次寫入失敗就會讓呼叫它的函式
// (多半是 render() 內的高頻路徑)直接中止，輕則背景監控靜默停擺，重則整頁
// 空白。這兩個是給「不是存 JSON 物件」的呼叫點(單純字串/時間戳)用的版本。
function safeGetItem(key, fallback = null) {
  try {
    const value = localStorage.getItem(key);
    return value === null ? fallback : value;
  } catch {
    return fallback;
  }
}

function safeSetItem(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Ignore quota or private-mode storage failures; in-memory state remains authoritative.
  }
}

function safeRemoveItem(key) {
  try {
    localStorage.removeItem(key);
  } catch {
    // Ignore quota or private-mode storage failures.
  }
}

function readFreshIntradayCache(key, maxAgeMs = 2 * 60 * 1000) {
  const payload = readUiCache(key);
  const updatedAt = String(payload?.updatedAt || "");
  const timestamp = Date.parse(updatedAt.replace(" ", "T"));
  if (!payload || !Number.isFinite(timestamp)) return null;
  if (Date.now() - timestamp <= maxAgeMs) return payload;
  try {
    localStorage.removeItem(key);
  } catch {
    // Ignore private-mode storage failures.
  }
  return null;
}

let monsterBackendState = { loading: false, error: "", payload: readUiCache("stock-vibe-monster-payload-v1") };
let radarScoreTrackState = { loading: false, error: "", payload: readUiCache("stock-vibe-radar-score-track-v2") };
let themeSearchState = { loading: false, error: "", text: "", groundedHotSectors: [], generatedAt: "", attempted: false };
let monsterIntradayState = readFreshIntradayCache("stock-vibe-monster-intraday-v1") || { ok: true, active: false, quotes: {}, updatedAt: "", error: "" };
let monsterScanPollTimer = null;
// pollMonsterScanStatus 每 1 秒打一次 /api/monster-scan/status；後端忙碌
// (例如重訓)時單次請求可能逾時或連線失敗，但掃描本身通常還在背景跑。
// 舊版只要失敗一次就整個 stopMonsterScanPolling+解鎖按鈕+顯示錯誤，讓
// 使用者以為掃描已經停掉，其實只是這一秒剛好沒讀到。改成累計連續失敗，
// 容忍幾次暫時性失敗，真的連續多次失敗才視為掃描讀不到、停止輪詢。
let monsterScanPollFailCount = 0;
const MONSTER_SCAN_POLL_FAIL_LIMIT = 5;
let tokenConfigured = false;
const brainDecisionCache = new Map();
const brainDecisionRequestMap = new Map();
const brainDecisionBatchRequestMap = new Map();
const brainDecisionBatchPendingCodes = new Map();
const BRAIN_DECISION_TIMEOUT_MS = 18000;
const BRAIN_DECISION_BATCH_TIMEOUT_MS = 45000;
// portfolio_exit 的 Brain 結論超過這個時間就該重抓：通知放行/阻擋不能一直
// 沿用早上(甚至昨天)的結論。監控 tick 每輪會對「過期或缺漏」的持股補抓。
const BRAIN_DECISION_PORTFOLIO_TTL_MS = 10 * 60 * 1000;

function brainDecisionPersistKey() {
  return "stock-vibe-yongfeng-brain-portfolio-exit-v2";
}

// 賣出通知鏈的死穴修復：brainDecisionCache 原本只存在記憶體，唯一填充入口
// 是手動按「建立提醒」——頁面重載/分頁被 Chrome 回收後快取全空，監控迴圈又
// 永遠不重抓，之後所有賣出通知靜默死亡而 UI 仍顯示「背景監控中」。
// 這裡把 portfolio_exit 的成功結論持久化到 localStorage，開頁時水合回來，
// 讓重載後的第一輪監控就有 Brain 結論可用(過期的由 TTL 驅動重抓)。
function persistPortfolioBrainDecisions() {
  const rows = [];
  brainDecisionCache.forEach((value, key) => {
    if (!key.endsWith("::portfolio_exit")) return;
    if (!value || value.loading || value.ok !== true) return;
    rows.push(value);
  });
  if (rows.length) {
    writeUiCache(brainDecisionPersistKey(), { updatedAt: new Date().toISOString(), decisions: rows.slice(0, 80) });
  }
}

function hydratePortfolioBrainDecisions() {
  const cached = readUiCache(brainDecisionPersistKey());
  const rows = Array.isArray(cached?.decisions) ? cached.decisions : [];
  rows.forEach((decision) => {
    const code = normalizeStockCode(decision?.symbol);
    if (!code || decision?.ok !== true) return;
    const key = brainDecisionCacheKey(code, "portfolio_exit");
    if (!brainDecisionCache.has(key)) brainDecisionCache.set(key, decision);
  });
}
const brainDecisionQueue = { high: [], normal: [], running: false };
let backendMlStatus = null;
let analysisMode = safeGetItem("stock-vibe-analysis-mode-v1", "simple");
let analysisRequested = false;
let sinopacSyncInFlight = false;
let sinopacLastSyncSuccessAt = 0;
// 節流一定要用「上次嘗試時間」，不能用「上次成功時間」：這個節流原本
// 就是為了擋永豐連續登入被拒(400)——而「持續回傳400」正是從未成功過的
// 情境，如果用sinopacLastSyncSuccessAt判斷，節流在最需要生效的時候
// (一直失敗)反而恆為通過，形同虛設。
let sinopacLastSyncAttemptAt = 0;
const SINOPAC_SYNC_MIN_GAP_MS = 30 * 1000;
let sinopacAutoSyncTimer = null;
let sinopacAutoSyncClockTimer = null;
let portfolioAlertMonitorTimer = null;
// 通知開關(賣出/停損通知)的持久化 key:勾掉後要跨重整記住「不通知」,不會每次
// 重載又變回預設開啟。runPortfolioAlertMonitorTick/startPortfolioAlertMonitor 都
// 吃 portfolioAlertMonitor.checked,勾掉=背景監控不跑=完全不推播(含硬停損)。
const PORTFOLIO_ALERT_NOTIFY_KEY = "stock-vibe-portfolio-notify-enabled-v1";
// 細分通知開關(2026-07-08):停利 / 停損(帳上獲利部位轉弱出場) / 停損(帳上虧損部位,含
// -7% 硬停損)。各自持久化、預設開啟,只影響 shouldNotifyPortfolioAlert 的類型閘門。
const NOTIFY_TAKEPROFIT_KEY = "stock-vibe-notify-takeprofit-v1";
const NOTIFY_STOP_PROFIT_KEY = "stock-vibe-notify-stop-profit-v1";
const NOTIFY_STOP_LOSS_KEY = "stock-vibe-notify-stop-loss-v1";
// 賣出 LINE 全域靜音:狀態存伺服器(跨裝置一致),loadUserPrefs 載入時同步進來。
// 上面三個 localStorage 開關是「各裝置各自」+ 只擋類型;這個是「全域一鍵靜音賣出 LINE」,
// 不受單一瀏覽器 localStorage 影響(伺服器端 handle_line_notify 也會擋,authoritative)。
let sellLineMutedGlobal = false;
function notifyTypeEnabled(key) {
  try {
    const v = localStorage.getItem(key);
    return v === null ? true : v === "1";   // 沒設定過預設開啟
  } catch {
    return true;
  }
}
function wireNotifyToggle(id, key) {
  const el = document.getElementById(id);
  if (!el) return;
  try {
    const saved = localStorage.getItem(key);
    if (saved !== null) el.checked = saved === "1";
  } catch {}
  el.addEventListener("change", () => {
    try { localStorage.setItem(key, el.checked ? "1" : "0"); } catch {}
    renderPortfolioAlerts();
  });
}

function applyTheme(theme = safeGetItem("stock-vibe-theme-v1", "dark")) {
  const dark = theme !== "light";
  document.body.classList.toggle("dark", dark);
  safeSetItem("stock-vibe-theme-v1", dark ? "dark" : "light");
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
    return "AI 請求中斷，可能是本機 server 剛重啟或瀏覽器連線暫時斷線。請先重新整理頁面，再按一次產生 AI 說明。";
  }
  return message;
}

function seededRandom(seed) {
  let value = seed % 2147483647;
  return () => {
    value = (value * 16807) % 2147483647;
    return (value - 1) / 2147483646;
  };
}

function generateSeries(stock) {
  const rand = seededRandom(Number(stock.code) * 97);
  let close = stock.base;
  const rows = [];
  const start = new Date();
  start.setDate(start.getDate() - 900);

  for (let i = 0; i < 780; i += 1) {
    const cycle = Math.sin(i / 34) * 0.0016 + Math.cos(i / 89) * 0.001;
    const meanReversion = ((stock.base - close) / stock.base) * 0.0018;
    const noise = (rand() - 0.49) * stock.vol;
    const shock = i % 137 === 0 ? (rand() - 0.45) * 0.06 : 0;
    close = Math.max(stock.base * 0.45, close * (1 + stock.drift + cycle + meanReversion + noise + shock));
    const open = close * (1 + (rand() - 0.5) * stock.vol);
    const high = Math.max(open, close) * (1 + rand() * stock.vol * 0.9);
    const low = Math.min(open, close) * (1 - rand() * stock.vol * 0.9);
    const volume = Math.round((6500 + rand() * 21000) * (1 + Math.abs(noise) * 24));
    const foreign = Math.round((rand() - 0.44) * 6200 + cycle * 210000);
    const trust = Math.round((rand() - 0.5) * 2600 + stock.drift * 900000);
    const margin = Math.round((rand() - 0.5) * 1800 - cycle * 50000);
    const short = Math.round((rand() - 0.52) * 1100);
    const revenueGrowth = 5 + Math.sin(i / 55) * 12 + (rand() - 0.5) * 8 + stock.drift * 7000;
    const grossMargin = 28 + Math.cos(i / 70) * 8 + rand() * 10 + (stock.sector === "半導體" ? 14 : 0);
    const twIndex = 18000 + i * 6 + Math.sin(i / 40) * 800 + (rand() - 0.5) * 180;
    const usdTwd = 31.5 + Math.cos(i / 95) * 0.8 + (rand() - 0.5) * 0.25;
    const date = new Date(start);
    date.setDate(start.getDate() + i);
    rows.push({ date, open, high, low, close, volume, foreign, trust, margin, short, revenueGrowth, grossMargin, twIndex, usdTwd });
  }

  return rows;
}

function enrichMarketRows(rows, stock) {
  const finiteOr = (value, fallback) => Number.isFinite(Number(value)) ? Number(value) : fallback;
  return rows.map((row, index) => {
    return {
      ...row,
      foreign: finiteOr(row.foreign, null),
      trust: finiteOr(row.trust, null),
      margin: finiteOr(row.margin, null),
      short: finiteOr(row.short, null),
      revenueGrowth: finiteOr(row.revenueGrowth, null),
      grossMargin: finiteOr(row.grossMargin, null),
      operatingMargin: finiteOr(row.operatingMargin, null),
      roe: finiteOr(row.roe, null),
      debtRatio: finiteOr(row.debtRatio, null),
      operatingCashflowRatio: finiteOr(row.operatingCashflowRatio, null),
      per: finiteOr(row.per, null),
      pbr: finiteOr(row.pbr, null),
      dividendYield: finiteOr(row.dividendYield, null),
      dayTradeRatio: finiteOr(row.dayTradeRatio, null),
      dayTradeImbalance: finiteOr(row.dayTradeImbalance, null),
      securitiesLendingVolume: finiteOr(row.securitiesLendingVolume, null),
      securitiesLendingFeeRate: finiteOr(row.securitiesLendingFeeRate, null),
      brokerBranchNetBuy: finiteOr(row.brokerBranchNetBuy, null),
      mainForceBuySell: finiteOr(row.mainForceBuySell, null),
      realtimeMoneyFlow: finiteOr(row.realtimeMoneyFlow, null),
      realtimeLargeOrderFlow: finiteOr(row.realtimeLargeOrderFlow, null),
      twIndex: finiteOr(row.twIndex, null),
      usdTwd: finiteOr(row.usdTwd, null)
    };
  });
}

async function fetchRealSeries(stock, range = "3y") {
  const suffix = String(stock.market || "").toLowerCase() === "tpex" ? "TWO" : "TW";
  const response = await fetch(`/api/stock?symbol=${stock.code}.${suffix}&range=${encodeURIComponent(range)}&interval=1d`);
  const payload = await response.json();
  if (!response.ok || !payload.ok || !payload.rows?.length) {
    throw new Error(payload.error || `無法取得 ${stock.code} 實際資料`);
  }
  return {
    source: payload.source,
    tokenMode: payload.tokenMode,
    fetchedAt: payload.fetchedAt,
    fallbackReason: payload.fallbackReason || "",
    warnings: payload.warnings || [],
    rows: enrichMarketRows(payload.rows.map((row) => ({
      date: new Date(`${row.date}T00:00:00+08:00`),
      open: Number(row.open),
      high: Number(row.high),
      low: Number(row.low),
      close: Number(row.close),
      volume: Number(row.volume),
      foreign: row.foreign == null ? undefined : Number(row.foreign),
      trust: row.trust == null ? undefined : Number(row.trust),
      margin: row.margin == null ? undefined : Number(row.margin),
      short: row.short == null ? undefined : Number(row.short),
      revenueGrowth: row.revenueGrowth == null ? undefined : Number(row.revenueGrowth),
      grossMargin: row.grossMargin == null ? undefined : Number(row.grossMargin),
      operatingMargin: row.operatingMargin == null ? undefined : Number(row.operatingMargin),
      roe: row.roe == null ? undefined : Number(row.roe),
      debtRatio: row.debtRatio == null ? undefined : Number(row.debtRatio),
      operatingCashflowRatio: row.operatingCashflowRatio == null ? undefined : Number(row.operatingCashflowRatio),
      per: row.per == null ? undefined : Number(row.per),
      pbr: row.pbr == null ? undefined : Number(row.pbr),
      dividendYield: row.dividendYield == null ? undefined : Number(row.dividendYield),
      dayTradeRatio: row.dayTradeRatio == null ? undefined : Number(row.dayTradeRatio),
      dayTradeImbalance: row.dayTradeImbalance == null ? undefined : Number(row.dayTradeImbalance),
      securitiesLendingVolume: row.securitiesLendingVolume == null ? undefined : Number(row.securitiesLendingVolume),
      securitiesLendingFeeRate: row.securitiesLendingFeeRate == null ? undefined : Number(row.securitiesLendingFeeRate),
      brokerBranchNetBuy: row.brokerBranchNetBuy == null ? undefined : Number(row.brokerBranchNetBuy),
      mainForceBuySell: row.mainForceBuySell == null ? undefined : Number(row.mainForceBuySell),
      realtimeMoneyFlow: row.realtimeMoneyFlow == null ? undefined : Number(row.realtimeMoneyFlow),
      realtimeLargeOrderFlow: row.realtimeLargeOrderFlow == null ? undefined : Number(row.realtimeLargeOrderFlow),
      twIndex: row.twIndex == null ? undefined : Number(row.twIndex),
      usdTwd: row.usdTwd == null ? undefined : Number(row.usdTwd)
    })), stock)
  };
}

async function loadRealData(stock, range = "3y") {
  try {
    const data = await fetchRealSeries(stock, range);
    dataCache.set(stock.code, data);
    dataErrors.delete(stock.code);
  } catch (error) {
    dataErrors.set(stock.code, friendlyError(error));
  }
}

function dataSourceText(stock) {
  const cached = dataCache.get(stock.code);
  if (cached) {
    const tokenText = cached.tokenMode && cached.tokenMode !== "no-token" ? "Sponsor token" : "未使用 token";
    const realtimeText = portfolioRealtimeQuote(stock) ? "，持股分析套用永豐最新庫存價" : "";
    const fallbackText = cached.fallbackReason ? `，Yahoo fallback：${cached.fallbackReason}` : "";
    const yahooLimitedText = String(cached.source || "").includes("Yahoo") ? "，籌碼/財務：無資料" : "";
    const warningText = cached.warnings?.length ? `，缺資料：${cached.warnings.length} 項顯示無資料` : "";
    return `實際日線：${cached.source}（${tokenText}），更新 ${cached.fetchedAt}${realtimeText}${fallbackText}${yahooLimitedText}${warningText}`;
  }
  if (dataErrors.has(stock.code)) return `實際資料抓取失敗：${dataErrors.get(stock.code)}。未使用模擬股價。`;
  return "正在載入正式日線資料...";
}

async function loadTokenStatus() {
  const badge = document.getElementById("tokenStatus");
  try {
    const response = await fetch("/api/settings/finmind-token");
    const payload = await response.json();
    tokenConfigured = Boolean(payload.configured);
    if (badge) {
      badge.textContent = payload.configured ? `已設定 ${payload.masked}` : "尚未設定";
      badge.style.background = payload.configured ? "#e7f8ef" : "#fff4e6";
      badge.style.color = payload.configured ? "#00ff2a" : "#fff200";
    }
  } catch (error) {
    tokenConfigured = false;
    if (badge) {
      badge.textContent = friendlyError(error);
      badge.style.background = "#fff4e6";
      badge.style.color = "#fff200";
    }
  }
}

async function reloadRealData() {
  dataCache.clear();
  dataErrors.clear();
  render();
  await Promise.allSettled(stocks.map(loadRealData));
  render();
}

async function loadBackendStatus() {
  // 首頁(index.html)沒有 backendStatus/backendPredictions 這兩個 DOM 元素
  // (那個「每天自動學習」面板只在 settings.html，由 settings.js 自己渲染)，
  // 主頁只讀資料健康；模型預測留在獨立模型複盤，不載入持股/雷達分析。
  // Brain 結論一起水合：不做的話頁面重載後 brainDecisionCache 全空，
  // 所有提醒卡變 observeOnly、賣出通知靜默死亡(監控UI卻顯示正常)。
  hydratePortfolioBrainDecisions();
  try {
    const statusResponse = await fetch("/api/ml/status");
    const status = await statusResponse.json();
    if (!status.ok) throw new Error(status.error || "資料更新狀態讀取失敗");
    backendMlStatus = status;
    if (document.getElementById("watchlist")) renderScanner();
  } catch (error) {
    backendMlStatus = { error: friendlyError(error) };
    if (document.getElementById("watchlist")) renderScanner();
  }
}

function formalDataHealth() {
  const health = backendMlStatus?.dataHealth;
  if (health && health.ok === false) return health;
  return { ok: true, mode: "normal", reason: "" };
}

function intradayHealth(state) {
  // state.ok 由前端在 fetch 失敗時設成 false（state.health 此時仍保留
  // 上一次成功請求的快取值，不能信任），必須先檢查 state.ok。
  if (state?.ok === false) {
    return { ok: false, mode: "observe_only", reason: state.error || "盤中報價取得失敗，暫停決策" };
  }
  const health = state?.health;
  if (health && health.ok === false) return health;
  return { ok: true, mode: "normal", reason: "" };
}

function portfolioHeldItems(holdings = readPortfolioHoldings()) {
  return portfolioDisplayItems(holdings).filter((item) => {
    const code = normalizeStockCode(item?.stock?.code);
    const holding = code ? holdings[code] : null;
    return Number(holding?.quantity || 0) > 0 && Number(holding?.price || 0) > 0;
  });
}


async function ensurePortfolioBrainDecisions(items = portfolioHeldItems(), options = {}) {
  let codes = [...new Set((items || [])
    .map((item) => normalizeStockCode(item?.stock?.code))
    .filter(Boolean))];
  if (options.onlyStale) {
    // 監控 tick 用：只補「沒有成功結論」或「結論已超過 TTL」的持股，
    // 新鮮的不重抓，避免每輪都全量打 Brain API。
    codes = codes.filter((code) => {
      const cached = brainDecisionStatus(code, "portfolio_exit");
      if (!cached || cached.loading || cached.ok !== true) return true;
      const cachedAt = Number(cached.updatedAt || 0);
      return !cachedAt || Date.now() - cachedAt >= BRAIN_DECISION_PORTFOLIO_TTL_MS;
    });
  }
  if (!codes.length) return { ok: true, checked: 0, failed: [] };
  try {
    // 這支 fetch 原本沒有逾時：後端忙碌(例如重訓中)時會無限期掛著不
    // resolve/reject，讓 runPortfolioAlertMonitorTick 這一輪卡在這裡
    // 不動。setInterval 不等上一輪回呼跑完就會排下一輪，卡住的這次一旦
    // 疊加下一次呼叫，會變成多個 ensurePortfolioBrainDecisions 同時
    // 打相同的 /api/brain/decisions，越忙越重。加上 45 秒逾時，逾時就
    // fall back 到下面逐檔讀取(本身有 18 秒逾時保護)，不會無限期卡住。
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 45000);
    let response;
    try {
      response = await fetch("/api/brain/decisions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbols: codes, context: "portfolio_exit", maxSymbols: codes.length }),
        signal: controller.signal
      });
    } finally {
      clearTimeout(timeoutId);
    }
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "型態量能規則 批次讀取失敗");
    const decisions = Array.isArray(payload.decisions) ? payload.decisions : [];
    const failed = Array.isArray(payload.failed) ? [...payload.failed] : [];
    decisions.forEach((decision) => {
      const code = normalizeStockCode(decision?.symbol);
      if (!code) return;
      brainDecisionCache.set(brainDecisionCacheKey(code, "portfolio_exit"), { ...decision, context: "portfolio_exit", updatedAt: Date.now() });
      if (decision?.ok === false && !failed.some((item) => item.startsWith(`${code}:`))) {
        failed.push(`${code}: ${decision.error || "型態量能規則 讀取失敗"}`);
      }
    });
    persistPortfolioBrainDecisions();
    return { ok: !failed.length, checked: codes.length, failed };
  } catch (error) {
    console.warn("型態量能規則 批次讀取失敗，改用逐檔讀取", error);
  }
  const results = await Promise.allSettled(codes.map((code) => ensureBrainDecision(code, "portfolio_exit")));
  const failed = results.map((result, index) => {
    const value = result.status === "fulfilled" ? result.value : null;
    if (result.status === "rejected") return `${codes[index]}: ${friendlyError(result.reason)}`;
    if (value?.error) return `${codes[index]}: ${value.error}`;
    if (value?.ok === false) return `${codes[index]}: ${value.error || "型態量能規則 讀取失敗"}`;
    return "";
  }).filter(Boolean);
  return { ok: !failed.length, checked: codes.length, failed };
}

async function runBackendUpdate() {
  const button = document.getElementById("runMlUpdate");
  button.disabled = true;
  button.textContent = "正在更新與學習";
  try {
    const response = await fetch("/api/ml/update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbols: analysisStockCodes() })
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "更新失敗");
    await loadBackendStatus();
    await reloadRealData();
  } catch (error) {
    document.getElementById("backendStatus").innerHTML = `<div class="plan-item"><span>更新失敗</span><strong>${escapeHtml(friendlyError(error))}</strong></div>`;
  } finally {
    button.disabled = false;
    button.textContent = "現在更新一次";
  }
}

function sma(rows, key, period) {
  return rows.map((_, index) => {
    if (index < period - 1) return null;
    return rows.slice(index - period + 1, index + 1).reduce((sum, row) => sum + row[key], 0) / period;
  });
}

function ema(values, period) {
  const multiplier = 2 / (period + 1);
  let previous = values[0];
  return values.map((value, index) => {
    if (index === 0) return value;
    previous = value * multiplier + previous * (1 - multiplier);
    return previous;
  });
}

function calcIndicators(rows) {
  const closes = rows.map((row) => row.close);
  const ma5 = sma(rows, "close", 5);
  const ma20 = sma(rows, "close", 20);
  const ma60 = sma(rows, "close", 60);
  const ma120 = sma(rows, "close", 120);
  const ema12 = ema(closes, 12);
  const ema26 = ema(closes, 26);
  const dif = ema12.map((value, index) => value - ema26[index]);
  const dea = ema(dif, 9);
  const macd = dif.map((value, index) => (value - dea[index]) * 2);
  const rsi = rows.map((_, index) => {
    if (index < 14) return 50;
    let gain = 0;
    let loss = 0;
    for (let i = index - 13; i <= index; i += 1) {
      const diff = rows[i].close - rows[i - 1].close;
      if (diff >= 0) gain += diff;
      else loss -= diff;
    }
    return 100 - 100 / (1 + gain / Math.max(loss, 0.01));
  });
  const atr = rows.map((row, index) => {
    if (index === 0) return row.high - row.low;
    if (index < 14) return Math.max(row.high - row.low, Math.abs(row.high - rows[index - 1].close), Math.abs(row.low - rows[index - 1].close));
    return rows.slice(index - 13, index + 1).reduce((sum, item, offset) => {
      const prev = rows[index - 13 + offset - 1]?.close ?? item.close;
      return sum + Math.max(item.high - item.low, Math.abs(item.high - prev), Math.abs(item.low - prev));
    }, 0) / 14;
  });
  const bollMid = ma20;
  const bollUpper = rows.map((_, index) => {
    if (index < 19) return null;
    const values = rows.slice(index - 19, index + 1).map((row) => row.close);
    const mean = bollMid[index];
    const deviation = Math.sqrt(values.reduce((sum, value) => sum + (value - mean) ** 2, 0) / 20);
    return mean + deviation * 2;
  });
  const bollLower = rows.map((_, index) => {
    if (index < 19) return null;
    const values = rows.slice(index - 19, index + 1).map((row) => row.close);
    const mean = bollMid[index];
    const deviation = Math.sqrt(values.reduce((sum, value) => sum + (value - mean) ** 2, 0) / 20);
    return mean - deviation * 2;
  });
  return { ma5, ma20, ma60, ma120, dif, dea, macd, rsi, atr, bollMid, bollUpper, bollLower };
}

function fmt(value, digits = 2) {
  if (value === null || value === undefined || value === "") return "無資料";
  if (!Number.isFinite(Number(value))) return "無資料";
  return Number(value).toLocaleString("zh-TW", { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

function valueText(value, digits = 0, suffix = "") {
  if (value === null || value === undefined || value === "" || !Number.isFinite(Number(value))) return "無資料";
  return `${fmt(value, digits)}${suffix}`;
}

function money(value) {
  if (value === null || value === undefined || value === "") return "-";
  if (!Number.isFinite(Number(value))) return "-";
  return `${Number(value).toLocaleString("zh-TW", { maximumFractionDigits: 0 })} 元`;
}

function priceText(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "-";
  return `${fmt(Number(value))} 元`;
}

function pct(value) {
  if (value === null || value === undefined || value === "" || !Number.isFinite(Number(value))) return "無資料";
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
    <div class="system-health-summary ${health?.ok ? "up" : "down"}">
      <strong>${health?.ok ? "獨立模型可用" : "獨立模型不可用；正式規則分析不受影響"}</strong>
      <span>${escapeHtml(health?.checkedAt || "")}</span>
    </div>
    <div class="backend-status system-health-grid">
      <div class="plan-item"><span>Python 路徑</span><strong>${escapeHtml(health?.python?.executable || "未知")}</strong></div>
      <div class="plan-item"><span>Python 版本</span><strong>${escapeHtml(health?.python?.version || "未知")}</strong></div>
      <div class="plan-item"><span>model.pkl</span><strong>${health?.model?.loadOk ? "可載入" : "不可載入"}</strong></div>
      <div class="plan-item"><span>模型版本</span><strong>${escapeHtml(health?.model?.version || "無")}</strong></div>
      <div class="plan-item"><span>訓練時間</span><strong>${escapeHtml(health?.model?.trainedAt || "無")}</strong></div>
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
  const now = new Date();
  const yyyy = now.getFullYear();
  const mm = String(now.getMonth() + 1).padStart(2, "0");
  const dd = String(now.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

function runDailyFirstSystemHealthCheck() {
  // 手機純閱讀：不自動彈每日健康檢查彈窗(使用者明確不要)。健康檢查
  // 照常由桌面的每日首開觸發，手機只看結果資料。
  if (window.matchMedia("(max-width: 720px)").matches) return;
  try {
    const today = todayDateKey();
    if (localStorage.getItem(DAILY_SYSTEM_HEALTH_CHECK_KEY) === today) return;
    localStorage.setItem(DAILY_SYSTEM_HEALTH_CHECK_KEY, today);
    setTimeout(() => runSystemHealthCheck({ dailyStartup: true, skipBackendReload: true }), 1200);
  } catch {
    setTimeout(() => runSystemHealthCheck({ dailyStartup: true, skipBackendReload: true }), 1200);
  }
}

async function runSystemHealthCheck(options = {}) {
  const button = document.getElementById("systemHealthCheck");
  if (button) {
    button.disabled = true;
    button.textContent = options.dailyStartup ? "每日首次檢查中" : "檢查中";
  }
  try {
    const response = await fetch("/api/system/health");
    const payload = await response.json();
    if (!response.ok && !payload) throw new Error("系統健康檢查失敗");
    showPortfolioPopup("系統健康檢查", systemHealthHtml(payload), { sticky: true, wide: true, html: true });
    if (!options.skipBackendReload) await loadBackendStatus();
  } catch (error) {
    showPortfolioPopup("系統健康檢查", `<strong>獨立模型檢查失敗；正式規則分析不受影響</strong><br>${escapeHtml(friendlyError(error))}`, { sticky: true, wide: true, html: true });
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "系統健康檢查";
    }
  }
}

function periodReturn(rows, days) {
  const latest = rows.at(-1);
  const base = rows.at(-(days + 1));
  if (!latest || !base || !base.close) return null;
  return ((latest.close - base.close) / base.close) * 100;
}

function readExitAlertLog() {
  try {
    const stored = JSON.parse(localStorage.getItem(portfolioExitAlertStorageKey()) || "{}");
    return stored && typeof stored === "object" ? stored : {};
  } catch {
    return {};
  }
}

function saveExitAlertLog(log) {
  // key 格式是 `${日期}-代號-alert-類型`，判斷永遠只比對當日 key——
  // 昨日以前的項目純屬垃圾，寫入時順手清掉，不讓 localStorage 無限累積。
  const todayPrefix = `${dateKey(new Date())}-`;
  const pruned = {};
  Object.entries(log || {}).forEach(([key, value]) => {
    if (key.startsWith(todayPrefix)) pruned[key] = value;
  });
  safeSetItem(portfolioExitAlertStorageKey(), JSON.stringify(pruned));
}

function closePortfolioPopup(popup) {
  popup.classList.remove("show");
  setTimeout(() => popup.remove(), 260);
}

function showPortfolioPopup(title, message, options = {}) {
  let container = document.getElementById("portfolioPopupStack");
  if (!container) {
    container = document.createElement("div");
    container.id = "portfolioPopupStack";
    container.className = "portfolio-popup-stack";
    document.body.appendChild(container);
  }
  const popup = document.createElement("div");
  popup.className = `portfolio-popup${options.sticky ? " sticky" : ""}${options.wide ? " wide" : ""}`;
  const titleHtml = options.html ? escapeHtml(title) : escapeHtml(title);
  const messageHtml = options.html ? message : escapeHtml(message);
  popup.innerHTML = `
    <strong>${titleHtml}</strong>
    <div class="portfolio-popup-body">${messageHtml}</div>
    ${options.sticky ? `<button type="button" class="portfolio-popup-close">關閉</button>` : ""}
  `;
  container.prepend(popup);
  setTimeout(() => popup.classList.add("show"), 20);
  const closeButton = popup.querySelector(".portfolio-popup-close");
  if (closeButton) {
    closeButton.addEventListener("click", () => closePortfolioPopup(popup));
  } else {
    setTimeout(() => closePortfolioPopup(popup), 9000);
  }
}

const decisionTraceRegistry = new Map();

// 每個 scope 一個遞增序號,resetDecisionTraceScope 會歸零 → 相同資料每次重繪產生
// 「相同」的 trace id。原本用 Date.now()+random 讓每次重繪的 HTML 都不同,使
// 「內容沒變就跳過重繪」的優化永遠失效(名單每秒重建、使用者往下捲被彈回最上面)。
const decisionTraceSeq = new Map();

function resetDecisionTraceScope(scope) {
  if (!scope) return;
  [...decisionTraceRegistry.entries()].forEach(([id, trace]) => {
    if (trace.scope === scope) decisionTraceRegistry.delete(id);
  });
  decisionTraceSeq.set(scope, 0);
}

function registerDecisionTrace(trace) {
  const scope = trace.scope || "generic";
  const seq = decisionTraceSeq.get(scope) || 0;
  decisionTraceSeq.set(scope, seq + 1);
  const id = `trace-${scope}-${seq}`;
  decisionTraceRegistry.set(id, trace);
  return id;
}

function decisionTraceButton(traceId) {
  if (!traceId) return "";
  return `<span class="decision-trace-button decision-trace-open" role="button" ${decisionTraceOpenAttrs(traceId)}>判斷細節</span>`;
}

function decisionTraceOpenAttrs(traceId) {
  if (!traceId) return "";
  return `data-decision-trace="${escapeHtml(traceId)}" tabindex="0"`;
}

function sourceLabel(value) {
  const text = String(value || "").trim();
  if (!text) return "等待來源標記";
  if (/shioaji|sinopac/i.test(text)) return "Shioaji / 永豐";
  if (/twse|tpex|official/i.test(text)) return "TWSE / TPEx 官方";
  if (/finmind/i.test(text)) return "FinMind";
  if (/yahoo/i.test(text)) return "Yahoo fallback";
  return text;
}

function tracePriceText(value) {
  return Number.isFinite(Number(value)) ? `${fmt(Number(value))} 元` : "無資料";
}

function tracePctText(value) {
  return Number.isFinite(Number(value)) ? pct(Number(value)) : "無資料";
}

function traceBoolClass(ok) {
  if (ok === true) return "up";
  if (ok === false) return "down";
  return "warn";
}

function traceBoolText(ok) {
  if (ok === true) return "通過";
  if (ok === false) return "未通過";
  return "無資料";
}

function traceConditionsRows(conditions = []) {
  return conditions.map((item) => ({
    label: item.label || "-",
    ok: item.ok,
    value: item.value ?? "",
    source: item.source || "",
  }));
}

function decisionTraceHtml(trace) {
  const dataHealth = trace.dataHealth || formalDataHealth();
  const observeOnly = dataHealth?.ok === false || trace.observeOnly;
  const conditions = traceConditionsRows(trace.conditions);
  const stockText = [trace.code, trace.name].filter(Boolean).join(" ");
  const metaRows = [
    ["股票", stockText || "-"],
    ["判斷", trace.decision || "-"],
    ["判斷來源", trace.decisionSource || "等待來源"],
    ["使用資料日期", trace.dataDate || "等待資料日期"],
    ["現價來源", sourceLabel(trace.priceSource)],
    ["現價", tracePriceText(trace.currentPrice)],
    ["資料狀態", observeOnly ? `只觀察，不通知買賣${dataHealth?.reason ? `：${dataHealth.reason}` : ""}` : "可用"],
  ];
  return `
    <div class="decision-trace">
      <div class="decision-trace-summary">
        <strong>${escapeHtml(trace.summary || trace.decision || "判斷追溯")}</strong>
        <small>${escapeHtml(trace.note || "所有可買、該賣、確認賣出都必須能追溯；資料不足時只觀察。")}</small>
      </div>
      <div class="trace-grid">
        ${metaRows.map(([label, value]) => `
          <div class="trace-meta">
            <span>${escapeHtml(label)}</span>
            <strong>${escapeHtml(value)}</strong>
          </div>
        `).join("")}
      </div>
      <section>
        <h4>買賣條件逐項檢查</h4>
        <table class="trace-table">
          <thead>
            <tr><th>條件</th><th>狀態</th><th>數值/來源</th></tr>
          </thead>
          <tbody>
            ${conditions.length ? conditions.map((row) => `
              <tr>
                <td>${escapeHtml(row.label)}</td>
                <td class="${traceBoolClass(row.ok)}">${traceBoolText(row.ok)}</td>
                <td>${escapeHtml([row.value, row.source].filter(Boolean).join(" / ") || "-")}</td>
              </tr>
            `).join("") : `<tr><td colspan="3">尚無逐項條件，不能作為交易依據。</td></tr>`}
          </tbody>
        </table>
      </section>
      ${trace.detail ? `<p class="trace-detail">${escapeHtml(trace.detail)}</p>` : ""}
    </div>
  `;
}

async function showDecisionTrace(traceId, trigger = null) {
  const trace = decisionTraceRegistry.get(traceId);
  if (!trace) return;
  const isButton = trigger?.tagName === "BUTTON";
  const originalText = trigger?.textContent;
  if (trigger) {
    if (isButton) {
      trigger.disabled = true;
      trigger.textContent = "讀取追溯";
    } else {
      trigger.classList.add("trace-loading");
    }
  }
  try {
    const brainHtml = trace.scope === "monster" ? brainDecisionHtml(trace.code, "", "monster") : "";
    showPortfolioPopup("判斷追溯", `${decisionTraceHtml(trace)}${brainHtml}`, { sticky: true, wide: true, html: true });
  } catch (error) {
    showPortfolioPopup(
      "判斷追溯",
      `<div class="decision-trace"><p class="trace-detail">追溯視窗產生失敗：${escapeHtml(friendlyError(error))}</p></div>`,
      { sticky: true, wide: true, html: true }
    );
  } finally {
    if (trigger) {
      if (isButton) {
        trigger.disabled = false;
        trigger.textContent = originalText || "判斷追溯";
      } else {
        trigger.classList.remove("trace-loading");
      }
    }
  }
}

function bindDecisionTraceButtons() {
  document.querySelectorAll("[data-decision-trace]").forEach((trigger) => {
    if (trigger.dataset.boundDecisionTrace) return;
    trigger.dataset.boundDecisionTrace = "1";
    trigger.addEventListener("click", () => showDecisionTrace(trigger.dataset.decisionTrace, trigger));
    trigger.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      showDecisionTrace(trigger.dataset.decisionTrace, trigger);
    });
  });
}

function portfolioAlertTrace(group, alert, usingCache) {
  const code = normalizeStockCode(group?.code || alert?.code);
  const dataHealth = {
    ok: alert?.dataReady === true,
    reason: alert?.dataReady === true
      ? ""
      : `後端資料門檻未通過（歷史 ${alert?.historyRows ?? 0}/${alert?.minimumHistoryRows ?? 0}、即時報價 ${alert?.quoteFresh ? "新鮮" : "過期"}）`,
  };
  const backendConditions = Array.isArray(alert?.conditions)
    ? alert.conditions.map((condition) => ({
      label: condition?.label || "後端條件",
      ok: condition?.ok,
      value: condition?.value ?? "",
      source: "portfolio-exit-v2",
    }))
    : [];
  return {
    scope: "portfolio-alerts",
    code,
    name: group?.name || alert?.name || "",
    decision: alert?.status || "-",
    summary: `${code} ${group?.name || ""}｜${alert?.status || "提醒判斷"}`,
    note: alert?.fullNote || alert?.note || "",
    currentPrice: group?.currentPrice ?? alert?.currentPrice,
    priceSource: alert?.priceSource || "Shioaji / 永豐庫存",
    dataDate: alert?.dataDate || dateKey(new Date()),
    decisionSource: usingCache ? "後端統一出場快照" : `後端統一出場引擎 ${alert?.policyVersion || ""}`,
    dataHealth,
    observeOnly: alert?.decisionVerified !== true,
    conditions: [
      { label: "成交時策略週期", ok: ["short_trade", "mid_swing", "long_trend"].includes(alert?.strategyHorizon), value: `${alert?.strategyHorizonLabel || "未知"}｜${alert?.strategyHorizonDays || ""}` },
      { label: "FIFO 真實買進日", ok: alert?.buyDateKnown === true, value: alert?.buyDate || "未知，時間出場停用" },
      { label: "後端資料門檻", ok: dataHealth.ok, value: dataHealth.ok ? "通過" : dataHealth.reason },
      { label: "成本後估算損益（含手續費、證交稅、0.1% 出場滑價）", ok: Number(group?.pnlRate) > 0, value: `${tracePctText(group?.pnlRate)}｜${signedMoney(group?.estimatedNetPnl)}` },
      { label: "賣出計算驗證", ok: alert?.decisionVerified === true, value: alert?.decisionVerified ? `通過｜${(alert?.decisionReasons || []).join("、")}` : "未通過，不得通知賣出" },
      ...backendConditions,
      { label: "真正通知賣出", ok: alert?.canNotify === true, value: alert?.canNotify ? "允許通知" : "只觀察或等待確認" },
      { label: "買進均價（成本比較基準）", ok: null, value: tracePriceText(group?.buyPrice) },
    ],
  };
}

function monsterRealtimeFlowText(flow) {
  // tick collector 累積的當日主力動向：money_flow 單位是元、large_order_flow
  // 單位是股(÷1000=張)。null 代表這檔不在訂閱池或收集器沒開，跟「大單中性
  // (0)」是兩回事，不能混著顯示。
  if (!flow || flow.realtimeMoneyFlow == null) return "無tick資料（未在訂閱池）";
  if (flow.realtimeFlowStale) {
    const updatedText = flow.realtimeFlowUpdatedAt ? String(flow.realtimeFlowUpdatedAt).slice(11, 16) : "";
    return `即時主力資料已過期${updatedText ? `（最後 ${updatedText}）` : ""}，不納入判斷`;
  }
  const lots = Number(flow.realtimeLargeOrderFlow || 0) / 1000;
  const money = Number(flow.realtimeMoneyFlow || 0);
  const moneyText = Math.abs(money) >= 1e8 ? `${fmt(money / 1e8, 2)} 億` : `${fmt(money / 1e4, 0)} 萬`;
  const base = `大單淨 ${lots > 0 ? "+" : ""}${fmt(lots, 0)} 張｜主動買盤淨 ${money > 0 ? "+" : ""}${moneyText}（${flow.realtimeTickCount || 0} ticks）`;
  return base;
}

// 部位大小建議：短線散戶最大的洞是「知道買什麼、卻不知道買幾張」，同樣一筆
// 訊號押1張跟押10張的曝險天差地遠。用「單筆最大虧損控制在可用資金 X%」反推
// 建議張數——跌到防守價的虧損=(現價-防守價)×1000/張，買得起的上限=可用資金/
// (現價×1000)，取兩者較小的整數張。純計算、不碰自動下單。
const RADAR_POSITION_RISK_PCT = 2; // 單筆交易最大虧損佔可用資金比例(部位大小建議基準)

function suggestPositionSize(entryPrice, stopPrice, availableCash, riskPct = RADAR_POSITION_RISK_PCT) {
  const entry = Number(entryPrice);
  if (!Number.isFinite(entry) || entry <= 0) return { ok: false, reason: "no_entry" };
  const stop = Number(stopPrice);
  // 防守價必須是正值且低於現價才算得出「每張下跌風險」；否則風險項不可用。
  const perLotRisk = Number.isFinite(stop) && stop > 0 && stop < entry ? (entry - stop) * 1000 : null;
  const cash = Number(availableCash);
  if (!Number.isFinite(cash) || cash <= 0) {
    return { ok: false, reason: "no_cash", perLotRisk, entry, stop };
  }
  const affordableLots = Math.floor(cash / (entry * 1000));
  const riskBudget = cash * (riskPct / 100);
  // 有防守價就用風險反推張數，沒有就只受「買得起」限制。
  const riskLots = perLotRisk ? Math.floor(riskBudget / perLotRisk) : affordableLots;
  const lots = Math.max(0, Math.min(riskLots, affordableLots));
  const maxLoss = perLotRisk ? lots * perLotRisk : null;
  const deployed = lots * entry * 1000;
  return {
    ok: true,
    lots,
    perLotRisk,
    hasStop: perLotRisk != null,
    maxLoss,
    maxLossPct: maxLoss != null && cash ? (maxLoss / cash) * 100 : null,
    deployed,
    deployedPct: cash ? (deployed / cash) * 100 : null,
    riskPct,
    entry,
    stop,
  };
}

function positionSizingTraceText(item) {
  const summary = readPortfolioSummary() || {};
  const cash = firstFiniteNumber([summary.availableAfterSettlement, summary.originalAvailable]);
  const entry = Number(item?.intraday?.executionEntryPrice) > 0
    ? Number(item.intraday.executionEntryPrice)
    : Number(item?.currentPrice) > 0 ? Number(item.currentPrice) : Number(item?.buyTrigger);
  const stop = Number(item?.intraday?.executionStopPrice) > 0
    ? Number(item.intraday.executionStopPrice)
    : item?.stopPrice;
  const sizing = suggestPositionSize(entry, stop, cash);
  if (!sizing.ok) {
    if (sizing.reason === "no_entry") return "尚無現價，無法估算";
    // no_cash：沒有可用資金(未連永豐)——至少把「每張下跌風險」算給使用者參考。
    const perLot = sizing.perLotRisk != null
      ? `每張風險約 ${fmt(sizing.perLotRisk, 0)} 元（跌到防守價 ${fmt(sizing.stop, 2)}）`
      : "尚無有效防守價";
    return `${perLot}｜連接永豐證券後顯示建議張數`;
  }
  if (sizing.lots <= 0) {
    return `依單筆風險 ${sizing.riskPct}% 與可用資金，這檔建議暫不進場（單張風險或價位超過預算）`;
  }
  const parts = [`建議 ${sizing.lots} 張`];
  if (sizing.hasStop) {
    parts.push(`最大虧損約 ${fmt(sizing.maxLoss, 0)} 元（${fmt(sizing.maxLossPct, 1)}% 資金）`);
  }
  parts.push(`投入約 ${fmt(sizing.deployed, 0)} 元（${fmt(sizing.deployedPct, 0)}% 資金）`);
  parts.push(`單筆風險上限 ${sizing.riskPct}%`);
  return parts.join("｜");
}

function monsterDecisionTrace(item, open, decision, method, health) {
  const code = normalizeStockCode(item?.stock?.code || item?.symbol);
  const brainCondition = brainDecisionTraceCondition(code, health?.brainContext || "monster");
  const intradayFlow = monsterIntradayState?.quotes?.[code] || null;
  return {
    scope: "monster",
    code,
    name: stockDisplayName(code, item?.stock?.name || item?.name),
    decision,
    summary: `${code} ${stockDisplayName(code, item?.stock?.name || item?.name)}｜${decision}`,
    note: compactMonsterReasons(item) || item?.status || "",
    currentPrice: item?.currentPrice,
    priceSource: open?.intraday ? "Shioaji / 盤中確認" : monsterIntradayState?.source || "TWSE/TPEx/FinMind 日線",
    dataDate: item?.priceDate || monsterBackendState.payload?.scanDate || "",
    decisionSource: "型態量能規則判斷 + 盤中時間/價格硬規則",
    dataHealth: health,
    observeOnly: health?.ok === false || brainCondition.ok === false,
    conditions: [
      brainCondition,
      { label: "盤中資料健康", ok: health?.ok !== false, value: health?.ok === false ? health.reason || "資料異常" : "通過" },
      { label: "妖股分數（型態量能：量能＋突破月高＋比大盤強＋逆勢）", ok: Number(item?.score || 0) > 0, value: fmt(item?.score, 2) },
      {
        label: "風險型態旗標（danger 直接否決；warn 提醒避開追高）",
        ok: (Array.isArray(item?.riskFlags) && item.riskFlags.length) ? false : null,
        value: (Array.isArray(item?.riskFlags) && item.riskFlags.length)
          ? "🚩 " + item.riskFlags.map((f) => f?.label).filter(Boolean).join("、")
          : "無（未偵測到倒貨/轉弱/過熱/處置/注意）",
      },
      { label: "流動性與量能通過", ok: Number(item?.volumeRatio || 0) >= 1.2 || item?.volumeRatio == null, value: item?.volumeRatio == null ? "無盤中量能" : `${fmt(item.volumeRatio, 2)} 倍` },
      {
        // 純顯示資訊(不進 canBuy 判斷式)：突破當下有沒有大單在買，是判斷
        // 突破真假的確認維度。✓=大單淨買、✗=大單淨賣、—=中性或無資料。
        label: "主力動向（盤中大單，僅供確認）",
        ok: intradayFlow?.realtimeLargeOrderFlow == null || Number(intradayFlow.realtimeLargeOrderFlow) === 0
          ? null
          : Number(intradayFlow.realtimeLargeOrderFlow) > 0,
        value: monsterRealtimeFlowText(intradayFlow),
      },
      { label: "目前價突破觀察買點", ok: Number(item?.currentPrice || 0) >= Number(item?.buyTrigger || 0), value: `${tracePriceText(item?.currentPrice)} / 買點 ${tracePriceText(item?.buyTrigger)}` },
      { label: "沒有跌破停損", ok: !(Number(item?.currentPrice || 0) <= Number(item?.stopPrice || 0)), value: `停損 ${tracePriceText(item?.stopPrice)}` },
      {
        label: "成本後風險報酬（手續費、證交稅、估計滑價後）",
        ok: intradayFlow?.rewardRiskPassed == null ? null : Boolean(intradayFlow.rewardRiskPassed),
        value: intradayFlow?.netRewardRiskRatio == null
          ? "等待即時成交價"
          : `${fmt(intradayFlow.netRewardRiskRatio, 2)} / 最低 ${fmt(intradayFlow.minimumNetRewardRiskRatio, 2)}｜成交 ${tracePriceText(intradayFlow.executionEntryPrice)}｜停損 ${tracePriceText(intradayFlow.executionStopPrice)}｜目標 ${tracePriceText(intradayFlow.executionTargetPrice)}`,
      },
      {
        label: "實際成交價未過度追價",
        ok: intradayFlow?.entryDriftBlocked == null ? null : !intradayFlow.entryDriftBlocked,
        value: intradayFlow?.entryDriftPct == null ? "等待即時成交價" : `偏離買點 ${fmt(intradayFlow.entryDriftPct, 2)}% / 上限 ${fmt(intradayFlow.maximumEntryDriftPct, 2)}%`,
      },
      { label: "建議張數（依可用資金＋防守價控制單筆風險）", ok: null, value: positionSizingTraceText(item) },
      { label: "買進方式", ok: decision === "可進場", value: method || "-" },
      { label: "轉強差距", ok: null, value: entryStrengthGapText(code, health?.brainContext || "monster", health) },
      {
        label: "題材／類股輪動（依候選池 5 日超額報酬純數據計算）",
        ok: item?.sectorHot ? true : null,
        value: item?.stock?.sector && item?.sectorExcessRet5 != null
          ? `產業：${item.stock.sector}｜超額 ${fmt(item.sectorExcessRet5, 1)}%${item.sectorHot ? "｜今日熱門族群" : ""}`
          : `產業：${item?.stock?.sector || "未知"}（尚無輪動數據）`,
      },
      {
        label: "規則引擎燈號（實驗性，不影響上方判斷）",
        ok: item?.ruleEngine?.action === "CAN_BUY_NOW" ? true : item?.ruleEngine?.action === "REJECT" ? false : null,
        value: item?.ruleEngine?.action
          ? [
              { CAN_BUY_NOW: "可進場", WATCH_ONLY: "觀察", REJECT: "否決" }[item.ruleEngine.action] || item.ruleEngine.action,
              (item.ruleEngine.rules || []).filter((rule) => rule.ok === false).map((rule) => rule.label).join("、") || item.ruleEngine.vetoReason || ""
            ].filter(Boolean).join("｜")
          : "尚無規則資料",
      },
      {
        label: "完整原因（手機無法hover看title，這裡補完整版）",
        ok: null,
        value: [
          `分數 ${item.score}`,
          `狀態 ${item.status}`,
          `今日 ${pct(item.todayChangePct)}`,
          `5日 ${pct(item.change5)}`,
          item.volumeRatio == null ? "" : `量能 ${fmt(item.volumeRatio, 1)} 倍`,
          ...(item.reasons || []),
          ...(open.checks || []),
        ].filter(Boolean).join("；"),
      },
    ],
  };
}

// analysisDecisionTrace 已移除:個股完整分析功能(#33)已下架,此決策追溯函式零呼叫,
// 且 decisionSource 寫死「正式模型 + 個股完整分析條件」、引用模型機率,一併清除。

function clearLegacyDefensePopups() {
  document.querySelectorAll(".portfolio-popup").forEach((popup) => {
    if (popup.textContent.includes("跌破停損防守價")) {
      closePortfolioPopup(popup);
    }
  });
}

async function sendLineNotification(message, options = {}) {
  try {
    const body = { message, priority: "critical" };
    // category 讓伺服器認出通知類型:賣出提醒鏈帶 "portfolio_sell",伺服器端
    // 「賣出 LINE 全域靜音」旗標開啟時會擋掉這類(跨裝置、authoritative)。
    if (options.category) body.category = options.category;
    if (options.decision && typeof options.decision === "object") body.decision = options.decision;
    const response = await fetch("/api/line/notify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // 前端只有賣出提醒鏈與通知測試會走到這裡，都屬於「額度快用完也要送」
      // 的關鍵通知——月底額度進入保留池時，例行的晨報/摘要會讓位給這些。
      body: JSON.stringify(body)
    });
    const payload = await response.json().catch(() => ({}));
    if (payload.muted) {
      // 賣出 LINE 全域靜音(伺服器旗標)擋下:是使用者設定不是故障,不彈假異常視窗。
      return { ok: true, sent: false, muted: true };
    }
    if (payload.disabled) {
      // 使用者刻意停用 LINE 是設定不是故障：不能當成「通道異常」，
      // 不然每次真實賣訊都彈假異常視窗，養成使用者忽略警示(狼來了)，
      // 真正的異常出現時反而被習慣性關掉。
      return { ok: true, sent: false, disabled: true };
    }
    if (!response.ok || !payload.ok || payload.sent !== true) {
      return { ok: false, sent: false, error: payload.error || "LINE 未確認送出" };
    }
    return { ok: true, sent: true, targetMasked: payload.targetMasked || "" };
  } catch (error) {
    console.warn("LINE notification failed", error);
    return { ok: false, sent: false, error: friendlyError(error) };
  }
}

async function ensureDesktopNotificationPermission() {
  if (!("Notification" in window)) {
    return { ok: false, reason: "此瀏覽器不支援桌面通知" };
  }
  if (Notification.permission === "granted") return { ok: true };
  if (Notification.permission === "denied") {
    return { ok: false, reason: "桌面通知已被瀏覽器封鎖，請到瀏覽器網站設定允許通知" };
  }
  try {
    const permission = await Notification.requestPermission();
    return permission === "granted"
      ? { ok: true }
      : { ok: false, reason: "尚未允許桌面通知" };
  } catch (error) {
    return { ok: false, reason: friendlyError(error) };
  }
}

async function sendNativeDesktopNotification(title, message, options = {}) {
  try {
    const body = { title, message };
    if (options.category) body.category = options.category;
    if (options.decision && typeof options.decision === "object") body.decision = options.decision;
    const response = await fetch("/api/desktop/notify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || !payload.ok || payload.sent !== true) {
      return { ok: false, sent: false, error: payload.error || "Windows 桌面通知未確認送出" };
    }
    return { ok: true, sent: true, channel: payload.channel || "windows" };
  } catch (error) {
    return { ok: false, sent: false, error: friendlyError(error) };
  }
}

async function sendDesktopNotification(title, message, options = {}) {
  const nativeResult = await sendNativeDesktopNotification(title, message, options);
  if (nativeResult.sent) return nativeResult;
  const permission = await ensureDesktopNotificationPermission();
  if (!permission.ok) return { ok: false, sent: false, error: `${nativeResult.error || "Windows 桌面通知失敗"}；${permission.reason}` };
  try {
    const notification = new Notification(title, {
      body: message,
      tag: options.tag || `stockai-${Date.now()}`,
      requireInteraction: options.requireInteraction !== false,
      silent: false,
    });
    notification.onclick = () => {
      window.focus();
      notification.close();
    };
    return { ok: true, sent: true };
  } catch (error) {
    return { ok: false, sent: false, error: `${nativeResult.error || "Windows 桌面通知失敗"}；${friendlyError(error)}` };
  }
}

async function sendMandatoryExitNotifications(alert, title, message) {
  const lineMessage = `【StockAI ${title}】\n${message}`;
  const decision = {
    code: alert.code,
    name: alert.name,
    currentPrice: alert.currentPrice,
    stopLoss: alert.stopLoss,
    confirmSellPrice: alert.typeClass === "confirm" ? alert.targetPrice : null,
    decisionType: alert.decisionType,
    decisionVerified: alert.decisionVerified === true,
    decisionReasons: alert.decisionReasons || [],
    decisionAt: alert.decisionAt,
    decisionDataDate: alert.dataDate,
    quoteSource: alert.priceSource,
  };
  const [desktopResult, lineResult] = await Promise.all([
    sendDesktopNotification(title, message, {
      tag: `stockai-exit-${dateKey(new Date())}-${alert.code}-${alert.typeClass}`,
      requireInteraction: true,
      category: "portfolio_sell",
      decision,
    }),
    // 賣出 LINE 全域靜音(伺服器旗標):本機已知靜音就直接略過 LINE(桌面通知照發,
    // 電腦前照樣看得到);萬一本機旗標過期,伺服器端也會擋(帶 category 讓它認得)。
    sellLineMutedGlobal
      ? Promise.resolve({ ok: true, sent: false, muted: true })
      : sendLineNotification(lineMessage, { category: "portfolio_sell", decision }),
  ]);
  const failures = [];
  if (!desktopResult.sent) failures.push(`桌面通知未送出：${desktopResult.error || "未知原因"}`);
  // LINE 被使用者刻意停用(disabled)或全域靜音(muted)都不算通道異常，不進 failures
  if (!lineResult.sent && !lineResult.disabled && !lineResult.muted) failures.push(`LINE 未送出：${lineResult.error || "未知原因"}`);
  if (failures.length) {
    showPortfolioPopup("賣出提醒通道異常", failures.map(escapeHtml).join("<br>"), { sticky: true, wide: true });
    state.alertLog.unshift({
      time: new Date().toLocaleString("zh-TW"),
      rule: `${alert.code} 通知通道異常`,
      target: failures.join("；")
    });
    renderAlerts();
  }
  return { desktop: desktopResult, line: lineResult, ok: !failures.length };
}

function shouldNotifyPortfolioAlert(alert) {
  if (!alert) return false;
  // 通知不再自行重算或另問 Brain；只接受後端同一份 portfolio-exit-v2
  // 結果。資料、新鮮度、週期、FIFO 買進日與至少兩項證據都由後端封裝。
  if (alert.policyVersion !== "portfolio-exit-v2") return false;
  if (alert.dataReady !== true) return false;
  if (alert.canNotify !== true) return false;
  if (alert.decisionVerified !== true) return false;
  if (!Array.isArray(alert.decisionReasons) || alert.decisionReasons.filter(Boolean).length < 2) return false;
  const pnlPositive = Number(alert.pnlRate) > 0;
  if (alert.typeClass === "sell") {
    return alert.decisionType === "phase1"
      && notifyTypeEnabled(NOTIFY_TAKEPROFIT_KEY)
      && pnlPositive;
  }
  if (alert.typeClass === "confirm") {
    if (!["stop", "phase2", "phase3", "time_stop"].includes(alert.decisionType)) return false;
    return Number(alert.pnlRate) <= 0
      ? notifyTypeEnabled(NOTIFY_STOP_LOSS_KEY)
      : notifyTypeEnabled(NOTIFY_STOP_PROFIT_KEY);
  }
  return false;
}

function notifyPortfolioAlertRowOnce(alert) {
  if (!shouldNotifyPortfolioAlert(alert)) return;
  const session = taiwanMarketSession();
  if (!session.isOpen) return;
  const key = `${dateKey(new Date())}-${alert.code}-alert-${alert.typeClass}`;
  const log = readExitAlertLog();
  if (log[key]) return;
  // 先落 key 防同一輪/並行 render 重複觸發；但「桌面+LINE 全部送失敗」時
  // 要回滾——舊版 fire-and-forget 不看結果，一次暫時性失敗(server 重啟中、
  // 網路瞬斷、LINE 429)就吃掉當日該股該型唯一一次通知機會，之後條件持續
  // 成立也永遠沉默。回滾後下一輪監控 tick 條件仍成立就會重送；只要任一
  // 通道成功就保留 key，維持「每日每股每類型最多一次」的防轟炸設計。
  log[key] = new Date().toISOString();
  saveExitAlertLog(log);
  const title = alert.typeClass === "sell" ? "後端停利提醒" : "後端出場提醒";
  const conditionText = Number.isFinite(Number(alert.targetPrice))
    ? `，後端條件價 ${alertPriceText(alert.targetPrice)}`
    : "";
  const message = `${alert.code} ${alert.name}：目前 ${alertPriceText(alert.currentPrice)}${conditionText}。${alert.status}；${alert.fullNote || alert.note}`;
  showPortfolioPopup(title, message, { sticky: alert.typeClass === "confirm" });
  state.alertLog.unshift({
    time: new Date().toLocaleString("zh-TW"),
    rule: `${alert.code} ${title}`,
    target: message
  });
  renderAlerts();
  const rollbackDedupKey = () => {
    const current = readExitAlertLog();
    if (current[key]) {
      delete current[key];
      saveExitAlertLog(current);
    }
  };
  sendMandatoryExitNotifications(alert, title, message)
    .then((result) => {
      if (!result?.desktop?.sent && !result?.line?.sent) rollbackDedupKey();
    })
    .catch(() => rollbackDedupKey());
}

function dateKey(date) {
  const normalized = date instanceof Date ? date : new Date(date);
  if (!(normalized instanceof Date) || Number.isNaN(normalized.getTime())) {
    const text = String(date || "");
    return text.length >= 10 ? text.slice(0, 10) : "";
  }
  const year = normalized.getFullYear();
  const month = `${normalized.getMonth() + 1}`.padStart(2, "0");
  const day = `${normalized.getDate()}`.padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function currentStock() {
  return analysisStocks().find((stock) => stock.code === state.ticker) || stocks[0] || makeStock("2330");
}

function renderTickerOptions(includePlaceholder = false) {
  const tickerSelect = document.getElementById("tickerSelect");
  if (!tickerSelect) return;
  const options = analysisStocks();
  tickerSelect.innerHTML = [
    includePlaceholder ? `<option value="">請選擇股票</option>` : "",
    ...options.map((stock) => `<option value="${stock.code}">${stockLabel(stock)}</option>`)
  ].join("");
  if (includePlaceholder && !analysisRequested) {
    tickerSelect.value = "";
    return;
  }
  if (!options.some((stock) => stock.code === state.ticker)) {
    state.ticker = options[0]?.code || "2330";
  }
  tickerSelect.value = state.ticker;
}

function portfolioRealtimeQuote(stock) {
  const holding = readPortfolioHoldings()[stock.code] || {};
  const lots = Number(holding.quantity || 0);
  const avgPrice = Number(holding.price || 0);
  const pnl = Number(holding.pnl);
  const shares = lots > 0 ? lots * 1000 : 0;
  if (!shares || !avgPrice || !Number.isFinite(pnl)) return null;
  const marketValue = avgPrice * shares + pnl;
  const snapshotPrice = Number(holding.currentPrice);
  const price = Number.isFinite(snapshotPrice) && snapshotPrice > 0 ? snapshotPrice : marketValue / shares;
  if (!Number.isFinite(price) || price <= 0) return null;
  return {
    price,
    marketValue,
    pnl,
    shares,
    updatedAt: new Date()
  };
}

function applyPortfolioRealtimeQuote(stock, rows) {
  const quote = portfolioRealtimeQuote(stock);
  if (!quote || !rows.length) return rows;
  const output = rows.map((row) => ({ ...row }));
  const last = output[output.length - 1];
  last.close = quote.price;
  const holding = readPortfolioHoldings()[stock.code] || {};
  const snapshotOpen = Number(holding.openPrice);
  const snapshotHigh = Number(holding.highPrice);
  const snapshotLow = Number(holding.lowPrice);
  const snapshotVolume = Number(holding.totalVolume);
  if (Number.isFinite(snapshotOpen) && snapshotOpen > 0) last.open = snapshotOpen;
  last.high = Number.isFinite(snapshotHigh) && snapshotHigh > 0 ? snapshotHigh : Math.max(Number(last.high || quote.price), quote.price);
  last.low = Number.isFinite(snapshotLow) && snapshotLow > 0 ? snapshotLow : Math.min(Number(last.low || quote.price), quote.price);
  if (Number.isFinite(snapshotVolume) && snapshotVolume > 0) last.volume = snapshotVolume * 1000;
  last.realtimeSource = "sinopac_holdings";
  last.realtimeAt = quote.updatedAt;
  return output;
}

function priceSnapshotForAnalysis(stock, latest = {}, previous = {}) {
  const code = normalizeStockCode(stock?.code);
  const holding = readPortfolioHoldings()[code] || {};
  const currentPrice = Number(holding.currentPrice);
  const referencePrice = Number(holding.referencePrice);
  const changePrice = Number(holding.changePrice);
  const changeRate = Number(holding.changeRate);
  const hasRealtimePrice = Number.isFinite(currentPrice) && currentPrice > 0;
  if (hasRealtimePrice) {
    let resolvedChangePrice = Number.isFinite(changePrice) ? changePrice : null;
    if (resolvedChangePrice === null && Number.isFinite(referencePrice) && referencePrice > 0) {
      resolvedChangePrice = currentPrice - referencePrice;
    }
    let resolvedChangeRate = Number.isFinite(changeRate) ? changeRate : null;
    if (resolvedChangeRate === null && Number.isFinite(referencePrice) && referencePrice > 0) {
      resolvedChangeRate = ((currentPrice - referencePrice) / referencePrice) * 100;
    } else if (
      resolvedChangeRate === null &&
      resolvedChangePrice !== null &&
      currentPrice - resolvedChangePrice > 0
    ) {
      resolvedChangeRate = (resolvedChangePrice / (currentPrice - resolvedChangePrice)) * 100;
    }
    return {
      price: currentPrice,
      changePrice: resolvedChangePrice,
      changePct: resolvedChangeRate,
      referencePrice: Number.isFinite(referencePrice) && referencePrice > 0 ? referencePrice : null,
      source: "Shioaji / 永豐庫存",
    };
  }
  const latestClose = Number(latest?.close);
  const previousClose = Number(previous?.close);
  const fallbackChange = Number.isFinite(latestClose) && Number.isFinite(previousClose) && previousClose > 0
    ? latestClose - previousClose
    : null;
  return {
    price: Number.isFinite(latestClose) ? latestClose : null,
    changePrice: fallbackChange,
    changePct: fallbackChange === null ? null : (fallbackChange / previousClose) * 100,
    referencePrice: Number.isFinite(previousClose) && previousClose > 0 ? previousClose : null,
    source: "正式日線",
  };
}

function applyStockInfo(stockInfo) {
  Object.entries(stockInfo || {}).forEach(([code, info]) => {
    const cleanCode = normalizeStockCode(code || info?.symbol || info?.code);
    const cleanName = sanitizeStockName(info?.name, cleanCode);
    if (!cleanCode || !cleanName) return;
    if (isEtfLikeStock({ code: cleanCode, ...info, name: cleanName })) return;
    knownStockProfiles[cleanCode] = {
      ...(knownStockProfiles[cleanCode] || {}),
      name: cleanName,
      sector: info.sector || knownStockProfiles[cleanCode]?.sector || "台股",
      market: info.market || info.marketType || knownStockProfiles[cleanCode]?.market || ""
    };
  });
  stocks = stocks.map((stock) => {
    const profile = knownStockProfiles[stock.code];
    if (!profile?.name) return stock;
    return {
      ...stock,
      name: profile.name,
      sector: profile.sector || stock.sector,
      market: profile.market || stock.market || ""
    };
  });
}

async function loadStockInfo(codes = analysisStockCodes()) {
  const targets = [...new Set(codes.map(normalizeStockCode).filter(Boolean))];
  if (!targets.length) return;
  const response = await fetch(`/api/stock-info?codes=${encodeURIComponent(targets.join(","))}`);
  const payload = await response.json();
  if (!response.ok || !payload.ok) throw new Error(payload.error || "股票名稱讀取失敗");
  applyStockInfo(payload.stocks);
}

async function setPortfolioCodes(codes, options = {}) {
  const preserveView = Boolean(options.preserveView);
  codes = [...new Set((codes || []).map(normalizeStockCode).filter((code) => code && !isEtfLikeStock(code)))];
  if (!codes.length) return;
  stocks = codes.map(makeStock);
  safeSetItem(portfolioStorageKey(), JSON.stringify(codes));
  await loadStockInfo(codes).catch((error) => console.warn("股票名稱讀取失敗", error));
  if (!analysisStockCodes().includes(state.ticker)) {
    state.ticker = stocks[0].code;
  }
  if (!preserveView) {
    dataCache.clear();
    dataErrors.clear();
  }
  renderTickerOptions();
  if (!preserveView) render();
  await Promise.allSettled(stocks.map(loadRealData));
  if (preserveView) {
    for (const key of [...dataCache.keys()]) {
      if (!codes.includes(key)) dataCache.delete(key);
    }
    for (const key of [...dataErrors.keys()]) {
      if (!codes.includes(key)) dataErrors.delete(key);
    }
  }
  render();
  await loadBackendStatus();
}

async function syncSinopacHoldings() {
  const status = document.getElementById("sinopacSyncStatus");
  if (sinopacSyncInFlight) {
    if (status) status.textContent = "永豐庫存正在同步中，請稍候...";
    return;
  }
  // 自動同步持股(startSinopacAutoSync)跟買賣停損背景監控(runPortfolioAlertMonitorTick)
  // 是兩個各自獨立的計時器，間隔不對齊時常常在同一分鐘內先後各呼叫一次這裡；
  // 永豐/Shioaji 的登入機制不喜歡短時間內重複登入，第二次幾乎必定被拒絕(400)。
  // 用「上次嘗試時間」節流(不是成功時間，見上面變數註解)，太密集的重複呼叫
  // 直接跳過、沿用剛更新過的持股資料，不用再重打一次永豐 API。
  if (Date.now() - sinopacLastSyncAttemptAt < SINOPAC_SYNC_MIN_GAP_MS) {
    if (status) status.textContent = "剛同步過，請稍後再試（避免永豐 API 短時間內重複登入）";
    return;
  }
  sinopacLastSyncAttemptAt = Date.now();
  sinopacSyncInFlight = true;
  // status 缺 null 守衛(其他行都有 if(status))會在設 inFlight=true 後、進 try 前拋錯,
  // 害 finally 不執行、sinopacSyncInFlight 永久卡 true→整個背景監控靜默死掉。補守衛。
  if (status) status.textContent = "正在讀取永豐 API 庫存...";
  try {
    // 加逾時：這個 fetch 沒有時限的話，後端 Shioaji 登入卡住時
    // sinopacSyncInFlight 會一直是 true，之後每一輪監控 tick 都在
    // 進門處直接 return，整個背景監控等同死掉。
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 45000);
    let response;
    try {
      response = await fetch("/api/sinopac/holdings", { signal: controller.signal });
    } finally {
      clearTimeout(timeoutId);
    }
    const payload = await response.json();
    const stockCodes = (payload.codes || []).filter((code) => !isEtfLikeStock(code));
    if (!response.ok || !payload.ok) throw new Error(payload.error || "永豐庫存讀取失敗");
    if (!stockCodes.length) throw new Error("永豐庫存目前沒有可同步的一般股票");
    const mappedHoldings = savePortfolioHoldings(payload.holdings || []);
    savePortfolioSummary(payload, mappedHoldings);
    status.textContent = `已讀取永豐 API 庫存：${stockCodes.join(", ")}（帳號 ${payload.accountMasked}），正在更新分析資料...`;
    render();
    await setPortfolioCodes(stockCodes, { preserveView: true });
    await portfolioSummarySyncPromise.catch(() => null);
    renderScanner();
    syncSinopacOrderSymbolMode();
    sinopacLastSyncSuccessAt = Date.now();
    // 交易複盤自動化(2026-07-09):每天第一次同步庫存時,後端會搭同一次永豐登入順便把已實現損益
    // 存進 sinopac_realized_pnl(payload.realizedSaved)。複盤卡已搬到獨立頁 trades.html,主頁不再
    // 渲染它,所以這裡不需要刷新;trades.html 開啟時會自己讀最新資料。
    status.textContent = `已同步永豐 API 庫存：${stockCodes.join(", ")}（帳號 ${payload.accountMasked}），已重算停損、停利與移動停利`;
  } catch (error) {
    // AbortError(45 秒逾時)是原生 DOMException，訊息通常是英文
    // "The user aborted a request."之類，friendlyError 認不得也不會
    // 特別處理，會讓使用者以為「永豐庫存讀取異常」是永久性故障。
    // 其實這只是後端這一輪比較忙(例如重訓中)還沒回應，下一輪
    // (自動同步計時器/監控 tick)會自動重打，不需要重新整理頁面。
    status.textContent = error?.name === "AbortError"
      ? "永豐庫存讀取逾時（伺服器忙碌中，例如正在重新訓練），下一輪會自動重試，不需重新整理頁面。"
      : friendlyError(error);
  } finally {
    sinopacSyncInFlight = false;
  }
}

let sinopacOrderPending = null;
let sinopacOrderConfirmTimer = null;
let sinopacOrderSourceContext = null;
let sinopacOrderSubmitting = false;
// 前端 fetch 逾時是 20 秒，但後端子行程真正的 timeout 是 90 秒——如果永豐
// 回應落在這 20-89 秒之間，逾時當下無法確定委託到底有沒有送出去，後端那筆
// 請求可能還在背景跑、最後仍然成功送出。這種情況下不能整個解鎖讓使用者
// 立刻重按，否則背景那筆一旦成功，加上使用者重送的第二筆，就會變成兩筆
// 完全獨立、各自有效的真實委託。逾時後改成鎖定，強制使用者先去永豐官網
// 確認狀態、按下「我已確認」才解鎖，不再只靠一段提醒文字要求使用者自律。
//
// 這個鎖之前只存在記憶體變數(let)裡：使用者在鎖定狀態下重新整理頁面/
// 關掉分頁再重開，畫面會靜默恢復成「可以送出」，等於前面這整套防重複
// 送單設計完全防不住「使用者以為卡住了乾脆重整頁面」這個最直覺的操作。
// 改成同時寫進 localStorage，頁面載入時 restoreSinopacOrderLockState()
// 會把鎖復原，強制使用者還是要先按「我已確認」才能繼續操作下單面板。
let sinopacOrderResultUnknown = false;

function sinopacOrderLockStorageKey() {
  return "stock-vibe-yongfeng-order-unknown-lock-v1";
}

function persistSinopacOrderLockState(locked) {
  if (locked) {
    safeSetItem(sinopacOrderLockStorageKey(), JSON.stringify({ locked: true, lockedAt: Date.now() }));
  } else {
    safeRemoveItem(sinopacOrderLockStorageKey());
  }
}

function restoreSinopacOrderLockState() {
  const stored = safeGetItem(sinopacOrderLockStorageKey());
  if (!stored) return;
  try {
    const parsed = JSON.parse(stored);
    if (parsed && parsed.locked) lockSinopacOrderUnknownResult({ persist: false });
  } catch {
    // 壞掉的紀錄視為沒有鎖，不阻擋使用者操作。
  }
}

const SINOPAC_ORDER_LOT_RULES = {
  COMMON: { text: "整張", unit: "張", max: 100, sharesPerUnit: 1000 },
  INTRADAY_ODD: { text: "零股", unit: "股", max: 999, sharesPerUnit: 1 }
};

function sinopacOrderLotRule(orderLot) {
  return SINOPAC_ORDER_LOT_RULES[orderLot] || SINOPAC_ORDER_LOT_RULES.COMMON;
}

function sinopacOrderHoldingRows() {
  return Object.values(readPortfolioHoldings())
    .map((holding) => {
      const code = normalizeStockCode(holding?.code);
      const shares = holdingShares(holding);
      return { ...holding, code, shares };
    })
    .filter((holding) => holding.code && holding.shares > 0 && !isEtfLikeStock(holding.code))
    .sort((a, b) => a.code.localeCompare(b.code));
}

function populateSinopacOrderHoldings(selectedCode = "") {
  const select = document.getElementById("sinopacOrderHolding");
  if (!select) return [];
  const rows = sinopacOrderHoldingRows();
  const currentCode = normalizeStockCode(selectedCode || select.value);
  select.innerHTML = rows.length
    ? rows.map((holding) => {
      const name = stockDisplayName(holding.code);
      const label = `${holding.code}${name ? ` ${name}` : ""}｜${fmt(holding.shares, 0)} 股`;
      return `<option value="${escapeHtml(holding.code)}">${escapeHtml(label)}</option>`;
    }).join("")
    : `<option value="">請先更新永豐庫存</option>`;
  if (currentCode && rows.some((holding) => holding.code === currentCode)) {
    select.value = currentCode;
  }
  return rows;
}

function selectedSinopacOrderHolding() {
  const select = document.getElementById("sinopacOrderHolding");
  const code = normalizeStockCode(select?.value || "");
  if (!code) return null;
  return readPortfolioHoldings()[code] || null;
}

function holdingOrderCurrentPrice(holding) {
  const candidates = [
    holding?.currentPrice,
    holding?.current_price,
    holding?.lastPrice,
    holding?.price,
    holding?.avgPrice
  ];
  for (const value of candidates) {
    const number = Number(value);
    if (Number.isFinite(number) && number > 0) return number;
  }
  return null;
}

function syncSinopacSellHoldingPrice(force = false) {
  const action = document.getElementById("sinopacOrderAction")?.value || "BUY";
  if (action !== "SELL") return;
  const priceType = document.getElementById("sinopacOrderPriceType");
  const price = document.getElementById("sinopacOrderPrice");
  if (!priceType || !price) return;
  const currentPrice = holdingOrderCurrentPrice(selectedSinopacOrderHolding());
  if (!Number.isFinite(currentPrice) || currentPrice <= 0) return;
  if (force || priceType.value !== "LMT") {
    priceType.value = "LMT";
    price.disabled = false;
  }
  if (force || !Number(price.value)) {
    price.value = fmt(currentPrice, 2);
  }
}

function syncSinopacOrderSymbolMode() {
  const action = document.getElementById("sinopacOrderAction")?.value || "BUY";
  const input = document.getElementById("sinopacOrderSymbol");
  const select = document.getElementById("sinopacOrderHolding");
  const label = document.getElementById("sinopacOrderSymbolLabel");
  const orderLot = document.getElementById("sinopacOrderLot");
  const isSell = action === "SELL";
  if (label) label.textContent = isSell ? "賣出庫存" : "股票代號";
  if (input) {
    input.classList.toggle("hidden", isSell);
    input.disabled = isSell;
  }
  if (select) {
    populateSinopacOrderHoldings(input?.value || select.value);
    select.classList.toggle("hidden", !isSell);
    select.disabled = !isSell;
  }
  const oddLotOption = orderLot?.querySelector('option[value="INTRADAY_ODD"]');
  if (oddLotOption) oddLotOption.disabled = isSell;
  if (isSell && orderLot && orderLot.value !== "COMMON") {
    orderLot.value = "COMMON";
    orderLot.dispatchEvent(new Event("change"));
  }
  if (isSell) syncSinopacSellHoldingPrice(true);
}

function readSinopacOrderForm() {
  const action = document.getElementById("sinopacOrderAction")?.value || "BUY";
  const typedSymbol = document.getElementById("sinopacOrderSymbol")?.value || "";
  const holdingSymbol = document.getElementById("sinopacOrderHolding")?.value || "";
  const symbol = normalizeStockCode(action === "SELL" ? holdingSymbol : typedSymbol);
  const sourceContext = (
    action === "BUY"
    && sinopacOrderSourceContext?.symbol === symbol
  ) ? sinopacOrderSourceContext : null;
  return {
    symbol,
    action,
    priceType: document.getElementById("sinopacOrderPriceType")?.value || "LMT",
    price: Number(document.getElementById("sinopacOrderPrice")?.value || 0),
    orderLot: action === "SELL" ? "COMMON" : (document.getElementById("sinopacOrderLot")?.value || "COMMON"),
    quantity: Number(document.getElementById("sinopacOrderQuantity")?.value || 0),
    orderType: document.getElementById("sinopacOrderType")?.value || "ROD",
    orderContext: sourceContext?.orderContext || "",
    radarScanDate: sourceContext?.radarScanDate || ""
  };
}

function sinopacOrderKey(form) {
  return JSON.stringify({
    symbol: form.symbol,
    action: form.action,
    priceType: form.priceType,
    price: form.priceType === "MKT" ? 0 : Number(form.price || 0),
    orderLot: form.orderLot,
    quantity: Number(form.quantity || 0),
    orderType: form.orderType,
    orderContext: form.orderContext || "",
    radarScanDate: form.radarScanDate || ""
  });
}

function sinopacOrderShares(form) {
  const lotRule = sinopacOrderLotRule(form.orderLot);
  return Number(form.quantity || 0) * lotRule.sharesPerUnit;
}

function sinopacOrderEstimatedAmount(form) {
  if (form.priceType !== "LMT") return null;
  const shares = sinopacOrderShares(form);
  const price = Number(form.price || 0);
  if (!Number.isFinite(price) || price <= 0 || !shares) return null;
  return price * shares;
}

function sinopacOrderAmountText(form) {
  const label = form.action === "SELL" ? "賣出金額" : "購買金額";
  const amount = sinopacOrderEstimatedAmount(form);
  if (!Number.isFinite(amount)) return `${label}待成交確認`;
  return `${label}約 ${fmt(amount, 0)} 元`;
}

function validateSinopacOrderForm(form) {
  if (!/^\d{4}$/.test(form.symbol)) return "股票代號必須是 4 碼";
  if (!["BUY", "SELL"].includes(form.action)) return "買賣類型錯誤";
  if (!["LMT", "MKT"].includes(form.priceType)) return "價格類型錯誤";
  if (!["ROD", "IOC", "FOK"].includes(form.orderType)) return "委託類型錯誤";
  if (!Object.prototype.hasOwnProperty.call(SINOPAC_ORDER_LOT_RULES, form.orderLot)) return "下單單位錯誤";
  const lotRule = sinopacOrderLotRule(form.orderLot);
  if (!Number.isFinite(form.quantity) || form.quantity <= 0 || form.quantity > lotRule.max) return `${lotRule.unit}數必須是 1 到 ${lotRule.max}`;
  if (!Number.isInteger(form.quantity)) return `${lotRule.unit}數必須是整數`;
  if (form.orderLot === "INTRADAY_ODD" && form.priceType !== "LMT") return "零股下單目前只允許限價";
  if (form.action === "SELL") {
    if (form.orderLot !== "COMMON") return "賣出依偏好只允許整張，禁止零股委託";
    const holding = readPortfolioHoldings()[form.symbol];
    const availableShares = holdingShares(holding);
    if (!holding || availableShares <= 0) return "賣出只能選永豐庫存現有股票";
    const sellShares = form.quantity * lotRule.sharesPerUnit;
    if (sellShares > availableShares) return `賣出數量不可超過永豐庫存 ${fmt(availableShares, 0)} 股`;
    if (availableShares < 1000) return "目前庫存不足一張，依整張賣出偏好不建立委託";
  }
  if (form.priceType === "LMT" && (!Number.isFinite(form.price) || form.price <= 0)) return "限價單必須輸入價格";
  return "";
}

function setSinopacOrderBusy(active, label = "") {
  const confirmButton = document.getElementById("confirmSinopacOrderStep");
  const placeButton = document.getElementById("placeSinopacOrder");
  const status = document.getElementById("sinopacOrderStatus");
  if (confirmButton) confirmButton.disabled = Boolean(active) || sinopacOrderResultUnknown;
  if (placeButton) placeButton.disabled = Boolean(active) || !sinopacOrderPending || sinopacOrderResultUnknown;
  if (status && label) status.textContent = label;
}

function clearSinopacOrderConfirmTimer() {
  if (sinopacOrderConfirmTimer) {
    clearTimeout(sinopacOrderConfirmTimer);
    sinopacOrderConfirmTimer = null;
  }
}

function resetSinopacOrderConfirm(label = "手動送出") {
  clearSinopacOrderConfirmTimer();
  sinopacOrderPending = null;
  sinopacOrderSubmitting = false;
  const confirmButton = document.getElementById("confirmSinopacOrderStep");
  const placeButton = document.getElementById("placeSinopacOrder");
  const status = document.getElementById("sinopacOrderStatus");
  if (confirmButton) confirmButton.disabled = sinopacOrderResultUnknown;
  if (placeButton) placeButton.disabled = true;
  if (status) status.textContent = sinopacOrderResultUnknown ? status.textContent : label;
}

function lockSinopacOrderUnknownResult(options = {}) {
  sinopacOrderResultUnknown = true;
  clearSinopacOrderConfirmTimer();
  sinopacOrderPending = null;
  sinopacOrderSubmitting = false;
  if (options.persist !== false) persistSinopacOrderLockState(true);
  const confirmButton = document.getElementById("confirmSinopacOrderStep");
  const placeButton = document.getElementById("placeSinopacOrder");
  const ackButton = document.getElementById("acknowledgeSinopacOrderUnknown");
  const status = document.getElementById("sinopacOrderStatus");
  if (confirmButton) confirmButton.disabled = true;
  if (placeButton) placeButton.disabled = true;
  if (ackButton) ackButton.classList.remove("hidden");
  if (status) status.textContent = "委託狀態未知，已鎖定";
}

function acknowledgeSinopacOrderUnknownResult() {
  sinopacOrderResultUnknown = false;
  persistSinopacOrderLockState(false);
  const ackButton = document.getElementById("acknowledgeSinopacOrderUnknown");
  if (ackButton) ackButton.classList.add("hidden");
  resetSinopacOrderConfirm("手動送出");
}

function sinopacOrderSummary(form) {
  const actionText = form.action === "SELL" ? "賣出" : "買進";
  const priceText = form.priceType === "MKT" ? "市價" : `${fmt(form.price, 2)} 元`;
  const lotRule = sinopacOrderLotRule(form.orderLot);
  const sharesText = lotRule.sharesPerUnit > 1 ? `，約 ${sinopacOrderShares(form)} 股` : "";
  return `${form.symbol} ${actionText} ${lotRule.text} ${form.quantity} ${lotRule.unit}${sharesText}，${priceText}，${sinopacOrderAmountText(form)}，${form.orderType}`;
}

function sinopacOrderResultHtml(payload, title) {
  const order = payload?.order || payload || {};
  const trade = payload?.trade || {};
  const status = trade?.status || {};
  const orderLot = order.orderLotKey || (order.orderLot === "IntradayOdd" ? "INTRADAY_ODD" : "COMMON");
  const lotRule = sinopacOrderLotRule(orderLot);
  const orderUnit = order.unit || lotRule.unit;
  const orderLotText = order.orderLotText || lotRule.text;
  const estimatedAmount = Number(order.estimatedAmount);
  const amountLabel = order.action === "SELL" || order.actionText === "賣出" ? "預估賣出金額" : "預估購買金額";
  const amountText = Number.isFinite(estimatedAmount) && estimatedAmount > 0 ? `${fmt(estimatedAmount, 0)} 元` : "待成交確認";
  return `
    <strong>${escapeHtml(title)}</strong>
    <div class="backend-status system-health-grid">
      <div class="plan-item"><span>股票</span><strong>${escapeHtml(order.code || "-")}</strong></div>
      <div class="plan-item"><span>買賣</span><strong>${escapeHtml(order.actionText || order.action || "-")}</strong></div>
      <div class="plan-item"><span>單位</span><strong>${escapeHtml(orderLotText)}</strong></div>
      <div class="plan-item"><span>數量</span><strong>${escapeHtml(order.quantity || "-")} ${escapeHtml(orderUnit)}</strong></div>
      <div class="plan-item"><span>股數</span><strong>${escapeHtml(order.shares || "-")} 股</strong></div>
      <div class="plan-item"><span>${escapeHtml(amountLabel)}</span><strong>${escapeHtml(amountText)}</strong></div>
      <div class="plan-item"><span>委託</span><strong>${escapeHtml(`${order.priceType || ""} / ${order.orderType || ""}`)}</strong></div>
      <div class="plan-item"><span>模式</span><strong>${order.simulation || payload?.simulation ? "模擬" : "正式"}</strong></div>
      <div class="plan-item"><span>回報</span><strong>${escapeHtml(status.status || status.status_code || payload?.accountMasked || "已回傳")}</strong></div>
    </div>
  `;
}

function confirmSinopacOrderStep() {
  const form = readSinopacOrderForm();
  const result = document.getElementById("sinopacOrderResult");
  const error = validateSinopacOrderForm(form);
  if (error) {
    resetSinopacOrderConfirm("資料不完整");
    if (result) {
      result.className = "explanation-card down";
      result.innerHTML = `<strong>無法確認內容</strong>${escapeHtml(error)}`;
    }
    return;
  }
  clearSinopacOrderConfirmTimer();
  sinopacOrderPending = {
    key: sinopacOrderKey(form),
    expiresAt: Date.now() + 30000
  };
  const pendingKey = sinopacOrderPending.key;
  const placeButton = document.getElementById("placeSinopacOrder");
  const status = document.getElementById("sinopacOrderStatus");
  if (placeButton) placeButton.disabled = false;
  if (status) status.textContent = "已確認內容";
  if (result) {
    result.className = "explanation-card";
    result.innerHTML = `<strong>第一步完成</strong>${escapeHtml(sinopacOrderSummary(form))}<br>30 秒內可按第二步送出。若修改欄位，需重新確認。`;
  }
  sinopacOrderConfirmTimer = setTimeout(() => {
    if (!sinopacOrderPending || sinopacOrderPending.key !== pendingKey || sinopacOrderSubmitting) return;
    resetSinopacOrderConfirm("確認逾時");
    if (result) {
      result.className = "explanation-card down";
      result.innerHTML = "<strong>確認逾時</strong>30 秒已過，請重新按第一步確認內容。";
    }
  }, 30500);
}

async function placeSinopacOrder() {
  const form = readSinopacOrderForm();
  const result = document.getElementById("sinopacOrderResult");
  if (sinopacOrderSubmitting) {
    if (result) {
      result.className = "explanation-card";
      result.innerHTML = `<strong>送出中</strong>${escapeHtml(sinopacOrderSummary(form))}<br>請等待永豐回傳結果，不要重複送出。`;
    }
    return;
  }
  if (!sinopacOrderPending) {
    if (result) {
      result.className = "explanation-card down";
      result.innerHTML = "<strong>尚未確認內容</strong>請先按第一步確認內容。";
    }
    return;
  }
  if (Date.now() > sinopacOrderPending.expiresAt) {
    resetSinopacOrderConfirm("確認逾時");
    if (result) {
      result.className = "explanation-card down";
      result.innerHTML = "<strong>確認逾時</strong>請重新按第一步確認內容。";
    }
    return;
  }
  if (sinopacOrderKey(form) !== sinopacOrderPending.key) {
    resetSinopacOrderConfirm("內容已變更");
    if (result) {
      result.className = "explanation-card down";
      result.innerHTML = "<strong>內容已變更</strong>請重新按第一步確認內容。";
    }
    return;
  }
  // validateSinopacOrderForm 原本只在第一步「確認內容」跑過一次，第二步
  // 送出不會重跑。sinopacOrderKey 不含庫存股數，背景庫存同步(每60秒左右)
  // 如果剛好在30秒確認視窗內更新了永豐庫存下拉選單，key比對仍會通過，導致
  // 用舊的股數送出可能超過目前庫存的賣單。這裡在真正送出前，用「當下」的
  // 庫存資料重新驗證一次，賣出超過庫存就擋下——跟後端 place_order 的即時
  // 庫存核對是兩層防護，這裡先擋掉大部分情況，不用每次都等後端 API 往返。
  const revalidationError = validateSinopacOrderForm(form);
  if (revalidationError) {
    resetSinopacOrderConfirm("庫存已變動");
    if (result) {
      result.className = "explanation-card down";
      result.innerHTML = `<strong>送出前重新檢查未通過</strong>${escapeHtml(revalidationError)}，可能是庫存在確認期間有變動，請重新按第一步確認內容。`;
    }
    return;
  }
  if (result) {
    result.className = "explanation-card";
    result.innerHTML = `<strong>送出中</strong>${escapeHtml(sinopacOrderSummary(form))}`;
  }
  clearSinopacOrderConfirmTimer();
  sinopacOrderSubmitting = true;
  setSinopacOrderBusy(true, "送出中");
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch("/api/sinopac/order/place", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      body: JSON.stringify({
        ...form,
        manualConfirm: true,
        confirmText: "我確認下單",
        allowLiveOrder: true
      })
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "永豐下單失敗");
    resetSinopacOrderConfirm("已送出");
    if (result) {
      result.className = "explanation-card up";
      result.innerHTML = sinopacOrderResultHtml(payload, "永豐已回傳委託結果");
    }
  } catch (error) {
    if (error?.name === "AbortError") {
      // 前端 20 秒逾時，但後端子行程還在跑到 90 秒——這筆委託有沒有真的送出
      // 現在還不知道，不能解鎖讓使用者立刻重按，否則背景那筆一旦成功、加上
      // 使用者重送的第二筆，會變成兩筆完全獨立的真實委託。改成鎖定，強制
      // 使用者先去永豐官網查證、按「我已確認」才解鎖。
      lockSinopacOrderUnknownResult();
      if (result) {
        result.className = "explanation-card down";
        result.innerHTML = "<strong>永豐回應逾時，委託狀態未知</strong>系統已鎖定下單按鈕避免重複送出。請先到永豐官方委託/成交查詢確認這筆是否已送出，確認完再按下方「我已確認」解除鎖定。";
      }
    } else {
      resetSinopacOrderConfirm("送出失敗");
      if (result) {
        result.className = "explanation-card down";
        result.innerHTML = `<strong>永豐下單失敗</strong>${escapeHtml(friendlyError(error))}`;
      }
    }
  } finally {
    clearTimeout(timeoutId);
    sinopacOrderSubmitting = false;
  }
}

function initSinopacOrderControls() {
  const confirmButton = document.getElementById("confirmSinopacOrderStep");
  const placeButton = document.getElementById("placeSinopacOrder");
  const ackButton = document.getElementById("acknowledgeSinopacOrderUnknown");
  if (ackButton) ackButton.addEventListener("click", acknowledgeSinopacOrderUnknownResult);
  restoreSinopacOrderLockState();
  const action = document.getElementById("sinopacOrderAction");
  const priceType = document.getElementById("sinopacOrderPriceType");
  const price = document.getElementById("sinopacOrderPrice");
  const orderLot = document.getElementById("sinopacOrderLot");
  const quantity = document.getElementById("sinopacOrderQuantity");
  const quantityLabel = document.getElementById("sinopacOrderQuantityLabel");
  const inputs = [
    "sinopacOrderSymbol",
    "sinopacOrderAction",
    "sinopacOrderHolding",
    "sinopacOrderPriceType",
    "sinopacOrderPrice",
    "sinopacOrderLot",
    "sinopacOrderQuantity",
    "sinopacOrderType"
  ].map((id) => document.getElementById(id)).filter(Boolean);
  if (confirmButton) confirmButton.addEventListener("click", confirmSinopacOrderStep);
  if (placeButton) placeButton.addEventListener("click", placeSinopacOrder);
  inputs.forEach((input) => input.addEventListener("input", () => resetSinopacOrderConfirm("內容已變更")));
  inputs.forEach((input) => input.addEventListener("change", () => resetSinopacOrderConfirm("內容已變更")));
  document.getElementById("sinopacOrderSymbol")?.addEventListener("input", () => {
    sinopacOrderSourceContext = null;
  });
  action?.addEventListener("change", () => {
    if (action.value !== "BUY") sinopacOrderSourceContext = null;
  });
  if (action) action.addEventListener("change", syncSinopacOrderSymbolMode);
  if (priceType && price) {
    const syncPriceMode = () => {
      const market = priceType.value === "MKT";
      price.disabled = market;
      if (market) price.value = "0";
    };
    priceType.addEventListener("change", syncPriceMode);
    syncPriceMode();
  }
  if (orderLot && quantity) {
    const syncLotMode = () => {
      if (action?.value === "SELL" && orderLot.value !== "COMMON") orderLot.value = "COMMON";
      const lotRule = sinopacOrderLotRule(orderLot.value);
      if (quantityLabel) quantityLabel.textContent = `${lotRule.unit}數`;
      quantity.max = String(lotRule.max);
      quantity.placeholder = `1-${lotRule.max}`;
      if (!Number.isFinite(Number(quantity.value)) || Number(quantity.value) <= 0) quantity.value = "1";
      if (Number(quantity.value) > lotRule.max) quantity.value = String(lotRule.max);
      if (orderLot.value === "INTRADAY_ODD" && priceType) {
        priceType.value = "LMT";
        priceType.dispatchEvent(new Event("change"));
      }
    };
    orderLot.addEventListener("change", syncLotMode);
    const holdingSelect = document.getElementById("sinopacOrderHolding");
    holdingSelect?.addEventListener("change", syncLotMode);
    holdingSelect?.addEventListener("change", () => syncSinopacSellHoldingPrice(true));
    syncLotMode();
  }
  syncSinopacOrderSymbolMode();
  resetSinopacOrderConfirm();
}

function fillSinopacOrderFromPick(button, code) {
  if (!button?.dataset?.orderFill) return false;
  const action = document.getElementById("sinopacOrderAction");
  const symbol = document.getElementById("sinopacOrderSymbol");
  const priceType = document.getElementById("sinopacOrderPriceType");
  const price = document.getElementById("sinopacOrderPrice");
  const orderLot = document.getElementById("sinopacOrderLot");
  const quantity = document.getElementById("sinopacOrderQuantity");
  const holdingSelect = document.getElementById("sinopacOrderHolding");
  const result = document.getElementById("sinopacOrderResult");
  const panel = document.getElementById("sinopacOrderPanel");
  if (!action || !symbol || !priceType || !price) return false;

  action.value = button.dataset.orderAction || "BUY";
  syncSinopacOrderSymbolMode();
  sinopacOrderSourceContext = (
    action.value === "BUY" && button.dataset.orderContext
  ) ? {
    symbol: code,
    orderContext: button.dataset.orderContext,
    radarScanDate: button.dataset.radarScanDate || ""
  } : null;
  if (action.value === "SELL" && holdingSelect) {
    populateSinopacOrderHoldings(code);
    if ([...holdingSelect.options].some((option) => option.value === code)) {
      holdingSelect.value = code;
    }
  }
  symbol.value = code;
  priceType.value = "LMT";
  price.disabled = false;
  const orderPrice = Number(button.dataset.currentPrice || button.dataset.orderPrice || 0);
  if (Number.isFinite(orderPrice) && orderPrice > 0) {
    price.value = fmt(orderPrice, 2);
  }
  if (orderLot) {
    const holding = readPortfolioHoldings()[code];
    const shares = holdingShares(holding);
    orderLot.value = action.value === "SELL"
      ? "COMMON"
      : (button.dataset.orderLot || orderLot.value || "COMMON");
    orderLot.dispatchEvent(new Event("change"));
  }
  if (quantity && (!Number(quantity.value) || Number(quantity.value) <= 0)) quantity.value = "1";
  resetSinopacOrderConfirm("已帶入下單區");
  if (result) {
    const actionText = action.value === "SELL" ? "賣出" : "買進";
    const priceText = Number.isFinite(orderPrice) && orderPrice > 0 ? `${fmt(orderPrice, 2)} 元` : "請自行輸入限價";
    result.className = "explanation-card";
    result.innerHTML = `<strong>已帶入下單區</strong>${escapeHtml(`${code} ${actionText}，限價 ${priceText}。請確認張數/股數後按第一步。`)}`;
  }
  if (panel) panel.scrollIntoView({ behavior: "smooth", block: "start" });
  return true;
}

function readSinopacAutoSyncSettings() {
  try {
    const stored = JSON.parse(localStorage.getItem(sinopacAutoSyncStorageKey()) || "{}");
    return {
      enabled: Boolean(stored.enabled),
      seconds: normalizeAutoSyncSeconds(stored.seconds || 60)
    };
  } catch {
    return { enabled: false, seconds: 60 };
  }
}

function normalizeAutoSyncSeconds(value) {
  return Number(value || 60) <= 45 ? 30 : 60;
}

function saveSinopacAutoSyncSettings(settings) {
  safeSetItem(sinopacAutoSyncStorageKey(), JSON.stringify({
    enabled: Boolean(settings.enabled),
    seconds: normalizeAutoSyncSeconds(settings.seconds || 60)
  }));
}

function stopSinopacAutoSync() {
  if (sinopacAutoSyncTimer) {
    clearInterval(sinopacAutoSyncTimer);
    sinopacAutoSyncTimer = null;
  }
}

function taiwanMarketSession(now = new Date()) {
  const day = now.getDay();
  const minutes = now.getHours() * 60 + now.getMinutes();
  const open = 9 * 60;
  const close = 13 * 60 + 30;
  const isWeekday = day >= 1 && day <= 5;
  const isOpen = isWeekday && minutes >= open && minutes < close;
  const label = !isWeekday
    ? "今日休市，自動更新暫停。"
    : minutes < open
      ? "尚未開盤，09:00 後才會自動更新。"
      : minutes >= close
        ? "已收盤，自動更新已關閉。"
        : "開盤中，自動同步目前股價與永豐庫存。";
  return { isOpen, label };
}

function taiwanSellConfirmStage(now = new Date()) {
  const day = now.getDay();
  const minutes = now.getHours() * 60 + now.getMinutes();
  const isWeekday = day >= 1 && day <= 5;
  const confirmStart = 9 * 60 + 30;
  const close = 13 * 60 + 30;
  const allowed = isWeekday && minutes >= confirmStart && minutes < close;
  if (!isWeekday) return { allowed, label: "休市日只警戒" };
  if (minutes < confirmStart) return { allowed, label: "等待 09:30 確認" };
  if (minutes >= close) return { allowed, label: "已收盤，只保留警戒" };
  return { allowed, label: "盤中確認中" };
}

function taiwanBuyFlowStage(now = new Date()) {
  const day = now.getDay();
  const minutes = now.getHours() * 60 + now.getMinutes();
  if (day < 1 || day > 5) {
    return { phase: "closed", label: "今日休市，不進場", action: "休市日只整理名單", allowBuy: false, allowScan: true, tone: "down" };
  }
  if (minutes < 9 * 60 + 5) {
    return { phase: "premarket", label: "等待 09:05 初篩", action: "開盤前先不動作", allowBuy: false, allowScan: true, tone: "warn" };
  }
  if (minutes < 9 * 60 + 15) {
    return { phase: "filter", label: "09:05 初篩中，先不進場", action: "初篩候選名單，先排除跳空與風險", allowBuy: false, allowScan: true, tone: "warn" };
  }
  if (minutes < 9 * 60 + 30) {
    return { phase: "volume", label: "09:15 看量能，等 09:30 確認", action: "確認量能延續，還不進場", allowBuy: false, allowScan: true, tone: "warn" };
  }
  if (minutes < 10 * 60) {
    return { phase: "initial", label: "09:30-10:00 初次買進確認", action: "可突破、回測或 V 轉確認", allowBuy: true, allowScan: true, tone: "up" };
  }
  if (minutes < 13 * 60 + 15) {
    return { phase: "dip", label: "10:00-13:15 只看低接/V轉", action: "只看低接與 V 轉，不追突破", allowBuy: true, allowScan: true, tone: "warn" };
  }
  return { phase: "closed", label: "13:15 後不再進場", action: "今日進場時段結束", allowBuy: false, allowScan: true, tone: "down" };
}

function intradayStateMachine(open, decision, health, canEnter = null) {
  const decisionText = String(decision || "");
  const healthOk = health?.ok !== false;
  // canEnter 是呼叫端(entryDecisionWithBrain 的 brainEntry.canEnter，或已經算好
  // 的 open.canBuy)算好的真布林值，優先採用；只有沒傳入時才退回舊的字串關鍵字
  // 比對(給沒有布林值可傳的呼叫路徑當保底，避免破壞相容性)。
  const canBuy = healthOk && (canEnter === true || canEnter === false ? canEnter : decisionText.includes("可進場"));
  // 分數高不代表現在能買：分數是妖股整體強度排名，能不能買要看盤中即時
  // 型態有沒有真的被確認（還可能被開高走低、跳空、停損等安全閥擋下）。
  // 不能買時把真正的否決原因(open.action)放最前面，不要只顯示通用文案。
  const details = canBuy
    ? ["突破/回測/V轉", open?.setupType ? `型態 ${open.setupType}` : ""].filter(Boolean).join(" / ")
    : [
        healthOk ? "" : (health?.reason || "資料未通過"),
        open?.action && open.action !== "先不買" ? open.action : "",
        open?.setupType ? `型態 ${open.setupType}` : "",
        open?.hasIntradayQuote === false ? "等即時價" : "",
      ].filter(Boolean).join(" / ");
  return {
    status: canBuy ? "可買" : "不可買",
    tone: canBuy ? "up" : "down",
    details,
  };
}

function writeSinopacAutoSyncStatus(text) {
  const status = document.getElementById("sinopacSyncStatus");
  if (status) status.textContent = text;
}

function startSinopacAutoSync() {
  stopSinopacAutoSync();
  const settings = readSinopacAutoSyncSettings();
  if (!settings.enabled) return;
  const session = taiwanMarketSession();
  if (!session.isOpen) {
    writeSinopacAutoSyncStatus(`${session.label} 妖股流程：15:00-16:00 掃描名單，09:05 初篩，09:15 看量能，09:30-10:00 初次確認，10:00-13:15 只看低接/V轉。`);
    return;
  }
  syncSinopacHoldings();
  sinopacAutoSyncTimer = setInterval(() => {
    if (!taiwanMarketSession().isOpen) {
      stopSinopacAutoSync();
      writeSinopacAutoSyncStatus(`${taiwanMarketSession().label} 流程：收盤後停止同步。`);
      return;
    }
    syncSinopacHoldings();
  }, settings.seconds * 1000);
  writeSinopacAutoSyncStatus(`${session.label}每 ${settings.seconds} 秒更新一次，並重算停損、停利、移動停利；妖股買進含 09:30 初次確認與 10:00 後低接/V轉。`);
}

function startSinopacMarketClock() {
  if (sinopacAutoSyncClockTimer) clearInterval(sinopacAutoSyncClockTimer);
  sinopacAutoSyncClockTimer = setInterval(() => {
    const settings = readSinopacAutoSyncSettings();
    const session = taiwanMarketSession();
    if (settings.enabled && session.isOpen && !sinopacAutoSyncTimer) {
      startSinopacAutoSync();
    } else if (settings.enabled && !session.isOpen && sinopacAutoSyncTimer) {
      stopSinopacAutoSync();
      writeSinopacAutoSyncStatus(session.label);
    } else if (settings.enabled && !session.isOpen) {
      writeSinopacAutoSyncStatus(session.label);
    }
    const alertMonitor = document.getElementById("portfolioAlertMonitor");
    if (alertMonitor?.checked && session.isOpen && !portfolioAlertMonitorTimer) {
      startPortfolioAlertMonitor();
    } else if ((!alertMonitor?.checked || !session.isOpen) && portfolioAlertMonitorTimer) {
      stopPortfolioAlertMonitor();
      if (!session.isOpen) writePortfolioAlertStatus(session.label);
      // 只有使用者主動取消勾選才算「不用接管」；單純盤已收盤不算使用者
      // 表態不想被監控(盤中重開時心跳自然會恢復)。
      if (!alertMonitor?.checked) notifyExitWatchMonitoringDisabled();
    }
    if (session.isOpen) {
      loadMonsterIntraday();
    }
    // 不限盤中：15:00 自動掃描發生在收盤後，完成偵測要全天有效
    checkBackgroundMonsterScan();
  }, 30 * 1000);
}

function initSinopacAutoSyncControls() {
  const checkbox = document.getElementById("autoSyncSinopac");
  const secondsInput = document.getElementById("autoSyncSeconds");
  const settings = readSinopacAutoSyncSettings();
  checkbox.checked = settings.enabled;
  secondsInput.value = settings.seconds;

  const update = () => {
    const next = {
      enabled: checkbox.checked,
      seconds: normalizeAutoSyncSeconds(secondsInput.value || 60)
    };
    secondsInput.value = next.seconds;
    saveSinopacAutoSyncSettings(next);
    startSinopacAutoSync();
    const session = taiwanMarketSession();
    writeSinopacAutoSyncStatus(next.enabled
      ? `${session.label}${session.isOpen ? `每 ${next.seconds} 秒更新一次，盤中管理持股。` : ""} 妖股流程：15:00-16:00 掃描名單，09:05 初篩，09:15 看量能，09:30-10:00 初次確認，10:00-13:15 只看低接/V轉。`
      : "永豐庫存自動同步已關閉。"
    );
  };

  checkbox.addEventListener("change", update);
  secondsInput.addEventListener("change", update);
  startSinopacAutoSync();
  startSinopacMarketClock();
}

function preparedData(stock = currentStock(), period = state.period) {
  const cached = dataCache.get(stock.code);
  if (!cached?.rows?.length) {
    const emptyIndicators = calcIndicators([]);
    return { full: [], fullIndicators: emptyIndicators, rows: [], indicators: emptyIndicators, noRealData: true };
  }
  const sourceRows = cached.rows;
  const full = applyPortfolioRealtimeQuote(stock, sourceRows);
  const fullIndicators = calcIndicators(full);
  const rows = full.slice(-period);
  const offset = full.length - rows.length;
  const indicators = Object.fromEntries(Object.entries(fullIndicators).map(([key, values]) => [key, values.slice(offset)]));
  return { full, fullIndicators, rows, indicators };
}

function clamp(value, min = 0, max = 100) {
  return Math.max(min, Math.min(max, Number(value) || 0));
}

function brainV2ComponentPercent(stockCode, key, context = "analysis") {
  const cached = brainDecisionStatus(stockCode, context);
  const components = Array.isArray(cached?.brainV2?.components) ? cached.brainV2.components : [];
  const row = components.find((item) => String(item?.key || "") === key);
  const score = Number(row?.score);
  return Number.isFinite(score) ? clamp(score * 100) : null;
}

function brainV2ScorePercent(stockCode, context = "analysis") {
  const cached = brainDecisionStatus(stockCode, context);
  const score = Number(cached?.brainV2?.score);
  return Number.isFinite(score) ? clamp(score * 100) : null;
}

function hasScoreValue(value) {
  return value !== null && value !== undefined && Number.isFinite(Number(value));
}

// ── 前端本地瀏覽器 ML 訓練管線已整條移除：featureVector / ruleFallbackScore / samplesFromSeries / buildMlDataset /
//    scoreMetrics / trainLogisticModel / gini / buildTree / predictTree / trainRandomForestModel / trainModelSuite / calcModel。
//    calcModel 零外部呼叫（整條只在自己內部互相呼叫），完全不執行；目前股票分析只走真實資料與型態量能規則。

function historicalRuleScore(rows, indicators, index) {
  const row = rows[index];
  const ma20 = indicators.ma20[index];
  const ma60 = indicators.ma60[index];
  const rsi = indicators.rsi[index];
  const macd = indicators.macd[index];
  const base20 = rows[index - 20];
  if (![row?.close, ma20, ma60, rsi, macd, base20?.close].every(Number.isFinite)) return 0;
  const trendScore = row.close > ma20 ? 0.22 : -0.12;
  const maScore = ma20 > ma60 ? 0.22 : -0.12;
  const momentumScore = macd > 0 ? 0.16 : -0.08;
  const rsiScore = rsi >= 45 && rsi <= 72 ? 0.16 : rsi > 78 ? -0.18 : 0;
  const ret20 = (row.close - base20.close) / base20.close;
  const retScore = Math.max(-0.12, Math.min(0.16, ret20 * 1.8));
  return 0.5 + trendScore + maScore + momentumScore + rsiScore + retScore;
}

function backtest(rows, indicators) {
  let cash = 100;
  let holding = false;
  let entry = 0;
  let wins = 0;
  let trades = 0;
  let peak = 100;
  let maxDrawdown = 0;
  const equity = [];

  rows.forEach((row, index) => {
    if (index < 70) {
      equity.push(cash);
      return;
    }
    const ma20 = indicators.ma20[index] ?? row.close;
    const score = historicalRuleScore(rows, indicators, index);
    const stopPrice = entry - (indicators.atr[index] || 0) * state.atrMultiple;
    const buy = !holding && score * 100 >= RULE_BACKTEST_ENTRY_SCORE;
    const sell = holding && (score * 100 <= RULE_BACKTEST_EXIT_SCORE || row.close < ma20 || indicators.rsi[index] > 78 || row.close < stopPrice);
    if (!holding && buy) {
      holding = true;
      entry = row.close;
      trades += 1;
    } else if (holding && sell) {
      const ret = (row.close - entry) / entry - state.costPct / 100;
      cash *= 1 + ret;
      wins += ret > 0 ? 1 : 0;
      holding = false;
    }
    const mark = holding ? cash * (row.close / entry) : cash;
    peak = Math.max(peak, mark);
    maxDrawdown = Math.min(maxDrawdown, (mark - peak) / peak);
    equity.push(mark);
  });

  if (holding) {
    const last = rows.at(-1);
    const ret = (last.close - entry) / entry - state.costPct / 100;
    wins += ret > 0 ? 1 : 0;
  }

  return { equity, totalReturn: equity.at(-1) - 100, trades, wins, winRate: trades ? wins / trades : 0, maxDrawdown };
}

function positionPlan(last, indicators) {
  const i = indicators.atr.length - 1;
  const stopDistance = indicators.atr[i] * state.atrMultiple;
  const stopPrice = Math.max(last.close - stopDistance, 0);
  const riskBudget = state.capital * (state.riskPct / 100);
  const shares = Math.max(0, Math.floor(riskBudget / Math.max(stopDistance, 0.01)));
  const cost = shares * last.close;
  const exposure = cost / state.capital;
  const target1 = last.close + stopDistance * 1.5;
  const target2 = last.close + stopDistance * 2.5;
  const trailingStop = Math.max(stopPrice, last.close - stopDistance * 0.75);
  return { stopDistance, stopPrice, riskBudget, shares, cost, exposure, target1, target2, trailingStop };
}

function tradingDecision(rows, indicators, bt, plan) {
  const i = rows.length - 1;
  const latest = rows[i];
  const previous = rows[i - 1];
  const ma20 = indicators.ma20[i] ?? latest.close;
  const ma60 = indicators.ma60[i] ?? latest.close;
  const ma20Prev = indicators.ma20[Math.max(0, i - 5)] ?? ma20;
  const atr = indicators.atr[i] ?? 0;
  const atrPct = latest.close ? atr / latest.close : 0;
  const avgVolume = rows.slice(Math.max(0, i - 19), i + 1).reduce((sum, row) => sum + row.volume, 0) / Math.min(20, rows.length);
  const volumeRatio = latest.volume / Math.max(avgVolume, 1);
  const trendOk = latest.close > ma20 && ma20 > ma60 && ma20 > ma20Prev;
  const momentumOk = indicators.macd[i] > 0 && indicators.rsi[i] >= 45 && indicators.rsi[i] <= 72;
  const riskOk = atrPct <= state.maxRisk / 100 && plan.exposure <= 0.6;
  const backtestOk = bt.trades >= 2 && bt.maxDrawdown > -0.25;
  const exitRisk = latest.close <= plan.stopPrice || latest.close < ma20;
  const breakoutPrice = Math.max(previous.high + atr * 0.15, latest.close);
  const pullbackPrice = Math.max(ma20, latest.close - atr * 0.6);
  const checks = [
    { label: "股價站上重要均線", ok: trendOk, value: `收盤 ${fmt(latest.close)} / 月線 ${fmt(ma20)} / 季線 ${fmt(ma60)}` },
    { label: "沒有明顯過熱", ok: momentumOk, value: `強弱 ${fmt(indicators.rsi[i])} / 轉強訊號 ${fmt(indicators.macd[i])}` },
    { label: "波動與投入金額可控", ok: riskOk, value: `波動 ${fmt(atrPct * 100)}% / 資金使用 ${fmt(plan.exposure * 100)}%` },
    { label: "過去測試風險可接受", ok: backtestOk, value: `模擬交易 ${bt.trades} 次 / 最大下跌 ${pct(bt.maxDrawdown * 100)}` }
  ];
  const passCount = checks.filter((item) => item.ok).length;
  let action = "先等更好的買點";
  let badge = "等待";
  let tone = "neutral";
  if (exitRisk) {
    action = "注意賣出或降低持股";
    badge = "賣出提醒";
    tone = "sell";
  } else if (checks.every((item) => item.ok)) {
    action = "可設定買進條件";
    badge = "買進條件接近";
    tone = "buy";
  } else if (passCount >= 3 && trendOk && riskOk) {
    action = "少量觀察";
    badge = "觀察";
    tone = "watch";
  }
  return {
    action,
    badge,
    tone,
    confidence: passCount / checks.length,
    checks,
    buyTrigger: breakoutPrice,
    pullbackPrice,
    stopPrice: plan.stopPrice,
    target1: plan.target1,
    target2: plan.target2,
    shares: action === "可設定買進條件" ? plan.shares : Math.floor(plan.shares * 0.35),
    volumeRatio,
    invalidReasons: checks.filter((item) => !item.ok).map((item) => item.label)
  };
}

function linePath(values, x, y) {
  return values.map((value, index) => (value == null ? "" : `${index === 0 || values[index - 1] == null ? "M" : "L"} ${x(index)} ${y(value)}`)).join(" ");
}

function drawPriceChart(container, rows, indicators, riskLines = {}) {
  const width = Math.max(container.clientWidth, 720);
  const height = 420;
  if (!rows?.length) {
    container.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="等待真實資料">
      <rect width="${width}" height="${height}" fill="var(--surface)"/>
      <text x="${width / 2}" y="${height / 2}" text-anchor="middle" fill="var(--muted)" font-size="20">等待真實資料，不使用模擬股價</text>
    </svg>`;
    return;
  }
  const pad = { top: 20, right: 54, bottom: 48, left: 58 };
  const chartH = 288;
  const volTop = pad.top + chartH + 18;
  const riskLineValues = Object.values(riskLines)
    .filter((line) => line && Number.isFinite(Number(line.value)))
    .map((line) => Number(line.value));
  const overlayValues = rows.flatMap((row, index) => {
    if (state.overlay === "boll") return [row.high, row.low, indicators.bollUpper[index], indicators.bollLower[index]];
    if (state.overlay === "atr") return [row.high, row.low, row.close - indicators.atr[index] * state.atrMultiple];
    return [row.high, row.low, indicators.ma5[index], indicators.ma20[index], indicators.ma60[index]];
  }).concat(riskLineValues).filter((value) => Number.isFinite(value));
  const min = Math.min(...overlayValues) * 0.985;
  const max = Math.max(...overlayValues) * 1.015;
  const maxVol = Math.max(...rows.map((row) => row.volume));
  const x = (index) => pad.left + (index / Math.max(rows.length - 1, 1)) * (width - pad.left - pad.right);
  const y = (value) => pad.top + ((max - value) / (max - min)) * chartH;
  const yVol = (value) => volTop + 70 - (value / maxVol) * 70;
  const candleW = Math.max(3, Math.min(10, ((width - pad.left - pad.right) / rows.length) * 0.62));
  const axis = Array.from({ length: 5 }, (_, idx) => {
    const yy = pad.top + (idx / 4) * chartH;
    const label = max - (idx / 4) * (max - min);
    return `<line x1="${pad.left}" y1="${yy}" x2="${width - pad.right}" y2="${yy}" stroke="var(--line)"/><text x="12" y="${yy + 4}" fill="var(--muted)" font-size="20">${fmt(label, 0)}</text>`;
  }).join("");
  const candles = rows.map((row, index) => {
    const up = row.close >= row.open;
    const color = up ? "#ff1744" : "#00ff2a";
    const cx = x(index);
    const bodyY = Math.min(y(row.open), y(row.close));
    const bodyH = Math.max(Math.abs(y(row.open) - y(row.close)), 2);
    return `<line x1="${cx}" y1="${y(row.high)}" x2="${cx}" y2="${y(row.low)}" stroke="${color}" stroke-width="1.2"/>
      <rect x="${cx - candleW / 2}" y="${bodyY}" width="${candleW}" height="${bodyH}" rx="1" fill="${up ? color : "var(--surface)"}" stroke="${color}" stroke-width="1.2"/>
      <rect x="${cx - candleW / 2}" y="${yVol(row.volume)}" width="${candleW}" height="${volTop + 70 - yVol(row.volume)}" fill="${color}" opacity="0.28"/>`;
  }).join("");
  const overlay = state.overlay === "boll"
    ? `<path d="${linePath(indicators.bollUpper, x, y)}" fill="none" stroke="#fff200" stroke-width="2"/>
       <path d="${linePath(indicators.bollMid, x, y)}" fill="none" stroke="#2fa8ff" stroke-width="2"/>
       <path d="${linePath(indicators.bollLower, x, y)}" fill="none" stroke="#fff200" stroke-width="2"/>`
    : state.overlay === "atr"
      ? `<path d="${linePath(rows.map((row, index) => row.close - indicators.atr[index] * state.atrMultiple), x, y)}" fill="none" stroke="#ff1744" stroke-width="2" stroke-dasharray="6 5"/>
         <path d="${linePath(indicators.ma20, x, y)}" fill="none" stroke="#2fa8ff" stroke-width="2"/>`
      : `<path d="${linePath(indicators.ma5, x, y)}" fill="none" stroke="#18d8ff" stroke-width="2"/>
         <path d="${linePath(indicators.ma20, x, y)}" fill="none" stroke="#2fa8ff" stroke-width="2"/>
         <path d="${linePath(indicators.ma60, x, y)}" fill="none" stroke="#7c3aed" stroke-width="2"/>`;
  const riskLineSvg = Object.entries(riskLines)
    .filter(([, line]) => line && Number.isFinite(Number(line.value)))
    .map(([, line], index) => {
      const yy = y(Number(line.value));
      const color = line.color || "#64748b";
      const label = `${line.label} ${fmt(line.value)}`;
      return `<line x1="${pad.left}" y1="${yy}" x2="${width - pad.right}" y2="${yy}" stroke="${color}" stroke-width="1.8" stroke-dasharray="7 5"/>
        <rect x="${width - pad.right - 142}" y="${yy - 13 - index * 0}" width="138" height="20" rx="4" fill="var(--surface)" opacity="0.92"/>
        <text x="${width - pad.right - 136}" y="${yy + 4}" fill="${color}" font-size="20" font-weight="700">${label}</text>`;
    }).join("");
  container.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img">
    <rect width="${width}" height="${height}" fill="var(--surface)"/>
    ${axis}
    <line x1="${pad.left}" y1="${volTop + 70}" x2="${width - pad.right}" y2="${volTop + 70}" stroke="var(--line)"/>
    ${candles}
    ${overlay}
    ${riskLineSvg}
    <text x="${pad.left}" y="${height - 16}" fill="var(--muted)" font-size="20">${rows[0].date.toLocaleDateString("zh-TW")}</text>
    <text x="${width - pad.right - 86}" y="${height - 16}" fill="var(--muted)" font-size="20">${rows.at(-1).date.toLocaleDateString("zh-TW")}</text>
  </svg>`;
}

function drawLineChart(container, series) {
  const width = Math.max(container.clientWidth, 420);
  const height = 230;
  const pad = { top: 20, right: 24, bottom: 28, left: 44 };
  const values = series.flatMap((item) => item.values).filter(Number.isFinite);
  if (!values.length) {
    container.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="無資料">
      <rect width="${width}" height="${height}" fill="var(--surface)"/>
      <text x="${width / 2}" y="${height / 2}" text-anchor="middle" fill="var(--muted)" font-size="20">無資料</text>
    </svg>`;
    return;
  }
  const min = Math.min(...values) * 0.95;
  const max = Math.max(...values) * 1.05;
  const x = (index, len) => pad.left + (index / Math.max(len - 1, 1)) * (width - pad.left - pad.right);
  const y = (value) => pad.top + ((max - value) / Math.max(max - min, 1)) * (height - pad.top - pad.bottom);
  const paths = series.map((item) => `<path d="${item.values.map((value, index) => (
    Number.isFinite(value) ? `${index === 0 || !Number.isFinite(item.values[index - 1]) ? "M" : "L"} ${x(index, item.values.length)} ${y(value)}` : ""
  )).join(" ")}" fill="none" stroke="${item.color}" stroke-width="2.4"/>`).join("");
  container.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img">
    <rect width="${width}" height="${height}" fill="var(--surface)"/>
    <line x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}" stroke="var(--line)"/>
    <line x1="${pad.left}" y1="${pad.top}" x2="${pad.left}" y2="${height - pad.bottom}" stroke="var(--line)"/>
    ${paths}
    ${series.map((item, index) => `<circle cx="${pad.left + index * 92}" cy="14" r="4" fill="${item.color}"/><text x="${pad.left + 8 + index * 92}" y="18" fill="var(--muted)" font-size="20">${item.name}</text>`).join("")}
  </svg>`;
}

function allSignals() {
  return stocks.map((stock) => {
    const { rows, indicators, full, fullIndicators } = preparedData(stock, 250);
    if (full.length < 2) return null;
    const latest = full.at(-1);
    const previous = full.at(-2);
    const riskPct = (fullIndicators.atr.at(-1) / latest.close) * 100;
    return { stock, rows, indicators, fullIndicators, latest, previous, riskPct };
  }).filter(Boolean);
}

function portfolioFallbackSignal(code, holding) {
  const stock = makeStock(code);
  const currentPrice = Number(holding?.currentPrice || holding?.price || stock.base || 0);
  const openPrice = Number(holding?.openPrice || currentPrice);
  const highPrice = Number(holding?.highPrice || currentPrice);
  const lowPrice = Number(holding?.lowPrice || currentPrice);
  const volume = Number(holding?.totalVolume || 0) * 1000;
  const latest = {
    date: new Date().toISOString().slice(0, 10),
    open: openPrice,
    high: Math.max(highPrice, currentPrice),
    low: Math.min(lowPrice, currentPrice),
    close: currentPrice,
    volume,
    realtimeSource: "sinopac_holdings",
  };
  const previousClose = Number.isFinite(Number(holding?.currentPrice)) && Number.isFinite(Number(holding?.changePrice))
    ? Number(holding.currentPrice) - Number(holding.changePrice)
    : currentPrice;
  const previous = { ...latest, close: previousClose || currentPrice };
  return {
    stock,
    rows: [previous, latest],
    indicators: calcIndicators([previous, latest]),
    fullIndicators: calcIndicators([previous, latest]),
    latest,
    previous,
    riskPct: 0,
    fallbackSignal: true,
  };
}

function portfolioDisplayItems(holdings = readPortfolioHoldings()) {
  const portfolioOrder = readPortfolioCodes();
  const orderMap = new Map(portfolioOrder.map((code, index) => [code, index]));
  const signalItems = allSignals().sort((a, b) => {
    const aOrder = orderMap.has(a.stock.code) ? orderMap.get(a.stock.code) : Number.MAX_SAFE_INTEGER;
    const bOrder = orderMap.has(b.stock.code) ? orderMap.get(b.stock.code) : Number.MAX_SAFE_INTEGER;
    return aOrder - bOrder;
  });
  const signalMap = new Map(signalItems.map((item) => [item.stock.code, item]));
  const holdingCodes = Object.keys(holdings).map(normalizeStockCode).filter(Boolean);
  const displayCodes = [...new Set([...portfolioOrder, ...holdingCodes])];
  return displayCodes
    .map((code) => signalMap.get(code) || portfolioFallbackSignal(code, holdings[code] || {}))
    .filter((item) => holdings[item.stock.code] || signalMap.has(item.stock.code));
}

// scoreBreakdown / buyConditionSummary / sellTriggerSummary 已移除:三者皆為已下架的
// 「個股完整分析」(#33)殘留,零呼叫,內含模型分數閘門(totalScore 含 modelScore*0.28、
// 買進「正式模型分數達標」、賣出「正式模型看好下降」),一併清除以徹底切割模型與決策/顯示。

function marketContextFromRows(rows) {
  const enoughRows = rows.length >= 21;
  const latest = rows.at(-1);
  const monthAgo = rows.at(-21) || rows[0];
  const hasStockRet20 = Boolean(enoughRows && monthAgo?.close);
  const stockRet20 = hasStockRet20 ? (latest.close - monthAgo.close) / monthAgo.close : 0;
  const latestIndex = Number(latest?.twIndex);
  const pastIndex = Number(monthAgo?.twIndex);
  const ma20Index = enoughRows
    ? rows.slice(-20).reduce((sum, row) => sum + Number(row.twIndex || latestIndex || 0), 0) / 20
    : latestIndex;
  const hasMarketRet20 = Boolean(enoughRows && Number.isFinite(latestIndex) && Number.isFinite(pastIndex) && pastIndex);
  const marketRet20 = hasMarketRet20
    ? (latestIndex - pastIndex) / pastIndex
    : 0;
  const marketMaGap = Number.isFinite(latestIndex) && Number.isFinite(ma20Index) && ma20Index
    ? (latestIndex - ma20Index) / ma20Index
    : 0;
  const regime = marketMaGap > 0.02 && marketRet20 > 0.02
    ? "多頭"
    : marketMaGap < -0.02 && marketRet20 < -0.02
      ? "空頭"
      : "震盪";
  return {
    regime,
    marketRet20,
    marketMaGap,
    stockRet20,
    hasMarketRet20,
    hasStockRet20,
    stockVsMarket: stockRet20 - marketRet20,
    stockStrongerThanMarket: hasStockRet20 && hasMarketRet20 && stockRet20 > marketRet20
  };
}

function signedMoney(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "-";
  const number = Number(value);
  const prefix = number > 0 ? "+" : "";
  return `${prefix}${number.toLocaleString("zh-TW", { maximumFractionDigits: 0 })}`;
}

function signedPrice(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "-";
  const number = Number(value);
  return `${number > 0 ? "+" : ""}${fmt(number)}`;
}

function compactShares(shares) {
  return Number(shares || 0) ? `${fmt(shares, 0)} 股` : "-";
}

function dashboardTone(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number === 0) return "";
  return number > 0 ? "up" : "down";
}

function portfolioTrendBadge(market, priceChangePct, pnlRate, exitPlan) {
  // 只看出場計畫、今日漲跌、相對大盤強弱與損益。
  const dayChange = Number(priceChangePct);
  const profitRate = Number(pnlRate);
  const rising = Number.isFinite(dayChange) && dayChange > 0;
  const falling = Number.isFinite(dayChange) && dayChange < 0;
  const profitable = Number.isFinite(profitRate) && profitRate > 0;
  if (exitPlan?.strategyHorizon === "unknown" || exitPlan?.type === "unknown") {
    return { text: "週期未知｜只觀察", tone: "warn" };
  }
  if (exitPlan && exitPlan.dataReady === false) return { text: "資料未完成｜只觀察", tone: "warn" };
  if (exitPlan?.type === "time_stop") return { text: "時間到｜考慮出場", tone: "warn" };
  if (exitPlan?.type === "phase1" || exitPlan?.type === "phase2") return { text: "先觀察", tone: "warn" };
  if (exitPlan?.type === "stop" || exitPlan?.type === "phase3") return { text: "容易回跌", tone: "down" };
  if (Number(pnlRate) >= 18) return { text: "先觀察", tone: "warn" };
  if (market?.stockStrongerThanMarket && rising && profitable) return { text: "續漲機會", tone: "up" };
  if (market?.stockStrongerThanMarket && !falling) return { text: "還有機會", tone: "up" };
  if (falling || Number(pnlRate) < -8) return { text: "容易回跌", tone: "down" };
  return { text: "先觀察", tone: "warn" };
}

function portfolioForceBadge(market, priceChangePct, volumeRatio = null, pnlRate = null) {
  // 量價力道只看今日漲跌、量能倍數與相對大盤。
  const dayChange = Number(priceChangePct);
  const volume = Number(volumeRatio);
  const profitRate = Number(pnlRate);
  const hasVolume = Number.isFinite(volume);
  if (Number.isFinite(dayChange) && dayChange >= 1 && hasVolume && volume >= 1.2 && market?.stockStrongerThanMarket) {
    return { text: "買壓較強", tone: "up" };
  }
  if (Number.isFinite(dayChange) && dayChange > 0 && (!hasVolume || volume >= 0.9)) {
    return { text: "買方略強", tone: "up" };
  }
  if (Number.isFinite(dayChange) && dayChange <= -1 && hasVolume && volume >= 1.2) {
    return { text: "賣壓較強", tone: "down" };
  }
  if (Number.isFinite(dayChange) && dayChange < 0 && Number.isFinite(profitRate) && profitRate < 0) {
    return { text: "賣壓偏重", tone: "down" };
  }
  if (hasVolume && volume < 0.8) return { text: "量縮觀察", tone: "warn" };
  return { text: "買賣混亂", tone: "warn" };
}

function portfolioRiskBadge(exitPlan, pnlRate, priceChangePct = null, trend = null, force = null) {
  // 風險燈號只看出場計畫、今日漲跌與續漲/買賣力道。
  const dayChange = Number(priceChangePct);
  const isRisingToday = Number.isFinite(dayChange) && dayChange > 0;
  const strongToday = Number.isFinite(dayChange) && dayChange >= 1.5;
  const hasMomentum = ["續抱觀察", "續漲機會", "還有機會"].includes(trend?.text) || ["買壓較強", "買方略強"].includes(force?.text);
  if (exitPlan?.strategyHorizon === "unknown" || exitPlan?.type === "unknown") {
    return { text: "週期未知｜不觸發出場", tone: "warn" };
  }
  if (exitPlan && exitPlan.dataReady === false) return { text: "資料未完成｜不通知", tone: "warn" };
  if (exitPlan?.type === "time_stop") return { text: "時間停損｜換下一檔", tone: "danger" };
  if (exitPlan?.type === "stop" || exitPlan?.type === "phase3") {
    if (isRisingToday && hasMomentum) return { text: "反彈中｜看防守價", tone: "warn" };
    return { text: "警戒｜等確認賣出", tone: "danger" };
  }
  if (exitPlan?.type === "phase1" || exitPlan?.type === "phase2") return { text: "續抱｜看系統提醒", tone: "up" };
  if (Number(pnlRate) > 0 && (isRisingToday || hasMomentum)) return { text: "續抱｜上移防守價", tone: "up" };
  if (Number(pnlRate) < -8) {
    if (strongToday || hasMomentum) return { text: "反彈中｜等確認", tone: "warn" };
    return { text: "高風險｜只警戒", tone: "danger" };
  }
  if (!isRisingToday && !hasMomentum) {
    return { text: "轉弱｜守停損", tone: "danger" };
  }
  return { text: "先保護｜不加碼", tone: "warn" };
}

function retPct(rows, days) {
  const latest = rows.at(-1);
  const base = rows.at(-(days + 1));
  if (!latest || !base?.close) return 0;
  return ((latest.close - base.close) / base.close) * 100;
}

function sortMonsterCandidates(a, b) {
  // 後端是真實價量排名的唯一真相。前端不再使用另一套未回測的手調權重，
  // 也不讀模型機率，避免畫面名次與正式純規則候選流程分歧。
  if (Boolean(a.buyAllowed) !== Boolean(b.buyAllowed)) return a.buyAllowed ? -1 : 1;
  const scoreDiff = Number(b.score || 0) - Number(a.score || 0);
  if (Math.abs(scoreDiff) > 0.0001) return scoreDiff;
  return Number(b.volumeRatio ?? b.volume_ratio ?? 0) - Number(a.volumeRatio ?? a.volume_ratio ?? 0);
}

function isStrengtheningCandidate(item) {
  const score = Number(item.score || 0);
  const change5 = Number(item.change5 ?? item.change_5 ?? 0);
  const change20 = Number(item.change20 ?? item.change_20 ?? 0);
  const volumeRatio = Number(item.volume_ratio ?? item.volumeRatio ?? 0);
  return Boolean(item.surge_setup || item.surgeSetup || score >= 35 || (change5 >= 5 && change20 >= 5 && volumeRatio >= 1.2));
}

function isShortMonsterCandidate(item) {
  const score = Number(item.score || 0);
  const change1 = Number(item.change1 ?? item.change_1 ?? 0);
  const change5 = Number(item.change5 ?? item.change_5 ?? 0);
  const volumeRatio = Number(item.volume_ratio ?? item.volumeRatio ?? 0);
  const liquidityOk = item.liquidityOk == null && item.liquidity_ok == null ? true : Boolean(item.liquidityOk ?? item.liquidity_ok);
  return Boolean(
    item.buyAllowed ||
    item.buy_allowed ||
    item.surge_setup ||
    item.surgeSetup ||
    (liquidityOk && score >= 45) ||
    (liquidityOk && score >= 38 && change5 >= 2 && volumeRatio >= 1.2) ||
    (liquidityOk && change1 >= 2.5 && volumeRatio >= 1.5)
  );
}

function scanSummaryCards(items, options = {}) {
  const rows = Array.isArray(items) ? items : [];
  const scannedCount = Number(options.scannedCount ?? rows.length);
  const strengtheningCount = Number(options.strengtheningCount ?? rows.filter(isShortMonsterCandidate).length);
  const watchCount = Number(options.watchCount ?? rows.filter((item) => Boolean(item.buyAllowed ?? item.buy_allowed)).length);
  const buyCount = Number(options.buyCount ?? 0);
  const cards = [
    ["掃描檔數", scannedCount, "系統有納入檢查的股票"],
    ["短線妖股候選", strengtheningCount, "分數、漲幅、量能或異常啟動符合短線條件"],
    ["目前顯示", watchCount, "下方表格實際列出的前段名單"],
    ["真正可買檔數", buyCount, "盤中確認通過才可買"]
  ];
  return `
    <div class="scan-summary-grid">
      ${cards.map(([label, value, note], index) => `
        <article class="scan-summary-card${index === 3 && value > 0 ? " buy" : ""}">
          <span>${label}</span>
          <strong>${fmt(value, 0)} 檔</strong>
          <small>${note}</small>
        </article>
      `).join("")}
    </div>
  `;
}

function backendMonsterCandidates() {
  const rows = monsterBackendState.payload?.candidates || [];
  const portfolioCodes = new Set(readPortfolioCodes());
  return rows.map((item) => {
    const code = normalizeStockCode(item.symbol);
    const intraday = monsterIntradayState?.quotes?.[code] || null;
    const isHolding = portfolioCodes.has(code);
    return {
      source: "backend",
      stock: {
        code,
        name: stockDisplayName(code, item.name) || code,
        sector: item.sector || knownStockProfiles[code]?.sector || "上市櫃"
      },
      score: Number(item.score || 0),
      status: item.status || (item.buyAllowed ? "隔日觀察，開盤二次確認" : "等待"),
      buyAllowed: Boolean(item.buyAllowed),
      buyTrigger: Number(item.buy_trigger ?? item.buyTrigger ?? item.close ?? 0),
      pullbackPrice: Number(item.pullback_price ?? item.pullbackPrice ?? item.close ?? 0),
      stopPrice: Number(item.stop_price ?? item.stopPrice ?? 0),
      takeProfit: Number(item.take_profit ?? item.takeProfit ?? 0),
      trailingStop: Number(item.trailing_stop ?? item.trailingStop ?? 0),
      currentPrice: Number(intraday?.currentPrice ?? item.close ?? item.currentPrice ?? 0),
      // 掃描日收盤價與資料日期：停損保底 fallback 與「現價其實是收盤價」
      // 的誠實標示都需要這兩個欄位(盤中報價只算排名前段，後段沒有 live 價)。
      close: Number(item.close || 0),
      priceDate: item.price_date ?? item.priceDate ?? null,
      hasLiveQuote: intraday?.currentPrice != null,
      openPrice: intraday?.openPrice == null ? null : Number(intraday.openPrice),
      highPrice: intraday?.highPrice == null ? null : Number(intraday.highPrice),
      lowPrice: intraday?.lowPrice == null ? null : Number(intraday.lowPrice),
      totalVolume: intraday?.totalVolume == null ? null : Number(intraday.totalVolume),
      quoteSource: String(intraday?.source || ""),
      quoteAgeSeconds: intraday?.quoteAgeSeconds == null ? null : Number(intraday.quoteAgeSeconds),
      quoteFresh: intraday?.quoteFresh == null ? null : Boolean(intraday.quoteFresh),
      quoteFreshnessReason: String(intraday?.quoteFreshnessReason || ""),
      bidAskSpreadPct: intraday?.bidAskSpreadPct == null ? null : Number(intraday.bidAskSpreadPct),
      estimatedSlippagePct: intraday?.estimatedSlippagePct == null ? null : Number(intraday.estimatedSlippagePct),
      intraday,
      // change1 是「最近一個完整交易日」的日線漲跌幅(掃描當下算好存進資料庫，
      // 開盤後就是舊資料)。今天還在交易中時，畫面要顯示的「今日漲跌」必須用
      // 即時報價對比昨收(item.close，掃描存的正式收盤價)重新算，不能直接沿用
      // change1，否則會出現「現在跌 1.28%，卻顯示掃描當天的 +10%」這種矛盾。
      change1: Number(item.change1 ?? item.change_1 ?? 0),
      todayChangePct: (() => {
        const previousClose = Number(item.close || 0);
        const livePrice = Number(intraday?.currentPrice);
        // 掃描資料日期＝今天(15:00 掃描後的盤後時段)時，item.close 是
        // 「今天的收盤」而不是昨收——拿盤後報價對比今天收盤永遠 ≈0%，
        // 會把當日真實漲跌整個蓋掉；這種情況直接用掃描存的 change1。
        const scanPriceDate = String(item.price_date ?? item.priceDate ?? "");
        const closeIsToday = scanPriceDate && scanPriceDate === dateKey(new Date());
        if (!closeIsToday && intraday?.currentPrice != null && previousClose > 0 && Number.isFinite(livePrice)) {
          return ((livePrice - previousClose) / previousClose) * 100;
        }
        return Number(item.change1 ?? item.change_1 ?? 0);
      })(),
      change5: Number(item.change5 ?? item.change_5 ?? 0),
      change20: Number(item.change20 ?? item.change_20 ?? 0),
      volumeRatio: intraday?.volumeRatio == null
        ? (item.volume_ratio == null && item.volumeRatio == null ? null : Number(item.volume_ratio ?? item.volumeRatio))
        : Number(intraday.volumeRatio),
      latestVolumeLots: item.latest_volume_lots == null && item.latestVolumeLots == null ? null : Number(item.latest_volume_lots ?? item.latestVolumeLots),
      avgVolume20Lots: item.avg_volume20_lots == null && item.avgVolume20Lots == null ? null : Number(item.avg_volume20_lots ?? item.avgVolume20Lots),
      turnoverMillion: item.turnover_million == null && item.turnoverMillion == null ? null : Number(item.turnover_million ?? item.turnoverMillion),
      liquidityOk: item.liquidityOk == null && item.liquidity_ok == null ? true : Boolean(item.liquidityOk ?? item.liquidity_ok),
      riskPct: null,
      surgeSetup: Boolean(item.surge_setup ?? item.surgeSetup),
      counterTrendStrength: Boolean(item.counter_trend_strength ?? item.counterTrendStrength),
      overheated: Boolean(item.overheated),
      riskVetoed: Boolean(item.risk_vetoed ?? item.riskVetoed),
      riskFlags: Array.isArray(item.riskFlags) ? item.riskFlags : [],
      sectorExcessRet5: item.sector_excess_ret5 == null && item.sectorExcessRet5 == null ? null : Number(item.sector_excess_ret5 ?? item.sectorExcessRet5),
      sectorHot: (monsterBackendState.payload?.hotSectors || []).includes(item.sector || knownStockProfiles[code]?.sector),
      themeHeat: Number(item.themeHeat ?? item.theme_heat ?? 0),
      sectorThemeStreak: Number(item.sectorThemeStreak ?? item.sector_theme_streak ?? 0),
      marketRegime: String(item.marketRegime ?? item.market_regime ?? monsterBackendState.payload?.marketRegime?.key ?? "theme_rotation"),
      marketRegimeLabel: String(item.marketRegimeLabel ?? monsterBackendState.payload?.marketRegime?.label ?? "題材輪動"),
      minimumFormalScore: Number(item.minimumFormalScore ?? item.regimeThreshold ?? item.regime_threshold ?? 60),
      isHolding,
      reasons: Array.isArray(item.reasons) ? item.reasons : [],
      ruleEngine: item.ruleEngine || null,
      // 進榜天數(tenure)：後端純讀 scan_date 歷史算的雷達候選池生命週期，
      // 唯讀純顯示、不影響任何買賣判斷。
      tenure: item.tenure || null
    };
  })
    .filter(isShortMonsterCandidate)
    .sort(sortMonsterCandidates);
}

function compactMonsterReasons(item) {
  const reasons = Array.isArray(item?.reasons) ? item.reasons : [];
  const compact = [];
  const add = (value) => {
    const text = String(value || "").trim();
    if (text && !compact.includes(text)) compact.push(text);
  };
  for (const reason of reasons) {
    const text = String(reason || "");
    const percent = text.match(/(\d+(?:\.\d+)?)%/);
    const volume = text.match(/量能\s*(\d+(?:\.\d+)?)\s*倍/);
    if (text.includes("短線綜合分數")) add(`分數${item.score}`);
    else if (text.includes("短線門檻")) add(percent ? `門檻${percent[1]}%` : "門檻達標");
    else if (text.includes("勝率模型") || text.includes("排序模型") || text.includes("異常偵測") || text.includes("Isolation Forest")) continue;
    else if (text.includes("量能")) add(volume ? `量能${volume[1]}倍` : "量能放大");
    else if (text.includes("符合短線妖股")) add("妖股型態");
    else if (text.includes("大盤")) add("強於大盤");
    else if (text.includes("流動性足夠")) add("流動性足夠");
    else if (text.includes("5日")) add(`5日${pct(item.change5)}`);
  }
  if (!compact.length) return "-";
  return compact.slice(0, 6).join("、");
}

function monsterQuoteSourceBadge(item) {
  if (!item?.hasLiveQuote) return '<small class="stale-price-tag">收</small>';
  const source = String(item.quoteSource || "即時");
  const label = source.includes("Capital") ? "群益" : source.includes("Shioaji") ? "永豐" : "即時";
  const age = Number(item.quoteAgeSeconds);
  const safeAge = Number.isFinite(age) ? Math.max(0, age) : null;
  const ageText = safeAge === null
    ? "時間未知"
    : safeAge < 60
      ? `${Math.round(safeAge)}秒`
      : safeAge < 3600
        ? `${Math.round(safeAge / 60)}分`
        : safeAge < 86400
          ? `${safeAge < 36000 ? (safeAge / 3600).toFixed(1) : Math.round(safeAge / 3600)}小時`
          : `${Math.floor(safeAge / 86400)}天`;
  const preciseAgeText = safeAge === null ? "時間未知" : `${Math.round(safeAge)}秒`;
  const stale = item.quoteFresh === false;
  const cls = `quote-source-tag${source.includes("Capital") ? " capital" : ""}${stale ? " stale" : ""}`;
  const title = `${label}報價｜${preciseAgeText}${item.quoteFreshnessReason ? `｜${item.quoteFreshnessReason}` : ""}`;
  return `<small class="${cls}" title="${escapeHtml(title)}">${escapeHtml(label)}·${escapeHtml(ageText)}</small>`;
}

function compactMonsterOpenChecks(item) {
  const checks = Array.isArray(item?.checks) ? item.checks : [];
  const compact = [];
  const add = (value) => {
    const text = String(value || "").trim();
    if (text && !compact.includes(text)) compact.push(text);
  };
  for (const check of checks) {
    const text = String(check || "");
    if (text.includes("跳空")) {
      const pctValue = text.match(/[-+]?\d+(?:\.\d+)?%/);
      add(pctValue ? `跳空${pctValue[0]}` : "跳空正常");
    } else if (text.includes("開高走低")) {
      add(text.includes("取消") ? "開高走低" : "未開高走低");
    } else if (text.includes("量能")) {
      add(text.includes("延續") ? "量能延續" : "量能未確認");
    } else if (text.includes("突破")) {
      add(text.includes("尚未") || text.includes("不追") ? "未突破/不追突破" : "已突破");
    } else if (text.includes("V轉")) {
      add(text.includes("尚未") ? "未V轉" : "V轉成立");
    } else if (text.includes("停損")) {
      add(text.includes("未跌破") ? "未破停損" : "跌破停損");
    }
  }
  return compact.length ? compact.slice(0, 5).join("、") : "-";
}

function entryDecisionText(action, fallback = "等待") {
  const text = String(action || "");
  if (text.includes("資料異常")) return "只觀察";
  if (text.includes("可進場") || text.includes("可買")) return "可進場";
  if (text.includes("未通過") || text.includes("放棄") || text.includes("不買") || text.includes("先不買")) return "先不進";
  if (text.includes("不進場") || text.includes("休市") || text.includes("不再進場")) return "先不進";
  if (text.includes("等待") || text.includes("等")) return "等確認";
  return fallback;
}

function timedEntryDecision(open, healthOk = true) {
  if (!healthOk) return "只觀察";
  if (open?.canBuy === true) return "可進場";
  if (open?.backendStatus && String(open.backendStatus).includes("未通過")) return "先不進";
  return entryDecisionText(open?.action);
}

function entryMethodText(open, decision, healthOk = true) {
  if (!healthOk) return "暫停決策";
  if (decision === "可進場") {
    if (open?.setupType === "pullback") return "回測低接";
    if (open?.setupType === "v_rebound") return "V轉低接";
    if (open?.setupType === "breakout") return "突破買進";
    return open?.method || "買 1 張";
  }
  const action = String(open?.action || "");
  if (action.includes("V轉")) return "等V轉";
  if (action.includes("低接") || action.includes("回測")) return "等回測";
  if (action.includes("突破")) return "等突破";
  if (action.includes("等") || action.includes("等待")) return "等確認";
  return "先不買";
}

function compactBrainEntryReason(reason) {
  const text = String(reason || "").replace(/^型態量能規則：/, "").trim();
  if (!text) return "";
  const v2Match = text.match(/型態量能進場條件未通過：(.+)$/);
  if (v2Match) {
    return `型態量能未過：${v2Match[1].split(/[、；,]/).filter(Boolean).slice(0, 3).join("、")}`;
  }
  if (text.includes("尚未取得判斷")) return "型態量能尚未評估";
  if (text.includes("讀取中")) return "型態量能讀取中";
  if (text.includes("逾時") || text.includes("暫時只觀察")) return "型態量能暫時只觀察";
  if (text.includes("讀取失敗")) return "型態量能暫未回應";
  const narrativeMatch = text.match(/^(.+?)，尚未達到進場標準/);
  if (narrativeMatch) {
    return narrativeMatch[1].split(/[、,]/).filter(Boolean).slice(0, 2).join("、");
  }
  if (text.includes("資料") || text.includes("核心")) {
    const parts = text.split("；").map((part) => {
      const [label, ...rest] = part.split(/[:：]/);
      const detail = rest.join("：").trim();
      const detailIsChinese = /[一-鿿]/.test(detail);
      return (detailIsChinese ? part : label).trim();
    }).filter((part) => /[一-鿿]/.test(part));
    const compact = parts.length ? parts.slice(0, 3).join("、") : "型態量能資料品質未通過";
    return compact.length > 42 ? `${compact.slice(0, 42)}...` : compact;
  }
  return text.length > 42 ? `${text.slice(0, 42)}...` : text;
}

function brainBlockedEntryMethod(rawReason, baseDecision = "") {
  const text = String(rawReason || "");
  const baseText = String(baseDecision || "");
  if (text.includes("尚未取得判斷") || text.includes("讀取中") || text.includes("逾時") || text.includes("暫時只觀察")) {
    if (baseText.includes("可進場")) return "等型態量能";
    if (baseText.includes("先不進")) return "條件不足";
    if (baseText.includes("等")) return "等盤中確認";
    return "等型態量能";
  }
  if (text.includes("資料") || text.includes("核心")) {
    return "補資料後再看";
  }
  if (text.includes("六大分數未達標")) return "等分數達標";
  if (text.includes("八大分數未達標") || text.includes("九大分數未達標") || text.includes("本機核心分數未達標")) return "等本機指標";
  if (text.includes("正式模型分數")) return "等型態量能轉強";
  if (text.includes("K線型態")) return "等K線轉強";
  if (text.includes("量能") || text.includes("OBV")) return "等量能確認";
  if (text.includes("大盤強弱") || text.includes("大盤條件")) return "等大盤轉強";
  if (text.includes("籌碼") || text.includes("資金")) return "等籌碼轉強";
  if (text.includes("風險控管") || text.includes("風險")) return "等風險下降";
  if (text.includes("資料可信度")) return "等資料補齊";
  if (text.includes("09:30") || text.includes("盤中") || text.includes("確認")) return "等盤中確認";
  if (String(baseDecision || "").includes("先不進")) return "條件不足";
  return "等型態量能通過";
}

function brainV2RequiredScore(component, decision) {
  const key = String(component?.key || "");
  const label = String(component?.label || "");
  if (component?.auxiliary) return 0;
  if (key === "dataConfidence" || label.includes("資料可信")) return 0.65;
  if (key === "chipMoney" || label.includes("籌碼") || label.includes("資金")) return 0.55;
  if (key === "strategyBacktest" || label.includes("回測策略")) return 0.55;
  if (key === "kline" || label.includes("K線")) return 0.55;
  if (key === "volume" || label.includes("量能") || label.includes("OBV")) return 0.55;
  if (key === "market" || label.includes("大盤")) return 0.55;
  if (key === "risk" || label.includes("風險")) return 0.55;
  return 0.55;
}

function brainComponentShortLabel(component) {
  const key = String(component?.key || "");
  const label = String(component?.label || "");
  if (key === "dataConfidence" || label.includes("資料可信")) return "資料可信";
  if (key === "chipMoney" || label.includes("籌碼") || label.includes("資金")) return "籌碼";
  if (key === "strategyBacktest" || label.includes("回測策略")) return "回測策略";
  if (key === "kline" || label.includes("K線")) return "K線";
  if (key === "volume" || label.includes("量能") || label.includes("OBV")) return "量能";
  if (key === "market" || label.includes("大盤")) return "大盤";
  if (key === "risk" || label.includes("風險")) return "風險";
  return label.replace(/^型態量能：/, "") || "條件";
}

function integratedBrainConditionRows(cached = {}) {
  const components = (Array.isArray(cached.brainV2?.components) ? cached.brainV2.components : [])
    .filter((component) => component?.key !== "formalModel" && !String(component?.label || "").includes("正式模型"));
  const conditions = Array.isArray(cached.conditions) ? cached.conditions : [];
  const duplicateLabels = new Set([
    "型態量能",
    "型態量能 本機核心分數",
    "正式模型分數",
    "資料可信度",
    "資料可信分數",
    "籌碼資金分數",
    "籌碼分數",
    "短線資金分數",
    "回測策略分數",
    "K線型態分數",
    "K線分數",
    "量能分數",
    "K線量能配合",
    "OBV 能量潮",
    "大盤強弱分數",
    "K線大盤強弱",
    "風險控管分數",
    "分數達買進門檻",
    "Learning to Rank 排名前段",
  ]);
  const coreRows = components.map((component) => {
    const score = Number(component?.score);
    const required = brainV2RequiredScore(component, cached);
    const auxiliary = component?.auxiliary;
    const scoreText = Number.isFinite(score)
      ? (auxiliary ? `${fmt(score * 100, 1)}% / 輔助` : `${fmt(score * 100, 1)}% / 需${fmt(required * 100, 0)}%`)
      : "無資料";
    return {
      label: brainComponentShortLabel(component),
      ok: component?.ok,
      value: component?.text || scoreText,
      source: "型態量能",
      borderline: Boolean(component?.borderline)
    };
  });
  const attentionRows = conditions
    .filter((row) => row && !String(row.label || "").startsWith("型態量能："))
    .filter((row) => !duplicateLabels.has(String(row.label || "")))
    .filter((row) => row.ok !== true)
    .slice(0, 8)
    .map((row) => ({
      label: row.label || "資料/規則",
      ok: row.ok,
      value: brainDecisionValue(row.value),
      source: row.source || ""
    }));
  if (coreRows.length || attentionRows.length) return [...coreRows, ...attentionRows];
  return conditions.slice(0, 8).map((row) => ({
    label: row.label || "條件",
    ok: row.ok,
    value: brainDecisionValue(row.value),
    source: row.source || ""
  }));
}

function brainSourceShort(source = "") {
  const text = String(source || "");
  if (text.includes("STOCK_DAY_ALL")) return "TWSE日線";
  if (text.includes("official T86")) return "TWSE法人";
  if (text.includes("MI_MARGN")) return "TWSE融資券";
  if (text.includes("t187ap05_L")) return "TWSE月營收";
  if (text.includes("BWIBBU_ALL")) return "TWSE估值";
  if (text.includes("TaiwanStockPrice")) return "FinMind日線";
  if (text.includes("TaiwanStockInstitutionalInvestorsBuySell")) return "FinMind法人";
  if (text.includes("TaiwanStockMarginPurchaseShortSale")) return "FinMind融資券";
  if (text.includes("TaiwanStockMonthRevenue")) return "FinMind月營收";
  if (text.includes("TaiwanStockPER")) return "FinMind估值";
  if (text.includes("financial statements")) return "FinMind財報";
  if (text.includes("Yahoo")) return "Yahoo備援";
  return text || "無資料";
}

function brainSourceStatusShort(status = "") {
  const text = String(status || "");
  if (text === "正式/授權來源") return "正式";
  if (text === "Yahoo fallback") return "備援";
  return text || "無資料";
}

function scoreGapText(component, decision) {
  const score = Number(component?.score);
  const required = brainV2RequiredScore(component, decision);
  const label = brainComponentShortLabel(component);
  if (!Number.isFinite(score)) return `${label}無資料`;
  return `${label}${fmt(score * 100, 1)}%，需${fmt(required * 100, 0)}%`;
}

function pendingBrainGapText(rowHealth = null) {
  const reason = String(rowHealth?.reason || "");
  if (reason.includes("等待正式模型")) return "等待真實資料";
  if (reason.includes("尚未取得判斷") || reason.includes("讀取中")) return "等型態量能";
  if (reason.includes("逾時") || reason.includes("暫時只觀察")) return "型態量能稍後補判斷";
  if (reason.includes("讀取失敗") || reason.includes("暫時未回應")) return "型態量能暫未回應";
  return "等待型態量能";
}

function turnStrongerText(stockCode, context = "analysis", rowHealth = null) {
  const cached = brainDecisionStatus(stockCode, context);
  if (!cached) return pendingBrainGapText(rowHealth);
  if (cached.loading) return pendingBrainGapText(rowHealth);
  if (cached.error) {
    const compact = compactBrainEntryReason(cached.error);
    if (compact.includes("型態量能讀取中") || compact.includes("型態量能暫時只觀察") || compact.includes("型態量能暫未回應")) {
      return pendingBrainGapText({ reason: cached.error });
    }
    return compact || pendingBrainGapText(rowHealth);
  }
  const components = (Array.isArray(cached.brainV2?.components) ? cached.brainV2.components : [])
    .filter((component) => component?.key !== "formalModel" && !String(component?.label || "").includes("正式模型"));
  const failed = components.filter((component) => component && component.ok === false);
  if (failed.length) return failed.slice(0, 3).map((component) => scoreGapText(component, cached)).join(" / ");
  const blockers = Array.isArray(cached.blockers) ? cached.blockers.filter(Boolean) : [];
  if (blockers.length) return compactBrainEntryReason(`型態量能規則：${blockers[0]}`);
  if (rowHealth?.ok === false) return compactBrainEntryReason(rowHealth.reason);
  if (cached.entryAllowed === true) return "條件已通過";
  return cached.actionLabel || cached.recommendation || "等確認";
}

function entryStrengthGapText(stockCode, context, rowHealth = null) {
  return turnStrongerText(stockCode, context, rowHealth);
}

function entryBrainReasonText(brainEntry, rowHealth) {
  return String(brainEntry?.reason || (rowHealth?.ok === false ? rowHealth.reason : "") || "");
}

function brainDecisionReasonText(brainDecision, cachedBrain) {
  const components = Array.isArray(brainDecision?.brainV2?.components) ? brainDecision.brainV2.components : [];
  const failed = components.filter((component) => component && component.ok === false && !component.auxiliary);
  if (failed.length) return failed.slice(0, 2).map((component) => scoreGapText(component, brainDecision)).join(" / ");
  // 2026-07-07:能買時原因寫「能買的原因」——列出通過的型態量能條件,不再只吐「進場條件通過」空話。
  const passed = components.filter((component) => component && component.ok === true && !component.auxiliary);
  if (passed.length) {
    return "型態量能過關：" + passed.slice(0, 4).map((component) => String(component.label || "").replace(/^型態量能：/, "")).join("、");
  }
  return compactBrainEntryReason(brainDecisionBlockReason(brainDecision, cachedBrain));
}

function entryDecisionWithBrain(baseDecision, rowHealth, backendCanBuy = null) {
  const baseText = String(baseDecision || "");
  // backendCanBuy 是 monsterOpenCheck 已經算好的真布林值(open.canBuy)。原本這裡
  // 只能靠 baseText.includes("可進場") 反推，任何一層改了中文文案措辭都可能讓
  // 這個比對悄悄失效卻不會報錯；改用真布林值當唯一真相來源，字串只留給顯示用。
  const backendReady = backendCanBuy === true || backendCanBuy === false;
  const backendOk = backendReady ? backendCanBuy : baseText.includes("可進場");
  const brainDecision = rowHealth?.brainDecision || null;
  const cachedBrain = rowHealth?.brainCached || null;
  if (brainDecision) {
    const reason = brainDecisionReasonText(brainDecision, cachedBrain);
    if (brainDecisionBlocksDecision(brainDecision, { blockWhenMissing: true, context: brainDecision.context || rowHealth?.brainContext || "entry" })) {
      return {
        decision: brainDecision.observeOnly ? "只觀察" : "先不進",
        method: "型態量能",
        tone: brainDecision.observeOnly ? "warn" : "down",
        reason,
        canEnter: false
      };
    }
    if (brainDecision.entryAllowed === true) {  // 布林為唯一真相(見上方註解);後端 entryAllowed 恆為 bool,移除多餘的字串 includes 回退
      return {
        decision: backendOk ? "可進場" : "等確認",
        method: "型態量能",
        tone: backendOk ? "up" : "warn",
        reason,
        canEnter: backendOk
      };
    }
    return {
      decision: "先不進",
      method: "型態量能",
      tone: "down",
      reason,
      canEnter: false
    };
  }
  if (!rowHealth || rowHealth.ok !== false) {
    return {
      decision: "等確認",
      method: "等型態量能",
      tone: "warn",
      reason: compactBrainEntryReason(brainDecisionBlockReason(null, cachedBrain)),
      canEnter: false
    };
  }
  const reason = compactBrainEntryReason(rowHealth.reason);
  if (rowHealth.mode !== "brain_observe_only") {
    return {
      decision: "只觀察",
      method: "暫停決策",
      tone: "warn",
      reason,
      canEnter: false
    };
  }
  const rawReason = String(rowHealth.reason || "");
  const brainPending = rawReason.includes("尚未取得判斷") ||
    rawReason.includes("讀取中") ||
    rawReason.includes("逾時") ||
    rawReason.includes("暫時只觀察");
  if (brainPending) {
    return {
      decision: "等確認",
      method: brainBlockedEntryMethod(rawReason, baseDecision),
      tone: "warn",
      reason,
      canEnter: false
    };
  }
  const dataBlocked = rawReason.includes("資料不足") ||
    rawReason.includes("核心資料") ||
    rawReason.includes("讀取失敗") ||
    rawReason.includes("暫時未回應");
  if (dataBlocked) {
    return {
      decision: "只觀察",
      method: "暫停決策",
      tone: "warn",
      reason,
      canEnter: false
    };
  }
  return {
    decision: "先不進",
    method: brainBlockedEntryMethod(rawReason, baseDecision),
    tone: "down",
    reason,
    canEnter: false
  };
}

function compactEntryReason(parts, limit = 3) {
  const compact = [];
  for (const part of parts || []) {
    const text = String(part || "").trim();
    if (text && text !== "-" && !compact.includes(text)) compact.push(text);
  }
  return compact.length ? compact.slice(0, limit).join(" / ") : "-";
}

async function loadMonsterScores() {
  monsterBackendState = { ...monsterBackendState, loading: true, error: "" };
  renderMonsterRadar();
  try {
    const [scoreResponse, intradayResponse] = await Promise.all([
      fetch("/api/monster-scores?limit=100"),
      fetch("/api/monster-intraday")
    ]);
    const payload = await scoreResponse.json();
    const intradayPayload = await intradayResponse.json();
    if (!scoreResponse.ok || !payload.ok) throw new Error(payload.error || "妖股掃描結果讀取失敗");
    if (intradayResponse.ok && intradayPayload) monsterIntradayState = intradayPayload;
    monsterBackendState = { loading: false, error: "", payload };
    writeUiCache("stock-vibe-monster-payload-v1", payload);
    if (intradayResponse.ok && intradayPayload) writeUiCache("stock-vibe-monster-intraday-v1", intradayPayload);
    showMonsterProgressFromPayload(payload);
  } catch (error) {
    monsterBackendState = { loading: false, error: friendlyError(error), payload: monsterBackendState.payload };
    showMonsterProgressFromPayload(monsterBackendState.payload);
  }
  renderMonsterRadar();
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
    box.innerHTML = `<p class="radar-score-record-status">正在結算分數區間戰績…</p>`;
    return;
  }
  if (radarScoreTrackState.error && !buckets.length) {
    box.innerHTML = `<p class="radar-score-record-status">${escapeHtml(radarScoreTrackState.error)}</p>`;
    return;
  }
  if (!buckets.length) {
    box.innerHTML = `<p class="radar-score-record-status">尚無可結算的雷達候選；累積後會依相同成交口徑顯示。</p>`;
    return;
  }
  const overall = payload.overall || {};
  const eligible = payload.eligible || {};
  const deploymentReadiness = payload.deploymentReadiness || {};
  const readinessLive = deploymentReadiness.live || {};
  const readinessProxy = deploymentReadiness.proxy || {};
  const readinessWalkForward = deploymentReadiness.walkForward || {};
  const readinessReady = deploymentReadiness.formalReady === true;
  const readinessLabel = deploymentReadiness.readinessDate
    ? (readinessReady ? "可上線" : "只觀察")
    : "尚未驗證";
  const readinessDetail = deploymentReadiness.readinessDate
    ? `${deploymentReadiness.readinessDate}｜盤中可成交報價確認 n=${Number(readinessLive.settled || 0)}、命中 ${radarTrackRate(readinessLive.targetHitRate)}、淨報酬 ${radarTrackRate(readinessLive.avgNetReturn, 2)}、PF ${readinessLive.profitFactor == null ? "-" : Number(readinessLive.profitFactor).toFixed(2)}｜隔日開盤代理 n=${Number(readinessProxy.settled || 0)}、淨報酬 ${radarTrackRate(readinessProxy.avgNetReturn, 2)}、PF ${readinessProxy.profitFactor == null ? "-" : Number(readinessProxy.profitFactor).toFixed(2)}｜日線代理 walk-forward 淨報酬 ${radarTrackRate(readinessWalkForward.avgNetReturn, 2)}、PF ${readinessWalkForward.profitFactor == null ? "-" : Number(readinessWalkForward.profitFactor).toFixed(2)}、命中提升 ${radarTrackRate(readinessWalkForward.precisionLift, 2)}｜連續通過 ${Number(deploymentReadiness.consecutivePassDays || 0)}/${Number(deploymentReadiness.requiredPassDays || 5)} 日`
    : "等待交易日收盤或盤前驗證建立正式戰績紀錄";
  const ruleConfig = payload.ruleConfig || {};
  const configText = ruleConfig.source === "walk_forward_approved"
    ? "walk-forward 權重已通過並套用"
    : ruleConfig.source === "walk_forward_observation"
      ? "walk-forward 觀察模式，正式分數維持原權重"
      : "目前使用內建規則權重";
  const entryGuardrail = ruleConfig.entryGuardrailCalibration || {};
  const guardrailApproved = entryGuardrail.approved === true;
  const guardrailRecent = entryGuardrail.recentOos || {};
  const guardrailPressure = entryGuardrail.pressureTestOos || {};
  const guardrailRecentResult = guardrailRecent.recommended || {};
  const guardrailPressureResult = guardrailPressure.recommended || {};
  const guardrailChecks = entryGuardrail.adoptionChecks || {};
  const guardrailCheckValues = Object.values(guardrailChecks);
  const guardrailHasCalibration = Boolean(
    entryGuardrail.recommendedKey || guardrailCheckValues.length
  );
  const guardrailStatus = guardrailApproved
    ? "已採用"
    : guardrailHasCalibration ? "觀察中" : "尚未校準";
  const guardrailLabel = entryGuardrail.recommendedLabel || "基準（不限制）";
  const guardrailDetail = guardrailHasCalibration
    ? `建議：${guardrailLabel}｜近期 OOS ${Number(guardrailRecentResult.trades || 0)} 筆、命中提升 ${radarTrackRate(guardrailRecent.precisionLift, 2)}、淨報酬 ${radarTrackRate(guardrailRecentResult.avgNetReturn, 2)}、PF ${guardrailRecentResult.profitFactor == null ? "-" : Number(guardrailRecentResult.profitFactor).toFixed(2)}｜壓力測試 ${Number(guardrailPressureResult.trades || 0)} 筆、淨報酬 ${radarTrackRate(guardrailPressureResult.avgNetReturn, 2)}、PF ${guardrailPressureResult.profitFactor == null ? "-" : Number(guardrailPressureResult.profitFactor).toFixed(2)}｜穩定度 ${radarTrackRate(entryGuardrail.ruleStability, 1)}｜門檻 ${guardrailCheckValues.filter(Boolean).length}/${guardrailCheckValues.length} 通過`
    : "等待規則 walk-forward 產生觀察結果；未核准前不影響正式買賣";
  const entryModes = payload.entryModes || {};
  const entryModePerformance = payload.entryModePerformance || {};
  const confirmedEligible = entryModePerformance.intradayConfirmed?.eligible || {};
  const policy = payload.policy || {};
  const diagnostics = payload.diagnostics || {};
  const bestBucket = diagnostics.bestEligibleBucket || null;
  const regimeGroups = Array.isArray(diagnostics.regimeGroups) ? diagnostics.regimeGroups : [];
  const regimeCalibration = ruleConfig.regimeThresholdCalibration || {};
  const calibratedRegimes = regimeCalibration.regimes || {};
  const confirmedEntries = Object.entries(entryModes)
    .filter(([key]) => key.startsWith("intraday_confirmed") || key === "intraday_execution_analysis")
    .reduce((sum, [, count]) => sum + Number(count || 0), 0);
  const rows = buckets.map((bucket) => {
    const ci = Array.isArray(bucket.targetHitConfidence95) ? bucket.targetHitConfidence95 : [];
    const eligibleStats = bucket.eligible || {};
    const lowSample = Number(bucket.settled || 0) < 10;
    return `
      <tr class="${lowSample ? "low-sample" : ""}">
        <td>${escapeHtml(bucket.label || "-")}${lowSample ? "（樣本少）" : ""}</td>
        <td>${Number(bucket.settled || 0)}</td>
        <td>${radarTrackRate(bucket.targetHitRate)}</td>
        <td>${radarTrackRate(ci[0])}～${radarTrackRate(ci[1])}</td>
        <td class="${Number(bucket.avgNetReturn) >= 0 ? "up" : "down"}">${radarTrackRate(bucket.avgNetReturn, 2)}</td>
        <td>${radarTrackRate(bucket.stopRate)}</td>
        <td>${Number(eligibleStats.settled || 0)} / ${radarTrackRate(eligibleStats.targetHitRate)}</td>
      </tr>`;
  }).join("");
  const diagnosticText = diagnostics.scoreMonotonic === false
    ? `目前分數與命中率尚未呈單調關係，分數只作規則排序、不能當上漲機率。${bestBucket ? `現有可買樣本中，淨損益最佳區間為 ${bestBucket.label}（${radarTrackRate(bestBucket.avgNetReturn, 2)}）。` : ""}`
    : diagnostics.scoreMonotonic === true
      ? "目前已有足夠樣本的分數區間呈單調關係，仍須持續觀察。"
      : "分數區間樣本仍不足以判斷單調性。";
  const regimeRows = regimeGroups.map((group) => {
    const calibration = calibratedRegimes[group.key] || {};
    const effectiveThreshold = Number(calibration.effectiveThreshold ?? policy.regimeThresholds?.[group.key] ?? policy.minimumFormalScore ?? 60);
    const approved = calibration.approved === true;
    return `
      <tr>
        <td>${escapeHtml(group.label || group.key || "-")}</td>
        <td>${Number(group.settled || 0)}</td>
        <td>${radarTrackRate(group.targetHitRate)}</td>
        <td class="${Number(group.avgNetReturn) >= 0 ? "up" : "down"}">${radarTrackRate(group.avgNetReturn, 2)}</td>
        <td>${group.profitFactor == null ? "-" : Number(group.profitFactor).toFixed(2)}</td>
        <td>${effectiveThreshold.toFixed(0)} 分</td>
        <td>${approved ? "已採用" : "觀察"}</td>
      </tr>`;
  }).join("");
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
        <tbody>${rows}</tbody>
      </table>
    </div>
    ${regimeRows ? `
      <h4 class="radar-score-subtitle">市場狀態戰績</h4>
      <div class="radar-score-table-wrap">
        <table class="radar-score-table radar-regime-table">
          <thead><tr><th>市場狀態</th><th>已結算</th><th>命中率</th><th>平均淨損益</th><th>獲利因子</th><th>正式門檻</th><th>狀態</th></tr></thead>
          <tbody>${regimeRows}</tbody>
        </table>
      </div>
    ` : ""}
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
    writeUiCache("stock-vibe-radar-score-track-v2", payload);
  } catch (error) {
    radarScoreTrackState = {
      ...radarScoreTrackState,
      loading: false,
      error: friendlyError(error),
    };
  }
  renderRadarScoreTrackRecord();
}

function showMonsterProgressFromPayload(payload) {
  if (!payload?.ok && !payload?.candidates) return;
  const candidates = Array.isArray(payload.candidates) ? payload.candidates.length : 0;
  const quickCandidates = Number(payload.quickCandidates ?? payload.count ?? candidates);
  const scoredCandidates = Number(payload.scoredCandidates ?? payload.count ?? candidates);
  const universeTotal = Number(payload.universeTotal || 0);
  const liquidUniverse = Number(payload.liquidUniverse || payload.scanned || 0);
  const processed = Math.max(scoredCandidates, quickCandidates, liquidUniverse, candidates);
  const total = Math.max(processed, universeTotal || processed);
  renderMonsterScanProgress({
    running: false,
    phase: "妖股最新結果",
    total,
    processed: total,
    saved: candidates,
    errors: Number((payload.errors || []).length || 0),
    universeTotal,
    liquidUniverse,
    quickCandidates,
    scoredCandidates,
    message: `全市場 ${fmt(universeTotal, 0)} 檔｜流動性候選 ${fmt(liquidUniverse, 0)} 檔｜快速候選 ${fmt(quickCandidates, 0)} 檔｜純規則評分 ${fmt(scoredCandidates, 0)} 檔｜目前顯示 ${fmt(candidates, 0)} 檔`
  });
}

async function loadMonsterIntraday() {
  try {
    const response = await fetch("/api/monster-intraday");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "盤中確認讀取失敗");
    monsterIntradayState = payload || { ok: true, quotes: {} };
    writeUiCache("stock-vibe-monster-intraday-v1", monsterIntradayState);
    renderMonsterRadar();
  } catch (error) {
    monsterIntradayState = { ...monsterIntradayState, ok: false, error: friendlyError(error) };
  }
}

function renderMonsterScanProgress(status) {
  const box = document.getElementById("monsterScanProgress");
  if (!box) return;
  const title = document.getElementById("monsterScanProgressTitle");
  const text = document.getElementById("monsterScanProgressText");
  const bar = document.getElementById("monsterScanProgressBar");
  const detail = document.getElementById("monsterScanProgressDetail");
  const running = Boolean(status?.running);
  const phase = String(status?.phase || "");
  const isScorePhase = phase.includes("型態量能排序") || phase.includes("候選評分") || phase.includes("規則評分");
  const total = Number(status?.total || 0);
  const processed = Number(status?.processed || 0);
  const percent = total ? Math.min(100, Math.round((processed / total) * 100)) : 0;
  box.hidden = !running && !status?.message;
  if (title) title.textContent = status?.phase || (running ? "掃描中" : "掃描狀態");
  if (text) text.textContent = total ? `${percent}%` : "準備中";
  if (bar) bar.style.width = `${percent}%`;
  if (detail) {
    const current = status?.current ? `，目前 ${status.current}` : "";
    const saved = Number(status?.saved || 0);
    const errors = Number(status?.errors || 0);
    const chainText = Number(status?.universeTotal || 0) && Number(status?.liquidUniverse || 0)
      ? `全市場 ${fmt(status.universeTotal, 0)} 檔 → 流動性候選 ${fmt(status.liquidUniverse, 0)} 檔 → 快速候選 ${fmt(saved, 0)} 檔 → 純規則評分 ${isScorePhase ? fmt(total, 0) : 0} 檔`
      : "";
    const summaryText = Number(status?.universeTotal || 0)
      ? `全市場 ${fmt(status.universeTotal, 0)} 檔｜流動性候選 ${fmt(status.liquidUniverse || 0, 0)} 檔｜快速候選 ${fmt(status.quickCandidates || saved || 0, 0)} 檔｜純規則評分 ${fmt(status.scoredCandidates ?? saved, 0)} 檔${errors ? `｜錯誤 ${fmt(errors, 0)} 筆` : ""}`
      : "";
    detail.textContent = status?.message || summaryText || chainText || `已建立 ${saved} 筆候選，錯誤 ${errors} 筆${current}`;
  }
  renderMonsterScanStages(status);
}

function updateMonsterScanStage(stage, processed, total) {
  const row = document.querySelector(`#monsterScanStages [data-stage="${stage}"]`);
  if (!row) return;
  const safeTotal = Math.max(0, Number(total || 0));
  const safeProcessed = Math.max(0, Math.min(Number(processed || 0), safeTotal || Number(processed || 0)));
  const percent = safeTotal ? Math.min(100, Math.round((safeProcessed / safeTotal) * 100)) : 0;
  const label = row.querySelector("span");
  const bar = row.querySelector(".scan-progress-bar");
  if (label) label.textContent = `${fmt(safeProcessed, 0)} 檔`;
  if (bar) bar.style.width = `${percent}%`;
}

function renderMonsterScanStages(status) {
  const phase = String(status?.phase || "");
  const running = Boolean(status?.running);
  // "妖股最新結果" 是重新整理頁面、從已存掃描結果載入時用的 phase 字串
  // (見 showMonsterProgressFromPayload)，不是掃描過程中的階段名稱，但一樣
  // 代表「已經有完整的最終結果」，要跟"完成"視為同一種情況，否則快速候選/
  // 候選評分這兩格會因為文字比對不到，永遠顯示 0，即使數值早就正確算出來了。
  const completed = !running && (phase.includes("完成") || phase.includes("妖股最新結果"));
  const universeTotal = Number(status?.universeTotal || 0);
  const liquidUniverse = Number(status?.liquidUniverse || 0);
  const total = Number(status?.total || 0);
  const processed = Number(status?.processed || 0);
  const saved = Number(status?.saved || 0);
  const repair = status?.repair || {};
  const repairTotal = Number(repair.repairCandidates ?? (phase.includes("資料補齊") ? total : 0));
  const repairDone = Number((repair.repaired?.length || 0) + (repair.failed?.length || 0));
  const quickCandidates = Number(status?.quickCandidates ?? status?.saved ?? 0);
  const scoredCandidates = Number(status?.scoredCandidates ?? status?.saved ?? 0);
  const isRepair = phase.includes("資料補齊") || phase.includes("補齊");
  const isQuick = phase.includes("快速");
  const isScore = phase.includes("型態量能排序") || phase.includes("候選評分") || phase.includes("規則評分");
  updateMonsterScanStage("liquid", liquidUniverse, liquidUniverse);
  updateMonsterScanStage("repair", completed || isQuick || isScore ? repairTotal : isRepair ? processed : repairDone, repairTotal);
  updateMonsterScanStage("quick", completed ? quickCandidates : isScore ? quickCandidates || total : isQuick ? processed : 0, liquidUniverse || total || quickCandidates);
  updateMonsterScanStage("score", completed ? scoredCandidates : isScore ? processed : 0, isScore ? total : completed ? scoredCandidates : 0);
}

async function pollMonsterScanStatus() {
  try {
    const response = await fetch("/api/monster-scan/status");
    const status = await response.json();
    if (!response.ok || !status.ok) throw new Error(status.error || "掃描進度讀取失敗");
    monsterScanPollFailCount = 0;
    renderMonsterScanProgress(status);
    monsterBackendState = { ...monsterBackendState, loading: Boolean(status.running), error: "" };
    renderMonsterRadar();
    if (!status.running) {
      stopMonsterScanPolling();
      const button = document.getElementById("scanMonsterMarket");
      if (button) {
        button.disabled = false;
        button.textContent = "手動掃描短線妖股";
      }
      monsterScanSeenFinishedAt = String(status.finishedAt || "");
      await loadMonsterScores();
      // 掃描失敗結束時，loadMonsterScores 內的 showMonsterProgressFromPayload
      // 會用「上一次成功掃描」的結果把失敗訊息在 1 秒內蓋掉，使用者完全
      // 看不到失敗。失敗狀態要蓋回來，名單顯示舊結果沒關係，但狀態列
      // 必須誠實。
      if (String(status.phase || "").includes("失敗")) renderMonsterScanProgress(status);
    }
  } catch (error) {
    // 後端忙碌(例如重訓中)時，這支 1 秒輪詢很容易單次逾時/連線失敗，但
    // 掃描本身通常還在背景繼續跑。舊版一失敗就立刻 stopMonsterScanPolling
    // 並把按鈕解鎖成「手動掃描短線妖股」，使用者會誤以為掃描已經停止或
    // 失敗，甚至因此重按造成併發衝突；錯誤字樣也只有下一輪成功才會被蓋掉。
    // 改成累計連續失敗次數，未達門檻前只靜默重試(輪詢繼續、按鈕與狀態
    // 維持原樣)，真的連續多次讀不到才視為掃描失聯、停止輪詢並提示使用者。
    monsterScanPollFailCount += 1;
    if (monsterScanPollFailCount < MONSTER_SCAN_POLL_FAIL_LIMIT) return;
    stopMonsterScanPolling();
    monsterBackendState = { ...monsterBackendState, loading: false, error: `${friendlyError(error)}（已連續 ${monsterScanPollFailCount} 次讀取失敗，30 秒後會自動重新嘗試接管進度）` };
    const button = document.getElementById("scanMonsterMarket");
    if (button) {
      button.disabled = false;
      button.textContent = "手動掃描短線妖股";
    }
    renderMonsterRadar();
  }
}

function startMonsterScanPolling() {
  stopMonsterScanPolling();
  monsterScanPollFailCount = 0;
  pollMonsterScanStatus();
  monsterScanPollTimer = setInterval(pollMonsterScanStatus, 1000);
}

// 15:00 自動掃描完成後，已開啟的頁面要自動更新妖股名單：舊版只有手動按
// 掃描按鈕才會輪詢狀態，自動掃描(或另一個分頁觸發的掃描)完成後，這個頁面
// 的名單永遠停在舊結果；頁面重整時若自動掃描進行中也看不到進度。
// 掛在 30 秒 market clock 上：偵測「掃描進行中」就接上進度輪詢，偵測
// 「finishedAt 變了」就重載名單。null=開頁尚未初始化，首次只記基準不重載。
let monsterScanSeenFinishedAt = null;

async function checkBackgroundMonsterScan() {
  if (monsterScanPollTimer) return; // 已在輪詢(手動掃描中)，不重複接管
  try {
    const response = await fetch("/api/monster-scan/status");
    const status = await response.json();
    if (!response.ok || !status.ok) return;
    if (status.running) {
      startMonsterScanPolling();
      return;
    }
    const finishedAt = String(status.finishedAt || "");
    if (monsterScanSeenFinishedAt === null) {
      monsterScanSeenFinishedAt = finishedAt;
      return;
    }
    if (finishedAt && finishedAt !== monsterScanSeenFinishedAt) {
      monsterScanSeenFinishedAt = finishedAt;
      await loadMonsterScores();
    }
  } catch {
    // 安靜跳過，30 秒後 market clock 會再試
  }
}

function stopMonsterScanPolling() {
  if (monsterScanPollTimer) {
    clearInterval(monsterScanPollTimer);
    monsterScanPollTimer = null;
  }
}

async function runMonsterMarketScan() {
  const button = document.getElementById("scanMonsterMarket");
  if (button) {
    button.disabled = true;
    button.textContent = "補資料中";
  }
  monsterBackendState = { ...monsterBackendState, loading: true, error: "" };
  renderMonsterScanProgress({ running: true, phase: "資料補齊", total: 0, processed: 0, saved: 0, errors: 0, message: "掃描前先補齊正式資料來源" });
  renderMonsterRadar();
  // 2026-07-07 使用者要求「完全拿掉」Perplexity 主動搜尋題材股：不再於掃描時觸發。
  try {
    const response = await fetch("/api/monster-scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit: 300, scoreLimit: 100 })
    });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "全市場掃描失敗");
    renderMonsterScanProgress(payload.status || { running: true, phase: "準備掃描", total: 0, processed: 0 });
    startMonsterScanPolling();
  } catch (error) {
    monsterBackendState = { loading: false, error: friendlyError(error), payload: monsterBackendState.payload };
    renderMonsterScanProgress({ running: false, phase: "掃描失敗", message: friendlyError(error) });
    if (button) {
      button.disabled = false;
      button.textContent = "手動掃描短線妖股";
    }
    renderMonsterRadar();
    return;
  }
  renderMonsterRadar();
}

async function runThemeSearch() {
  // 2026-07-07 使用者要求「完全拿掉」Perplexity 主動搜尋題材股：停用觸發與顯示。
  // 端點/設定保留(Perplexity 金鑰可能與 AI 報告共用),但雷達不再呼叫或顯示題材股。
  const box = document.getElementById("themeSearchResult");
  if (box) { box.hidden = true; box.innerHTML = ""; }
}

function renderThemeSearch() {
  const box = document.getElementById("themeSearchResult");
  if (!box) return;
  if (!themeSearchState.attempted) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  if (themeSearchState.loading) {
    box.innerHTML = `<p class="section-note">Perplexity 正在搜尋目前市場題材股...</p>`;
    return;
  }
  if (themeSearchState.error) {
    const isNotConfigured = /API Key|尚未設定|新聞摘要尚未啟用/.test(themeSearchState.error);
    box.innerHTML = isNotConfigured
      ? `<p class="section-note">題材股搜尋尚未啟用：${escapeHtml(themeSearchState.error)}（可到<a href="./settings.html">資料設定</a>開啟 Perplexity 新聞摘要）</p>`
      : `<p class="section-note">題材股搜尋失敗：${escapeHtml(themeSearchState.error)}</p>`;
    return;
  }
  const bodyHtml = escapeHtml(themeSearchState.text || "").replace(/\n/g, "<br>");
  box.innerHTML = `
    <p class="section-note">Perplexity 主動搜尋題材股（${escapeHtml(themeSearchState.generatedAt || "")}，純參考、不影響評分與候選池）</p>
    <div class="explanation-card">${bodyHtml}</div>
  `;
}

function monsterOpenCheck(item, signalByCode = null) {
  const flow = taiwanBuyFlowStage();
  // signalByCode 由呼叫端(renderMonsterRadar)先建好一次共用,避免每列各跑一次
  // allSignals()(那會把全持股技術指標整套重算)。沒傳(如自測)才退回原本線性 find。
  const signal = signalByCode
    ? signalByCode.get(item.stock.code)
    : allSignals().find((s) => s.stock.code === item.stock.code);
  const { rows } = signal || {};
  const latest = rows?.at(-1);
  const previous = rows?.at(-2);
  const intraday = item.intraday || null;
  const hasBackendDecision = Boolean(intraday && Object.prototype.hasOwnProperty.call(intraday, "canBuy"));
  const backendCanBuy = Boolean(intraday?.canBuy);
  const backendSetupOk = Boolean(intraday?.setupOk);
  const backendStatus = String(intraday?.status || "");
  const openGap = intraday?.openGap == null
    ? (latest && previous?.close ? ((latest.open - previous.close) / previous.close) * 100 : 0)
    : Number(intraday.openGap);
  const openHighFade = intraday?.openHighFade == null
    ? (latest ? latest.open > (previous?.close || latest.open) && latest.close < latest.open * 0.985 : false)
    : Boolean(intraday.openHighFade);
  const volumeRatio = intraday?.volumeRatio == null ? item.volumeRatio : Number(intraday.volumeRatio);
  const volumeThreshold = intraday?.volumeThreshold == null ? 1.4 : Number(intraday.volumeThreshold);
  const volumeContinue = intraday?.volumeContinue == null
    ? (volumeRatio == null ? item.buyAllowed : Number(volumeRatio) >= volumeThreshold)
    : Boolean(intraday.volumeContinue);
  const pullbackPrice = Number(intraday?.pullbackPrice || item.pullbackPrice || 0);
  const pullbackHold = intraday?.pullbackHold == null
    ? (pullbackPrice > 0 && Number(item.currentPrice || latest?.close || 0) >= pullbackPrice * 0.995 && Number(item.currentPrice || latest?.close || 0) <= Number(item.buyTrigger || 0) * 1.01)
    : Boolean(intraday.pullbackHold);
  const pullbackVolumeOk = intraday?.pullbackVolumeOk == null
    ? (volumeRatio == null ? false : Number(volumeRatio) >= volumeThreshold * 0.75)
    : Boolean(intraday.pullbackVolumeOk);
  const intradayRebound = intraday?.intradayRebound == null ? false : Boolean(intraday.intradayRebound);
  const vRebound = intraday?.vRebound == null ? false : Boolean(intraday.vRebound);
  const lateBreakoutBlocked = Boolean(intraday?.lateBreakoutBlocked);
  const breakoutFadeBlocked = Boolean(intraday?.breakoutFadeBlocked);
  const spreadBlocked = Boolean(intraday?.spreadBlocked);
  const slippageBlocked = Boolean(intraday?.slippageBlocked);
  const capacityBlocked = Boolean(intraday?.capacityBlocked);
  const entryDriftBlocked = Boolean(intraday?.entryDriftBlocked);
  const rewardRiskBlocked = Boolean(intraday?.rewardRiskBlocked);
  const setupType = String(intraday?.setupType || "");
  const breakout = intraday?.breakout == null
    ? (latest ? latest.high >= item.buyTrigger || latest.close >= item.buyTrigger : false)
    : Boolean(intraday.breakout);
  // item.stopPrice 缺值時不能落到 0：currentPrice <= 0 恆為 false，等於
  // 靜默關閉停損保護。跟後端 server.py 比照同一套慣例，用前收盤(item.close)
  // 的 93% 當保底停損。
  const fallbackStopPrice = Number(item.stopPrice) > 0 ? Number(item.stopPrice) : (Number(item.close) > 0 ? Number(item.close) * 0.93 : 0);
  const stopBroken = intraday?.stopBroken == null
    ? (fallbackStopPrice > 0 && Number(item.currentPrice || 0) <= fallbackStopPrice)
    : Boolean(intraday.stopBroken);
  const tooHigh = openGap > 5;
  let action = "等待開盤確認";
  let tone = "";
  // flow.label 不能混進 checks：compactMonsterOpenChecks 用關鍵字掃這個
  // 陣列，時段文字裡的「V轉」「量能」會被誤判成條件成立/不成立。
  // 時段資訊由回傳物件的 flow 欄位獨立提供。
  const checks = [
    tooHigh ? "跳空超過 5%，禁止追價" : `跳空 ${pct(openGap)}，未超過追價上限`,
    openHighFade ? "開高走低，取消買進" : "沒有明顯開高走低",
    volumeContinue ? "量能有延續" : `量能 ${fmt(volumeRatio, 2)} / 門檻 ${fmt(volumeThreshold, 2)}`,
    breakout ? "有放量突破條件" : "尚未放量突破",
    pullbackHold ? "回測買點有守住" : "未到回測買點",
    pullbackVolumeOk ? "低接量能達低標" : "低接量能未確認",
    intradayRebound ? "盤中有止跌反彈" : "尚未止跌反彈",
    vRebound ? "V轉反彈成立" : "尚未V轉",
    breakoutFadeBlocked ? "突破後離高點過遠，暫不追" : "突破仍維持高檔強勢",
    spreadBlocked ? `買賣價差 ${fmt(intraday?.bidAskSpreadPct, 2)}%，過大` : "買賣價差未超標",
    slippageBlocked ? `預估滑價 ${fmt(intraday?.estimatedSlippagePct, 2)}%，過高` : "預估滑價未超標",
    capacityBlocked ? "即時成交量不足安全成交一張" : "一張成交容量足夠",
    entryDriftBlocked ? `成交價偏離買點 ${fmt(intraday?.entryDriftPct, 2)}%，禁止追價` : "成交價未超過追價上限",
    rewardRiskBlocked ? `成本後風報 ${fmt(intraday?.netRewardRiskRatio, 2)}，未達門檻` : "成本後風報達標",
    lateBreakoutBlocked ? "10:00 後不追突破" : "未禁止進場型態",
    stopBroken ? "跌破停損，不買" : "未跌破停損"
  ];

  if (hasBackendDecision) {
    if (backendCanBuy) {
      action = setupType === "pullback" ? "回測低接，可買 1 張"
        : setupType === "v_rebound" ? "V轉低接，可買 1 張"
        : "放量突破，可買 1 張";
      tone = "up";
    } else if (intraday?.windowBlocked) {
      // 不在進場時段（09:30 前 / 13:15 後 / 休市）：即使型態成立也要先
      // 顯示時段封鎖原因，不能讓 setupOk 分支顯示「等突破確認」誤導
      action = backendStatus || String(intraday?.entryWindowLabel || "不在進場時段");
      tone = "down";
    } else if (lateBreakoutBlocked) {
      action = backendStatus || "10:00 後不追突破，等回測/V轉";
      tone = "warn";
    } else if (breakoutFadeBlocked) {
      action = backendStatus || "突破後離高點過遠，等待重新轉強";
      tone = "warn";
    } else if (spreadBlocked || slippageBlocked || capacityBlocked) {
      action = backendStatus || "成交成本或容量不適合，僅觀察";
      tone = "down";
    } else if (entryDriftBlocked || rewardRiskBlocked) {
      action = backendStatus || "成交價或成本後風報不適合，僅觀察";
      tone = "down";
    } else if (backendSetupOk) {
      action = setupType === "pullback" ? "等低接確認" : setupType === "v_rebound" ? "等V轉確認" : "等突破確認";
      tone = "warn";
    } else if (backendStatus) {
      action = backendStatus;
      tone = backendStatus.includes("未通過") ? "down" : "warn";
    } else {
      action = "先不買";
      tone = "down";
    }
  } else if (stopBroken) {
    action = "跌破停損，不買";
    tone = "down";
  } else if (tooHigh) {
    action = "禁止追價";
    tone = "down";
  } else if (openHighFade) {
    action = "取消買進";
    tone = "down";
  } else if (item.buyAllowed && volumeContinue && breakout) {
    action = "等待後端確認";
    tone = "warn";
  } else if (item.score >= 65) {
    action = "等突破/回測/V轉確認";
  }

  return {
    ...item,
    action,
    tone,
    openGap,
    flow,
    canBuy: backendCanBuy,
    setupOk: backendSetupOk,
    backendStatus,
    setupType,
    checks,
    hasIntradayQuote: intraday ? Boolean(intraday.hasIntradayQuote) : false
  };
}

function tenureBadgeText(item) {
  // 進榜天數 badge：純顯示雷達候選池生命週期，不影響任何買賣判斷。
  // 目前無足夠已結算樣本驗證「進榜天數 vs 命中率」關係(measure_tenure_hit_rate
  // 樣本不足)，所以只顯示中性天數/輪次，不寫任何未驗證的勝率敘事。
  const t = item?.tenure;
  if (!t || !Number(t.daysOnRadar)) return "";
  const parts = [`進榜 ${t.daysOnRadar} 天`];
  if (Number(t.rounds) > 1) parts.push(`第 ${t.rounds} 輪`);
  const peakNote = t.isPeakToday
    ? "今日榜內新高"
    : (t.peakScoreDate ? `峰值 ${t.peakScoreDate}` : "");
  const cls = t.isPeakToday ? "tenure-badge peak-today" : "tenure-badge";
  const title =
    `以雷達候選池(純規則評分前段)為範圍：已連續在榜 ${t.daysOnRadar} 個掃描日` +
    (Number(t.rounds) > 1 ? `，近期進出榜 ${t.rounds} 輪` : "") +
    (t.firstSeen ? `，最早出現 ${t.firstSeen}` : "") +
    (t.isPeakToday
      ? "，今日分數為榜內新高"
      : (t.peakScore != null ? `，榜內最高分 ${t.peakScore}（${t.peakScoreDate}）` : "")) +
    "。掉出候選池不代表漲浪結束。";
  return `<span class="${cls}" title="${escapeHtml(title)}">${escapeHtml(parts.join("·"))}${
    peakNote ? ` <small class="tenure-peak">${escapeHtml(peakNote)}</small>` : ""
  }</span>`;
}

// 大漲版:「大漲相」是透明的參考標記——用既有欄位(突破點火/爆量/5日強勢/族群領漲)
// 判斷型態上比較有機會續強搏 +20%,純顯示、不動模型/門檻/買賣判斷,也不是保證。
function bigMoverTraits(item) {
  const t = [];
  if (item?.surgeSetup) t.push("突破點火");
  if (Number(item?.volumeRatio) >= 3) t.push("爆量");        // 3倍以上才算爆量(2倍在妖股候選裡太普通)
  if (Number(item?.change5) >= 8) t.push("5日強勢");         // 5日漲≥8%才算真動能
  if (item?.sectorHot || Number(item?.sectorExcessRet5) >= 4) t.push("族群領漲");
  return t;
}
function isBigMover(item) {
  // 4項強勢特徵中≥3項才標大漲相——寧缺勿濫,標記要能真的「跳出來」而非半數都有
  return bigMoverTraits(item).length >= 3;
}
function bigMoverBadge(item) {
  if (!isBigMover(item)) return "";
  const t = bigMoverTraits(item).join("、");
  return `<span class="big-mover-tag" title="大漲相(${escapeHtml(t)})：型態/量能/動能偏強,搏 +20% 的參考標記,非買進保證，不影響能不能買的判斷">🚀大漲相</span>`;
}

function monsterRiskBadges(item) {
  // 高風險型態旗標(倒貨/轉弱/過熱)——danger 旗標會在後端降級為觀察,
  // warn 旗標維持提醒使用者避開追高。
  const flags = Array.isArray(item?.riskFlags) ? item.riskFlags : [];
  if (!flags.length) return "";
  return flags.map((f) => {
    const danger = f && f.severity === "danger";
    const cls = danger ? "risk-flag-badge risk-flag-danger" : "risk-flag-badge risk-flag-warn";
    const label = escapeHtml(String((f && f.label) || "風險"));
    const title = danger
      ? `高風險型態(已降級為觀察，不列可買)：${label}`
      : `風險提醒(不單獨否決可買)：${label}`;
    return `<span class="${cls}" title="${escapeHtml(title)}">🚩${label}</span>`;
  }).join("");
}

// 記住上次真正寫進 DOM 的妖股名單 HTML:內容沒變就不重繪,保住使用者的捲動位置。
let lastMonsterListHtml = "";

function intradayMarketDiscoveryHtml(discovery) {
  const state = discovery && typeof discovery === "object" ? discovery : {};
  const leaders = Array.isArray(state.leaders) ? state.leaders : [];
  const checkedAt = String(state.checkedAt || "").trim();
  const requested = Number(state.requested ?? state.baselineCount ?? 0);
  const fresh = Number(state.fresh ?? 0);
  const qualified = Number(state.qualified ?? leaders.length);
  const confirmed = Number(state?.deepConfirmation?.confirmed ?? 0);
  const candidateSignals = Number(state.candidateSignalCount ?? 0);
  const formalBuyable = Number(state.formalBuyableCount ?? 0);
  const coverage = requested > 0 ? `${fresh}/${requested}` : "尚未掃描";
  const statusText = state.running
    ? "正在掃描完整流動性股票池"
    : state.skipped === "market_closed"
      ? "今日休市"
      : state.skipped === "outside_market_session"
        ? "目前非盤中時段"
        : checkedAt
          ? `最近更新 ${checkedAt}`
          : "等待下一次盤中掃描";
  const sourceText = String(state.source || state.fallbackProvider || "券商即時報價");
  const errorHtml = state.error
    ? `<div class="alert-message radar-aux intraday-discovery-error">
        <span>全市場盤中掃描異常</span>
        <strong>${escapeHtml(String(state.error))}</strong>
      </div>`
    : "";
  const rowsHtml = leaders.length
    ? leaders.map((item) => {
        const symbol = String(item.symbol || "");
        const name = String(item.name || symbol);
        const currentChange = Number(item.currentChangePct || 0);
        const stateTone = ({
          near_limit: "near-limit",
          active: "active",
          watch: "watch",
          faded: "faded",
          reversed: "reversed",
        })[item.state] || "watch";
        return `
          <tr>
            <td>
              <button type="button" class="portfolio-stock-button" data-pick="${escapeHtml(symbol)}">
                ${escapeHtml(`${symbol} ${name}`)}
              </button>
            </td>
            <td>${escapeHtml(String(item.sector || "上市櫃"))}</td>
            <td>${priceText(item.currentPrice)}</td>
            <td class="${currentChange >= 0 ? "up" : "down"}">${pct(currentChange)}</td>
            <td class="up">${pct(item.highChangePct)}</td>
            <td title="今日成交 ${fmt(item.totalVolumeLots || 0, 0)} 張／20 日均量 ${fmt(item.avgVolume20Lots || 0, 0)} 張">
              ${fmt(Number(item.volumeProgressRatio || 0) * 100, 0)}%
            </td>
            <td><span class="intraday-discovery-state ${stateTone}">${escapeHtml(String(item.status || "盤中新候選"))}</span><small class="intraday-discovery-confirmation">${escapeHtml(String(item.confirmationLabel || "等待連續確認"))}</small></td>
            <td><span class="intraday-discovery-source ${item.inRadar ? "existing" : "new"}">${item.inRadar ? "原雷達" : "盤中新候選"}</span></td>
          </tr>`;
      }).join("")
    : `<tr><td colspan="8">${state.running ? "正在取得全市場即時報價" : "目前沒有符合量價條件的盤中新強勢股"}</td></tr>`;
  return `
    <section class="intraday-discovery-section" aria-label="盤中全市場強勢掃描">
      <div class="intraday-discovery-head">
        <div>
          <h3 class="table-title">盤中全市場新強勢</h3>
          <p class="section-note">${escapeHtml(statusText)}｜本輪報價 ${escapeHtml(coverage)}｜召回 ${fmt(qualified, 0)}｜兩輪確認 ${fmt(confirmed, 0)}｜紙上訊號 ${fmt(candidateSignals, 0)}｜正式可買 ${fmt(formalBuyable, 0)}｜${escapeHtml(sourceText)}</p>
        </div>
        <span class="intraday-discovery-observe">盤中新候選</span>
      </div>
      ${errorHtml}
      <div class="feature-table-wrap intraday-discovery-table-wrap">
        <table class="portfolio-table intraday-discovery-table">
          <thead>
            <tr>
              <th>股票</th>
              <th>產業</th>
              <th>現價</th>
              <th>目前漲幅</th>
              <th>最高漲幅</th>
              <th>量能進度</th>
              <th>盤中狀態</th>
              <th>來源</th>
            </tr>
          </thead>
          <tbody>${rowsHtml}</tbody>
        </table>
      </div>
    </section>`;
}

function renderMonsterRadar() {
  const container = document.getElementById("monsterList");
  if (!container) return;
  resetDecisionTraceScope("monster");
  const allBackendRows = monsterBackendState.payload?.candidates || [];
  const backendCandidates = backendMonsterCandidates();
  const hasBackendScan = Boolean(monsterBackendState.payload?.candidates?.length);
  const usingBackend = backendCandidates.length > 0;
  const health = intradayHealth(monsterIntradayState);
  ensureBrainDecisionsForRows(backendCandidates, (item) => item?.stock?.code, "monster", backendCandidates.length);
  // 一輪重繪內只算一次全持股訊號,建 code→signal map 共用,取代 monsterOpenCheck
  // 內每列各跑一次 allSignals()(否則 C 候選 × H 持股各重算一次技術指標)。
  const signalByCode = new Map(allSignals().map((s) => [s.stock.code, s]));
  const openChecks = backendCandidates.map((item) => monsterOpenCheck(item, signalByCode));
  const openCheckByCode = new Map(openChecks.map((item) => [item.stock.code, item]));
  const displayRows = backendCandidates
    .map((item) => {
      const open = openCheckByCode.get(item.stock.code) || monsterOpenCheck(item, signalByCode);
      const rowHealth = brainGatedHealth(item.stock.code, health, { blockWhenMissing: true, context: "monster" });
      const baseDecision = timedEntryDecision(open, true);
      const brainEntry = entryDecisionWithBrain(baseDecision, rowHealth, open?.canBuy === true);
      const decision = brainEntry.decision;
      const rankDecision = decision;
      const canBuy = intradayStateMachine(open, decision, rowHealth, brainEntry.canEnter).status === "可買";
      return { item, open, decision, rankDecision, rowHealth, brainEntry, canBuy };
    })
    .sort((a, b) => {
      // 可買排最前面；同樣可不可買的狀態下，再按買進分數排序，最高分排最上面。
      // 分數是上次掃描時用日線資料算的，不會隨盤中價格即時更新——今天已經
      // 重挫(跌破-5%)的候選股即使分數還停在掃描當下的高分，也要先排到同一
      // 組的最後面，不然會跟還健康的候選股混在一起，看起來像還是強勢股。
      if (a.canBuy !== b.canBuy) return a.canBuy ? -1 : 1;
      const aCrashed = Number(a.item.todayChangePct) <= -5;
      const bCrashed = Number(b.item.todayChangePct) <= -5;
      if (aCrashed !== bCrashed) return aCrashed ? 1 : -1;
      // 同組直接採後端真實型態量能分數；大漲相只保留為顯示標記，不重排。
      const scoreDiff = Number(b.item.score || 0) - Number(a.item.score || 0);
      if (Math.abs(scoreDiff) > 0.0001) return scoreDiff;
      // 同分最終 tiebreak 改用量能(型態量能),不再用模型機率排序妖股候選。
      return Number(b.item.volumeRatio ?? b.item.volume_ratio ?? 0) - Number(a.item.volumeRatio ?? a.item.volume_ratio ?? 0);
    });
  // 股價上限篩選(純顯示層，不動掃描/評分)：資金有限買不起高價股，超過
  // 上限的候選不顯示。currentPrice 缺失(0/null)的列不過濾——沒有價格
  // 不代表買不起，靜默隱藏會讓使用者以為候選消失了。可買數/觀察數等
  // 統計一律用過濾後的名單算，「現在該做什麼」提示才不會指向買不起的股票。
  // 語意是「一張的預算(元)」不是每股股價——使用者的心智模型是「我有
  // 10萬，篩出買得起一張的」。換算成每股上限 = 預算/1000(一張=1000股)。
  const radarLotBudget = readRadarMaxPrice();
  const radarPriceCap = radarLotBudget ? radarLotBudget / 1000 : null;
  const hiddenByPriceCount = radarPriceCap
    ? displayRows.filter((row) => Number(row.item.currentPrice || 0) > radarPriceCap).length
    : 0;
  const visibleRows = hiddenByPriceCount
    ? displayRows.filter((row) => !(Number(row.item.currentPrice || 0) > radarPriceCap))
    : displayRows;
  // 輸入框跟著觀察名單標題走(使用者要求)，但它活在會被盤中更新重繪的
  // innerHTML 裡——重繪前先記住「正在打字中的值與焦點」，重繪後還原，
  // 打到一半不會被 30 秒自動更新洗掉。
  const priceCapInputHtml = `
    <label class="radar-price-cap-inline">一張預算
      <input id="radarMaxPrice" class="deposit-inline-input" type="number" min="0" step="10000" placeholder="如 100000" value="${radarLotBudget ?? ""}" />
      元${radarPriceCap ? `<span class="radar-price-cap-hint">＝股價 ${fmt(radarPriceCap, 0)} 元以下</span>` : ""}${hiddenByPriceCount ? `<span class="radar-price-cap-hidden">已隱藏 ${hiddenByPriceCount} 檔一張買不起的</span>` : ""}
    </label>`;
  const priceInputWasFocused = document.activeElement && document.activeElement.id === "radarMaxPrice";
  const priceInputDraft = priceInputWasFocused ? document.activeElement.value : null;
  // 使用者要求「妖股雷達只顯示可買的」(2026-07-08)：名單只列通過盤中確認的「可買」標的
  // (桌面列全部可買、手機取前 10)。0 檔可買時不留空白,改在名單區顯示「0 檔可買/X 檔觀察中」
  // 的清楚訊息(避免看起來像壞掉)——觀察檔數與掃描統計仍在上方摘要卡看得到。
  const isMobileRadarView = window.matchMedia("(max-width: 720px)").matches;
  const deploymentReadiness = monsterBackendState.payload?.deploymentReadiness || {};
  const radarListTitle = isMobileRadarView ? "短線買進觀察名單" : "短線強勢觀察名單";
  const radarDecisionColumnTitle = isMobileRadarView ? "能不能買" : "盤中狀態";
  const desktopReadinessHtml = !isMobileRadarView && deploymentReadiness.formalReady !== true
    ? `<div class="alert-message radar-aux radar-observation-banner">
        <span>雷達績效門檻未通過</span>
        <strong>目前僅供觀察，不是買進清單</strong>
      </div>`
    : "";
  const desktopDiscoveryHtml = !isMobileRadarView
    ? intradayMarketDiscoveryHtml(monsterIntradayState?.marketDiscovery)
    : "";
  const buyableRows = visibleRows.filter((row) => row.canBuy);
  const observeOnlyCount = visibleRows.length - buyableRows.length;
  // 手機版:優先顯示「可買」前10檔;有可買時絕不混入觀察(維持既有偏好)。
  // 但 0 檔可買時,退而顯示「觀察中」前10檔——否則整片空白,使用者反映「名單沒出來」。
  // 桌面版一律顯示完整名單(含觀察中),不隱藏。
  const mobileShowsObservingFallback = isMobileRadarView && buyableRows.length === 0 && visibleRows.length > 0;
  const rowsForDisplay = isMobileRadarView
    ? (buyableRows.length ? buyableRows.slice(0, 10) : visibleRows.slice(0, 10))
    : visibleRows;
  const candidates = visibleRows.map((row) => row.item);
  const trueBuyCount = visibleRows.filter((row) => row.decision === "可進場").length;
  const brainBlockedCount = visibleRows.filter((row) => row.rowHealth?.mode === "brain_observe_only").length;
  const quoteCoverage = monsterIntradayState?.quoteCoverage || {};
  const coverageText = Number.isFinite(Number(quoteCoverage.requested))
    ? ` / 即時報價 ${Number(quoteCoverage.received || 0)}/${Number(quoteCoverage.requested || 0)}`
    : "";
  const snapshotPipeline = monsterIntradayState?.snapshotPipeline || {};
  const snapshotText = snapshotPipeline.skipped === "market_closed"
    ? " / 紙上快照：休市"
    : snapshotPipeline.checkedAt
      ? ` / 紙上快照${snapshotPipeline.ok ? "" : "異常"} ${Number(snapshotPipeline.persisted || 0)}/${Number(snapshotPipeline.expected || 0)}`
      : " / 紙上快照：未檢查";
  const intradayText = monsterIntradayState?.updatedAt
    ? `盤中確認更新：${monsterIntradayState.updatedAt} / ${monsterIntradayState.source || "快取"}${coverageText} / 紙上確認 ${Number(monsterIntradayState.shadowBuyableCount || 0)} 檔 / 正式可買 ${Number(monsterIntradayState.buyableCount || 0)} 檔${snapshotText}${monsterIntradayState.active ? " / 自動更新中" : ""}`
    : "盤中確認：尚未更新即時報價";
  const summaryHtml = scanSummaryCards(candidates, {
    scannedCount: Number(monsterBackendState.payload?.universeTotal || allBackendRows.length),
    strengtheningCount: backendCandidates.length,
    watchCount: candidates.length,
    buyCount: trueBuyCount
  });
  const sourceText = usingBackend
    ? `後端快速掃描：${monsterBackendState.payload?.scanDate || "最新"}，全市場 ${fmt(monsterBackendState.payload?.universeTotal || 0, 0)} 檔 → 流動性候選 ${fmt(monsterBackendState.payload?.liquidUniverse || monsterBackendState.payload?.scanned || 0, 0)} 檔 → 快速候選 ${fmt(monsterBackendState.payload?.quickCandidates || 0, 0)} 檔 → 純規則評分 ${fmt(monsterBackendState.payload?.scoredCandidates ?? 0, 0)} 檔；模型獨立運行，不進候選`
    : hasBackendScan
      ? `後端全市場掃描：${monsterBackendState.payload?.scanDate || "最新"}，目前沒有短線妖股候選`
    : monsterBackendState.loading && !monsterBackendState.payload
      ? "正在讀取後端全市場掃描結果..."
      : monsterBackendState.error
        ? `後端掃描結果讀取失敗：${monsterBackendState.error}`
        : "目前沒有短線妖股候選。需要準備短線按「手動掃描短線妖股」。";
  const hotSectors = monsterBackendState.payload?.hotSectors || [];
  const marketRegime = monsterBackendState.payload?.marketRegime || {};
  const themeSnapshot = monsterBackendState.payload?.themeSnapshot || {};
  const themeSectors = themeSnapshot.sectors || monsterBackendState.payload?.sectorMomentum || {};
  const currentRegimeThreshold = Number(
    monsterBackendState.payload?.entryPolicy?.regimeThresholds?.[marketRegime.key]
    ?? marketRegime.minimumFormalScore
    ?? 60
  );
  const marketRegimeHtml = marketRegime.label
    ? `<p class="section-note radar-regime-line"><strong>市場狀態：${escapeHtml(marketRegime.label)}</strong>｜正式門檻 ${currentRegimeThreshold.toFixed(0)} 分｜${escapeHtml(marketRegime.reason || "")}</p>`
    : "";
  const hotSectorsHtml = hotSectors.length
    ? `<p class="section-note">今日題材熱度：${hotSectors.map((sector) => {
        const stat = themeSectors[sector] || {};
        return `${escapeHtml(sector)} ${Number(stat.themeHeat || 0).toFixed(0)} 分／${Number(stat.streakDays || 0)} 日`;
      }).join("、")}</p>`
    : "";
  const monsterHtml = candidates.length
    ? `
      <p class="section-note">${sourceText}</p>
      ${marketRegimeHtml}
      ${hotSectorsHtml}
      ${summaryHtml}
      ${(dailyReportMarket && dailyReportMarket.light === "red") ? `
        <div class="radar-red-caution">🔴 大盤紅燈（跌破月線）·妖股短線最怕弱市，追高尤其危險——下面「可買」仍屬高風險，建議極輕倉、或等紅燈轉黃再積極。</div>
      ` : ""}
      ${health.ok === false ? `
        <div class="alert-message radar-aux">
          <span>資料異常，暫停買進決策</span>
          <strong>${escapeHtml(health.reason || "盤中確認資料尚未穩定")}</strong>
        </div>
      ` : brainBlockedCount > 0 ? `
        <div class="alert-message radar-aux">
          <span>部分股票只觀察</span>
          <strong>${escapeHtml(`型態量能規則 阻擋 ${brainBlockedCount} 檔，未通過的列不會顯示可進場`)}</strong>
        </div>
      ` : ""}
      ${desktopReadinessHtml}
      ${desktopDiscoveryHtml}
      <h3 class="table-title radar-list-title">${radarListTitle}${priceCapInputHtml}</h3>
      <div class="feature-table-wrap portfolio-table-wrap monster-candidate-table-wrap">
        <table class="portfolio-table monster-table entry-decision-table">
          <thead>
            <tr>
              <th>股票</th>
              <th>分數</th>
              <th>題材</th>
              <th>現價</th>
              <th>今日漲跌</th>
              <th>${radarDecisionColumnTitle}</th>
              <th>原因</th>
            </tr>
          </thead>
          <tbody>
            ${mobileShowsObservingFallback ? `
              <tr class="mobile-note-row"><td colspan="7">目前 0 檔可買，${observeOnlyCount} 檔觀察中，以下為前 10 名（通過盤中確認才可買）</td></tr>
            ` : ""}${!rowsForDisplay.length ? `
              <tr class="mobile-empty-row"><td colspan="7">目前沒有妖股候選</td></tr>
            ` : ""}${rowsForDisplay.map(({ item, open, decision, rowHealth, brainEntry }) => {
              const method = brainEntry.method || entryMethodText(open, decision, rowHealth.ok !== false);
              const stateMachine = intradayStateMachine(open, decision, rowHealth, brainEntry.canEnter);
              const brainReason = entryBrainReasonText(brainEntry, rowHealth);
              const reason = compactEntryReason([
                brainReason,
                compactMonsterOpenChecks(open).replaceAll("、", " / "),
                item.volumeRatio == null ? "" : `量能${fmt(item.volumeRatio, 1)}倍`,
                `5日${pct(item.change5)}`,
                compactMonsterReasons(item)
              ]);
              const fullReason = [
                `分數 ${item.score}`,
                `狀態 ${item.status}`,
                `今日 ${pct(item.todayChangePct)}`,
                `5日 ${pct(item.change5)}`,
                item.volumeRatio == null ? "" : `量能 ${fmt(item.volumeRatio, 1)} 倍`,
                ...(item.reasons || []),
                ...(open.checks || []),
                brainReason
              ].filter(Boolean).join("；");
              const traceId = registerDecisionTrace(monsterDecisionTrace(item, open, decision, method, rowHealth));
              return `
              <tr>
                <td>
                  <button
                    type="button"
                    class="portfolio-stock-button"
                    data-pick="${item.stock.code}"
                    data-order-fill="1"
                    data-order-action="BUY"
                    ${!isMobileRadarView ? `data-order-context="monster_radar" data-radar-scan-date="${escapeHtml(String(monsterBackendState.payload?.scanDate || ""))}"` : ""}
                    data-order-price="${Number(item.currentPrice || 0)}"
                    data-current-price="${Number(item.currentPrice || 0)}"
                  >
                    ${stockLabel(item.stock)}
                  </button>
                  ${tenureBadgeText(item)}${bigMoverBadge(item)}${monsterRiskBadges(item)}
                </td>
                <td>${fmt(item.score, 1)}</td>
                <td>${escapeHtml(item.stock.sector || "-")}${item.sectorHot ? '<span class="sector-hot-tag">熱門</span>' : ""}${item.themeHeat > 0 ? `<span class="sector-hot-tag" title="題材熱度由入選家數、成交金額占比、5/20日超額報酬與持續日數計算">${fmt(item.themeHeat, 0)}｜${fmt(item.sectorThemeStreak, 0)}日</span>` : ""}</td>
                <td title="${item.hasLiveQuote ? escapeHtml(`${item.quoteSource || "盤中即時報價"}${Number.isFinite(item.quoteAgeSeconds) ? `｜${Math.round(item.quoteAgeSeconds)}秒前` : ""}`) : `掃描日(${escapeHtml(String(item.priceDate || "-"))})收盤價，非即時`}">${priceText(item.currentPrice)}${monsterQuoteSourceBadge(item)}</td>
                <td class="${Number(item.todayChangePct) >= 0 ? "up" : "down"}">${pct(item.todayChangePct)}</td>
                <td
                  class="monster-status-cell ${stateMachine.tone}"
                  title="${escapeHtml(stateMachine.details || rowHealth.reason || open.flow.label)}"
                >${escapeHtml(stateMachine.status)}</td>
                <td>
                  <small title="${escapeHtml(fullReason)}">${escapeHtml(reason)}</small>
                  ${decisionTraceButton(traceId)}
                </td>
              </tr>
            `; }).join("")}
          </tbody>
        </table>
      </div>
      <p class="section-note radar-live-status">${intradayText}</p>
      <p class="section-note">妖股流程：09:05 初篩，09:15 看量能，09:30-10:00 初次確認，10:00-13:15 只看低接/V轉，13:15 後不再進場。目前階段：${taiwanBuyFlowStage().label}。</p>
      <p class="section-note">排序方式：全市場先用資料庫 SQL 篩出流動性候選，再用成交金額、量能放大、強於大盤、逆勢創高建立快速候選，最後依真實日線的型態量能分數排序。模型獨立運行，不加入妖股候選、排序或買賣；未通過真實盤中報價確認前只列觀察。</p>
    `
    : `${summaryHtml}
      ${desktopReadinessHtml}
      ${desktopDiscoveryHtml}
      <h3 class="table-title radar-list-title">${radarListTitle}${priceCapInputHtml}</h3>
      <div class="alert-message"><span>${sourceText}</span><strong>${
        hiddenByPriceCount
          ? `全部 ${hiddenByPriceCount} 檔候選一張都超過預算 ${fmt(radarLotBudget, 0)} 元，調高預算即可顯示`
          : "目前沒有通過條件的妖股或持股觀察"
      }</strong></div>`;
  // 內容跟上次寫進 DOM 的一模一樣就直接跳過:掃描時每秒輪詢、盤中每 30 秒更新都會呼叫
  // 本函式,#monsterList 沒有自身捲軸(整頁在捲),innerHTML 重建會把頁面捲回最上面。
  // trace id 已改決定性序號 → 相同資料 = 相同 HTML → 這裡跳過,捲動位置自然不動。
  if (monsterHtml === lastMonsterListHtml) return;
  lastMonsterListHtml = monsterHtml;
  // 資料真的變了才重繪:先記住整頁捲動位置,重建後同步還原,避免盤中價格更新把
  // 使用者往下捲的位置彈回最上面(同一影格內同步還原,不會觸發 smooth 動畫)。
  const monsterScroller = document.scrollingElement || document.documentElement;
  const monsterSavedScrollTop = monsterScroller ? monsterScroller.scrollTop : 0;
  container.innerHTML = monsterHtml;
  if (monsterScroller) monsterScroller.scrollTop = monsterSavedScrollTop;
  // 還原重繪前打字中的值與焦點(輸入框在動態區裡，30秒盤中更新會重建它)
  if (priceInputWasFocused) {
    const restoredInput = document.getElementById("radarMaxPrice");
    if (restoredInput) {
      restoredInput.value = priceInputDraft;
      restoredInput.focus();
    }
  }
  bindStockPickButtons();
  bindDecisionTraceButtons();
}

// 雷達股價上限(使用者自填，存 localStorage)：資金有限時高價股看了也買不起，
// 純顯示層過濾。輸入框放在面板頭的靜態 HTML 裡(#radarMaxPrice)，不會被
// 30 秒盤中更新的 innerHTML 重繪洗掉打字中的值。
const RADAR_MAX_PRICE_STORAGE_KEY = "stock-vibe-radar-max-price-v1";

function readRadarMaxPrice() {
  const value = Number(safeGetItem(RADAR_MAX_PRICE_STORAGE_KEY, "0"));
  return Number.isFinite(value) && value > 0 ? value : null;
}

document.addEventListener("change", (event) => {
  if (!event.target || event.target.id !== "radarMaxPrice") return;
  const raw = Number(event.target.value);
  const normalized = Number.isFinite(raw) && raw > 0 ? String(Math.round(raw)) : "0";
  safeSetItem(RADAR_MAX_PRICE_STORAGE_KEY, normalized);
  renderMonsterRadar();
  // 同步到伺服器：跨裝置共用同一個預算，且盤中進場LINE推播用它過濾
  // 買不起一張的候選(fire-and-forget，失敗不影響本機篩選)
  fetch("/api/user-prefs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ radarLotBudget: Number(normalized) }),
  }).catch(() => {});
});


function modelDataValueText(item) {
  if (!item || item.value === null || item.value === undefined || item.value === "") return "無資料";
  if (item.key === "finance_source") return escapeHtml(item.value);
  if (item.key === "volume") return `${fmt(item.value, 0)} 股`;
  if ([
    "foreign_buy_sell",
    "trust_buy_sell",
    "margin_balance",
    "short_balance",
    "broker_branch_net_buy",
    "main_force_buy_sell",
    "realtime_money_flow",
    "realtime_large_order_flow"
  ].includes(item.key)) return fmt(item.value, 0);
  if (item.key === "monthly_revenue") return fmt(item.value, 0);
  if (item.key === "revenue_growth" || item.key === "gross_margin") return `${fmt(item.value, 2)}%`;
  return fmt(item.value, 2);
}

function modelDataStatusClass(status) {
  if (status === "ok") return "up";
  if (status === "fallback") return "info";
  if (status === "missing" || status === "missing-source") return "warn";
  return "down";
}

function brainDecisionStatusClass(ok) {
  if (ok === true) return "up";
  if (ok === false) return "down";
  return "warn";
}

function brainDecisionValue(value) {
  if (value === null || value === undefined || value === "") return "無資料";
  if (typeof value === "number") return fmt(value, 2);
  return String(value);
}

function brainDecisionContextKey(context = "analysis") {
  const raw = String(context || "analysis").trim().toLowerCase().replace(/_/g, "-");
  if (["monster", "entry", "buy", "intraday", "v-reversal", "pullback"].includes(raw)) {
    if (raw === "v-reversal") return "v_reversal";
    return raw;
  }
  if (["portfolio-exit", "exit", "sell-alert"].includes(raw)) return "portfolio_exit";
  return raw || "analysis";
}

function brainDecisionCacheKey(stockCode, context = "analysis") {
  return `${normalizeStockCode(stockCode)}::${brainDecisionContextKey(context)}`;
}

function rememberBrainDecisionPayload(decision, context = "analysis") {
  const code = normalizeStockCode(decision?.symbol);
  if (!code) return "";
  const contextKey = brainDecisionContextKey(context || decision.context || "analysis");
  const normalized = { ...decision, context: contextKey, updatedAt: Date.now() };
  brainDecisionCache.set(brainDecisionCacheKey(code, contextKey), normalized);
  if (contextKey === "portfolio_exit") persistPortfolioBrainDecisions();
  return code;
}

function brainDecisionIsPending(stockCode, context = "analysis") {
  const cacheKey = brainDecisionCacheKey(stockCode, context);
  const cached = brainDecisionCache.get(cacheKey);
  return !cached || cached.loading === true || brainDecisionRequestMap.has(cacheKey);
}

function brainDecisionStatus(stockCode, context = "analysis") {
  return brainDecisionCache.get(brainDecisionCacheKey(stockCode, context)) || null;
}

function brainDecisionFor(stockCode, context = "analysis") {
  const cached = brainDecisionStatus(stockCode, context);
  if (!cached || cached.loading || cached.error || cached.ok === false) return null;
  return cached;
}

function brainDecisionBlockReason(decision, cached = null) {
  const status = decision || cached;
  if (!status) return "型態量能規則 尚未取得判斷";
  if (status.loading) return "型態量能規則 讀取中";
  if (status.error) return `型態量能規則 暫時未回應：${status.error}`;
  const blockers = Array.isArray(status.blockers) ? status.blockers.filter(Boolean) : [];
  if (blockers.length) return blockers.join("；");
  if (Array.isArray(status.missingCore) && status.missingCore.length) return `核心資料缺漏：${status.missingCore.join("、")}`;
  return status.actionLabel || status.recommendation || "型態量能規則 要求只觀察";
}

function brainDecisionBlocksDecision(decision, options = {}) {
  if (!decision) return Boolean(options.blockWhenMissing);
  if (decision.ok === false) return true;
  if (decision.observeOnly === true) return true;
  const context = brainDecisionContextKey(options.context || decision.context || "analysis");
  const entryContext = ["monster", "v_reversal", "pullback", "entry", "buy", "intraday"].includes(context);
  const exitContext = context === "portfolio_exit";
  if (entryContext && decision.entryAllowed === false) return true;
  if (entryContext && decision.decisionBlocked === true) return true;
  if (exitContext && decision.sellDataReady === false) return true;
  if (String(decision.recommendation || "").includes("只觀察")) return true;
  return false;
}

function brainDecisionTraceCondition(stockCode, context = "analysis") {
  const code = normalizeStockCode(stockCode);
  const cached = brainDecisionStatus(code, context);
  const decision = brainDecisionFor(code, context);
  const blocked = brainDecisionBlocksDecision(decision, { blockWhenMissing: true, context });
  return {
    label: "型態量能規則 共用判斷",
    ok: !blocked,
    value: decision
      ? `${decision.recommendation || "只觀察"}｜${brainDecisionBlockReason(decision)}`
      : brainDecisionBlockReason(null, cached),
    source: decision?.engineVersion || "型態量能規則"
  };
}

function brainGatedHealth(stockCode, baseHealth, options = {}) {
  const health = baseHealth || { ok: true, mode: "normal", reason: "" };
  if (health.ok === false) return health;
  const code = normalizeStockCode(stockCode);
  const context = options.context || "analysis";
  const cached = brainDecisionStatus(code, context);
  const decision = brainDecisionFor(code, context);
  if (brainDecisionBlocksDecision(decision, { blockWhenMissing: options.blockWhenMissing !== false, context })) {
    return {
      ok: false,
      mode: "brain_observe_only",
      reason: `型態量能規則：${brainDecisionBlockReason(decision, cached)}`,
      brainDecision: decision,
      brainCached: cached,
      brainContext: brainDecisionContextKey(context)
    };
  }
  return {
    ...health,
    brainDecision: decision,
    brainCached: cached,
    brainContext: brainDecisionContextKey(context)
  };
}

function collectBrainDecisionCodes(rows, getCode, limit = 80) {
  const codes = [];
  (rows || []).forEach((row) => {
    const code = normalizeStockCode(typeof getCode === "function" ? getCode(row) : row?.code || row?.symbol);
    if (code && !codes.includes(code)) codes.push(code);
  });
  return codes.slice(0, Math.max(1, Number(limit) || 80));
}

function triggerBrainContextRender(context) {
  const contextKey = brainDecisionContextKey(context);
  if (contextKey === "portfolio_exit") {
    renderPortfolioAlerts(undefined, undefined, { allowWaitingOnlyCache: true, skipNotifications: true });
  } else if (contextKey === "monster") {
    renderMonsterRadar();
  }
}

function ensureBrainDecisionBatch(codes, context = "analysis", options = {}) {
  const contextKey = brainDecisionContextKey(context);
  const uniqueCodes = [...new Set((codes || []).map(normalizeStockCode).filter(Boolean))]
    .slice(0, Math.max(1, Number(options.limit) || 100));
  if (!uniqueCodes.length) return Promise.resolve({ ok: true, checked: 0, failed: [] });

  const pendingSet = brainDecisionBatchPendingCodes.get(contextKey) || new Set();
  const missingCodes = uniqueCodes.filter((code) => {
    const cached = brainDecisionStatus(code, contextKey);
    const cacheKey = brainDecisionCacheKey(code, contextKey);
    const cachedAt = Number(cached?.updatedAt || cached?.finishedAt || 0);
    const errorFresh = cached?.error && cachedAt && Date.now() - cachedAt < 60000;
    if (brainDecisionRequestMap.has(cacheKey) || pendingSet.has(code)) return false;
    if (!options.force && cached && !cached.loading && !cached.error) return false;
    if (!options.force && errorFresh) return false;
    return true;
  });
  const batchSize = Math.max(1, Math.min(Number(options.chunkSize) || 3, 4));
  const requestCodes = missingCodes.slice(0, batchSize);
  if (!missingCodes.length) return Promise.resolve({ ok: true, checked: uniqueCodes.length, failed: [] });

  requestCodes.forEach((code) => {
    pendingSet.add(code);
    brainDecisionCache.set(brainDecisionCacheKey(code, contextKey), { loading: true, context: contextKey, startedAt: Date.now() });
  });
  brainDecisionBatchPendingCodes.set(contextKey, pendingSet);

  const requestKey = `${contextKey}::${requestCodes.join(",")}`;
  if (brainDecisionBatchRequestMap.has(requestKey)) return brainDecisionBatchRequestMap.get(requestKey);

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), Math.max(5000, Number(options.timeoutMs) || BRAIN_DECISION_BATCH_TIMEOUT_MS));
  const request = fetch("/api/brain/decisions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbols: requestCodes, context: contextKey, maxSymbols: requestCodes.length }),
    signal: controller.signal
  })
    .then(async (response) => {
      const payload = await response.json();
      if (!response.ok || !payload.ok) throw new Error(payload.error || "型態量能規則 批次讀取失敗");
      const decisions = Array.isArray(payload.decisions) ? payload.decisions : [];
      const failed = Array.isArray(payload.failed) ? [...payload.failed] : [];
      const resolvedCodes = new Set();
      decisions.forEach((decision) => {
        const code = rememberBrainDecisionPayload(decision, contextKey);
        if (code) resolvedCodes.add(code);
        if (decision?.ok === false && code && !failed.some((item) => String(item).startsWith(`${code}:`))) {
          failed.push(`${code}: ${decision.error || brainDecisionBlockReason(decision)}`);
        }
      });
      requestCodes.forEach((code) => {
        if (resolvedCodes.has(code)) return;
        const failedText = failed.find((item) => String(item).startsWith(`${code}:`));
        if (failedText) {
          brainDecisionCache.set(brainDecisionCacheKey(code, contextKey), {
            ok: false,
            error: failedText.replace(`${code}:`, "").trim() || "型態量能規則 讀取失敗",
            context: contextKey,
            updatedAt: Date.now()
          });
        }
      });
      return { ok: !failed.length, checked: uniqueCodes.length, fetched: decisions.length, remaining: missingCodes.length - requestCodes.length, failed };
    })
    .catch((error) => {
      const message = error?.name === "AbortError" ? "型態量能規則 批次讀取逾時，暫時只觀察" : friendlyError(error);
      requestCodes.forEach((code) => {
        brainDecisionCache.set(brainDecisionCacheKey(code, contextKey), {
          ok: false,
          error: message,
          context: contextKey,
          updatedAt: Date.now()
        });
      });
      return { ok: false, checked: uniqueCodes.length, fetched: 0, failed: requestCodes.map((code) => `${code}: ${message}`) };
    })
    .finally(() => {
      clearTimeout(timeoutId);
      const currentPending = brainDecisionBatchPendingCodes.get(contextKey);
      requestCodes.forEach((code) => currentPending?.delete(code));
      if (currentPending && !currentPending.size) brainDecisionBatchPendingCodes.delete(contextKey);
      brainDecisionBatchRequestMap.delete(requestKey);
      triggerBrainContextRender(contextKey);
    });

  brainDecisionBatchRequestMap.set(requestKey, request);
  return request;
}

function ensureBrainDecisionsForRows(rows, getCode, context = "analysis", limit = 80) {
  const contextKey = brainDecisionContextKey(context);
  const codes = collectBrainDecisionCodes(rows, getCode, limit);
  if (["monster"].includes(contextKey)) {
    ensureBrainDecisionBatch(codes, contextKey, { limit, chunkSize: 3, timeoutMs: BRAIN_DECISION_BATCH_TIMEOUT_MS });
    return;
  }
  codes.forEach((code) => ensureBrainDecision(code, contextKey));
}

function brainDecisionHtml(stockCode, traceId = "", context = "analysis") {
  const cached = brainDecisionCache.get(brainDecisionCacheKey(stockCode, context));
  if (!cached) return "";
  if (cached.loading) {
    return `<div class="brain-engine-panel"><strong>判斷大腦</strong><span>正在讀取真實資料與規則證據...</span></div>`;
  }
  if (cached.error) {
    return `<div class="brain-engine-panel down"><strong>判斷大腦</strong><span>暫時無法取得：${escapeHtml(cached.error)}</span></div>`;
  }
  const ruleRows = Array.isArray(cached.ruleBreakdown) ? cached.ruleBreakdown : [];
  const sourceRows = Array.isArray(cached.sources) ? cached.sources : [];
  const blockers = Array.isArray(cached.blockers) ? cached.blockers : [];
  const nextSteps = Array.isArray(cached.nextSteps) ? cached.nextSteps : [];
  const integratedRows = integratedBrainConditionRows(cached);
  const coreRows = integratedRows.filter((row) => row.source === "型態量能");
  const attentionRows = integratedRows.filter((row) => row.source !== "型態量能");
  const statusClass = cached.observeOnly ? "warn" : cached.action === "BUY_CANDIDATE" ? "up" : "info";
  const v2ScoreText = Number.isFinite(Number(cached.brainV2?.score)) ? `${fmt(Number(cached.brainV2.score) * 100, 2)}%` : "無資料";
  const v2ScoreTrend = cached.brainV2?.scoreTrend;
  const v2ScoreTrendHtml = v2ScoreTrend?.text
    ? `<small class="brain-v2-trend ${escapeHtml(v2ScoreTrend.direction || "flat")}">${escapeHtml(v2ScoreTrend.text)}</small>`
    : "";
  const profile = cached.strategyProfile || {};
  const strategyName = profile.name || "妖股短打";
  const strategyEntryThresholdText = Number.isFinite(Number(profile.entryThreshold))
    ? `${fmt(Number(profile.entryThreshold) * 100, 2)}%`
    : "無資料";
  const strategyDataThresholdText = Number.isFinite(Number(profile.dataConfidenceThreshold))
    ? `${fmt(Number(profile.dataConfidenceThreshold) * 100, 2)}%`
    : "無資料";
  const strategyHorizon = cached.strategyHorizon || {};
  const strategyHorizonText = strategyHorizon.label
    ? `${strategyHorizon.label}${strategyHorizon.holdingDays ? `｜${strategyHorizon.holdingDays}` : ""}`
    : "未分類";
  return `
    <div class="brain-engine-panel ${cached.observeOnly ? "observe" : ""} ${traceId ? "decision-trace-open" : ""}" ${decisionTraceOpenAttrs(traceId)}>
      <div class="brain-engine-head">
        <div>
          <strong>判斷大腦 型態量能規則</strong>
          <span>${escapeHtml(cached.engineVersion || "brain-v1.0")}｜${escapeHtml(cached.generatedAt || "")}</span>
        </div>
        <div class="brain-engine-decision ${statusClass}">
          <b>${escapeHtml(cached.recommendation || "只觀察")}</b>
          <small>${escapeHtml(cached.actionLabel || "")}</small>
        </div>
      </div>
      <div class="brain-engine-grid">
        <div><span>資料日期</span><strong>${escapeHtml(cached.date || "無資料")}</strong></div>
        <div><span>目前價</span><strong>${escapeHtml(tracePriceText(cached.currentPrice))}</strong></div>
        <div><span>策略腦袋</span><strong>${escapeHtml(strategyName)}</strong></div>
        <div><span>策略週期</span><strong>${escapeHtml(strategyHorizonText)}</strong></div>
        <div><span>型態量能</span><strong>${escapeHtml(v2ScoreText)}</strong>${v2ScoreTrendHtml}</div>
        <div><span>策略門檻</span><strong>${escapeHtml(strategyEntryThresholdText)}</strong></div>
        <div><span>資料門檻</span><strong>${escapeHtml(strategyDataThresholdText)}</strong></div>
        <div><span>通知狀態</span><strong>${cached.canNotify ? "允許通知" : "不通知，只顯示判斷"}</strong></div>
      </div>
      ${blockers.length ? `
        <div class="brain-engine-blockers">
          <span>阻擋原因</span>
          <strong>${escapeHtml(blockers.join("；"))}</strong>
        </div>
      ` : ""}
      <div class="brain-engine-columns">
        <section class="brain-core-section">
          <h4>核心條件</h4>
          ${coreRows.length ? `
            <div class="brain-core-grid">
              ${coreRows.map((row) => `
                <div class="brain-core-card ${brainDecisionStatusClass(row.ok)} ${row.borderline ? "borderline" : ""}">
                  <span>${escapeHtml(row.label || "-")}</span>
                  <b>${escapeHtml(traceBoolText(row.ok))}</b>
                  <small>${escapeHtml(row.value || "無資料")}${row.borderline ? '<em class="brain-borderline-tag">門檻邊緣，資料來源稍有變動就可能翻盤</em>' : ""}</small>
                </div>
              `).join("")}
            </div>
          ` : `<div class="brain-empty">尚無核心條件，不能作為交易依據。</div>`}
          ${attentionRows.length ? `
            <h5>需注意</h5>
            <div class="brain-attention-list">
              ${attentionRows.map((row) => `
                <div class="brain-attention-item">
                  <span>${escapeHtml(row.label || "-")}</span>
                  <b class="${brainDecisionStatusClass(row.ok)}">${escapeHtml(traceBoolText(row.ok))}</b>
                  <small>${escapeHtml(row.value || "無資料")}</small>
                </div>
              `).join("")}
            </div>
          ` : ""}
        </section>
        <section>
          <h4>規則拆解</h4>
          <table>
            <tbody>
              ${ruleRows.map((row) => `
                <tr>
                  <td>${escapeHtml(row.label || "-")}</td>
                  <td>${escapeHtml(row.text || "無資料")}</td>
                  <td>${escapeHtml(row.role || "")}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </section>
        <section>
          <h4>資料來源</h4>
          <table>
            <tbody>
              ${sourceRows.map((row) => `
                <tr>
                  <td>${escapeHtml(row.label || "-")}</td>
                  <td>${escapeHtml(brainSourceShort(row.source))}</td>
                  <td class="${row.status === "正式/授權來源" ? "up" : row.status === "Yahoo fallback" ? "warn" : "down"}">${escapeHtml(brainSourceStatusShort(row.status))}</td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </section>
      </div>
      ${nextSteps.length ? `<p class="brain-engine-next">${escapeHtml(nextSteps.join(" "))}</p>` : ""}
    </div>
  `;
}

function enqueueBrainDecisionJob(job, priority = false) {
  return new Promise((resolve) => {
    const task = { job, resolve };
    (priority ? brainDecisionQueue.high : brainDecisionQueue.normal).push(task);
    pumpBrainDecisionQueue();
  });
}

function pumpBrainDecisionQueue() {
  if (brainDecisionQueue.running) return;
  const task = brainDecisionQueue.high.shift() || brainDecisionQueue.normal.shift();
  if (!task) return;
  brainDecisionQueue.running = true;
  Promise.resolve()
    .then(task.job)
    .then(task.resolve)
    .catch((error) => {
      task.resolve({ ok: false, error: friendlyError(error), updatedAt: Date.now() });
    })
    .finally(() => {
      brainDecisionQueue.running = false;
      setTimeout(pumpBrainDecisionQueue, 0);
    });
}

function ensureBrainDecision(stockCode, context = "analysis", options = {}) {
  const code = normalizeStockCode(stockCode);
  if (!code) return Promise.resolve(null);
  const cacheKey = brainDecisionCacheKey(code, context);
  const cached = brainDecisionCache.get(cacheKey);
  const cachedAt = Number(cached?.updatedAt || cached?.finishedAt || 0);
  const errorFresh = cached?.error && cachedAt && Date.now() - cachedAt < 60000;
  // portfolio_exit 的成功結論也有 TTL：通知放行/阻擋不能無限期沿用舊結論
  // (隔夜掛著的分頁隔天會用昨天的 Brain 判斷)。過期時重抓；判斷邏輯端
  // (brainDecisionFor)仍可先用舊值頂著，抓回來自動更新，不會空窗。
  const okFresh = cached?.ok && (
    brainDecisionContextKey(context) !== "portfolio_exit"
    || (cachedAt && Date.now() - cachedAt < BRAIN_DECISION_PORTFOLIO_TTL_MS)
  );
  if (!options.force && cached && !cached.loading && (okFresh || errorFresh)) return Promise.resolve(cached);
  if (!options.force && cached?.loading && brainDecisionRequestMap.has(cacheKey)) return brainDecisionRequestMap.get(cacheKey);

  const timeoutMs = Number(options.timeoutMs || BRAIN_DECISION_TIMEOUT_MS);
  brainDecisionCache.set(cacheKey, { loading: true, context, startedAt: Date.now() });
  const priority = Boolean(options.priority) || brainDecisionContextKey(context) === "portfolio_exit";
  const request = enqueueBrainDecisionJob(() => {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), Math.max(3000, timeoutMs));
      return fetch(`/api/brain/decision?symbol=${encodeURIComponent(code)}&context=${encodeURIComponent(context)}`, {
        signal: controller.signal
      })
        .then(async (response) => {
          const payload = await response.json();
          if (!response.ok || !payload.ok) throw new Error(payload.error || "判斷大腦讀取失敗");
          const normalized = { ...payload, context, updatedAt: Date.now() };
          brainDecisionCache.set(cacheKey, normalized);
          if (brainDecisionContextKey(context) === "portfolio_exit") persistPortfolioBrainDecisions();
          return normalized;
        })
        .catch((error) => {
          const message = error?.name === "AbortError" ? "型態量能規則 讀取逾時，暫時只觀察" : friendlyError(error);
          const normalized = { ok: false, error: message, context, updatedAt: Date.now() };
          brainDecisionCache.set(cacheKey, normalized);
          return normalized;
        })
        .finally(() => {
          clearTimeout(timeoutId);
        });
    }, priority)
    .finally(() => {
      brainDecisionRequestMap.delete(cacheKey);
      const contextKey = brainDecisionContextKey(context);
      if (contextKey === "portfolio_exit") {
        renderPortfolioAlerts(undefined, undefined, { allowWaitingOnlyCache: true, skipNotifications: true });
      } else if (contextKey === "monster") {
        renderMonsterRadar();
      }
    });
  brainDecisionRequestMap.set(cacheKey, request);
  return request;
}

function currentHoldingForCode(code) {
  const holdings = readPortfolioHoldings();
  return holdings?.[normalizeStockCode(code)] || null;
}

function bindStockPickButtons() {
  document.querySelectorAll("[data-pick]").forEach((button) => {
    if (button.dataset.boundPick) return;
    button.dataset.boundPick = "1";
    button.addEventListener("click", () => {
      const code = normalizeStockCode(button.dataset.pick);
      if (!code) return;
      fillSinopacOrderFromPick(button, code);
    });
  });
}

function render() {
  const stock = currentStock();
  document.getElementById("dataStatus").textContent = dataSourceText(stock);
  // 效能:股價圖/決策/評分面板整段(.main-grid.integrated-hidden)在妖股版是
  // display:none,offsetParent 會是 null。既然使用者看不到,就不必每次 render 都
  // 重建整張 SVG、跑 backtest/positionPlan/tradingDecision(產物只寫進隱藏節點、
  // lastSnapshot 又沒人讀)。連 preparedData()(會 calcIndicators 整包)都能省。
  // 只重繪真正可見的三張表——可見輸出一字不差,若日後解除隱藏 offsetParent 會自動復活。
  const chartHost = document.getElementById("priceChart");
  const chartVisible = chartHost && chartHost.offsetParent !== null;
  if (!chartVisible) {
    renderScanner();
    if (initialPortfolioExitReady) renderMonsterRadar();
    renderAlerts();
    return;
  }
  const { rows, indicators, full, fullIndicators } = preparedData();
  const latest = rows.at(-1);
  if (!latest) {
    const statusText = dataSourceText(stock);
    document.getElementById("chartTitle").textContent = `${stockLabel(stock)} 等待真實資料`;
    document.getElementById("signalBadge").textContent = "未分析";
    document.getElementById("signalBadge").style.background = "#000";
    document.getElementById("signalBadge").style.color = "#fff";
    document.getElementById("scoreValue").textContent = "--";
    document.getElementById("scoreRing").style.background = "conic-gradient(var(--line) 0deg, var(--line) 360deg)";
    document.getElementById("decisionList").innerHTML = [
      ["目前狀態", statusText],
      ["資料規則", "FinMind 失敗顯示錯誤；Yahoo fallback 會明確標示；籌碼/財務缺資料顯示無資料。"]
    ].map(([label, value]) => `<div class="decision-item"><span>${label}</span><strong>${value}</strong></div>`).join("");
    drawPriceChart(document.getElementById("priceChart"), [], calcIndicators([]));
    renderScanner();
    renderMonsterRadar();
    renderAlerts();
    return;
  }
  const bt = backtest(rows, indicators);
  const plan = positionPlan(latest, fullIndicators);
  const trade = tradingDecision(rows, indicators, bt, plan);
  const holding = readPortfolioHoldings()[stock.code] || {};
  const chartExitPlan = Number(holding.quantity || 0)
    ? portfolioExitItemFor(stock.code)
    : null;
  const i = rows.length - 1;
  lastSnapshot = { stock, rows, indicators, full, fullIndicators, latest, bt, plan, trade };
  document.getElementById("chartTitle").textContent = `${stockLabel(stock)} ${stock.sector} 股價走勢`;
  const rulePct = Math.round(Number(trade.confidence || 0) * 100);
  const signalPalette = trade.tone === "buy"
    ? { background: "#e7f8ef", color: "#00ff2a" }
    : trade.tone === "sell"
      ? { background: "#fff0f1", color: "#ff1744" }
      : trade.tone === "watch"
        ? { background: "#fff4e6", color: "#fff200" }
        : { background: "#eef4fb", color: "#2fa8ff" };
  document.getElementById("signalBadge").textContent = trade.badge;
  document.getElementById("signalBadge").style.background = signalPalette.background;
  document.getElementById("signalBadge").style.color = signalPalette.color;
  document.getElementById("scoreValue").textContent = `${rulePct}%`;
  const ringColor = signalPalette.color;
  const ringDegrees = Number(trade.confidence || 0) * 360;
  document.getElementById("scoreRing").style.background = `conic-gradient(${ringColor} 0deg, ${ringColor} ${ringDegrees}deg, var(--line) ${ringDegrees}deg)`;

  document.getElementById("decisionList").innerHTML = [
    ["目前建議", trade.action],
    ["判斷依據", "真實日線價量、技術型態、風控與規則回測"],
    ["規則符合度", `${rulePct}%（${trade.checks.filter((item) => item.ok).length}/${trade.checks.length} 項）`],
    ["股價方向", indicators.ma20[i] > indicators.ma60[i] ? "月線站上季線" : "均線還沒轉強"],
    ["量能狀態", `${fmt(trade.volumeRatio, 2)} 倍 20 日均量`],
    ["規則回測", `${bt.trades} 次 / 勝率 ${fmt(bt.winRate * 100, 1)}% / 最大下跌 ${pct(bt.maxDrawdown * 100)}`],
    ["風險提醒", `停損 ${fmt(plan.stopPrice)}，目標 ${fmt(plan.target1)} / ${fmt(plan.target2)}`]
  ].map(([label, value]) => `<div class="decision-item"><span>${label}</span><strong>${value}</strong></div>`).join("");

  drawPriceChart(document.getElementById("priceChart"), rows, indicators, {
    stop: { label: "風控停損", value: chartExitPlan ? chartExitPlan.stopLoss : plan.stopPrice, color: "#ff1744" },
    target: { label: "+10% 達標里程碑", value: chartExitPlan ? chartExitPlan.takeProfit : plan.target1, color: "#00ff2a" },
    trailing: { label: "移動保護", value: chartExitPlan ? chartExitPlan.trailingStop : plan.trailingStop, color: "#fff200" },
  });
  renderScanner();
  renderMonsterRadar();
  renderAlerts();
}

function renderPortfolioSummary() {
  let summary = readPortfolioSummary();
  let hasSummary = summary && Object.keys(summary).length;
  if (!hasSummary) {
    const holdings = readPortfolioHoldings();
    if (Object.keys(holdings).length) {
      summary = savePortfolioSummary({ holdings: Object.values(holdings) }, holdings);
      hasSummary = true;
    }
  }
  if (!hasSummary) {
    return `<div class="yongfeng-summary muted">更新我的永豐庫存後，這裡會顯示總投資、市值、損益與資金。</div>`;
  }
  const totalPnl = Number(summary.totalPnl);
  const returnRate = Number(summary.returnRate);
  const unsettled = summary.unsettledAmount === null || summary.unsettledAmount === undefined ? null : Number(summary.unsettledAmount);
  const availableAfterSettlement = firstFiniteNumber([summary.availableAfterSettlement, summary.originalAvailable]);
  const originalAvailable = summary.originalAvailable === null || summary.originalAvailable === undefined ? null : Number(summary.originalAvailable);
  const settlementUnavailable = !Number.isFinite(unsettled) && summary.settlementStatus && summary.settlementStatus !== "ok";
  const settlementText = Number.isFinite(unsettled)
    ? `${signedMoney(unsettled)} 元`
    : settlementUnavailable
      ? "無資料（永豐未回傳）"
      : "無資料";
  const updatedAt = summary.updatedAt || "-";
  return `
    <div class="yongfeng-summary">
      <div>
        <span>永豐庫存共 ${fmt(summary.count || 0, 0)} 檔</span>
        <span>總投資：${fmt(summary.totalCost, 0)} 元</span>
        <span>目前市值：${fmt(summary.currentValue, 0)} 元</span>
        <span class="${dashboardTone(totalPnl)}">目前賺賠：${signedMoney(totalPnl)} 元</span>
        <span class="${dashboardTone(returnRate)}">目前報酬率：${Number.isFinite(returnRate) ? pct(returnRate) : "無資料"}</span>
        ${renderDepositReturnSpans(summary)}
      </div>
      <div>
        <span>永豐資金</span>
        <span>扣交割後可用：${Number.isFinite(availableAfterSettlement) ? `${fmt(availableAfterSettlement, 0)} 元` : "無資料"}</span>
        <span>原始可用：${Number.isFinite(originalAvailable) ? `${fmt(originalAvailable, 0)} 元` : "無資料"}</span>
        <span class="${dashboardTone(unsettled)}">未到期交割款：${settlementText}</span>
        <span>更新：${updatedAt}</span>
      </div>
    </div>
  `;
}

// 整戶報酬：使用者自填「總入金金額」(存 localStorage，跨重整保留)，
// 用「目前市值 + 永豐原始可用資金」對入金算整戶賺賠/報酬率——持股報酬率
// 只看股票部位，看不出「這個帳戶整體到底賺賠多少」(現金部位/已實現損益
// 全部被排除)，這裡補上帳戶層級的視角。純顯示計算，不進任何判斷邏輯。
const DEPOSIT_AMOUNT_STORAGE_KEY = "stock-vibe-deposit-amount-v1";

function readDepositAmount() {
  const value = Number(safeGetItem(DEPOSIT_AMOUNT_STORAGE_KEY, "0"));
  return Number.isFinite(value) && value > 0 ? value : null;
}

function renderDepositReturnSpans(summary) {
  const deposit = readDepositAmount();
  // .deposit-input-span 在手機(≤720px)整組隱藏：手機純閱讀不輸入，入金值
  // 存在伺服器(/api/user-prefs)，電腦輸入一次、手機載入時同步後直接顯示結果。
  const inputSpan = `<span class="deposit-input-span">入金：<input type="number" id="depositAmountInput" class="deposit-inline-input" min="0" step="1000" placeholder="總入金" value="${deposit ?? ""}"> 元</span>`;
  if (!deposit) {
    return `${inputSpan}<span class="muted deposit-input-span">輸入總入金後，這裡用「市值＋扣交割後可用」算整戶報酬</span>`;
  }
  const currentValue = Number(summary.currentValue);
  // 用「扣交割後可用」不能用「原始可用」：今天買的股票已經算進市值，
  // 但交割款(T+2)還沒從現金扣走——用原始可用會把同一筆錢重複計入
  // (2026-07-03 實例：差了 78,410 元)。交割資料缺失時退回原始可用。
  const cashAvailable = firstFiniteNumber([summary.availableAfterSettlement, summary.originalAvailable]);
  if (!Number.isFinite(currentValue) || !Number.isFinite(Number(cashAvailable))) {
    return `${inputSpan}<span class="muted">整戶報酬需要市值與永豐資金資料</span>`;
  }
  const totalNow = currentValue + Number(cashAvailable);
  const delta = totalNow - deposit;
  const rate = (delta / deposit) * 100;
  return `${inputSpan}
        <span>帳戶總值：${fmt(totalNow, 0)} 元（市值＋扣交割後可用）</span>
        <span class="${dashboardTone(delta)}">整戶賺賠：${signedMoney(delta)} 元</span>
        <span class="${dashboardTone(rate)}">整戶報酬率：${pct(rate)}</span>`;
}

// 摘要列由 innerHTML 重繪，直接綁在 input 上的監聽器每次更新庫存都會被
// 洗掉——用 document 層級事件委派，一次綁定永久有效。
document.addEventListener("change", (event) => {
  if (!event.target || event.target.id !== "depositAmountInput") return;
  const raw = Number(event.target.value);
  const normalized = Number.isFinite(raw) && raw > 0 ? String(Math.round(raw)) : "0";
  safeSetItem(DEPOSIT_AMOUNT_STORAGE_KEY, normalized);
  renderScanner();
  // 同步到伺服器讓手機也看得到(fire-and-forget，失敗不影響本機顯示)
  fetch("/api/user-prefs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ depositAmount: Number(normalized) }),
  }).catch(() => {});
});

async function loadUserPrefs() {
  // 入金值的同步規則(2026-07-03修過一次資料遺失才定下來的)：
  // 1. 伺服器有值(>0) → 它是跨裝置真相，覆寫本機(手機因此能顯示電腦設定的值)
  // 2. 伺服器沒值但本機有 → 把本機值推上去補齊，**絕不能反向用 0 把使用者
  //    剛輸入的值洗掉**(第一版就是無條件覆寫本機，使用者輸入後一重整就被
  //    伺服器的 0 刪掉)。只有桌面(>720px，有輸入框的裝置)會往上推，手機
  //    純顯示不推，避免桌面刻意清空後被手機的舊快取復活。
  try {
    const response = await fetch("/api/user-prefs");
    const payload = await response.json();
    if (!response.ok || !payload.ok) return;
    // 賣出 LINE 全域靜音:伺服器是唯一真相,任何裝置載入時同步(手機也套用,雖然手機面板隱藏)。
    sellLineMutedGlobal = payload.sellLineMuted === true;
    const sellMuteToggle = document.getElementById("sellLineMute");
    if (sellMuteToggle) sellMuteToggle.checked = sellLineMutedGlobal;
    const isDesktopWidth = !window.matchMedia("(max-width: 720px)").matches;
    const uploads = {};
    const syncPref = (serverValue, storageKey, onApply) => {
      const server = Number(serverValue) || 0;
      const local = Number(safeGetItem(storageKey, "0")) || 0;
      if (server > 0) {
        if (server !== local) {
          safeSetItem(storageKey, String(server));
          onApply();
        }
        return null;
      }
      // 伺服器沒值、本機有：回傳本機值讓呼叫端推上去(桌面限定)
      return local > 0 ? local : null;
    };
    const depositUpload = syncPref(payload.depositAmount, DEPOSIT_AMOUNT_STORAGE_KEY, renderScanner);
    const budgetUpload = syncPref(payload.radarLotBudget, RADAR_MAX_PRICE_STORAGE_KEY, renderMonsterRadar);
    if (isDesktopWidth) {
      if (depositUpload !== null) uploads.depositAmount = depositUpload;
      if (budgetUpload !== null) uploads.radarLotBudget = budgetUpload;
    }
    if (Object.keys(uploads).length) {
      fetch("/api/user-prefs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(uploads),
      }).catch(() => {});
    }
  } catch (error) {
    console.warn("使用者偏好讀取失敗(沿用本機快取)", error);
  }
}
loadUserPrefs();

function readHiddenPortfolioAlerts() {
  // 隱藏清單只在「當天」有效：舊版沒有日期，刪一次＝該股賣出/確認通知
  // 無限期靜音(hidden 直接讓 buildPortfolioAlerts 不產生卡片，通知也一起
  // 消失)，直到再按一次「建立提醒」才復活。刪除的視覺語意是「清今天的
  // 版面」，效果就該只到今天；隔天自動恢復顯示與通知。
  try {
    const stored = JSON.parse(localStorage.getItem(portfolioAlertHiddenStorageKey()) || "null");
    if (Array.isArray(stored)) return new Set();
    if (!stored || stored.date !== dateKey(new Date())) return new Set();
    return new Set(Array.isArray(stored.keys) ? stored.keys : []);
  } catch (error) {
    console.warn("持股提醒隱藏清單讀取失敗", error);
    return new Set();
  }
}

function saveHiddenPortfolioAlerts(keys) {
  safeSetItem(portfolioAlertHiddenStorageKey(), JSON.stringify({
    date: dateKey(new Date()),
    keys: [...keys],
  }));
}

function alertPriceText(value) {
  if (value === null || value === undefined || value === "") return "-";
  return Number.isFinite(Number(value)) ? fmt(Number(value)) : "-";
}

function readPortfolioAlertCache() {
  try {
    const cached = JSON.parse(localStorage.getItem(portfolioAlertCacheStorageKey()) || "{}");
    const alerts = Array.isArray(cached.alerts) ? cached.alerts : [];
    const onlyWaitingFormal = alerts.length && alerts.every((alert) => alert?.type === "等待正式資料");
    const hasTransientBrainState = alerts.some((alert) => {
      const text = `${alert?.status || ""} ${alert?.note || ""}`;
      return text.includes("讀取中") || text.includes("等待Brain") || text.includes("逾時");
    });
    // 所有提醒只能來自後端 portfolio-exit-v2。任何舊版前端
    // 計算快取都直接丟棄，避免重載空窗把短線規則套回中長期部位。
    const legacyDecisionFormat = alerts.some((alert) => (
      alert?.type !== "等待正式資料"
      && (!Object.prototype.hasOwnProperty.call(alert, "decisionVerified")
        || !Object.prototype.hasOwnProperty.call(alert, "estimatedNetPnl")
        || alert?.policyVersion !== "portfolio-exit-v2")
    ));
    if (onlyWaitingFormal || hasTransientBrainState || legacyDecisionFormat) return { updatedAt: 0, alerts: [] };
    return {
      updatedAt: Number(cached.updatedAt || 0),
      alerts
    };
  } catch (error) {
    console.warn("買賣停損提醒快取讀取失敗", error);
    return { updatedAt: 0, alerts: [] };
  }
}

function writePortfolioAlertCache(alerts) {
  if (!Array.isArray(alerts) || !alerts.length) return;
  safeSetItem(portfolioAlertCacheStorageKey(), JSON.stringify({
    updatedAt: Date.now(),
    alerts: alerts.slice(0, 240)
  }));
}

function groupPortfolioAlerts(alerts) {
  return [...alerts.reduce((map, alert) => {
    const code = normalizeStockCode(alert.code);
    if (!map.has(code)) {
      map.set(code, {
        code,
        name: stockDisplayName(code, alert.name) || code,
        pnlRate: alert.pnlRate,
        grossPnlRate: alert.grossPnlRate,
        estimatedNetPnl: alert.estimatedNetPnl,
        estimatedExitCosts: alert.estimatedExitCosts,
        shares: alert.shares,
        currentPrice: alert.currentPrice,
        buyPrice: alert.buyPrice,
        holdingPrice: alert.holdingPrice,
        sell: null,
        defense: null,
        confirm: null
      });
    }
    const group = map.get(code);
    group.name = stockDisplayName(code, group.name || alert.name) || code;
    if (Number.isFinite(Number(alert.holdingPrice)) && Number(alert.holdingPrice) > 0) {
      group.holdingPrice = Number(alert.holdingPrice);
    }
    group[alert.typeClass] = alert;
    return map;
  }, new Map()).values()];
}

let exitWatchMonitoringOffSent = false;
async function notifyExitWatchMonitoringDisabled() {
  // 2026-07-04 稽核修復：使用者取消勾選「背景監控」，跟分頁真的斷線/關閉
  // 是完全不同的語意，但伺服器原本只看心跳斷了就接管——顯式告訴伺服器
  // 「這是使用者的選擇，不用接管」，避免關掉監控卻還被伺服器發通知。
  // 只送一次(exitWatchMonitoringOffSent旗標)，重新勾選後由既有的
  // syncExitWatchToServer 正常心跳自然恢復(不用特別再送monitoring:true)。
  if (exitWatchMonitoringOffSent) return;
  exitWatchMonitoringOffSent = true;
  try {
    await fetch("/api/portfolio/exit-watch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: [], monitoring: false }),
    });
  } catch (error) {
    console.warn("停損守門員關閉通知失敗(不影響前端)", error);
  }
}

let exitWatchLastSyncAt = 0;
async function syncExitWatchToServer(alertGroups) {
  // 伺服器端停損守門員的資料來源+心跳：每分鐘轉送後端統一出場快照
  // 給 /api/portfolio/exit-watch 再做快照一致性核對。瀏覽器活著時這個 POST 就是心跳，
  // 伺服器保持靜默；分頁真的被回收/關閉超過10分鐘(EXIT_GUARDIAN_HEARTBEAT_MAX_AGE，
  // 2026-07-04從180秒拉高，原本的門檻遠低於瀏覽器背景分頁節流實測的延遲，
  // 常態性誤判離線)，後端守門員才接手用這份價位盯盤。
  // fire-and-forget：同步失敗不影響前端提醒鏈本身。
  const now = Date.now();
  if (now - exitWatchLastSyncAt < 60000) return;
  const items = (alertGroups || []).map((group) => {
    if (!["stop", "phase2", "phase3", "time_stop"].includes(group.confirm?.decisionType)) return null;
    const stop = Number(group.defense?.targetPrice);
    if (!Number.isFinite(stop) || stop <= 0) return null;
    // 一併同步「確認賣出價」(= 防守價再跌1%)。守門員用它當賣出觸發線,只在真的跌破確認價
    // 才發 LINE,不在剛摸到防守價(警戒)就發——使用者要「真的要賣才提醒」。
    const confirm = Number(group.confirm?.targetPrice);
    // 絕對停損線(avgPrice 基準、不隨現價下移)。防守價 stop 會被夾在現價下方,跳空/急殺時
    // 追不上,一定要另外把絕對線送給後端守門員兜底(否則後端跳空兜底形同死碼)。
    const absStop = Number(group.defense?.stopLossBase);
    return {
      code: group.code,
      name: group.name || "",
      stopLoss: stop,
      confirmSell: (Number.isFinite(confirm) && confirm > 0) ? confirm : null,
      absStop: (Number.isFinite(absStop) && absStop > 0) ? absStop : null,
      decisionVerified: group.confirm?.decisionVerified === true,
      decisionType: String(group.confirm?.decisionType || ""),
      decisionReasons: Array.isArray(group.confirm?.decisionReasons) ? group.confirm.decisionReasons : [],
      decisionAt: String(group.confirm?.decisionAt || ""),
      decisionDate: String(group.confirm?.decisionDate || ""),
      decisionDataDate: String(group.confirm?.dataDate || ""),
      decisionDataReady: group.confirm?.dataReady === true,
      quoteSource: String(group.confirm?.priceSource || ""),
      policyVersion: String(group.confirm?.policyVersion || ""),
    };
  }).filter(Boolean);
  if (!items.length) return;
  exitWatchLastSyncAt = now;
  const monitoringOn = document.getElementById("portfolioAlertMonitor")?.checked === true;
  // 只在確實在監控時重置「已送過停用通知」旗標；重新勾選後才會再送一次停用通知。
  if (monitoringOn) exitWatchMonitoringOffSent = false;
  try {
    await fetch("/api/portfolio/exit-watch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // 2026-07-04 稽核修復 A1：心跳一定要帶上目前「背景監控」勾選狀態。之前 body 只有
      // items，伺服器 payload.get("monitoring") is not False = True，會把使用者「取消監控」
      // 的意圖洗回 True，瀏覽器離線時守門員違反意願繼續發停損 LINE（誤發）。
      body: JSON.stringify({ items, monitoring: monitoringOn }),
    });
  } catch (error) {
    console.warn("停損守門員同步失敗(不影響前端提醒)", error);
  }
}

function renderRiskOverview(alertGroups, holdings) {
  // 風險總覽：回答「如果所有防守價同時跌破，我最多再賠多少」——短線多檔
  // 持倉時單看個股停損看不出總曝險。輔助資訊定位：資料不足就整塊隱藏。
  const box = document.getElementById("riskOverview");
  if (!box) return;
  let totalValue = 0;
  let maxLoss = 0;
  let stopCount = 0;
  let noStopCount = 0;
  const sectorValue = new Map();
  (alertGroups || []).forEach((group) => {
    const holding = (holdings || {})[group.code] || {};
    const analyzedShares = Number(group.shares);
    const shares = Number.isFinite(analyzedShares) && analyzedShares > 0
      ? analyzedShares
      : holdingShares(holding);
    const current = Number(group.currentPrice || holding.currentPrice || holding.price || 0);
    if (!shares || !(current > 0)) return;
    const value = shares * current;
    totalValue += value;
    const defense = Number(group.defense?.targetPrice);
    if (Number.isFinite(defense) && defense > 0 && defense < current) {
      maxLoss += (current - defense) * shares;
      stopCount += 1;
    } else {
      // 沒有防守價(或防守價已在現價之上，形同鎖利)的檔數分開計——
      // 沒防守價的部位理論下檔是無限的，不能假裝算進「最大損失」裡。
      noStopCount += 1;
    }
    const sector = sanitizeStockName(knownStockProfiles[group.code]?.sector, group.code) || "其他";
    sectorValue.set(sector, (sectorValue.get(sector) || 0) + value);
  });
  if (!totalValue || (!stopCount && !noStopCount)) {
    box.hidden = true;
    return;
  }
  const parts = [];
  if (stopCount) {
    const lossPct = (maxLoss / totalValue) * 100;
    parts.push(`若 ${stopCount} 檔防守價同時跌破，最大再損失約 ${Math.round(maxLoss).toLocaleString("zh-TW")} 元（市值的 ${lossPct.toFixed(1)}%）`);
  }
  if (noStopCount) {
    parts.push(`⚠️ ${noStopCount} 檔沒有有效防守價`);
  }
  let topSector = null;
  sectorValue.forEach((value, sector) => {
    if (sector === "其他") return;
    if (!topSector || value > topSector.value) topSector = { sector, value };
  });
  if (topSector && topSector.value / totalValue >= 0.5) {
    parts.push(`⚠️ ${topSector.sector} 佔市值 ${Math.round((topSector.value / totalValue) * 100)}%，產業集中`);
  }
  box.textContent = `🛡️ 風險總覽：${parts.join("｜")}`;
  box.hidden = false;
}

function portfolioAlertStatusClass(alert) {
  // 顏色必須跟後端可執行狀態一致，不能從中文文案猜。否則「跌破 -7%
  // 但只觀察」會被標紅，而真正已確認的結構失敗反而落到黃色。
  if (alert?.canNotify === true && alert?.decisionVerified === true) {
    if (alert.typeClass === "sell") return "ok";
    if (alert.typeClass === "confirm") return "danger";
  }
  return "wait";
}

function buildPortfolioAlerts() {
  const hidden = readHiddenPortfolioAlerts();
  const alerts = portfolioExitAnalysisState.payload?.alerts;
  if (!Array.isArray(alerts)) return [];
  return alerts
    .filter((alert) => alert?.key && !hidden.has(alert.key))
    .map((alert) => ({ ...alert, brainPending: false }));
}

function portfolioSignalSide(alert) {
  if (alert?.typeClass === "confirm") return "EXIT_CONFIRM";
  if (alert?.typeClass === "sell") return "SELL_TARGET";
  if (alert?.typeClass === "defense") return "RISK_GUARD";
  return "OBSERVE_ONLY";
}

function currentPaperSignalSession(now = new Date()) {
  const minutes = now.getHours() * 60 + now.getMinutes();
  const session = (key, label, time) => ({ key, label, time });
  if (minutes >= 9 * 60 + 5 && minutes < 9 * 60 + 15) return session("open_0905", "開盤初篩", "09:05");
  if (minutes >= 9 * 60 + 15 && minutes < 9 * 60 + 30) return session("volume_0915", "量能確認", "09:15");
  if (minutes >= 9 * 60 + 30 && minutes < 13 * 60 + 20) return session("intraday_0930", "盤中確認", "09:30");
  if (minutes >= 13 * 60 + 20 && minutes < 13 * 60 + 35) return session("preclose_1320", "收盤前", "13:20");
  if (minutes >= 15 * 60 + 20) return session("close_1520", "收盤後", "15:20");
  return session("manual", "手動快照", "");
}

function recordPortfolioAlertSignals(alerts) {
  const session = currentPaperSignalSession();
  const signals = (alerts || []).map((alert) => {
    const code = normalizeStockCode(alert?.code);
    const horizonKey = String(alert?.strategyHorizon || "").trim();
    const strategyHorizon = {
      key: horizonKey,
      label: alert?.strategyHorizonLabel || "",
      holdingDays: alert?.strategyHorizonDays || "",
    };
    const side = portfolioSignalSide(alert);
    const isRiskSide = side === "RISK_GUARD" || side === "EXIT_CONFIRM";
    return {
      signalDate: dateKey(new Date()),
      signalSession: session.key,
      signalSessionLabel: session.label,
      signalTime: session.time,
      strategy: horizonKey && horizonKey !== "unknown" ? `portfolio_exit_${horizonKey}` : "portfolio_exit",
      side,
      symbol: code,
      name: alert?.name || code,
      decision: alert?.status || "",
      score: null,
      modelVersion: "",
      price: Number.isFinite(Number(alert?.currentPrice)) ? Number(alert.currentPrice) : null,
      buyPoint: Number.isFinite(Number(alert?.buyPrice)) ? Number(alert.buyPrice) : null,
      stopPrice: isRiskSide && Number.isFinite(Number(alert?.targetPrice)) ? Number(alert.targetPrice) : null,
      targetPrice: !isRiskSide && Number.isFinite(Number(alert?.targetPrice)) ? Number(alert.targetPrice) : null,
      tradeHorizon: horizonKey,
      tradeHorizonLabel: strategyHorizon.label,
      tradeHorizonDays: strategyHorizon.holdingDays,
      tradeHorizonScore: null,
      dataDate: alert?.dataDate || dateKey(new Date()),
      dataSource: alert?.priceSource || "Shioaji / 永豐庫存",
      decisionSource: alert?.canNotify
        ? "後端統一出場引擎 + 成交時鎖定週期 + FIFO 真實買進日"
        : "後端統一出場引擎；未達已驗證通知條件",
      evidence: {
        canNotify: Boolean(alert?.canNotify),
        pnlRate: Number.isFinite(Number(alert?.pnlRate)) ? Number(alert.pnlRate) : null,
        grossPnlRate: Number.isFinite(Number(alert?.grossPnlRate)) ? Number(alert.grossPnlRate) : null,
        estimatedNetPnl: Number.isFinite(Number(alert?.estimatedNetPnl)) ? Number(alert.estimatedNetPnl) : null,
        estimatedExitCosts: Number.isFinite(Number(alert?.estimatedExitCosts)) ? Number(alert.estimatedExitCosts) : null,
        decisionVerified: alert?.decisionVerified === true,
        decisionType: alert?.decisionType || "",
        decisionReasons: Array.isArray(alert?.decisionReasons) ? alert.decisionReasons : [],
        note: alert?.fullNote || alert?.note || "",
        policyVersion: alert?.policyVersion || "",
        buyDate: alert?.buyDate || "",
        buyDateKnown: alert?.buyDateKnown === true,
        sellShares: Number(alert?.sellShares || 0),
        strategyHorizon,
      },
    };
  }).filter((signal) => signal.symbol);
  if (!signals.length) return;
  fetch("/api/strategy-signals", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ signals }),
  }).catch(() => {});
}

function renderPortfolioAlerts(items = null, holdings = readPortfolioHoldings(), options = {}) {
  const tbody = document.getElementById("portfolioAlertRows");
  if (!tbody) return 0;
  resetDecisionTraceScope("portfolio-alerts");
  clearLegacyDefensePopups();
  const status = document.getElementById("portfolioAlertStatus");
  const stats = document.getElementById("portfolioAlertStats");
  const alertItems = Array.isArray(items) ? items : portfolioDisplayItems(holdings);
  const liveAlerts = buildPortfolioAlerts(alertItems, holdings, { fetchBrain: options.fetchBrain === true });
  const hasFormalAlerts = liveAlerts.some((alert) => alert?.type !== "等待正式資料");
  const hasBrainPendingAlerts = liveAlerts.some((alert) => alert?.brainPending);
  const hasCanonicalBackendPayload = initialPortfolioExitReady
    && Array.isArray(portfolioExitAnalysisState.payload?.alerts);
  // 後端已回傳統一結果後，它就是唯一真相。即使新結果全是「週期未知／只觀察」
  // 或空清單，也不能用 localStorage 裡先前的短期停損快照蓋回畫面。
  if (hasCanonicalBackendPayload) safeRemoveItem(portfolioAlertCacheStorageKey());
  if (liveAlerts.length && !hasBrainPendingAlerts && (hasFormalAlerts || options.allowWaitingOnlyCache)) writePortfolioAlertCache(liveAlerts);
  const hidden = readHiddenPortfolioAlerts();
  const cached = readPortfolioAlertCache();
  const cachedAlerts = cached.alerts.filter((alert) => alert?.key && !hidden.has(alert.key));
  const useLiveAlerts = hasCanonicalBackendPayload || (liveAlerts.length && (
    (hasFormalAlerts && (!hasBrainPendingAlerts || !cachedAlerts.length || options.allowBrainPendingLive)) ||
    options.allowWaitingOnlyLive ||
    (!cachedAlerts.length && !hasFormalAlerts)
  ));
  const alerts = useLiveAlerts ? liveAlerts : cachedAlerts;
  const usingCache = alerts.length && !useLiveAlerts;
  const lastBuild = Number(safeGetItem(portfolioAlertLastBuildStorageKey(), "0"));
  const backendGeneratedAt = String(portfolioExitAnalysisState.payload?.generatedAt || "").replace(" ", "T");
  const displayTime = usingCache && cached.updatedAt
    ? new Date(cached.updatedAt)
    : backendGeneratedAt ? new Date(backendGeneratedAt) : lastBuild ? new Date(lastBuild) : new Date();
  const nowText = displayTime.toLocaleTimeString("zh-TW", { hour: "2-digit", minute: "2-digit", hour12: false });
  const hiddenCount = hidden.size;
  const alertGroups = groupPortfolioAlerts(alerts);
  renderRiskOverview(alertGroups, holdings);
  syncExitWatchToServer(alertGroups);
  if (status) status.textContent = alerts.length ? `${usingCache ? "上次後端快照" : "後端統一計算"} ${alertGroups.length} 檔` : "尚未建立";
  if (stats) stats.textContent = alerts.length
    ? `共 ${alertGroups.length} 檔｜提醒 ${alerts.length} 筆｜${usingCache ? "快照" : "後端更新"} ${nowText}｜隱藏 ${hiddenCount} 筆`
    : liveAlerts.length && !hasFormalAlerts
      ? "正式資料與規則載入中，先顯示持股保底列"
      : "尚未建立提醒";
  if (!alerts.length) {
    tbody.innerHTML = `
      <tr>
        <td colspan="9" class="empty-state">目前沒有持股提醒。更新永豐庫存後，按「建立提醒」即可重建。</td>
      </tr>
    `;
    return 0;
  }
  tbody.innerHTML = alertGroups.map((group, index) => {
    const priorityAlert = [group.sell, group.confirm].find((alert) => alert?.canNotify)
      || [group.confirm, group.sell].find((alert) => alert?.decisionVerified)
      || group.confirm || group.sell || group.defense;
    const orderPrice = Number(group.currentPrice || priorityAlert?.currentPrice || priorityAlert?.targetPrice || group.sell?.targetPrice || group.confirm?.targetPrice || group.defense?.targetPrice || 0);
    const statusClass = portfolioAlertStatusClass(priorityAlert);
    const traceId = registerDecisionTrace(portfolioAlertTrace(group, priorityAlert, usingCache));
    return `
      <tr>
        <td class="row-index">
          <label class="alert-row-check">
            <input type="checkbox" value="group:${escapeHtml(group.code)}" />
            <span>${index + 1}</span>
          </label>
        </td>
        <td>
          <button
            type="button"
            class="portfolio-stock-button code-button"
            data-pick="${escapeHtml(group.code)}"
            data-order-fill="1"
            data-order-action="SELL"
            data-order-price="${Number.isFinite(orderPrice) ? orderPrice : 0}"
            data-current-price="${Number(group.currentPrice || priorityAlert?.currentPrice || 0)}"
          >
            ${escapeHtml(group.code)}
          </button>
        </td>
        <td class="stock-name">${escapeHtml(stockDisplayName(group.code, group.name) || group.code)}</td>
        <td>${alertPriceText(group.holdingPrice)}</td>
        <td class="${dashboardTone(group.pnlRate)}">${alertPriceText(group.currentPrice)}</td>
        <td class="up">${alertPriceText(group.sell?.targetPrice)}</td>
        <td class="down">${alertPriceText(group.defense?.targetPrice)}</td>
        <td class="portfolio-alert-status ${statusClass} decision-trace-open" ${decisionTraceOpenAttrs(traceId)}>${escapeHtml(priorityAlert?.status || "-")}</td>
        <td class="portfolio-alert-note">
          <span class="decision-trace-open" ${decisionTraceOpenAttrs(traceId)}>${escapeHtml(priorityAlert?.note || "-")}</span>
          ${decisionTraceButton(traceId)}
        </td>
      </tr>
    `;
  }).join("");
  bindStockPickButtons();
  bindDecisionTraceButtons();
  if (!usingCache && !options.skipNotifications) {
    recordPortfolioAlertSignals(alerts);
    alerts.forEach(notifyPortfolioAlertRowOnce);
  }
  return alertGroups.length;
}

function bindPortfolioAlertControls() {
  const buildButton = document.getElementById("buildPortfolioAlerts");
  const deleteButton = document.getElementById("deletePortfolioAlerts");
  const testButton = document.getElementById("testPortfolioAlertPopup");
  const monitor = document.getElementById("portfolioAlertMonitor");
  // 「只通知賣點」開關已移除：它從未被任何通知判斷讀取(死開關)，
  // 且現行通知本來就只有賣出類(sell/confirm)，掛著只會誤導。
  const interval = document.getElementById("portfolioAlertInterval");
  if (buildButton) {
    buildButton.addEventListener("click", async () => {
      const defaultText = buildButton.dataset.defaultText || buildButton.textContent;
      buildButton.dataset.defaultText = defaultText;
      const status = document.getElementById("portfolioAlertStatus");
      buildButton.disabled = true;
      buildButton.textContent = "建立中";
      if (status) status.textContent = "正在讀取後端統一出場計算";
      try {
        saveHiddenPortfolioAlerts(new Set());
        await loadPortfolioExitAnalysis({ render: false });
        safeSetItem(portfolioAlertLastBuildStorageKey(), String(Date.now()));
        const count = renderPortfolioAlerts(undefined, undefined, { allowWaitingOnlyLive: true, allowWaitingOnlyCache: true });
        startPortfolioAlertMonitor();
        buildButton.textContent = count ? `已建立 ${count} 檔提醒` : "目前沒有可建立的提醒";
        if (!count && status) status.textContent = portfolioExitAnalysisState.error || "沒有可建立提醒，請先同步券商庫存";
      } catch (error) {
        if (status) status.textContent = friendlyError(error);
        buildButton.textContent = "建立失敗";
      } finally {
        setTimeout(() => {
          buildButton.disabled = false;
          buildButton.textContent = defaultText;
        }, 1500);
      }
    });
  }
  if (deleteButton) {
    deleteButton.addEventListener("click", () => {
      const checked = [...document.querySelectorAll("#portfolioAlertRows input[type='checkbox']:checked")].map((item) => item.value);
      const status = document.getElementById("portfolioAlertStatus");
      if (!checked.length) {
        if (status) status.textContent = "請先勾選要刪除的提醒";
        return;
      }
      const hidden = readHiddenPortfolioAlerts();
      checked.forEach((key) => {
        if (key.startsWith("group:")) {
          const code = key.slice("group:".length);
          ["sell", "defense", "confirm"].forEach((type) => hidden.add(`${code}-${type}`));
        } else {
          hidden.add(key);
        }
      });
      saveHiddenPortfolioAlerts(hidden);
      renderPortfolioAlerts();
      if (status) status.textContent = `已刪除 ${checked.length} 檔`;
    });
  }
  if (testButton) {
    testButton.addEventListener("click", async () => {
      const message = "這是測試通知：如果你看到這個視窗，代表網頁提醒可以正常顯示。";
      showPortfolioPopup("買賣停損提醒測試", message, { sticky: true });
      const [desktopResult, lineResult] = await Promise.all([
        sendDesktopNotification("買賣停損提醒測試", message, { tag: "stockai-alert-test", requireInteraction: true }),
        sendLineNotification(`【StockAI 買賣停損提醒測試】\n${message}`),
      ]);
      const channelText = [
        desktopResult.sent ? "桌面通知已送出" : `桌面通知未送出：${desktopResult.error || "未知原因"}`,
        lineResult.sent ? "LINE 已送出" : `LINE 未送出：${lineResult.error || "未知原因"}`,
      ].join("；");
      showPortfolioPopup("通知通道測試結果", channelText, { sticky: true, wide: true });
      state.alertLog.unshift({
        time: new Date().toLocaleString("zh-TW"),
        rule: "測試通知",
        target: `${message} ${channelText}`
      });
      renderAlerts();
    });
  }
  [monitor, interval].filter(Boolean).forEach((control) => {
    control.addEventListener("change", () => {
      if (control === monitor) {
        // 記住使用者對「賣出/停損通知」的選擇(勾掉=不通知),跨重整保留。
        try { localStorage.setItem(PORTFOLIO_ALERT_NOTIFY_KEY, monitor.checked ? "1" : "0"); } catch {}
      }
      renderPortfolioAlerts();
      startPortfolioAlertMonitor();
    });
  });
  // 還原上次的通知開關選擇;沒存過就沿用 HTML 預設(開啟)。
  if (monitor) {
    try {
      const savedNotify = localStorage.getItem(PORTFOLIO_ALERT_NOTIFY_KEY);
      if (savedNotify !== null) monitor.checked = savedNotify === "1";
    } catch {}
  }
  // 三個細分通知開關(停利/獲利部位停損/虧損部位停損):各自還原+存檔,只影響通知類型閘門。
  wireNotifyToggle("notifyTakeProfit", NOTIFY_TAKEPROFIT_KEY);
  wireNotifyToggle("notifyStopProfit", NOTIFY_STOP_PROFIT_KEY);
  wireNotifyToggle("notifyStopLoss", NOTIFY_STOP_LOSS_KEY);
  // 賣出 LINE 全域靜音:存伺服器(非 localStorage),勾選即 POST /api/user-prefs,任何裝置一致。
  // 初始勾選狀態由 loadUserPrefs() 從伺服器同步(見上面)。
  const sellLineMuteToggle = document.getElementById("sellLineMute");
  if (sellLineMuteToggle) {
    sellLineMuteToggle.addEventListener("change", () => {
      sellLineMutedGlobal = sellLineMuteToggle.checked;
      fetch("/api/user-prefs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sellLineMuted: sellLineMutedGlobal }),
      }).catch(() => {});
    });
  }
  startPortfolioAlertMonitor();
}

function stopPortfolioAlertMonitor() {
  if (portfolioAlertMonitorTimer) {
    clearInterval(portfolioAlertMonitorTimer);
    portfolioAlertMonitorTimer = null;
  }
}

function portfolioAlertIntervalMs() {
  const input = document.getElementById("portfolioAlertInterval");
  const minutes = Math.max(1, Number(input?.value || 1));
  return minutes * 60 * 1000;
}

function writePortfolioAlertStatus(text) {
  const status = document.getElementById("portfolioAlertStatus");
  if (status) status.textContent = text;
}

async function runPortfolioAlertMonitorTick() {
  const monitor = document.getElementById("portfolioAlertMonitor");
  if (!monitor?.checked) {
    stopPortfolioAlertMonitor();
    writePortfolioAlertStatus("背景監控已關閉");
    notifyExitWatchMonitoringDisabled();
    return;
  }
  const session = taiwanMarketSession();
  if (!session.isOpen) {
    stopPortfolioAlertMonitor();
    writePortfolioAlertStatus(session.label);
    return;
  }
  // 每一輪都要走完「同步庫存 → 後端統一出場分析 → 顯示/通知」，
  // 各步失敗不影響下一輪：
  // - 舊版只呼叫 syncSinopacHoldings()，同步失敗(永豐重複登入 400 很常見)
  //   那一輪就完全沒有通知評估；Brain 結論更是從來不重抓，重載後全滅。
  // - renderPortfolioAlerts 一定要跑：就算庫存/Brain 都沒更新，價格快取與
  //   既有結論仍能評估通知，這才是「背景監控」的最低保證。
  try {
    await syncSinopacHoldings();
  } catch (error) {
    console.warn("監控輪永豐同步失敗，仍繼續評估提醒", error);
  }
  try {
    await loadPortfolioExitAnalysis({ render: false });
  } catch (error) {
    console.warn("監控輪後端出場分析失敗，沿用上一份後端快照", error);
  }
  renderPortfolioAlerts();
  // lastBuild 時間戳只在真的完成一輪評估後才寫：舊版不管同步成功/失敗/
  // 被節流跳過都無條件更新，讓「更新 HH:MM」顯示假的新鮮度。
  safeSetItem(portfolioAlertLastBuildStorageKey(), String(Date.now()));
}

function startPortfolioAlertMonitor() {
  stopPortfolioAlertMonitor();
  const monitor = document.getElementById("portfolioAlertMonitor");
  if (!monitor?.checked) {
    writePortfolioAlertStatus("背景監控已關閉");
    return;
  }
  const session = taiwanMarketSession();
  const minutes = Math.round(portfolioAlertIntervalMs() / 60000);
  if (!session.isOpen) {
    writePortfolioAlertStatus(`${session.label} 開盤後每 ${minutes} 分鐘監控一次。`);
    return;
  }
  writePortfolioAlertStatus(`背景監控中，每 ${minutes} 分鐘檢查一次`);
  portfolioAlertMonitorTimer = setInterval(runPortfolioAlertMonitorTick, portfolioAlertIntervalMs());
}

function renderScanner() {
  const holdings = readPortfolioHoldings();
  const items = portfolioDisplayItems(holdings);
  const rendered = items.map(({ stock, rows, fullIndicators, latest, previous, missingSignal, fallbackSignal }, index) => {
            const holding = holdings[stock.code] || {};
            const lots = Number(holding.quantity || 0);
            const shares = lots > 0 ? lots * 1000 : 0;
            const buyAmount = shares && holding.price ? holding.price * shares : null;
            const snapshotPrice = Number(holding.currentPrice);
            const fallbackLatest = {
              close: Number.isFinite(snapshotPrice) && snapshotPrice > 0 ? snapshotPrice : Number(holding.price || 0),
              volume: Number(holding.totalVolume || 0)
            };
            latest = latest || fallbackLatest;
            const pnl = Number.isFinite(Number(holding.pnl))
              ? Number(holding.pnl)
              : (shares && holding.price ? latest.close * shares - buyAmount : null);
            const currentAmount = Number.isFinite(buyAmount) && Number.isFinite(pnl)
              ? buyAmount + pnl
              : (shares ? latest.close * shares : null);
            const brokerPrice = shares && Number.isFinite(currentAmount) ? currentAmount / shares : null;
            const currentPrice = Number.isFinite(snapshotPrice) && snapshotPrice > 0
              ? snapshotPrice
              : Number.isFinite(brokerPrice) ? brokerPrice : latest.close;
            const priceChange = Number.isFinite(Number(holding.changePrice)) ? Number(holding.changePrice) : null;
            const priceChangePct = Number.isFinite(Number(holding.changeRate)) ? Number(holding.changeRate) : null;
            const pnlRate = Number.isFinite(Number(pnl)) && Number.isFinite(Number(buyAmount)) && Number(buyAmount)
              ? (Number(pnl) / Number(buyAmount)) * 100
              : null;
            const hasRealDailyRows = Boolean(dataCache.get(stock.code)?.rows?.length);
            const portfolioMarket = hasRealDailyRows ? marketContextFromRows(rows) : { stockStrongerThanMarket: false };
            const exitPlan = shares ? portfolioExitItemFor(stock.code) : null;
            const volumeWindow = Array.isArray(rows) ? rows.slice(-20) : [];
            const avgVolume20 = volumeWindow.reduce((sum, row) => sum + Number(row.volume || 0), 0) / Math.max(volumeWindow.length, 1);
            const volumeRatio = Number(latest.volume || 0) / Math.max(avgVolume20, 1);
            const trend = fallbackSignal && !hasRealDailyRows
              ? portfolioTrendBadge(portfolioMarket, priceChangePct, pnlRate, exitPlan)
              : portfolioTrendBadge(portfolioMarket, priceChangePct, pnlRate, exitPlan);
            const force = portfolioForceBadge(portfolioMarket, priceChangePct, volumeRatio, pnlRate);
            const risk = shares ? portfolioRiskBadge(exitPlan, pnlRate, priceChangePct, trend, force) : { text: "未持有｜觀察", tone: "warn" };
            const tableRow = `
              <tr>
                <td class="row-index">${index + 1}</td>
                <td>
                  <button type="button" class="portfolio-stock-button code-button" data-pick="${stock.code}">
                    ${stock.code}
                  </button>
                </td>
                <td class="stock-name">${escapeHtml(stockDisplayName(stock.code, holding.name || stock.name) || stock.code)}</td>
                <td>${compactShares(shares)}</td>
                <td>${Number(holding.price) ? fmt(holding.price) : "-"}</td>
                <td>${Number.isFinite(Number(currentPrice)) ? fmt(currentPrice) : "-"}</td>
                <td class="${dashboardTone(priceChange)}">
                  ${Number.isFinite(priceChange) && Number.isFinite(priceChangePct) ? `${signedPrice(priceChange)} (${pct(priceChangePct)})` : "-"}
                </td>
                <td class="money-cell">${Number.isFinite(Number(buyAmount)) ? fmt(buyAmount, 0) : "-"}</td>
                <td class="money-cell">${Number.isFinite(Number(currentAmount)) ? fmt(currentAmount, 0) : "-"}</td>
                <td class="${dashboardTone(pnl)}">${signedMoney(pnl)}</td>
                <td class="${dashboardTone(pnlRate)}">${Number.isFinite(Number(pnlRate)) ? pct(pnlRate) : "-"}</td>
                <td class="${trend.tone} status-cell">${trend.text}</td>
                <td class="${force.tone} status-cell">${force.text}</td>
                <td class="${risk.tone} risk-cell">${risk.text}</td>
              </tr>
            `;
            // 手機卡片:同一筆持股資料的直向卡片(名稱大字/損益%大字/現價/今日/狀態),
            // ≤720px 隱藏表格、改顯示這些卡片(電腦版反之);點卡片=同代號按鈕行為(data-pick)。
            const holdingName = escapeHtml(stockDisplayName(stock.code, holding.name || stock.name) || stock.code);
            const card = `
              <button type="button" class="holding-card" data-pick="${stock.code}">
                <div class="hc-top">
                  <span class="hc-name">${holdingName} <b>${stock.code}</b></span>
                  <span class="hc-pnl-wrap">
                    <span class="hc-pnl ${dashboardTone(pnlRate)}">損益 ${Number.isFinite(Number(pnlRate)) ? pct(pnlRate) : "-"}</span>
                    <span class="hc-pnl-amt ${dashboardTone(pnl)}">${Number.isFinite(Number(pnl)) ? `${signedMoney(pnl)} 元` : "-"}</span>
                  </span>
                </div>
                <div class="hc-mid">
                  <span>現價 ${Number.isFinite(Number(currentPrice)) ? fmt(currentPrice) : "-"}</span>
                  <span class="hc-today ${dashboardTone(priceChange)}">今日 ${Number.isFinite(priceChangePct) ? pct(priceChangePct) : "-"}</span>
                </div>
                <div class="hc-status ${risk.tone}">${risk.text}</div>
              </button>
            `;
            return { tableRow, card };
          });

  document.getElementById("watchlist").innerHTML = `
    ${renderPortfolioSummary()}
    <div class="feature-table-wrap portfolio-table-wrap yongfeng-holdings-table-wrap holdings-desktop-only">
      <table class="portfolio-table yongfeng-dashboard-table">
        <thead>
          <tr>
            <th class="row-index"></th>
            <th>代號</th>
            <th>名稱</th>
            <th>持有股數</th>
            <th>買進均價</th>
            <th>現價</th>
            <th>今日漲跌</th>
            <th>持股成本</th>
            <th>庫存市值</th>
            <th>未實現損益</th>
            <th>損益率</th>
            <th>續漲</th>
            <th>買賣力道</th>
            <th>風險</th>
          </tr>
        </thead>
        <tbody>${rendered.map((r) => r.tableRow).join("")}</tbody>
      </table>
    </div>
    <div class="holdings-cards-mobile">${rendered.map((r) => r.card).join("")}</div>
  `;
  renderPortfolioAlerts(items, holdings, { allowWaitingOnlyLive: false, allowWaitingOnlyCache: false });
  bindStockPickButtons();
}

function renderScannerLegacy() {
  const items = allSignals();
  const holdings = readPortfolioHoldings();
  document.getElementById("watchlist").innerHTML = `
    <div class="feature-table-wrap portfolio-table-wrap">
      <table class="portfolio-table">
        <thead>
          <tr>
            <th>股票</th>
            <th>目前股價</th>
            <th>漲跌</th>
            <th>股數</th>
            <th>買入金額</th>
            <th>目前金額</th>
            <th>盈虧金額</th>
            <th>風控停損價</th>
            <th>+10% 達標里程碑</th>
            <th>移動保護價</th>
            <th>規則調整原因</th>
            <th>提醒</th>
          </tr>
        </thead>
        <tbody>
          ${items.map(({ stock, rows, fullIndicators, latest, previous, riskPct }) => {
            const holding = holdings[stock.code] || {};
            const lots = Number(holding.quantity || 0);
            const shares = lots > 0 ? lots * 1000 : 0;
            const buyAmount = shares && holding.price ? holding.price * shares : null;
            const pnl = Number.isFinite(Number(holding.pnl))
              ? Number(holding.pnl)
              : (shares && holding.price ? latest.close * shares - buyAmount : null);
            const currentAmount = Number.isFinite(buyAmount) && Number.isFinite(pnl)
              ? buyAmount + pnl
              : (shares ? latest.close * shares : null);
            const brokerPrice = shares && Number.isFinite(currentAmount) ? currentAmount / shares : null;
            const priceSnapshot = priceSnapshotForAnalysis(stock, latest, previous);
            const currentPrice = Number.isFinite(Number(priceSnapshot.price))
              ? Number(priceSnapshot.price)
              : Number.isFinite(brokerPrice) ? brokerPrice : latest.close;
            const hasRealDailyRows = Boolean(dataCache.get(stock.code)?.rows?.length);
            const priceChange = Number.isFinite(Number(priceSnapshot.changePrice))
              ? Number(priceSnapshot.changePrice)
              : hasRealDailyRows && previous?.close ? latest.close - previous.close : null;
            const priceChangePct = Number.isFinite(Number(priceSnapshot.changePct))
              ? Number(priceSnapshot.changePct)
              : hasRealDailyRows && previous?.close ? (priceChange / previous.close) * 100 : null;
            const portfolioMarket = marketContextFromRows(rows);
            const exitPlan = portfolioExitItemFor(stock.code) || {
              type: "unknown", status: "等待後端出場分析", note: "尚未取得統一結果"
            };
            const exitTone = exitPlan.type === "stop" || exitPlan.type === "phase3"
              ? "down"
              : exitPlan.type === "phase1" || exitPlan.type === "phase2"
                ? "up"
                : "";
            return `
              <tr>
                <td>
                  <button type="button" class="portfolio-stock-button" data-pick="${stock.code}">
                    ${stockLabel(stock)}
                  </button>
                  <small>${stock.sector} / 真實價量規則 / 波動 ${fmt(riskPct, 1)}%</small>
                </td>
                <td>${priceText(currentPrice)}</td>
                <td class="${Number(priceChange) >= 0 ? "up" : "down"}">
                  ${Number.isFinite(priceChange) ? `${priceChange >= 0 ? "+" : ""}${fmt(priceChange)} 元` : "-"}
                  <small>${Number.isFinite(priceChangePct) ? pct(priceChangePct) : ""}</small>
                </td>
                <td>${shares ? `${fmt(shares, 0)} 股` : "-"}</td>
                <td>${money(buyAmount)}</td>
                <td>${money(currentAmount)}</td>
                <td class="${Number(pnl) >= 0 ? "up" : "down"}">${money(pnl)}</td>
                <td>${priceText(exitPlan.stopLoss)}</td>
                <td>${priceText(exitPlan.takeProfit)}</td>
                <td>${priceText(exitPlan.trailingStop)}</td>
                <td><small>${escapeHtml(exitPlan?.strategyHorizonLabel || "週期未知")}｜後端統一出場引擎</small></td>
                <td class="${exitTone}">
                  ${exitPlan.status}
                  <small>${exitPlan.note}</small>
                </td>
              </tr>
            `;
          }).join("")}
        </tbody>
      </table>
    </div>
  `;
  bindStockPickButtons();
}

function renderAlerts() {
  // 記憶體防護:state.alertLog 是無界成長陣列(跨天不重置),分頁開久了會單調變大、
  // 對應 DOM 也線性膨脹=「開越久越卡」。截到最新 100 筆釋放舊記憶體。放在最前面,
  // 即使顯示元件 #alertLog 已從妖股版移除、下面 early-return,陣列本身仍會被截斷。
  // 不動賣出決策,也不動 localStorage 的 exitAlertLog 去重紀錄(那條另有當日 prune)。
  if (state.alertLog.length > 100) state.alertLog.length = 100;
  const log = document.getElementById("alertLog");
  if (!log) return;
  if (!state.alertLog.length) {
    log.innerHTML = `<div class="alert-message"><span>尚未建立提醒</span><strong>等待條件</strong></div>`;
    return;
  }
  log.innerHTML = state.alertLog.map((item) => `<div class="alert-message"><span>${item.time}<br>${item.rule}</span><strong>${item.target}</strong></div>`).join("");
}


// 交易複盤【探測版】:抓永豐已實現損益(你已賣出的真實買賣+損益)。由使用者按鈕觸發永豐登入
// (Claude 不自己登券商)。伺服器把完整原始記錄寫進探測檔,前端只顯示 count+樣本;確認真實欄位
// 長相後,下一步才把它映射寫進 trades 表、接「買進日對齊妖股推薦」的複盤比對。唯讀、不下單。
function bindRealizedPnlProbe() {
  const btn = document.getElementById("probeRealizedPnl");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const status = document.getElementById("sinopacSyncStatus");
    const prevText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "抓取中…";
    if (status) status.textContent = "🔄 抓永豐已實現損益中…(需登入永豐,約 10–30 秒,唯讀不下單)";
    try {
      const response = await fetch("/api/sinopac/realized-pnl?days=180");
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.ok) {
        if (status) status.textContent = `❌ 抓取失敗：${payload.error || "未知錯誤"}`;
      } else {
        const savedTxt = (payload.saved != null) ? `，已存入系統 ${payload.saved} 筆` : "";
        if (status) status.textContent = `✅ 永豐已實現損益：抓到 ${payload.count} 筆${savedTxt}（交易複盤用）`;
        console.log("[realized-pnl] count =", payload.count, "saved =", payload.saved, "accountMasked =", payload.accountMasked, "sample =", payload.sample);
      }
    } catch (error) {
      if (status) status.textContent = `❌ 抓取錯誤：${friendlyError(error)}`;
    } finally {
      btn.disabled = false;
      btn.textContent = prevText;
    }
  });
}

function loadInitialRadarData() {
  loadMonsterScores();
}

function initControls(options = {}) {
  bindPortfolioAlertControls();
  bindPortfolioHorizonControls();
  document.getElementById("syncSinopacHoldings").addEventListener("click", syncSinopacHoldings);
  bindRealizedPnlProbe();
  const scanMonsterButton = document.getElementById("scanMonsterMarket");
  if (scanMonsterButton) scanMonsterButton.addEventListener("click", runMonsterMarketScan);
  if (options.deferRadarLoads !== true) {
    renderMonsterRadar();
  }
  if (options.deferRadarLoads !== true) window.setTimeout(loadInitialRadarData, 800);
  initSinopacOrderControls();
  initSinopacAutoSyncControls();
  loadStockInfo().then(() => {
    renderTickerOptions();
    render();
  }).catch((error) => console.warn("股票名稱讀取失敗", error));
  const strategySelect = document.getElementById("strategySelect");
  if (strategySelect) {
    strategySelect.addEventListener("change", (event) => {
      state.strategy = event.target.value;
      render();
    });
  }
  document.querySelectorAll("[data-overlay]").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll("[data-overlay]").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      state.overlay = button.dataset.overlay;
      render();
    });
  });
  applyTheme();
  initSidebarToggle();
  document.getElementById("themeToggle").addEventListener("click", () => {
    applyTheme(document.body.classList.contains("dark") ? "light" : "dark");
    render();
  });
  const saveTokenButton = document.getElementById("saveFinmindToken");
  if (saveTokenButton) saveTokenButton.addEventListener("click", async () => {
    const input = document.getElementById("finmindTokenInput");
    const badge = document.getElementById("tokenStatus");
    const token = input.value.trim();
    badge.textContent = "儲存中";
    badge.style.background = "#eef4fb";
    badge.style.color = "#2fa8ff";
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
      await reloadRealData();
    } catch (error) {
      badge.textContent = friendlyError(error);
      badge.style.background = "#fff4e6";
      badge.style.color = "#fff200";
    }
  });
  const clearTokenButton = document.getElementById("clearFinmindToken");
  if (clearTokenButton) clearTokenButton.addEventListener("click", async () => {
    const badge = document.getElementById("tokenStatus");
    badge.textContent = "清除中";
    badge.style.background = "#eef4fb";
    badge.style.color = "#2fa8ff";
    await fetch("/api/settings/finmind-token", { method: "DELETE" });
    await loadTokenStatus();
    await reloadRealData();
  });
  const alertForm = document.getElementById("alertForm");
  if (alertForm) alertForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const email = document.getElementById("emailInput").value.trim();
    const phone = document.getElementById("phoneInput").value.trim();
    const rule = document.getElementById("alertRule").value;
    const target = [email, phone].filter(Boolean).join(" / ") || "站內通知";
    state.alertLog.unshift({ time: new Date().toLocaleString("zh-TW"), rule, target });
    renderAlerts();
  });
  const refreshMlStatusButton = document.getElementById("refreshMlStatus");
  if (refreshMlStatusButton) refreshMlStatusButton.addEventListener("click", loadBackendStatus);
  const runMlUpdateButton = document.getElementById("runMlUpdate");
  if (runMlUpdateButton) runMlUpdateButton.addEventListener("click", runBackendUpdate);
  const systemHealthButton = document.getElementById("systemHealthCheck");
  if (systemHealthButton) systemHealthButton.addEventListener("click", runSystemHealthCheck);
  initForceReloadAndBuildStamp();
}

// 前端版本戳 + 強制更新按鈕。伺服器對 index.html/app.js 送 Cache-Control: no-store,
// 瀏覽器本來就每次載入都拿最新、不快取,所以「強制更新」在重新載入這件事上其實是多餘的;
// 真正的用途是讓使用者「看得到、確認得了自己在最新版」——版本戳直接讀 app.js 自己的
// ?v= 建置字串(YYYYMMDDHHmm),部署新版時它會跟著變,按鈕只是把重新載入變成看得到的鈕。
function appBuildVersion() {
  const tag = document.querySelector('script[src*="app.js"]');
  const m = tag && String(tag.src).match(/[?&]v=(\d+)/);
  return m ? m[1] : "";
}

function formatBuildVersion(v) {
  if (!/^\d{12}$/.test(v)) return v || "—";
  return `${v.slice(4, 6)}/${v.slice(6, 8)} ${v.slice(8, 10)}:${v.slice(10, 12)}`;
}

function initForceReloadAndBuildStamp() {
  const stamp = document.getElementById("appBuildStamp");
  if (stamp) {
    const v = appBuildVersion();
    stamp.textContent = `版本 ${formatBuildVersion(v)}`;
    stamp.title = `目前載入的前端版本 ${v || "未知"}｜伺服器 no-store,每次開都是最新`;
  }
  const btn = document.getElementById("forceReloadBtn");
  if (btn) btn.addEventListener("click", () => window.location.reload());
}

// resize 拖動視窗時每秒可觸發數十次,每次 render() 都重繪多張表——debounce 收斂成
// 停手後才繪一次,避免拖視窗瞬間掉影格。
let _resizeRenderTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(_resizeRenderTimer);
  _resizeRenderTimer = setTimeout(render, 150);
});
// 瀏覽器對背景分頁的 setInterval 會節流(甚至完全暫停)，賣出警示輪詢
// (portfolioAlertMonitorTimer)在分頁切到背景時可能延遲數分鐘到數十分鐘
// 才執行。分頁切回前景時立刻補跑一次評估，不用等下一個排定的 interval。
async function reconcileExitGuardianNotifiedToday() {
  // 2026-07-04 稽核修復：分頁在背景期間，伺服器端停損守門員可能已經接管
  // 並對某些代碼送出 critical LINE，但前端 localStorage 的去重紀錄
  // (exitAlertLog)完全不知道這件事——切回前景補跑監控時，如果不先同步
  // 這份資訊，會對伺服器已經通知過的同一次跌破再通知一次(桌面+可能再一則
  // LINE)。伺服器 check_portfolio_exit_guardian 的 breach 判斷等同前端
  // 「confirm」型(跌破防守價確認賣出)，只需要補這個型別的去重紀錄。
  try {
    const response = await fetch("/api/portfolio/exit-watch/notified-today");
    const payload = await response.json();
    if (!response.ok || !payload.ok || !Array.isArray(payload.symbols) || !payload.symbols.length) return;
    const log = readExitAlertLog();
    const todayPrefix = dateKey(new Date());
    let changed = false;
    payload.symbols.forEach((code) => {
      const key = `${todayPrefix}-${code}-alert-confirm`;
      if (!log[key]) {
        log[key] = new Date().toISOString();
        changed = true;
      }
    });
    if (changed) saveExitAlertLog(log);
  } catch (error) {
    console.warn("查詢伺服器停損守門員已通知清單失敗(不影響前端提醒)", error);
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible") return;
  // 2026-07-08：分頁切回前景時主動重抓妖股雷達。手機尤其常見——分頁切到背景時
  // 行動瀏覽器會暫停 setInterval，每 30 秒偵測新掃描的 market clock(checkBackgroundMonsterScan)
  // 就停了，切回前景又沒重抓的話，妖股名單會停在背景前的舊結果、跟一直開著前景的
  // 電腦不同步。這裡回前景就重抓一次跨裝置共享的 /api/monster-scores 分數，並接回
  // 掃描進度輪詢(若正好有掃描在跑)，確保手機/電腦看到同一份名單。
  loadMonsterScores().catch((error) => console.warn("分頁切回前景後重抓妖股名單失敗", error));
  checkBackgroundMonsterScan().catch(() => {});
  reconcileExitGuardianNotifiedToday()
    .catch((error) => console.warn("同步伺服器停損守門員通知紀錄失敗", error))
    .finally(() => {
      runPortfolioAlertMonitorTick().catch((error) => {
        console.warn("分頁切回前景後補跑監控失敗", error);
      });
    });
});
// ⚠️ 已停用(2026-07-09):此函式已搬到獨立頁 trades.js(交易複盤獨立頁),主頁不再呼叫它。
// 保留在此僅為 diff 可讀,無任何呼叫點。要改複盤卡外觀請改 trades.js,別改這裡。
async function loadRealizedRadarReview() {
  // 真實成交複盤：讀永豐已實現損益(sinopac_realized_pnl)對齊 monster_scores 推薦史，
  // 誠實三態(✅雷達曾判可買／⚠️只進候選／⬜當時沒掃到／—雷達未上線)。不硬湊「跟單勝率」——
  // 多數舊交易發生在雷達上線前，把那些當自選會製造假訊號。輔助定位：讀不到/無資料就整塊隱藏。
  const box = document.getElementById("realizedRadarReview");
  if (!box) return;
  try {
    const response = await fetch("/api/portfolio/realized-review");
    const payload = await response.json();
    if (!response.ok || !payload.ok) { box.hidden = true; return; }
    const trades = payload.trades || [];
    const s = payload.summary || {};
    if (!trades.length) { box.hidden = true; return; }
    box.replaceChildren();

    const money = (v) => {
      const n = Number(v);
      if (!Number.isFinite(n)) return "-";
      return `${n >= 0 ? "+" : ""}${Math.round(n).toLocaleString("en-US")}`;
    };
    const pct = (v) => (Number.isFinite(Number(v)) ? `${Number(v) >= 0 ? "+" : ""}${Number(v).toFixed(1)}%` : "-");
    const cls = (v) => (Number(v) >= 0 ? "rrv-pos" : "rrv-neg"); // 台股漲紅跌綠

    // 標題
    const head = document.createElement("div");
    head.className = "rrv-head";
    const title = document.createElement("strong");
    title.textContent = "🧾 真實成交複盤（永豐已實現損益 × 雷達推薦）";
    head.append(title);
    box.append(head);

    // 摘要：真實勝率/總損益(這兩個是真的)
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

    // 誠實歸因(對抗覆核 HIGH):這行勝率/總損益是「使用者自己」的整體交易成績、不是雷達績效——
    // 多數交易在雷達上線前、只有極少數雷達當時真的判可買,不標清楚會被誤讀成雷達好棒。
    const attribution = document.createElement("div");
    attribution.className = "rrv-note rrv-attribution";
    attribution.textContent = "↑ 這是你整體交易的成績，不是雷達績效；雷達當時對每筆的判斷看下表「雷達當時」欄。";
    box.append(attribution);

    // 誠實 caveat（後端算好的 note：涵蓋窗/樣本不足/會累積）
    if (payload.note) {
      const note1 = document.createElement("div");
      note1.className = "rrv-note rrv-caveat";
      note1.textContent = payload.note;
      box.append(note1);
    }

    // 逐筆表格
    const stateMeta = {
      recommended: { label: "✅ 雷達曾判可買", clsName: "rrv-st-rec" },
      candidate_only: { label: "◐ 進過候選未判可買", clsName: "rrv-st-cand" },
      not_scanned: { label: "⬜ 當時沒掃到", clsName: "rrv-st-none" },
      no_history: { label: "— 雷達未上線", clsName: "rrv-st-nohist" },
    };
    const table = document.createElement("table");
    table.className = "rrv-table";
    const thead = document.createElement("thead");
    const htr = document.createElement("tr");
    ["股票", "賣出日", "損益", "報酬率", "雷達當時"].forEach((t) => {
      const th = document.createElement("th");
      th.textContent = t;
      htr.append(th);
    });
    thead.append(htr);
    table.append(thead);
    const tbody = document.createElement("tbody");
    trades.forEach((t) => {
      const tr = document.createElement("tr");
      const tdStock = document.createElement("td");
      tdStock.textContent = `${t.code}${t.name ? " " + t.name : ""}`;
      const tdDate = document.createElement("td");
      tdDate.textContent = t.sellDate || "-";
      const tdPnl = document.createElement("td");
      tdPnl.textContent = money(t.pnl);
      tdPnl.className = cls(t.pnl);
      const tdPct = document.createElement("td");
      tdPct.textContent = pct(t.pnlPct);
      tdPct.className = cls(t.pnlPct);
      const tdRadar = document.createElement("td");
      const meta = stateMeta[t.radarState] || stateMeta.no_history;
      const badge = document.createElement("span");
      badge.className = `rrv-badge ${meta.clsName}`;
      let label = meta.label;
      if (t.radarState === "recommended" && t.radarScore != null) label += `（分 ${t.radarScore}）`;
      badge.textContent = label;
      tdRadar.append(badge);
      tr.append(tdStock, tdDate, tdPnl, tdPct, tdRadar);
      tbody.append(tr);
    });
    table.append(tbody);
    // 近似對齊警語上移到表格正上方(對抗覆核 MEDIUM:原本沉在最底灰字太不顯眼),改白話。
    const approxNote = document.createElement("div");
    approxNote.className = "rrv-note rrv-approx";
    approxNote.textContent = "⚠️「雷達當時」欄是用賣出當天往前找最近一次掃描推估的，只能當參考、非精準對帳（永豐沒給買進日）。";
    box.append(approxNote);
    const wrap = document.createElement("div");
    wrap.className = "rrv-table-wrap";
    wrap.append(table);
    box.append(wrap);

    // 底部：三態分佈 + 近似對齊說明
    const st = s.byState || {};
    const cnt = (k) => ((st[k] || {}).count || 0);
    const footer = document.createElement("div");
    footer.className = "rrv-note";
    footer.textContent =
      `對齊分佈：✅可買 ${cnt("recommended")}／◐候選 ${cnt("candidateOnly")}` +
      `／⬜沒掃到 ${cnt("notScanned")}／—未上線 ${cnt("noHistory")}。`;
    box.append(footer);

    box.hidden = false;
  } catch (error) {
    box.hidden = true;
  }
}

async function loadCandidateFollowThrough() {
  // 候選複盤看板：用 prices 直算雷達候選掃描後 10 日實際走勢，補雷達戰績看板
  // (predictions.hit 未回填)的空窗。命中率只算滿10日的成熟候選；未滿10日只當
  // 「曾摸到+10%」的實例展示、不進分母。範圍=純規則評分前段候選池，非實際買賣績效。
  const box = document.getElementById("candidateFollowThrough");
  if (!box) return;
  try {
    const response = await fetch("/api/radar/candidate-followthrough");
    const payload = await response.json();
    if (!response.ok || !payload.ok) { box.hidden = true; return; }
    const o = payload.overall || {};
    const show = payload.topFollowThrough || [];
    // 完全沒有窗內候選、也沒有任何實例可秀 → 整塊隱藏，不干擾
    if (!o.candidatesInWindow && !show.length) { box.hidden = true; return; }
    box.className = "candidate-followthrough";
    box.replaceChildren();
    const pct = (v) => (Number.isFinite(Number(v)) ? `${(Number(v) * 100).toFixed(0)}%` : "-");
    const pct1 = (v) => (Number.isFinite(Number(v)) ? `${Number(v) >= 0 ? "+" : ""}${(Number(v) * 100).toFixed(1)}%` : "-");

    const head = document.createElement("div");
    head.className = "cft-head";
    const title = document.createElement("strong");
    title.textContent = `📊 候選複盤（近 ${payload.lookbackDays} 個掃描日．滿 ${payload.horizonDays} 交易日才計命中）`;
    head.append(title);
    box.append(head);

    const summary = document.createElement("div");
    summary.className = "cft-summary";
    if (o.settled > 0) {
      summary.textContent =
        `${o.settled} 檔已滿 ${payload.horizonDays} 日候選中，${pct(o.touched10pctRate)} 曾摸到 +10%` +
        `（第 ${payload.horizonDays} 日收盤達標 ${pct(o.hitCloseRate)}）；平均最大漲幅 ${pct1(o.avgMaxFavorable)}`;
    } else {
      summary.textContent =
        `窗內 ${o.candidatesInWindow} 檔候選，尚無滿 ${payload.horizonDays} 交易日可結算（數字待累積）；先看下方雷達已抓到過的實例`;
    }
    box.append(summary);

    // 分群小表(只在有成熟樣本時顯示；樣本不足的群標明不下比率)
    const groups = payload.groups || {};
    const groupRows = [];
    const pushGroup = (label, stat) => {
      if (!stat) return;
      if (stat.insufficientSample) {
        groupRows.push(`${label}：樣本不足（${stat.sample} 檔，先不下結論）`);
      } else {
        groupRows.push(`${label}：摸+10% ${pct(stat.touched10pctRate)}／收盤達標 ${pct(stat.hitCloseRate)}（${stat.sample} 檔）`);
      }
    };
    if (groups.bySurgeSetup) {
      pushGroup("點火型(surgeSetup)", groups.bySurgeSetup.surge);
      pushGroup("一般型", groups.bySurgeSetup.normal);
    }
    if (groups.byVolumeRatio) {
      pushGroup("爆量(量比≥5)", groups.byVolumeRatio.high);
      pushGroup("常量", groups.byVolumeRatio.normal);
    }
    if (groupRows.length) {
      const gl = document.createElement("ul");
      gl.className = "cft-groups";
      groupRows.forEach((t) => {
        const li = document.createElement("li");
        li.textContent = t;
        gl.append(li);
      });
      box.append(gl);
    }

    // 實例展示：雷達確實抓到過的 runner(曾摸+10%)，即使還沒滿10日也有今天可看的證據
    if (show.length) {
      const lbl = document.createElement("div");
      lbl.className = "cft-showcase-label";
      lbl.textContent = "雷達抓到過的實例（掃描後曾摸到 +10%）：";
      box.append(lbl);
      const ul = document.createElement("ul");
      ul.className = "cft-showcase";
      show.forEach((s) => {
        const li = document.createElement("li");
        const mature = s.matured ? "" : `／觀察 ${s.daysObserved} 日`;
        li.textContent = `${s.symbol} ${s.name || ""} 最大 ${pct1(s.maxFavorable)}（${s.scanDate} 起，${s.daysToPeak ?? "-"} 日達峰${mature}）`;
        ul.append(li);
      });
      box.append(ul);
    }

    const note = document.createElement("div");
    note.className = "cft-note";
    note.textContent = "範圍＝純規則評分前段候選池的後續價格，非你實際買賣績效；掉出候選≠漲浪結束。";
    box.append(note);
    box.hidden = false;
  } catch (error) {
    box.hidden = true;
  }
}

async function loadDividendCalendar() {
  // 持股的除權息日曆：除權息日參考價下調會打亂停損/停利價位語意，短線
  // 持股要提前知道。後端有日快取，這裡每次頁面載入問一次即可。
  const box = document.getElementById("dividendCalendar");
  if (!box) return;
  try {
    const response = await fetch("/api/holdings/dividend-calendar");
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "除權息日曆讀取失敗");
    const items = payload.items || [];
    if (!items.length) {
      box.hidden = true;
      return;
    }
    const parts = items.slice(0, 6).map((item) =>
      `${item.symbol} ${item.exDate} ${item.kind}(剩${item.daysUntil}天)`
    );
    box.textContent = `📅 除權息提醒：${parts.join("｜")}${items.length > 6 ? `｜…共 ${items.length} 筆` : ""}（除權息日參考價會下調，停損/停利價位記得跟著調整）`;
    box.hidden = false;
  } catch (error) {
    box.hidden = true;
  }
}

async function renderMarketLight() {
  // 大盤環境紅綠燈：妖股短線最怕大盤 turbulent，開頁面就用一句話+顏色告訴使用者
  // 今天適不適合積極做。綠燈可積極/黃燈保守/紅燈避開。後端純讀市場資料算 regime。
  const box = document.getElementById("marketLightBanner");
  if (!box) return;
  try {
    const response = await fetch("/api/market/status");
    const payload = await response.json();
    if (!response.ok || !payload.ok) { box.hidden = true; return; }
    dailyReportMarket = payload;   // 今日報告的「大盤環境」段直接複用,不再另外打一次 /api/market/status
    const icon = { green: "🟢", yellow: "🟡", red: "🔴" }[payload.light] || "⚪";
    const gapPct = Number(payload.taiexMaGapPct);
    const ret20Pct = Number(payload.taiexRet20Pct);
    const sign = (n) => (Number.isFinite(n) && n >= 0 ? "+" : "");
    const staleNote = payload.stale ? `（⚠️ 大盤資料 ${payload.staleDays} 天沒更新，僅供參考）` : "";
    box.className = `market-light-banner light-${payload.light || "unknown"}`;
    // 所有值皆後端產生(非使用者輸入)，用 textContent 組裝避免任何注入疑慮
    box.replaceChildren();
    const dot = document.createElement("span");
    dot.className = "market-light-dot";
    dot.textContent = icon;
    const body = document.createElement("span");
    body.className = "market-light-body";
    const head = document.createElement("strong");
    head.textContent = `大盤環境：${payload.regime || "-"}｜${payload.advice || ""}`;
    body.append(head);
    // 加權指數:交易日盤中會跳動→標「即時」;非交易日(週末/收盤後隔日)→標最近交易日「收盤」。
    // 台股慣例紅漲綠跌。
    const liveChg = Number(payload.taiexLiveChangePct);
    const livePrice = Number(payload.taiexLivePrice);
    if (Number.isFinite(liveChg) && Number.isFinite(livePrice)) {
      const live = document.createElement("span");
      live.className = `market-light-live ${liveChg >= 0 ? "up" : "down"}`;
      const todayStr = taiwanTodayStr();   // 台灣日,對齊後端 taiexLiveDate(UTC+8),避免非台灣時區瀏覽器誤標即時/收盤
      const isToday = String(payload.taiexLiveDate || "") === todayStr;
      // 2026-07-09 大盤即時(走A):tag 直接標來源——永豐即時(Shioaji 快照、跟個股同鮮度)
      // 或 Yahoo延遲(fallback、約延遲10-20分),讓使用者一眼知道現在是哪個源。
      const liveSrc = escapeHtml(String(payload.taiexLiveSource || "即時"));
      const tag = isToday
        ? (payload.taiexLiveTime ? `（${payload.taiexLiveTime} ${liveSrc}）` : `（${liveSrc}）`)
        : `（${String(payload.taiexLiveDate || "").slice(5) || "最近"} 收盤）`;
      const label = isToday ? "今日" : "加權";
      live.textContent = `加權 ${livePrice.toLocaleString()}　${label} ${sign(liveChg)}${liveChg}%${tag}`;
      body.append(live);
    }
    const meta = document.createElement("span");
    meta.className = "market-light-meta";
    // 2026-07-09 大盤要即時:liveGate=true 時「距月線」是用即時加權 vs 月線算的→標「即時」;
    // 否則是昨天日線收盤→標「收盤 日期」。20日報酬本質是日線,不另標。
    const gapWhen = payload.liveGate ? "即時" : `收盤 ${payload.latestDate || "-"}`;
    meta.textContent = `距月線 ${sign(gapPct)}${gapPct}%（${gapWhen}）、20日 ${sign(ret20Pct)}${ret20Pct}%${staleNote}`;
    body.append(meta);
    box.append(dot, body);
    box.hidden = false;
  } catch (error) {
    box.hidden = true;
  }
}

async function renderDataFreshness() {
  // 資料新鮮度儀表板：一眼看出每個資料源多久沒更新。現有健康檢查偏重「模型能不能用」，
  // 這張卡專門看「資料是不是新的」——哪個源斷更(FinMind/模型/大盤/個股K/tick)會亮紅點。
  const box = document.getElementById("dataFreshnessCard");
  if (!box) return;
  try {
    const response = await fetch("/api/data-freshness");
    const payload = await response.json();
    if (!response.ok || !payload.ok || !Array.isArray(payload.sources)) { box.hidden = true; return; }
    box.className = `data-freshness-card ${payload.overallOk ? "" : "has-stale"}`.trim();
    // 全部值皆後端產生(非使用者輸入)，用 textContent 組裝避免任何注入疑慮
    box.replaceChildren();
    const head = document.createElement("div");
    head.className = "data-freshness-head";
    const title = document.createElement("strong");
    title.textContent = payload.overallOk ? "🟢 資料新鮮度：全部最新" : "🟡 資料新鮮度：有資料源待更新";
    const stamp = document.createElement("span");
    stamp.className = "data-freshness-stamp";
    stamp.textContent = `檢查於 ${payload.checkedAt || "-"}`;
    head.append(title, stamp);
    const list = document.createElement("ul");
    list.className = "data-freshness-list";
    payload.sources.forEach((s) => {
      const li = document.createElement("li");
      li.className = s.ok ? "fresh-ok" : "fresh-stale";
      const dot = document.createElement("span");
      dot.className = "data-freshness-dot";
      dot.textContent = s.ok ? "●" : "○";
      const name = document.createElement("span");
      name.className = "data-freshness-name";
      name.textContent = s.name || "-";
      const detail = document.createElement("span");
      detail.className = "data-freshness-detail";
      detail.textContent = s.detail || "";
      li.append(dot, name, detail);
      list.append(li);
    });
    box.append(head, list);
    box.hidden = false;
  } catch (error) {
    box.hidden = true;
  }
}

async function renderSectorLeaderMap() {
  // 族群龍頭連動地圖：熱門族群不只顯示名字，攤開「這族群裡有哪些候選、誰是龍頭、
  // 誰在跟」。龍頭=系統評分最高者，其餘依序為跟漲候補——熱門族群龍頭領軍時
  // 跟漲股常補漲，讓使用者看出族群內部輪動候補。純讀上次掃描結果，不重掃。
  const box = document.getElementById("sectorLeaderMap");
  if (!box) return;
  try {
    const response = await fetch("/api/sector-leaders");
    const payload = await response.json();
    if (!response.ok || !payload.ok || !Array.isArray(payload.sectors) || !payload.sectors.length) {
      box.hidden = true;
      return;
    }
    box.className = "sector-leader-map";
    box.replaceChildren();
    const head = document.createElement("div");
    head.className = "sector-leader-head";
    const title = document.createElement("strong");
    title.textContent = "🔥 熱門族群龍頭連動";
    const note = document.createElement("span");
    note.className = "sector-leader-note";
    note.textContent = `依 ${payload.scanDate || "-"} 掃描．★為族群龍頭`;
    head.append(title, note);
    box.append(head);
    const fmtPct = (v) => (Number.isFinite(Number(v)) ? `${Number(v) >= 0 ? "+" : ""}${Number(v)}%` : "-");
    payload.sectors.forEach((sec) => {
      const group = document.createElement("div");
      group.className = "sector-leader-group";
      const gh = document.createElement("div");
      gh.className = "sector-leader-group-head";
      const sname = document.createElement("strong");
      const persist = sec.persistentHot ? "🔥" : "";
      sname.textContent = `${sec.sector}${persist}`;
      const smeta = document.createElement("span");
      smeta.className = "sector-leader-group-meta";
      const excess = Number.isFinite(Number(sec.excessRet5)) ? `超額 ${fmtPct(sec.excessRet5)}` : "";
      smeta.textContent = `${sec.memberCount} 檔${excess ? "．" + excess : ""}`;
      gh.append(sname, smeta);
      group.append(gh);
      (sec.members || []).forEach((m) => {
        const row = document.createElement("div");
        row.className = m.isLeader ? "sector-leader-row is-leader" : "sector-leader-row";
        const mark = document.createElement("span");
        mark.className = "sector-leader-mark";
        mark.textContent = m.isLeader ? "★" : "·";
        const nm = document.createElement("span");
        nm.className = "sector-leader-name";
        nm.textContent = `${m.symbol} ${m.name || ""}`.trim();
        const buy = document.createElement("span");
        buy.className = m.buyAllowed ? "sector-leader-buy can" : "sector-leader-buy no";
        buy.textContent = m.buyAllowed ? "可買" : "觀察";
        const chg = document.createElement("span");
        chg.className = "sector-leader-chg";
        chg.textContent = `今 ${fmtPct(m.change1)}／5日 ${fmtPct(m.change5)}`;
        row.append(mark, nm, buy, chg);
        group.append(row);
      });
      box.append(group);
    });
    box.hidden = false;
  } catch (error) {
    box.hidden = true;
  }
}

async function renderLossStreak() {
  // 連續虧損熔斷：短線散戶連續踩雷時最容易情緒化加碼凹單。這條只在「已連續虧損
  // 逼近/觸發熔斷」時才浮現(平時隱藏,不干擾)，提醒暫停或縮小部位。資料源=真實
  // 已平倉交易(跟④複盤日誌同一份)，trades 表空時不顯示。
  const box = document.getElementById("lossStreakBanner");
  if (!box) return;
  try {
    const response = await fetch("/api/portfolio/loss-streak");
    const payload = await response.json();
    // 只有真的在連虧(caution/circuit)才顯示；正常/無資料一律隱藏
    if (!response.ok || !payload.ok || !payload.hasData ||
        (payload.level !== "caution" && payload.level !== "circuit")) {
      box.hidden = true;
      return;
    }
    box.className = `loss-streak-banner level-${payload.level}`;
    box.replaceChildren();
    const icon = document.createElement("span");
    icon.className = "loss-streak-icon";
    icon.textContent = payload.level === "circuit" ? "🛑" : "⚠️";
    const body = document.createElement("span");
    body.className = "loss-streak-body";
    const head = document.createElement("strong");
    head.textContent = payload.level === "circuit"
      ? `連續虧損 ${payload.streak} 筆．已觸發熔斷`
      : `連續虧損 ${payload.streak} 筆．接近熔斷`;
    const advice = document.createElement("span");
    advice.className = "loss-streak-advice";
    advice.textContent = payload.advice || "";
    body.append(head, advice);
    box.append(icon, body);
    box.hidden = false;
  } catch (error) {
    box.hidden = true;
  }
}

async function renderOrderSuggestions() {
  // 今日可買清單：把系統判定可買的候選集中成一張精簡表(進場/回檔/停損/停利)，
  // 省得在整個雷達清單裡東翻西找。**純參考清單，不會自動下單**——要買仍需
  // 自己到永豐手動下單面板逐筆送出。純讀上次掃描結果。
  const box = document.getElementById("orderSuggestionList");
  if (!box) return;
  try {
    const response = await fetch("/api/order-suggestions");
    const payload = await response.json();
    if (!response.ok || !payload.ok || !Array.isArray(payload.suggestions) || !payload.suggestions.length) {
      box.hidden = true;
      return;
    }
    box.className = "order-suggestion-list";
    box.replaceChildren();
    const head = document.createElement("div");
    head.className = "order-suggestion-head";
    const title = document.createElement("strong");
    title.textContent = `📋 今日可買清單（${payload.count} 檔）`;
    const note = document.createElement("span");
    note.className = "order-suggestion-note";
    note.textContent = "僅供參考，需自行到永豐手動下單面板逐筆送出，系統不會自動委託";
    head.append(title, note);
    box.append(head);
    const fmt = (v) => (Number.isFinite(Number(v)) ? Number(v).toFixed(2) : "-");
    const table = document.createElement("table");
    table.className = "order-suggestion-table";
    const thead = document.createElement("thead");
    const htr = document.createElement("tr");
    ["代號", "名稱", "分數", "進場參考", "回檔買點", "停損", "停利"].forEach((h) => {
      const th = document.createElement("th");
      th.textContent = h;
      htr.append(th);
    });
    thead.append(htr);
    table.append(thead);
    const tbody = document.createElement("tbody");
    payload.suggestions.forEach((s) => {
      const tr = document.createElement("tr");
      const cells = [
        s.symbol || "-",
        s.name || "-",
        Number.isFinite(Number(s.score)) ? Number(s.score).toFixed(1) : "-",
        fmt(s.entryTrigger),
        fmt(s.pullbackPrice),
        fmt(s.stopPrice),
        fmt(s.takeProfit),
      ];
      cells.forEach((c, i) => {
        const td = document.createElement("td");
        td.textContent = c;
        if (i === 5) td.className = "order-suggestion-stop";
        if (i === 6) td.className = "order-suggestion-target";
        tr.append(td);
      });
      tbody.append(tr);
    });
    table.append(tbody);
    box.append(table);
    box.hidden = false;
  } catch (error) {
    box.hidden = true;
  }
}

// ===== 今日報告(首頁最下面,收盤後 15:30 為最終版)=====
// 純用前端已有的真實資料狀態組裝；雷達成交戰績已移至交易複盤。
// 模型訓練與模型回測保留在獨立模型頁，不混入首頁股票分析。
let dailyReportMarket = null;     // 大盤環境:renderMarketLight 抓到的 /api/market/status payload(報告直接複用,不重打)

// 台灣日(UTC+8),不受瀏覽器時區影響——後端的 scanDate/trainedAt/taiexLiveDate 都是台灣交易日,
// 用瀏覽器本地日比對在非 UTC+8 時區會誤判(把盤中即時誤標成收盤等)。shift +8h 後讀 UTC 日期即為台灣日。
function taiwanTodayStr() {
  return new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10);
}

function renderDailyReport() {
  const box = document.getElementById("dailyReportCard");
  if (!box) return;
  // 用台灣時間判斷收盤(15:30)與時間戳,不用瀏覽器本地時(非 UTC+8 時區會把盤中誤標收盤後)。
  const now = new Date(new Date().toLocaleString("en-US", { timeZone: "Asia/Taipei" }));
  const todayStr = taiwanTodayStr();   // 台灣日,對齊後端 scanDate/trainedAt(非瀏覽器本地日)
  const finalized = now.getHours() > 15 || (now.getHours() === 15 && now.getMinutes() >= 30);
  const stamp = finalized
    ? `✅ 收盤後最終版 · ${now.toTimeString().slice(0, 5)}`
    : "⏳ 盤中即時 · 收盤 15:30 後為最終版";

  const scan = monsterBackendState?.payload || {};
  const cands = Array.isArray(scan.candidates) ? scan.candidates : [];
  const scanDate = String(scan.scanDate || "");
  const buyable = cands.filter(c => c && c.buyAllowed);
  const scannedToday = scanDate === todayStr;

  const cls = (v) => v > 0 ? "dr-pos" : v < 0 ? "dr-neg" : "";

  const parts = [
    `<div class="dr-head"><span class="dr-title">📋 今日報告 ${todayStr.slice(5)}</span><span class="dr-stamp">${stamp}</span></div>`,
  ];
  // 📊 今日盈虧區塊已依需求移除(持股市值/未實現損益/可用 在上方「帳戶總資產」卡已有,重複)。

  // 🌐 大盤環境:紅綠燈+一句話建議+加權指數(直接複用上方 banner 抓到的 /api/market/status,不重打)
  if (dailyReportMarket) {
    const m = dailyReportMarket;
    const icon = { green: "🟢", yellow: "🟡", red: "🔴" }[m.light] || "⚪";
    let mktRows = `<div class="dr-row"><span>今日燈號</span><strong>${icon} ${escapeHtml(String(m.regime || "-"))}｜${escapeHtml(String(m.advice || ""))}</strong></div>`;
    const liveChg = Number(m.taiexLiveChangePct);
    const livePrice = Number(m.taiexLivePrice);
    if (Number.isFinite(liveChg) && Number.isFinite(livePrice)) {
      const isToday = String(m.taiexLiveDate || "") === todayStr;
      // 台股慣例紅漲綠跌:漲用 dr-pos(紅)、跌用 dr-neg(綠)——沿用報告內既有的漲跌色語意
      mktRows += `<div class="dr-row"><span>加權指數（${isToday ? "今日即時" : "最近收盤"}）</span><strong class="${cls(liveChg)}">${livePrice.toLocaleString()}　${liveChg >= 0 ? "+" : ""}${fmt(liveChg, 2)}%</strong></div>`;
    }
    const gap = Number(m.taiexMaGapPct);
    const ret20 = Number(m.taiexRet20Pct);
    if (Number.isFinite(gap) || Number.isFinite(ret20)) {
      // 2026-07-09 大盤要即時:距月線 liveGate=true 是即時加權算的→標「即時」,否則標收盤日
      const gapWhen = m.liveGate ? "（即時）" : (m.latestDate ? `（${String(m.latestDate).slice(5)} 收盤）` : "");
      mktRows += `<div class="dr-cand">距月線 ${Number.isFinite(gap) ? (gap >= 0 ? "+" : "") + fmt(gap, 2) + "%" : "—"}${gapWhen}　·　近20日 ${Number.isFinite(ret20) ? (ret20 >= 0 ? "+" : "") + fmt(ret20, 2) + "%" : "—"}${m.stale ? `　·　⚠️ 大盤資料 ${m.staleDays} 天沒更新` : ""}</div>`;
    }
    parts.push(`<div class="dr-section"><div class="dr-section-title">🌐 大盤環境</div>${mktRows}</div>`);
  }

  // 今日判斷程度：真實雷達候選與已結算命中率，不讀模型門檻。
  let judgeRows = "";
  if (scannedToday) {
    judgeRows += `<div class="dr-row"><span>雷達候選</span><strong>${cands.length} 檔（可買 ${buyable.length}）</strong></div>`;
    const top = [...cands].sort((a, b) => Number(b.score || 0) - Number(a.score || 0)).slice(0, 6);
    for (const c of top) {
      const mark = c.buyAllowed ? "✅" : "👀";
      const extras = [];
      const vr = Number(c.volumeRatio ?? c.volume_ratio);
      if (Number.isFinite(vr) && vr > 0) extras.push(`量比 ${fmt(vr, 1)}倍`);
      const tenure = Number(c.tenure);
      if (Number.isFinite(tenure) && tenure > 0) extras.push(`進榜 ${tenure} 天`);
      const extraStr = extras.length ? `　${extras.join("　·　")}` : "";
      judgeRows += `<div class="dr-cand">${mark} ${escapeHtml(String(c.symbol || ""))} ${escapeHtml(String(c.name || "").trim())}　分數 ${Math.round(Number(c.score || 0))}${extraStr}</div>`;
    }
  } else {
    judgeRows = `<div class="dr-row"><span>今天還沒有新的雷達掃描</span><strong>${scanDate ? `最近：${escapeHtml(scanDate)}` : "無"}</strong></div>`;
  }
  parts.push(`<div class="dr-section"><div class="dr-section-title">🎯 今日判斷程度</div>${judgeRows}</div>`);

  // 系統狀態只反映正式資料與通知模式；模型狀態不再是買賣關卡。
  let sysRows = "";
  const dh = formalDataHealth();
  if (dh.ok !== false) {
    sysRows += `<div class="dr-row"><span>每日正式資料</span><strong class="dr-pos">今日資料可供規則判斷</strong></div>`;
  } else {
    sysRows += `<div class="dr-row"><span>每日正式資料</span><strong class="dr-neg">${escapeHtml(String(dh.reason || "資料健康檢查未通過"))}</strong></div>`;
    sysRows += `<div class="dr-cand">此狀態下系統只觀察、暫停買賣通知;等今天的每日更新成功後會自動恢復。</div>`;
  }
  sysRows += `<div class="dr-row"><span>LINE 通知</span><strong>只發賣出/停損提醒(其餘已靜音)</strong></div>`;
  parts.push(`<div class="dr-section"><div class="dr-section-title">🩺 系統狀態</div>${sysRows}</div>`);

  box.innerHTML = parts.join("");
}

initControls({ deferRadarLoads: true });
render();
loadTokenStatus();
const initialPortfolioExitAnalysis = loadPortfolioExitAnalysis({ render: true }).catch(() => null);
// 真實成交複盤(loadRealizedRadarReview)2026-07-09 已搬到獨立頁 trades.html / trades.js,主頁不再載入。
// 候選複盤 / 熱門族群龍頭連動 / 今日可買清單 三個面板已依需求從首頁移除,只留妖股雷達掃描清單。
// 後端端點(/api/radar/candidate-followthrough、sector-leaders、order-suggestions)與掃描/通知邏輯照留;
// 要恢復把下面三行取消註解即可(對應 index.html 的三個 div 也要加回)。
// loadCandidateFollowThrough();
// renderSectorLeaderMap();
// renderOrderSuggestions();
initialPortfolioExitAnalysis.finally(() => {
  loadBackendStatus();
  loadInitialRadarData();
  loadDividendCalendar();
  renderMarketLight();
  // 資料新鮮度儀表板已依需求從首頁移除;後端 /api/data-freshness 監控照留,要恢復取消下行註解+加回 index.html 的 div。
  // renderDataFreshness();
  renderLossStreak();
  renderDailyReport();
  Promise.allSettled(stocks.map(loadRealData)).then(() => { render(); renderDailyReport(); });
});
// 今日報告每 20 秒用最新大盤、掃描與資料狀態重繪一次（純讀 state，不打網路）
setInterval(renderDailyReport, 20000);
// 大盤紅綠燈每 60 秒重抓(盤中即時加權指數會跳動);後端 fetch_taiex_live 快取 30s 擋重複 Yahoo。
setInterval(renderMarketLight, 60000);

// 妖股「能不能買」判斷鏈(monsterOpenCheck → timedEntryDecision →
// entryDecisionWithBrain → intradayStateMachine)的整合自測，不會自動執行，
// 從瀏覽器主控台呼叫 __testMonsterDecisionChain() 才會跑。這裡涵蓋跟
// server.py tests/test_monster_intraday_state.py 對應的情境(canBuy 布林值
// 正確傳遞、型態量能規則 否決、缺報價提示)，之後改動這條鏈就能直接重跑驗證，
// 不用每次都手動點畫面。凍結的合成資料，不碰真實 API/DOM。
function __testMonsterDecisionChain() {
  const results = [];
  const assert = (name, condition, detail = "") => {
    results.push({ name, pass: Boolean(condition), detail });
  };

  const makeItem = (intradayOverrides = {}, itemOverrides = {}) => ({
    stock: { code: "TEST0001", name: "測試股" },
    score: 70,
    probability: 0.5,
    threshold: 0.4,
    buyAllowed: true,
    close: 100,
    buyTrigger: 105,
    intraday: {
      canBuy: true,
      setupOk: true,
      setupType: "breakout",
      status: "突破可觀察",
      windowBlocked: false,
      lateBreakoutBlocked: false,
      hasIntradayQuote: true,
      openGap: 0,
      openHighFade: false,
      stopBroken: false,
      volumeContinue: true,
      pullbackHold: false,
      pullbackVolumeOk: false,
      intradayRebound: false,
      vRebound: false,
      breakout: true,
      ...intradayOverrides,
    },
    ...itemOverrides,
  });

  const brainDecision = (overrides = {}) => ({
    ok: true,
    context: "monster",
    entryAllowed: true,
    decisionBlocked: false,
    observeOnly: false,
    recommendation: "可列入買進觀察",
    actionLabel: "妖股短打：型態量能進場條件通過，仍需盤中確認",
    ...overrides,
  });

  const rowHealthOk = (decision) => ({ ok: true, mode: "normal", reason: "", brainDecision: decision, brainCached: null });

  const runChain = (item, rowHealth) => {
    const open = monsterOpenCheck(item);
    const baseDecision = timedEntryDecision(open, true);
    const brainEntry = entryDecisionWithBrain(baseDecision, rowHealth, open?.canBuy === true);
    const decision = brainEntry.decision;
    const canBuy = intradayStateMachine(open, decision, rowHealth, brainEntry.canEnter).status === "可買";
    return { open, baseDecision, brainEntry, canBuy };
  };

  // 1. 後端可買 + Brain 允許 → 最終可買
  {
    const item = makeItem();
    const { canBuy } = runChain(item, rowHealthOk(brainDecision()));
    assert("後端可買+Brain允許 → 可買", canBuy === true);
  }

  // 2. 後端不可買(時段封鎖) → 最終不可買，就算 Brain 允許也一樣
  {
    const item = makeItem({ canBuy: false, windowBlocked: true, status: "13:15 後不再進場" });
    const { canBuy, open } = runChain(item, rowHealthOk(brainDecision()));
    assert("時段封鎖 → 不可買", canBuy === false);
    assert("時段封鎖的原因文字有帶到", String(open.action || "").includes("13:15") || String(open.backendStatus || "").includes("13:15"));
  }

  // 3. 後端可買，但 型態量能規則 否決(entryAllowed=false) → 最終不可買
  {
    const item = makeItem();
    const decision = brainDecision({ entryAllowed: false, recommendation: "只觀察" });
    const { canBuy } = runChain(item, rowHealthOk(decision));
    assert("後端可買但Brain否決 → 不可買", canBuy === false);
  }

  // 4. 後端可買，但 Brain 還在讀取中(沒有 brainDecision) → 不能顯示可買
  {
    const item = makeItem();
    const pendingHealth = { ok: true, mode: "normal", reason: "", brainDecision: null, brainCached: null };
    const { canBuy, brainEntry } = runChain(item, pendingHealth);
    assert("Brain讀取中 → 不能顯示可買", canBuy === false);
    assert("Brain讀取中 → canEnter明確是false不是undefined", brainEntry.canEnter === false);
  }

  // 5. 缺即時報價 → hasIntradayQuote 要反映在 open 上，不是死值
  {
    const item = makeItem({ hasIntradayQuote: false, canBuy: false, setupOk: false });
    const open = monsterOpenCheck(item);
    assert("缺報價 → hasIntradayQuote為false", open.hasIntradayQuote === false);
  }
  {
    const item = makeItem({ hasIntradayQuote: true });
    const open = monsterOpenCheck(item);
    assert("有報價 → hasIntradayQuote為true", open.hasIntradayQuote === true);
  }

  // 6. canEnter 是真布林值，不是靠字串 includes 反推(即使 recommendation 文字
  //    不含「可列入買進觀察」，只要 entryAllowed===true 一樣要能判定可買)
  {
    const item = makeItem();
    const decision = brainDecision({ entryAllowed: true, recommendation: "續抱觀察" });
    const { canBuy, brainEntry } = runChain(item, rowHealthOk(decision));
    assert("entryAllowed=true但文案不含關鍵字 → canEnter仍為true", brainEntry.canEnter === true);
    assert("canEnter=true → 最終仍判定可買", canBuy === true);
  }

  // 7. portfolio_exit context 不該被這條鏈的 entryContext 白名單誤判成擋下
  {
    const decision = brainDecision({ context: "portfolio_exit", entryAllowed: false, sellDataReady: true, recommendation: "留意賣出訊號" });
    const blocked = brainDecisionBlocksDecision(decision, { blockWhenMissing: true, context: "portfolio_exit" });
    assert("portfolio_exit情境不因entryAllowed=false被擋", blocked === false);
  }

  // 10. 獨立 Brain 快取仍可運作；正式通知只接受後端統一出場結果。
  {
    const TEST_CODE = "9998";
    const cacheKey = brainDecisionCacheKey(TEST_CODE, "portfolio_exit");
    const previousEntry = brainDecisionCache.get(cacheKey);
    const previousPersisted = readUiCache(brainDecisionPersistKey());
    const decision = {
      ok: true, symbol: TEST_CODE, context: "portfolio_exit", updatedAt: Date.now(),
      observeOnly: false, sellDataReady: true, recommendation: "留意賣出訊號", actionLabel: "測試",
    };

    // 10a. 持久化→清空→水合，重載後 Brain 結論要還原(通知鏈不再全滅)
    brainDecisionCache.set(cacheKey, decision);
    persistPortfolioBrainDecisions();
    brainDecisionCache.delete(cacheKey);
    hydratePortfolioBrainDecisions();
    const hydrated = brainDecisionCache.get(cacheKey);
    assert("Brain結論持久化後水合還原", hydrated?.ok === true && hydrated?.symbol === TEST_CODE);

    // 10b. 通知閘門只認 portfolio-exit-v2、資料就緒、計算驗證及兩項證據。
    let previousStopLossToggle = null;
    try {
      previousStopLossToggle = localStorage.getItem(NOTIFY_STOP_LOSS_KEY);
      localStorage.setItem(NOTIFY_STOP_LOSS_KEY, "1");
    } catch {}
    const lossConfirm = {
      code: TEST_CODE,
      typeClass: "confirm",
      status: "中期移動停利成立",
      policyVersion: "portfolio-exit-v2",
      dataReady: true,
      canNotify: true,
      decisionVerified: true,
      decisionType: "phase2",
      decisionReasons: ["跌破 MA20", "移動停利被跌破"],
      pnlRate: -6.5,
    };
    assert("後端已驗證的虧損出場要通知", shouldNotifyPortfolioAlert(lossConfirm) === true);
    const unverifiedConfirm = { ...lossConfirm, decisionVerified: false };
    assert("未通過後端計算驗證不得通知", shouldNotifyPortfolioAlert(unverifiedConfirm) === false);
    assert("報價或歷史資料未就緒不得通知", shouldNotifyPortfolioAlert({ ...lossConfirm, dataReady: false }) === false);
    assert("舊版前端提醒快取不得通知", shouldNotifyPortfolioAlert({ ...lossConfirm, policyVersion: "" }) === false);
    try {
      if (previousStopLossToggle === null) localStorage.removeItem(NOTIFY_STOP_LOSS_KEY);
      else localStorage.setItem(NOTIFY_STOP_LOSS_KEY, previousStopLossToggle);
    } catch {}

    // 還原快取與 localStorage，不留測試污染
    if (previousEntry) brainDecisionCache.set(cacheKey, previousEntry);
    else brainDecisionCache.delete(cacheKey);
    if (previousPersisted) writeUiCache(brainDecisionPersistKey(), previousPersisted);
    else {
      try { localStorage.removeItem(brainDecisionPersistKey()); } catch {}
    }
  }

  // 11. changeRate 尺度異常防護：SDK 若回傳小數比例(0.052)而非百分比(5.2)，
  // 要用 currentPrice/referencePrice 反推的百分比自我修正，並且維持正常
  // 情況下(raw 本來就是合理的百分比)不誤動既有正確資料。
  {
    const normal = sanitizeChangeRatePercent(5.2, 105.2, 100);
    assert("changeRate正常情況維持原值", Math.abs(normal - 5.2) < 0.01, String(normal));

    const scaledDown = sanitizeChangeRatePercent(0.052, 105.2, 100);
    assert("changeRate小數比例被自我修正為百分比", Math.abs(scaledDown - 5.2) < 0.1, String(scaledDown));

    const missingRaw = sanitizeChangeRatePercent(null, 105, 100);
    assert("changeRate缺值時用參考價推算補上", Math.abs(missingRaw - 5) < 0.01, String(missingRaw));

    const noReference = sanitizeChangeRatePercent(3.3, 103.3, null);
    assert("沒有參考價時信任原始值", Math.abs(noReference - 3.3) < 0.01, String(noReference));

    const tinyMove = sanitizeChangeRatePercent(0.01, 100.01, 100);
    assert("極小漲跌幅不誤判為尺度異常", Math.abs(tinyMove - 0.01) < 0.001, String(tinyMove));
  }

  // 12. runBackendUpdate 的錯誤訊息寫入 innerHTML 前要跳脫，避免伺服器/
  // Error.message 若剛好含 HTML 特殊字元造成 DOM-based XSS(這是全檔案
  // friendlyError()+innerHTML 組合裡唯一漏跳脫的地方，其餘27處都有escapeHtml())。
  {
    const maliciousMessage = '<img src=x onerror=alert(1)>';
    const escaped = escapeHtml(maliciousMessage);
    assert(
      "escapeHtml能把惡意HTML字元轉成安全實體",
      !escaped.includes("<img") && escaped.includes("&lt;img"),
      escaped,
    );
    const originalInnerHTML = document.getElementById("backendStatus")?.innerHTML;
    const statusEl = document.getElementById("backendStatus");
    if (statusEl) {
      statusEl.innerHTML = `<div class="plan-item"><span>更新失敗</span><strong>${escapeHtml(maliciousMessage)}</strong></div>`;
      assert("runBackendUpdate錯誤訊息不會被當成HTML標籤解析", !statusEl.querySelector("img"), statusEl.innerHTML);
      if (originalInnerHTML !== undefined) statusEl.innerHTML = originalInnerHTML;
    }
  }

  const passed = results.filter((r) => r.pass).length;
  const failed = results.filter((r) => !r.pass);
  const summary = `${passed}/${results.length} 通過`;
  if (failed.length) {
    console.error(`[__testMonsterDecisionChain] ${summary}，失敗項目：`, failed);
  } else {
    console.log(`[__testMonsterDecisionChain] ${summary}，全部通過`);
  }
  return { summary, passed, total: results.length, results, failed };
}
window.__testMonsterDecisionChain = __testMonsterDecisionChain;
