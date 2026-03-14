****# FB 留言 API 結構（從 HAR 還原）

來源：`www.facebook.com-multiple-layer-full-with-log.har`  
情境：從社團動態牆 → 點進一則貼文 → 在專用留言視窗一層一層點開留言。

---

## 設計用途（兩種可能）

| 做法 | 程式角色 | 風險 | 紀錄範圍 |
|------|----------|------|----------|
| **A. 程式自動展開** | 程式自己發 GraphQL（帶 token 分頁），拉完所有留言 | 高（易鎖帳） | 可達「該貼文底下全部留言」 |
| **B. 手動展開 + 純紀錄** | 使用者手動點「查看更多」；程式只**攔截並儲存**瀏覽器收到的回應，不發任何請求 | 極低 | 僅「使用者有點開的那幾層」的完整內容 |

本文件描述的是 **API 長怎樣、回應結構如何**，兩者都會用到：

- **A**：用這份文件知道要打哪個 doc_id、帶哪些變數、如何分頁。
- **B**：用這份文件來 **解析**「使用者點開時，我們攔截到的 GraphQL response」— 例如用 CDP `Network.responseReceived` 或擴充功能監聽，當回應是 Depth1/Depth2/CommentsList 時把 body 存下來，再依文件裡的結構 parse 成留言樹。

**手動展開 + 純紀錄** 可以做到完整紀錄「使用者有展開到的」留言，且不必程式發請求；差別只是紀錄範圍 = 你有點開的區塊，而不是「整篇貼文全部留言」。

---

## 請求順序（與操作對應）

| 順序 | 操作 / 觸發 | GraphQL 名稱 | doc_id | 用途 |
|------|-------------|--------------|--------|------|
| 1 | 載入社團動態 | GroupsCometFeedRegularStoriesPaginationQuery | 34509753922002692 | 貼文列表 |
| 2 | 點進貼文連結 | (導向 **CometSinglePostDialog**，無 GraphQL) | - | 開留言視窗 |
| 3 | 開啟留言視窗 | CometFocusedStoryViewUFIQuery | 26902344142700705 | 留言介面 + 貼文 feedbackID |
| 4 | 同上 | CometFocusedStoryViewStoryQuery | 34245137785132223 | 貼文內容 |
| 5 | 載入第一批「頂層留言」 | **CommentsListComponentsPaginationQuery** | **26619250424347780** | 貼文底下的第一層留言（分頁） |
| 6 | 點「查看 X 則回覆」 | **Depth1CommentsListPaginationQuery** | **26276906848640473** | 某則留言的「直接回覆」 |
| 7+ | 再點回覆底下的「查看回覆」 | **Depth2CommentsListPaginationQuery** | (見多層 HAR) | 回覆的回覆 |

---

## 關鍵 ID 與變數

### 貼文 → 頂層留言

- **貼文 feedback ID（base64）**  
  - 範例：`ZmVlZGJhY2s6NzgzNzEyNDUxNzg4NTg2`  
  - 解碼：`feedback:783712451788586`（post_id = 783712451788586）
- **CommentsListComponentsPaginationQuery** 的 `variables` 要帶：
  - `id`: 貼文的 feedback ID（base64）
  - `feedLocation`: `"DEDICATED_COMMENTING_SURFACE"`
  - 分頁：`commentsAfterCursor` / `commentsBeforeCursor`（第一次可 null），回應裡會給下一頁 cursor。

### 某則留言 → 其回覆（Depth1）

- **該則留言的 feedback ID（base64）**  
  - 範例：`ZmVlZGJhY2s6NzgzNzEyNDUxNzg4NTg2Xzc4NDMxMjI0NTA2MTk0MA==`  
  - 解碼：`feedback:783712451788586_784312245061940`（貼文_留言 comment_id）
- **Depth1CommentsListPaginationQuery** 的 `variables` 要帶：
  - `id`: 該則留言的 feedback ID（base64）
  - `expansionToken`: 上一筆回應裡的 `expansion_token` 或 null（第一頁）
  - `feedLocation`: `"DEDICATED_COMMENTING_SURFACE"`
  - 分頁：`repliesAfterCursor` / `repliesBeforeCursor` 等

### 回覆的回覆（Depth2）

- 用該則「回覆」的 feedback ID 當 `id`，變數裡會有 `subRepliesAfterCursor` 等（與先前多層 HAR 一致）。

---

## 取得「貼文 feedback ID」的方式（此 HAR 內）

- **CometFocusedStoryViewUFIQuery** 的請求變數裡有  
  `feedbackID`: `"ZmVlZGJhY2s6NzgzNzEyNDUxNzg4NTg2"`  
  即貼文的 feedback base64。
- 若已知 **post_id**（例如從 URL `.../posts/783712451788586/`），可自行組：  
  `feedback:{post_id}` 再 base64，或從 UFI 回應的 `node.feedback.id` 取得。

---

## 回應結構（擷取留言用）

- **CommentsListComponentsPaginationQuery**  
  - 回應內會有頂層留言列表與分頁資訊（cursor、has_next_page 等），需從實際 response JSON 對應欄位（每版可能略異）。
- **Depth1 / Depth2**  
  - `data.node.replies_connection.edges[]`：每則為一筆留言。  
  - 每筆 `node` 含：`id`、`body.text`、`author.name`、`created_time`、`legacy_fbid`。  
  - 該則留言的 `feedback.expansion_info.expansion_token`：用於該則底下「下一頁」或下一層。  
  - `replies_connection.page_info.has_next_page`、`end_cursor`：分頁用。

---

## 實作流程摘要

1. **單則貼文**：從 URL 或 UFI 取得貼文 feedback ID（base64）。
2. **頂層留言**：用 **CommentsListComponentsPaginationQuery**（doc_id 26619250424347780），`id` = 貼文 feedback ID，依 `commentsAfterCursor` 分頁直到沒有下一頁。
3. **每則頂層留言的回覆**：用 **Depth1CommentsListPaginationQuery**（doc_id 26276906848640473），`id` = 該則留言的 feedback ID，依 `expansionToken` / cursor 分頁。
4. **回覆底下的再回覆**：用 **Depth2CommentsListPaginationQuery**，`id` = 該則回覆的 feedback ID，依 subReplies cursor 分頁。
5. 所有請求需帶同 session 的 `fb_dtsg`、`lsd`、cookie 等（僅分析用，勿把 HAR 內 cookie 用於自動化發請求）。

此文件足以依「貼文 → 頂層留言 → 一層一層回覆」實作抓取與分頁邏輯；若 FB 改版 doc_id 或變數名，需再錄一筆 HAR 對照更新。
