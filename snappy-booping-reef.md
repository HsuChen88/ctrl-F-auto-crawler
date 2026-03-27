# Plan: 自動點擊「查看更多/查看回覆」按鈕

## Context

目前專案是**純被動**架構——所有「點擊、捲動、展開」都由使用者手動操作，程式只透過 CDP 攔截 GraphQL 回應。使用者希望自動化「點擊展開按鈕」這一步，讓程式能無人值守地收集完整留言樹。

風險前提：Facebook 反爬蟲偵測極嚴格，被判定為機器人會鎖帳號。因此必須以**最低風險**方式實現，使用者可接受程式執行非常長的時間。

---

## 方案比較與建議

### 四種可行方案

| 方案 | 偵測風險 | 說明 |
|------|----------|------|
| **A. Playwright `connect_over_cdp()`** | **高** | 會注入 `__playwright*` 全域物件，FB 已知會偵測；與現有 pychrome 連線可能衝突 |
| **B. CDP `Input.dispatchMouseEvent`** (pychrome) | **中** | 事件是 trusted，但繞過 OS 輸入管線，缺少 OS 層級的時間戳與裝置簽章 |
| **C. 純 OS 層級** (pyautogui) | **低** | 移動真實游標，瀏覽器無法區分真人與程式 |
| **D. 混合：CDP 讀 + OS 寫** | **最低** | CDP 只負責「找按鈕座標」（與現有 extract_posts.js 相同手法），pyautogui 負責「移動滑鼠 + 點擊」 |

### 建議：方案 D（混合式）

理由：
1. CDP 連線**已經存在**（被動攔截器），用它讀 DOM 座標不增加任何新的偵測面
2. pyautogui 透過 Windows `SendInput` API 移動真實游標，產生的事件與使用者手動操作**完全一致**
3. 唯一偵測風險來自**行為模式**——透過超慢速度 + 隨機化可有效規避

**不建議 Playwright** 的原因：它會在每個 frame 注入 JavaScript binding（`window.__playwright*`、`window.__pwInitScripts`），Facebook 已知會偵測這些物件。即使用 stealth 插件也只能部分遮蔽。且與現有 pychrome CDP 連線共存可能造成 protocol 衝突。

---

## 架構設計

### 運作模型

```
Terminal 1: unified_collector.py (既有，不修改)
  └─ CDP 被動攔截 GraphQL 回應

Terminal 2: auto_clicker.py (新增)
  └─ CDP 讀取按鈕位置 → pyautogui 移動游標 + 點擊
  └─ 點擊觸發 FB GraphQL 請求 → Terminal 1 自動攔截到

兩者透過瀏覽器隱式溝通，無需直接通訊協定。
```

### 新增檔案

```
auto_clicker.py          # 主程式入口 (argparse CLI)
human_input.py           # HumanMouseSimulator：Bezier 曲線移動、log-normal 延遲
find_expand_buttons.js   # 注入瀏覽器的 JS，找到所有展開按鈕並回傳座標
```

### 修改檔案

```
common.py                # 新增 get_window_bounds() 輔助函式（CDP Browser.getWindowBounds）
pyproject.toml           # 新增 pyautogui 依賴
```

---

## 核心模組設計

### 1. 按鈕偵測 (`find_expand_buttons.js`)

仿照現有 [extract_posts.js](extract_posts.js) 的模式，透過 `Runtime.evaluate` 注入純讀取的 JavaScript：

**目標按鈕文字** (中/英)：
- `查看更多留言` / `查看更多回答` / `View more comments`
- `查看 N 則回覆` / `查看全部的 N 則回覆` / `View N replies`
- `查看之前的留言` / `View previous comments`
- `更多回覆` / `More replies`
- `顯示更多回覆` / `Show more replies`

**做法：**
1. 查詢所有 `div[role="button"]`、`span[role="button"]`，篩選文字匹配的元素
2. 對每個元素呼叫 `getBoundingClientRect()` 取得 viewport 座標
3. 檢查可見性（非 `display:none`、非零尺寸、在可視範圍內）
4. 回傳 `[{ text, x, y, width, height, isInViewport }]`

### 2. 座標轉換 (page → screen)

```python
# 在 common.py 新增
def get_content_area_offset(tab):
    """用 CDP 取得 Chrome 視窗位置 + content area 偏移量"""
    # Browser.getWindowBounds() → 視窗的 left, top, width, height
    # window.screenX, window.screenY, window.outerHeight, window.innerHeight
    # → 計算出 chrome UI 高度 (網址列+書籤列)
    # window.devicePixelRatio → 處理 Windows DPI 縮放
```

公式：
```
screen_x = window_left + element.x + element.width/2  (加隨機偏移)
screen_y = window_top + chrome_ui_height + element.y + element.height/2
```

### 3. 擬人滑鼠模擬 (`human_input.py`)

