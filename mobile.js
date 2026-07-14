/*
 * 手機版「一次只看一個模組」積木模組——獨立於 app.js 之外，跟主程式的耦合只有
 * 讀取既有的 #scanner / #monsterRadar / 模組連結這幾個 DOM 節點，沒有互相
 * 呼叫對方的函式。拔掉這個檔案(移除index.html裡的<script>那行)，手機版會退回
 * 「兩個模組都顯示、要滑很長」的樣子，但不影響其他任何功能。
 *
 * 手機斷點(≤720px)本身定義在 styles.css，這裡只負責切換 .module-hidden
 * class；桌面寬度下 class 加不加都沒有視覺效果。
 */
function initMobileModuleTabs() {
  const scanner = document.getElementById("scanner");
  const monsterRadar = document.getElementById("monsterRadar");
  if (!scanner || !monsterRadar) return;
  const navLinks = Array.from(document.querySelectorAll(
    '.nav-list a[href="#scanner"], .nav-list a[href="#monsterRadar"], ' +
    '.mobile-module-tabs a[href="#scanner"], .mobile-module-tabs a[href="#monsterRadar"]'
  ));

  function showModule(hash) {
    scanner.classList.toggle("module-hidden", hash !== "#scanner");
    monsterRadar.classList.toggle("module-hidden", hash !== "#monsterRadar");
    navLinks.forEach((link) => {
      const active = link.getAttribute("href") === hash;
      link.classList.toggle("active", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
  }

  navLinks.forEach((link) => {
    link.addEventListener("click", () => showModule(link.getAttribute("href")));
  });

  // <a href="#hash">是標準連結，點擊時瀏覽器原生行為會把 location.hash
  // 改掉並產生一筆 history 紀錄；使用者按上一頁/下一頁時瀏覽器只會改
  // location.hash，不會重新觸發 click 事件，沒有這個監聽器的話畫面顯示
  // 的模組會跟網址列的 hash 脫鉤。
  window.addEventListener("hashchange", () => {
    const hash = window.location.hash;
    if (hash === "#scanner" || hash === "#monsterRadar") {
      showModule(hash);
    }
  });

  const initialHash = window.location.hash === "#monsterRadar" ? "#monsterRadar" : "#scanner";
  showModule(initialHash);
}

initMobileModuleTabs();

/*
 * 手機版「大戶投」風格總資產卡（使用者要求：像永豐大戶投 APP 那樣）。
 *
 * 手機用一張卡取代整條文字摘要(.yongfeng-summary 在手機 CSS 隱藏)：
 * 大字帳戶總資產、整戶賺賠(對入金)、下方一排市值/可用資金/未實現/交割款。
 * 資料全部讀 app.js 已存好的 localStorage 快取(summary + 入金)，不打新 API、
 * 不呼叫 app.js 函式——維持本檔「可獨立拔除」的積木原則。台股配色沿用
 * 全站慣例：.up=紅(漲/賺)、.down=綠(跌/賠)。
 */
(function initMobileAssetCard() {
  if (!window.matchMedia("(max-width: 720px)").matches) return; // 桌面不建卡
  const scanner = document.getElementById("scanner");
  if (!scanner) return;
  const card = document.createElement("div");
  card.id = "mobileAssetCard";
  card.className = "mobile-asset-card";
  card.hidden = true;
  const anchor = document.getElementById("watchlist");
  scanner.insertBefore(card, anchor || scanner.firstChild);

  const money = (n) => Math.round(n).toLocaleString("zh-TW");
  const signed = (n) => `${n > 0 ? "+" : ""}${money(n)}`;
  const tone = (n) => (n > 0 ? "up" : n < 0 ? "down" : "");

  // 伺服器上的摘要快取(桌面每次同步成功後上傳)。手機自己不跑永豐同步，
  // 本機 localStorage 可能停在很舊的值(例如交割款修好前的假可用資金)，
  // 以 updatedAt 比新舊、用較新的那份。
  let serverSummary = null;
  async function fetchServerSummary() {
    try {
      const response = await fetch("/api/portfolio/summary-cache");
      const payload = await response.json();
      if (response.ok && payload.ok && payload.summary) {
        serverSummary = payload.summary;
        render();
      }
    } catch (error) { /* 讀不到就沿用本機快取 */ }
  }

  function render() {
    let summary = {};
    try {
      summary = JSON.parse(localStorage.getItem("stock-vibe-yongfeng-summary-v1") || "{}") || {};
    } catch (error) { /* 快取壞了就顯示不了，維持隱藏 */ }
    if (serverSummary && String(serverSummary.updatedAt || "") > String(summary.updatedAt || "")) {
      summary = serverSummary;
    }
    const currentValue = Number(summary.currentValue);
    if (!Number.isFinite(currentValue) || currentValue <= 0) { card.hidden = true; return; }
    const cashCandidates = [summary.availableAfterSettlement, summary.originalAvailable].map(Number);
    const cash = cashCandidates.find(Number.isFinite);
    const total = currentValue + (Number.isFinite(cash) ? cash : 0);
    const deposit = Number(localStorage.getItem("stock-vibe-deposit-amount-v1") || 0);
    const totalPnl = Number(summary.totalPnl);
    const returnRate = Number(summary.returnRate);
    const unsettled = Number(summary.unsettledAmount);
    let deltaHtml = "";
    if (deposit > 0) {
      const delta = total - deposit;
      const rate = (delta / deposit) * 100;
      deltaHtml = `<div class="mobile-asset-delta ${tone(delta)}">${signed(delta)} 元（${rate > 0 ? "+" : ""}${rate.toFixed(2)}%）</div>`;
    }
    card.hidden = false;
    card.innerHTML = `
      <div class="mobile-asset-label">帳戶總資產（市值＋扣交割後可用）</div>
      <div class="mobile-asset-total">${money(total)}<small> 元</small></div>
      ${deltaHtml}
      <div class="mobile-asset-rows">
        <span>市值 ${money(currentValue)}</span>
        <span>可用 ${Number.isFinite(cash) ? money(cash) : "-"}</span>
        ${Number.isFinite(totalPnl) ? `<span class="${tone(totalPnl)}">未實現 ${signed(totalPnl)}${Number.isFinite(returnRate) ? `（${returnRate > 0 ? "+" : ""}${returnRate.toFixed(2)}%）` : ""}</span>` : ""}
        ${Number.isFinite(unsettled) && unsettled !== 0 ? `<span>交割款 ${signed(unsettled)}</span>` : ""}
      </div>`;
  }

  render();
  fetchServerSummary();
  // 資料由 app.js 的自動更新/入金同步寫進 localStorage，這裡定期重讀；
  // 伺服器快取每 60 秒重抓一次(盤中桌面同步後手機約一分鐘內跟上)。
  setInterval(render, 30000);
  setInterval(fetchServerSummary, 60000);
})();

/*
 * 手機點股票名稱/代號 → 什麼都不做(使用者 2026-07-04 明確表示手機不需要
 * 判斷追溯/判斷細節，卡片上已有全部資料)。用 capture-phase 把 [data-pick]
 * 的點擊吃掉，避免 app.js 原本的處理器去填被 display:none 隱藏的下單面板
 * 造成奇怪的捲動。桌面寬度不介入。
 */
(function initMobilePickNoop() {
  const mobileQuery = window.matchMedia("(max-width: 720px)");
  document.addEventListener("click", (event) => {
    if (!mobileQuery.matches) return;
    const pickButton = event.target.closest("[data-pick]");
    if (!pickButton) return;
    event.preventDefault();
    event.stopPropagation();
  }, true);
})();
