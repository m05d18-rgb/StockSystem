# 把股票看盤系統搬到 Synology DS218+(Docker 部署指南)

> 我(Claude)沒辦法連進你的 NAS,所以下面每一步是**你在 NAS 上操作**;
> 涉及帳密(FinMind Token、LINE、永豐券商登入)的部分一律你自己來——我不會、
> 也不該代為登入券商或輸入密碼。

---

## 0. 先看:這台 NAS 跑得動嗎?(誠實評估)

| 項目 | 狀況 | 說明 |
|---|---|---|
| CPU | Intel Celeron J3355(x86-64) | ✅ 相容;本映像用 amd64,不是 ARM |
| **RAM** | **2GB(這是瓶頸)** | ⚠️ 看盤/掃描結果顯示/LINE/每日更新持股 OK;**全市場掃描(~6700檔)+ 模型重訓(xgboost/lightgbm)可能撐爆 2GB 被系統砍掉** |
| Docker | Container Manager 套件 | ✅ DS218+ 支援 |
| 相依 | numpy/scipy/sklearn/xgboost/lightgbm | 映像約 1~1.5GB,NAS 硬碟空間沒問題 |

**強烈建議:把 RAM 加到 6GB**(DS218+ 官方支援,加一條 4GB DDR3L SO-DIMM,約幾百元)。
加了之後整套(含重訓/全市場掃描)就跑得舒服。**維持 2GB 的話**建議走「減負模式」(見 §7):
NAS 只做常駐看盤 + 每日更新 + LINE,**重訓/全市場掃描留在 PC 上**或半夜小心跑。

---

## 1. 把整包專案複製到 NAS

1. NAS 開 **File Station**,建資料夾,例如 `docker/stock-system`(實際路徑會是 `/volume1/docker/stock-system`)。
2. 把**整個專案資料夾的內容**傳上去(拖拉上傳,或用 `rsync`/SMB 掛載複製)。**要一起搬的關鍵檔**:
   - 程式:`server.py`、`ml_backend.py`、`daily_update.py`、`line_notify.py`、`sinopac_backend.py`、`modules/`、`index.html`、`app.js`、`styles.css`、`settings.html`、`mobile.js` … 等全部
   - **資料庫 `stock_system.sqlite3`(約 659MB,網路傳輸會花幾分鐘)**
   - 模型 `model.pkl`、`model_env.json`
   - 設定 `finmind_usage.json` 等 `*.json`(注意:`sinopac_api.json`/`line_api.json`/`ai_api.json` 含金鑰,確定你信任這台 NAS 再放)
   - `deploy/` 資料夾(裡面就是這份指南 + Dockerfile + docker-compose.yml + requirements.txt)
3. **不用搬**:`.ps1`/`.bat`(那是 Windows 啟動腳本,Linux 用不到)、`tests/`(可留可不留)、`__pycache__`、`.git`。

## 2. 裝 Docker(Container Manager)

DSM → **套件中心** → 搜尋 **Container Manager** → 安裝。

## 3. 建立容器(兩種方式,擇一)

### 方式 A:Container Manager GUI(不用打指令)
1. Container Manager → **專案(Project)** → **新增**。
2. 專案名稱:`stock-system`;路徑選 `docker/stock-system/deploy`(有 docker-compose.yml 的那層)。
3. 來源選「使用現有的 docker-compose.yml」→ 下一步 → 建置。
4. 第一次會 build image(裝 numpy/xgboost/lightgbm,**約 5~15 分鐘**,看 NAS 速度),之後啟動很快。

### 方式 B:SSH(較快、看得到 log)
DSM 開啟 SSH,用你的管理員帳號登入後:
```bash
cd /volume1/docker/stock-system/deploy
sudo docker compose up -d --build      # 第一次:build + 背景啟動
sudo docker compose logs -f            # 看啟動 log,Ctrl+C 離開(容器繼續跑)
```

## 4. 開起來看看

瀏覽器連 **`http://<NAS的區網IP>:8008`**(NAS IP 在 DSM → 控制台 → 網路 看)。
應該會看到跟你 PC 上一樣的看盤介面。手機在同一個 Wi-Fi 也能開。

> 若連不進去:DSM → 控制台 → 安全性 → **防火牆**,放行 8008 埠(或暫時關防火牆測試)。

## 5. 設定(你自己來,我不碰帳密)

