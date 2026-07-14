/*
 * 手機版「一次只看一個模組」積木模組——獨立於 app.js 之外，跟主程式的耦合只有
 * 讀取既有的 #scanner / #monsterRadar / .nav-list a 這幾個 DOM 節點，沒有互相
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
  const navLinks = Array.from(document.querySelectorAll('.nav-list a[href="#scanner"], .nav-list a[href="#monsterRadar"]'));

  function showModule(hash) {
    scanner.classList.toggle("module-hidden", hash !== "#scanner");
    monsterRadar.classList.toggle("module-hidden", hash !== "#monsterRadar");
    navLinks.forEach((link) => link.classList.toggle("active", link.getAttribute("href") === hash));
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

  // 首頁初始載入兩個模組都顯示(妖股雷達也要在首頁看得到，不用先切換)，
  // 側欄選單仍可用來切換成只看單一模組。
}

initMobileModuleTabs();