**滑鼠移動：Bezier 曲線**
- 3~5 個控制點，隨機分布製造自然弧線
- 起點快、中段最快、終點慢（ease-in-ease-out）
- 每步加 1~3px 微抖動（手部自然震動）
- 移動時間 0.3~1.5 秒，依距離成正比
- 點擊位置：按鈕中心 + 隨機偏移 (±5~15px)，不要每次都點正中央

**捲動模擬：**
- 使用 `pyautogui.scroll()` (OS 層級滾輪事件)
- 每次 2~5 格滾輪，速度隨機
- 偶爾多滾一點再回滾（模擬滾過頭）

**時間延遲：Log-normal 分佈**
```python
import random, math
def human_delay(median_sec=3.0, sigma=0.5):
    return random.lognormvariate(math.log(median_sec), sigma)
```

| 場景 | 中位數延遲 |
|------|-----------|
| 兩次點擊之間 | 8~15 秒 |
| 捲動後 | 3~5 秒 |
| 每 5~10 次點擊後 | 30~120 秒（模擬暫停閱讀）|
| 隨機長休息 | 5~15 分鐘（模擬切頁籤/離開）|

**擬人行為強化：**
- 隨機跳過 10~20% 的按鈕（之後再回來或不回來）
- 不按照上到下順序點擊，隨機 shuffle
- 點擊間穿插無意義滑鼠漫遊（hover 到其他留言、移到捲軸等）
- 偶爾在留言上停留較久（模擬閱讀）

### 4. 安全監控

**即時偵測：**
- 每次點擊後檢查 DOM 是否出現 captcha / checkpoint 頁面
  - 偵測 `checkpoint`、`captcha`、`security check`、`確認你的身分`、`安全驗證` 等關鍵字
  - 偵測 URL 是否跳轉離開目標頁面
- 若點擊後沒有新留言載入（GraphQL 回空或 error），啟動退避

**分級退避：**
| 等級 | 觸發條件 | 動作 |
|------|----------|------|
| L1 | 連續 2 次點擊無新留言 | 延遲翻倍 |
| L2 | 連續 5 次無回應 | 暫停 5~10 分鐘 |
| L3 | 偵測到 captcha/checkpoint | **立即停止**，發出警告，等待使用者手動處理 |

**緊急停止：**
- 監聽 Ctrl+C 或 ESC 鍵，立即中斷所有自動操作
- Chrome 視窗必須保持前景且可見（pyautogui 的前提）

### 5. 主程式 (`auto_clicker.py`)

```
用法: uv run python auto_clicker.py --port 9222 [options]

--port              Chrome debug port (default: 9222)
--max-clicks        單次 session 最大點擊數 (default: 50)
--max-runtime       最大執行時間，分鐘 (default: 120)
--min-delay         兩次點擊最小間隔，秒 (default: 8)
--max-delay         兩次點擊最大間隔，秒 (default: 20)
--dry-run           只找按鈕並 log，不實際點擊
```

主迴圈：
1. CDP `Runtime.evaluate(find_expand_buttons.js)` → 取得按鈕清單
2. 若無按鈕 → 捲動一小段 → 重新掃描 → 連續 N 次無按鈕則結束
3. 隨機選一個按鈕 → 座標轉換 → Bezier 曲線移動游標 → 點擊
4. 等待 DOM 變化（新留言出現）或 timeout
5. 安全檢查（captcha / URL 跳轉）
6. 隨機延遲 → 回到步驟 1

---

## 限制與注意事項

1. **Chrome 視窗必須在前景且可見**——pyautogui 移動的是真實 OS 游標，需要視窗不被遮蓋
2. **執行時不能手動移動滑鼠**——否則會干擾自動化
3. **永遠無法達到零風險**——任何自動化都比純手動高風險，但方案 D 是所有選項中最安全的
4. **Facebook 可能更新按鈕的 DOM 結構**——需要定期維護 `find_expand_buttons.js` 中的選擇器
5. **Windows DPI 縮放**——座標轉換需處理 `devicePixelRatio`，在 Windows 11 特別需注意

---

## 實作順序

1. **Phase 1: 按鈕偵測** — `find_expand_buttons.js` + `common.py` 座標轉換 + dry-run 測試
2. **Phase 2: 擬人滑鼠** — `human_input.py` (Bezier 移動 + log-normal 延遲) + 非 FB 頁面測試
3. **Phase 3: 主迴圈** — `auto_clicker.py` 整合按鈕偵測 + 滑鼠模擬 + 安全監控
4. **Phase 4: 調校** — 在 FB 上實測，調整延遲參數、退避策略

---

## 驗證方式

1. `--dry-run` 模式：確認能正確找到所有展開按鈕並 log 座標
2. 非 FB 頁面測試：確認 Bezier 移動看起來自然（可錄影檢查）
3. FB 實測：搭配 `unified_collector.py` 執行，確認點擊後攔截器能收到新留言
4. 長時間測試：連續執行 1~2 小時，確認無 captcha 觸發
