# ctrl-F-auto-crawler

Facebook 私密社團貼文收集器。透過 Chrome DevTools Protocol (CDP) 連接你的**真實瀏覽器**，在你手動瀏覽社團頁面時，在背景靜默讀取 DOM 中已載入的貼文內容，匯出為結構化 JSON。

**所有頁面操作（滾動、點擊展開留言、導航）都由你本人手動完成。腳本不會對頁面做任何操作，只讀取 DOM。**

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- Google Chrome 或 Chromium
- 你必須是該 Facebook 私密社團的成員

## Install

```bash
git clone <repo-url> && cd ctrl-F-auto-crawler
uv sync
```

## Usage

整個流程需要**兩個 Terminal 視窗**同時運作。

### Terminal 1 — 啟動 Chrome (Debug Mode)

```bash
./start_chrome.sh
```

Chrome 會以 remote debugging 模式開啟（port 9222）。

> 首次啟動會使用獨立的瀏覽器 profile（`~/.config/chrome-debug-profile`），不會影響你平常的 Chrome。

### Terminal 2 — 啟動收集器

```bash
uv run python collector.py
```

腳本連線後會顯示提示，然後進入等待狀態。

### 在 Chrome 中手動操作

1. 登入你的 Facebook 帳號
2. 導航到目標私密社團頁面
3. **像平常一樣瀏覽**：往下滾動、點擊「N則留言」展開留言
4. Terminal 2 會即時顯示目前已收集到多少篇貼文
5. 覺得夠了就回到 Terminal 2 按 **Ctrl+C** 結束，資料自動存檔

#### 參數說明

| 參數 | 預設值 | 說明 |
|---|---|---|
| `--port` | `9222` | Chrome remote debugging port |
| `--output` | `posts.json` | 輸出檔案路徑 |
| `--interval` | `3.0` | DOM 快照間隔秒數（多久讀一次畫面） |

#### 範例

```bash
# 基本用法
uv run python collector.py

# 自訂輸出檔和讀取間隔
uv run python collector.py --output outputs/batch1.json --interval 5
```

## Output Format

輸出為 JSON 陣列，每個元素代表一篇貼文：

```json
[
  {
    "author": "王小明",
    "post_text": "想詢問認識地球（下學期）的評價...",
    "timestamp": "8月24日 下午5:27",
    "post_link": "https://www.facebook.com/groups/123456/posts/789012/",
    "comment_count": "2則留言",
    "comments": [
      {
        "author": "李大華",
        "text": "我修過下學期的，基本上有上課就輕鬆",
        "time": "1 週前"
      }
    ]
  }
]
```

## How It Works

```
┌──────────────────────────────────────────────────┐
│  Terminal 1: Chrome (Debug Mode, port 9222)       │
│  ┌────────────────────────────────────────────┐  │
│  │  Facebook 社團頁面                          │  │
│  │  你手動：滾動、點擊展開留言、正常瀏覽        │  │
│  └────────────────────────────────────────────┘  │
└──────────────────────┬───────────────────────────┘
                       │ CDP (localhost:9222)
                       │ 純本地連線，FB 看不到
┌──────────────────────▼───────────────────────────┐
│  Terminal 2: collector.py (純被動模式)             │
│                                                   │
│  每 N 秒做一件事：                                  │
│    讀取當前 DOM → 提取可見貼文+留言 → 去重合併       │
│                                                   │
│  ✅ 只讀取 DOM，不產生任何網路請求                    │
│  ✅ 不滾動、不點擊、不輸入、不導航                    │
│  ✅ FB 伺服器端完全無法感知腳本的存在                 │
│                                                   │
│  Ctrl+C → 存檔為 JSON                              │
└───────────────────────────────────────────────────┘
```

## Safety

此腳本的設計原則是**零風險**：

- 腳本**不操作瀏覽器**，不觸發任何 Facebook API 請求
- 所有操作等同你在 Chrome DevTools Console 中按 F12 查看 DOM
- Facebook 伺服器端無法區分「你在看頁面」和「腳本在讀 DOM」
- 唯一的風險來源是**你自己的手動操作速度**，正常瀏覽即可

## File Structure

```
├── start_chrome.sh      # 啟動 Chrome Debug Mode
├── collector.py         # 主程式：CDP 連接、被動讀取、匯出
├── extract_posts.js     # 注入瀏覽器的 DOM 解析邏輯（純讀取）
├── pyproject.toml       # Python 依賴管理
└── README.md
```