- **FinMind Token**:開 `http://<NAS_IP>:8008/settings.html` → 填 Token 存檔(或確認搬過來的 `finmind_usage.json`/token 設定還在)。
- **LINE 通知**:設定頁填 LINE 權杖。
- **永豐券商(選配)**:見 §6。

## 6. 永豐 Shioaji(選配 — 庫存/下單/盤中即時報價)

**預設不啟用,系統照常運作**(儀表板/妖股掃描/LINE/每日更新全部正常,只有券商相關顯示「未設定」)。
要在 NAS 上用券商功能:
1. 編輯 `deploy/requirements.txt`,把 `# shioaji==1.3.3` 取消註解。
2. 重 build:`sudo docker compose up -d --build`。
3. 在 NAS 上完成**永豐登入 + CA 憑證**設定(`sinopac_api.json` + 憑證檔)。**這步你自己做**——我不會代為登入券商或輸入密碼;送單一律維持手動兩步驟確認,系統不會自動下單。

> 提醒:盤中即時 tick 收集(`realtime_tick_collector.py`)也靠 shioaji;要在 NAS 跑得另外啟動,先確認 §0 的 RAM 夠。

## 7. RAM 只有 2GB 的「減負模式」(沒加記憶體才需要)

全市場掃描 + 重訓最吃記憶體。若維持 2GB 想穩定,擇一:
- **A(推薦):加 4GB RAM 到 6GB**,一勞永逸,以下都免。
- **B:NAS 只做輕量**——每日更新只更新持股(本來就是)、**避免在 NAS 觸發「全市場掃描」和「立即重訓」**;重訓/全市場回測繼續在 PC 上跑,把新的 `model.pkl`+`stock_system.sqlite3` 再同步回 NAS。
- **C:開 Synology 虛擬記憶體/Swap**(控制台或 SSH 建 swap file)——能防 OOM,但 swap 很慢,掃描會拖很久,治標。

## 8. ⚠️ 最重要:不要 PC 和 NAS「同時」跑

兩台一起跑會:**FinMind 額度雙倍消耗、LINE 通知重複發、兩邊各寫各的資料庫**。
搬遷步驟應該是:
1. NAS 版確認能正常看盤 + 收到 LINE 測試通知。
2. **把 PC 上的舊服務關掉**(關掉 `server.py`/開機自啟),之後只留 NAS 這台。
3. 若之後要在 PC 重訓,重訓完把 `model.pkl` + DB 同步回 NAS,再把 PC 關掉。

## 9. 開機自動跑 & 備份

- **自動啟動**:`restart: unless-stopped` 已設,NAS 重開機容器會自己起來。
- **備份**:資料庫在 NAS,用 Synology **快照(Snapshot)** 或 **Hyper Backup** 排程備份 `docker/stock-system` 整個資料夾(系統自己也有每日 SQLite 備份機制)。

## 10. 疑難排解

| 症狀 | 可能原因 / 處理 |
|---|---|
| 容器一直重啟、log 有 `model.pkl` 載入錯誤 | 套件版本沒對上 → 確認 `requirements.txt` 版本沒被改;或改成在 NAS 重訓一次產生新 model.pkl |
| 掃描/重訓跑到一半容器被 kill(exit 137) | **OOM,記憶體不夠** → §7,優先加 RAM |
| 排程時間不對、報告 15:30 判斷怪怪的 | 確認 compose 的 `TZ=Asia/Taipei` 有生效(容器內 `date` 應顯示台北時間) |
| 連不進 8008 | 防火牆放行 8008;確認容器 `docker compose ps` 是 Up |
| build 很久/失敗 | 第一次裝 xgboost/lightgbm 本來就久;失敗多半是 NAS 對外網路/DNS,重試或設 DNS |

---

### 我做了什麼 / 沒做什麼
- **做了**:幫你把系統容器化(Dockerfile / docker-compose / 鎖版本的 requirements),確認程式碼是跨平台的(只有 `.ps1`/`.bat` 啟動腳本是 Windows 專用、Linux 用不到,已排除),寫這份指南。
- **沒做(也做不到)**:連進你的 NAS 實際部署、複製 659MB 資料庫、輸入任何帳密、登入永豐。這些是你在 NAS 上完成的。
- **沒實測**:我無法在真實 DS218+ 上跑起來驗證,所以第一次 `docker compose up` 是真正的驗收;卡住就照 §10 或把 log 貼給我。
