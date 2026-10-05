# 🎧 即時上課翻譯＋筆記

上課時即時把老師的英文／法文翻成繁體中文，下課後一鍵整理成 Markdown 筆記。全部使用 Mistral（Voxtral 語音辨識＋Mistral LLM）。

```
麥克風 → Voxtral Realtime（語音轉文字）→ 斷句 → Mistral 翻譯成繁中 → 畫面即時顯示＋自動存檔
下課 → 按「Generate notes」→ Mistral 分段整理 → 筆記.md
```

---

## 一、第一次安裝（只要做一次）

打開「終端機」（Terminal），一行一行貼上：

```bash
cd ~/python/lecture_translator

# 1. 確認 Python 版本（需要 3.12 以上）
python3 --version

# 2. 建立虛擬環境並安裝套件
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. 設定 API Key
cp .env.example .env
open -e .env          # 用文字編輯器打開，把「貼上你的金鑰」換成你的 Mistral API Key，存檔
```

**API Key 從哪裡拿**：登入 Mistral Studio（console.mistral.ai）→ API Keys → 建立一把新的金鑰。
`.env` 裡的金鑰等於你所在組織的 API 額度，**不要分享給別人、不要上傳到 GitHub**（`.gitignore` 已經排除它）。

---

## 二、每次使用

**方法 A**：在 Finder 雙擊 `run.command`
（第一次可能被 macOS 擋下：對檔案按右鍵 →「打開」→ 再按「打開」）

**方法 B**：終端機輸入
```bash
cd ~/python/lecture_translator
source .venv/bin/activate
streamlit run app.py
```

瀏覽器會自動開啟介面，然後：

1. 左邊填 **Course name**（課程名稱）
2. （建議）在 **Lecture slides** 上傳這堂課的簡報（PDF 或 PPTX）。程式會先讀一遍（約 10 秒），整理出這堂課的領域和簡報裡的專有名詞譯名：翻譯會依照這個領域用詞、專有名詞採用通用譯名，筆記也會照用。側邊欄會顯示讀到的摘要和名詞清單，有翻錯的可以用下面的「Fix a translation」改掉。簡報檔最大 500 MB；文字太多的簡報（大約超過 80–100 頁）只會讀前面的頁面，側邊欄會提醒讀到第幾頁
3. 按 **● Start recording** → 中文字幕即時出現，最新一句會放大顯示，原文在下面（可以用「Show original text」關掉）
4. 下課按 **⏹ Stop**（會多等幾秒，把最後幾句辨識和翻譯完）
5. 按 **📝 Generate notes**

**修正翻錯的專有名詞**：側邊欄的「Fix a translation」，「Term (original)」填沒翻好的原文名詞（例如 `merit goods`），「Correct translation」填查到的正確翻譯（例如 `有益財`），按「Apply」：
- **之後的翻譯**馬上改用新譯名（已經顯示的舊翻譯不會改）
- **產生筆記**時全部用你修正過的譯名
- 修正會依課程名稱存在 `專有名詞/` 資料夾，**下次填同一個課程名稱自動套用**
- 簡報讀出來的譯名偶爾也會錯（例如把 merit goods 音譯成「梅里特財」），用這裡修正就會蓋掉簡報的譯名
- 請填**完整的名詞**，不要只填 `board`、`goods` 這種常見單字：翻譯模型不太會分辨同一個字的不同意思，可能把「寫在黑板上」也翻成「董事會」

**產生筆記失敗怎麼辦**：遇到網路不穩或請求太多，程式會自動重試。還是失敗的話，已經整理好的段落會先暫存，再按一次會從失敗的地方接續，不會重複扣費。

**程式當掉或關掉後**：打開主畫面的「📂 Generate notes from an earlier transcript」，選擇那堂課，就能用 `records/` 裡存好的逐字稿產生筆記。

### 上課中出狀況會怎樣
- **電腦不會自己睡眠**：錄音期間會讓 Mac 保持清醒、螢幕不自動關，按停止後恢復。但**蓋上筆電螢幕**（沒接外接螢幕時）還是會睡眠，錄音會中斷，上課時請保持打開
- **網路斷掉**：會一直自動重連（狀態列顯示「Connection lost — reconnecting」），斷線期間的聲音會先存著，連回來後補送辨識，字幕會一口氣補上。斷線那一刻正在傳送的幾秒可能會漏掉，但錄音檔裡都有
- **麥克風斷掉**（例如外接麥克風鬆掉）：3 秒沒收到聲音就自動重新接上；指定的麥克風連續失敗 3 次會改用系統預設麥克風。斷掉的那段時間沒有聲音可錄

### 第一次使用 macOS 會詢問麥克風權限
如果按「Start recording」後一直沒有字出現：
**系統設定 → 隱私權與安全性 → 麥克風** → 打開「終端機」（如果你用 VS Code 執行，就打開 VS Code）→ 重新啟動程式。

---

## 三、建議先用音訊檔測試

左邊「Audio」選 **Audio file**，上傳一段 WAV 錄音（例如用 iPhone 錄 2 分鐘的英文影片，再轉成 WAV）。
「Playback speed」可以調快，快速檢查整個流程有沒有問題。
⚠️ 模擬也會照音訊長度扣費（約 $0.006／分鐘），所以測試用短檔就好。

---

## 四、產生的檔案

每堂課一個資料夾，放在 `records/日期_時間_課程名稱/`：

| 檔案 | 內容 | 什麼時候產生 |
|---|---|---|
| `逐字稿_原文.txt` | 語音辨識原文（含時間戳） | 錄音中**即時寫入** |
| `逐字稿_雙語.md` | 原文＋繁中翻譯對照 | 錄音中**即時寫入** |
| `錄音.flac` | 上課錄音（無損壓縮，可以直接用 QuickTime 播放），每小時約 60–90 MB。不想存可以在「Audio」關掉「Also save the recording (FLAC)」 | 錄音中**即時寫入** |
| `筆記.md` | 整理好的筆記（摘要、詳細筆記、作業提醒、專有名詞表、待確認處） | 按「Generate notes」後 |

逐字稿是一邊錄一邊存的，就算程式當掉或網路斷掉，已經辨識的內容也不會不見。
錄音中不小心重新整理或關掉網頁也沒關係：重新打開 http://localhost:8501 就會接回正在進行的錄音。

---

## 筆記傳到 Notion（選填）

產生筆記後，按筆記旁邊的 **「Send to Notion」**，會在 Notion 的「課堂筆記」資料庫新增一頁（標題是「課程名稱｜日期」，並自動填好「課程」「日期」欄位）。第一次要先設定（約 5 分鐘）：

1. 打開 https://www.notion.so/profile/integrations →「New integration」→ 名稱隨便取、選你的 workspace、類型選 **Internal** → 存檔
2. 複製 **Internal Integration Secret**（`ntn_` 開頭），在 `.env` 加一行 `NOTION_TOKEN=貼上金鑰`，**重新啟動程式**（`.env` 在 Finder 裡是隱藏檔，用 `open -e .env` 打開）
3. 在 Notion 打開要放筆記的頁面（例如「NEOMA」）→ 右上角「⋯」→「Connections」→ 加入剛剛建立的 integration

第一次傳的時候，會在那個頁面裡**自動建立「課堂筆記」資料庫**，之後都傳到同一個資料庫。也可以自己先建好一個名稱有「筆記」的資料庫並分享給 integration，就會用那個。學校帳號的 Notion 如果不能建立 integration，就是管理員沒開放。

## 五、費用（以 2026 年 9 月查到的官方價格估算）

| 項目 | 價格 | 一堂 3 小時的課 |
|---|---|---|
| Voxtral Realtime 語音辨識 | $0.006／分鐘 | 約 $1.08 |
| 翻譯（mistral-small） | 依字數計費 | 通常比辨識便宜，實際看用量 |
| 產生筆記（mistral-medium） | 依字數計費 | 一次 |
| 讀簡報（mistral-large） | 依字數計費 | 每份簡報一次，通常幾美分以內 |

畫面上的「Recognition cost」只算語音辨識，翻譯和筆記的費用沒有算進去。

**費用算在誰身上**：費用會算在建立這把金鑰的**組織**。登入 Mistral Studio 後，左下角會顯示目前所在的組織。
- **學校或公司的組織**：費用由組織支付，不會扣你個人的錢。但組織的管理員看得到用量，使用前最好先確認組織允許這種用途、有沒有用量上限。
- **你自己的個人帳號**：依照你帳號的方案和付款設定計費。

**怎麼看實際用量**：admin.mistral.ai → **Usage**（帳單在 **Billing**）。如果這些選項旁邊有鎖頭、點了打不開，代表你沒有權限，要請組織的管理員幫你查，或開放權限給你。用量頁面可能要過一段時間才會更新。

---

## 六、進階設定（左邊「Advanced settings」）

| 設定 | 說明 |
|---|---|
| Recognition delay（辨識延遲） | 越短字幕越快出現，越長通常越準。預設由伺服器決定 |
| Minimum characters per translated segment（每段至少幾個字元才翻譯） | 越大 → 翻譯次數越少、每段越長；越小 → 翻譯更即時但請求更多 |
| Translation model／Notes model | 預設 `mistral-small-latest`／`mistral-medium-latest`，可以改成其他 Mistral 模型名稱 |

---

## 七、已知限制

- **收音品質決定一切**：坐離老師近一點；筆電內建麥克風在大教室效果有限，外接麥克風會好很多
- **時間戳**是「這一段辨識完成時」的錄音時間，不是這段話開始的時間
- 翻譯模型不一定知道各領域的通用譯名，可能把專有名詞翻錯（例如把 arm's length principle 照字面翻）。專有名詞都會附上原文；發現翻錯可以在側邊欄「Fix a translation」修正，之後的翻譯和筆記就會照用
- 語音辨識可能聽錯專有名詞；翻譯和筆記都有要求模型「只根據逐字稿、不自行補充」，但仍然可能出錯，重要內容請對照投影片
- Voxtral Realtime 目前不能區分說話者（官方文件：Realtime 不支援 diarize）
- 錄音前請確認學校和老師允許上課錄音
- 本程式在開發時用**模擬的 Mistral 回應**測試過流程，第一次實際使用請先用短音訊檔確認

---

## 八、檔案說明

| 檔案 | 用途 |
|---|---|
| `app.py` | Streamlit 介面 |
| `core.py` | 核心邏輯：收音、串流辨識、斷句、翻譯、存檔、產生筆記 |
| `requirements.txt` | 需要的套件 |
| `.env.example` | API Key 設定範本 |
| `run.command` | 雙擊啟動 |
| `.streamlit/config.toml` | 只允許本機連線（同一個 Wi-Fi 的人連不進來） |
