# 概念書：手動展開 + 攔截回應、純紀錄留言（超低風險）

**目標**：只紀錄「紀錄者手動在畫面上展開的」留言，且**絕不代發任何請求**，避免帳號被鎖。

---

## 一、原則（零風險前提）

| 要做 | 不要做 |
|------|--------|
| 只**讀取**瀏覽器已經發出的請求與已經收到的回應 | 不發任何 HTTP/GraphQL 請求 |
| 只**訂閱**網路事件（listen-only） | 不修改請求、不注入腳本到頁面邏輯 |
| 解析並儲存「回應內容」 | 不使用 cookie / session 做任何額外連線 |
| 所有「點擊、捲動、展開」都由**使用者**操作 | 不做自動點擊、不自動捲動、不自動分頁 |

程式角色 = **被動的錄影機**：只錄下瀏覽器與 FB 之間的對話，不代使用者發言。

---

## 二、整體流程

```
[使用者] 用 Chrome 開 FB → 登入 → 點進社團 → 點進某則貼文 → 手動點「查看更多留言」「查看 X 則回覆」
                    ↓
[瀏覽器] 對 FB 發送 GraphQL（CommentsList / Depth1 / Depth2），FB 回傳 JSON
                    ↓
[本程式] 透過 CDP 連到同一個 Chrome，監聽 Network.requestWillBeSent + Network.responseReceived
         → 用 requestId 配對 request postData 與 response，篩選留言相關 doc_id
         → 取得 response body（JSON）
         → 依 docs/fb-comments-api-from-har.md 解析
         → 依 post_id / comment_id 合併成樹狀結構，寫入本地 JSON
```

- **程式不發包、不點擊、不登入**，只接在既有 Chrome 上「聽」回應。
- 紀錄範圍 = 使用者有在畫面上展開的那幾次請求所回傳的內容。

---

## 三、技術方案：CDP 攔截（建議）

- **Chrome 以 remote debugging 啟動**（與現有 collector 相同，例如 `--remote-debugging-port=9222`）。
- 紀錄程式以 **CDP** 連到該 Chrome，只做：
  1. `Network.enable`
  2. 訂閱 **`Network.requestWillBeSent`** 與 **`Network.responseReceived`**
- **為何也要訂閱 requestWillBeSent**：`responseReceived` 的 payload **不包含 request body**。篩選條件 `fb_api_req_friendly_name` 在 request 的 postData 裡，因此必須在 `requestWillBeSent` 時用 `requestId` 把 postData 暫存到一個 map，等對應的 `responseReceived` 觸發時再查表決定是否處理、並取得 request variables（如 feedback_id）。
- 當 **responseReceived** 觸發時：
  - 若 `response.url` 為 `https://www.facebook.com/api/graphql/` 且對應 request 為 POST，
  - 用 requestId 從 map 取出 request postData，檢查是否為 CommentsList / Depth1 / Depth2（fb_api_req_friendly_name 或 doc_id）。
  - 用 `Network.getResponseBody(requestId)` 取得回應 body。
- **篩選**：只處理「留言相關」的回應；其餘忽略。若 **比對不上已知 doc / friendly name**，仍將 raw response 寫入 `unused_graphql.jsonl` 一類檔案，方便日後 FB 改 doc_id 時 debug。
- 通過篩選的 response body → 見下節「回應 body 格式」處理後，寫入 raw 儲存或送解析器。

**回應 body 格式（實作必慮）**：
- FB GraphQL 常回傳 **multi-line JSON**（NDJSON：每行一個 JSON object），或帶 **`for (;;);`** 前綴（防 XSSI）。解析前須：strip 前綴（若有）、若為多行則逐行 `JSON.parse`。
- 回應可能經 gzip；CDP 的 `getResponseBody` 通常已回傳解壓後內容，實作時需驗證。

**為何風險極低**：CDP 在此只做「讀取」；不修改請求、不代發請求、不碰 cookie。行為等同你在 DevTools Network 面板手動把某筆回應「Copy response」存檔，只是改由程式自動做。

---

## 四、解析與儲存（依文件）

- 解析邏輯依 **docs/fb-comments-api-from-har.md**：
  - **CommentsListComponentsPaginationQuery** 回應 → 頂層留言列表（與貼文 feedback ID 對應）。
  - **Depth1CommentsListPaginationQuery** 回應 → `data.node.replies_connection.edges`，每則有 `id`、`body.text`、`author`、`created_time`、`feedback.id`、`expansion_info.expansion_token`。
  - **Depth2CommentsListPaginationQuery** 回應 → 同上結構，為「回覆的回覆」。

- **post_id ↔ feedback_id 對應**：Depth1/Depth2 的 request variables 裡帶的是**該則留言的 feedback_id**（base64），不是貼文 ID。要正確把回覆掛到「哪一則貼文」底下，必須建立 **feedback_id → post_id** 的 mapping。方式可二擇或並用：
  - 從 **CometFocusedStoryViewUFIQuery** 的回應中取得目前聚焦貼文的 feedback_id（decode 得 post_id），使用者切換貼文時會再發此 query，可更新「當前貼文」；
  - 或從 feedback_id base64 解碼：格式為 `feedback:783712451788586`（貼文）或含 comment 的 ID，可還原出 post_id。設計上須明確：何時建立/更新 mapping、Depth1/Depth2 回應抵達時如何查表歸屬到正確的 post。

- **合併策略**：
  - 同一貼文可能對應多筆回應（先載入頂層、再載入某則留言的回覆、再載入回覆的回覆）。
  - 以 **post_id** 為 key，維護一棵「留言樹」：頂層 = CommentsList 的結果；底下的回覆 = Depth1/Depth2 的 `edges`，依 `feedback.id` 或 comment_id 掛到對應父節點。
  - 若同一則留言有多筆 Depth1 分頁，用 `expansion_token` / cursor 區分，合併成該留言底下的完整回覆列表。
  - **Race condition**：使用者快速連點多個「查看回覆」時，多個 responseReceived 會非同步抵達。合併時須有明確策略：例如以 **queue 序列化**處理同一 post_id 的 merge，或對「該 post 的樹」加 **lock**，避免並行寫入導致資料錯亂。

- **輸出**：
  - **建議**：先以 **append-only JSONL** 寫入每筆攔截到的 raw response（每行一筆），再另跑 **merge script** 產出樹狀結構。raw 保留可避免 merge bug 導致原始資料遺失。
  - 合併後的產物：例如單一 JSON 以 post_id 為 key，或 `comments_{post_id}.json`；格式自訂（陣列樹狀或扁平帶 parent_id 皆可）。

---

## 五、風險控管檢查表

- [ ] 程式**從未**呼叫 `fetch` / `requests` / `httpx` 等向 `facebook.com` 或 `fbcdn.net` 發送請求。
- [ ] 程式**從未**使用 HAR 或任何檔案內的 cookie / session 向 FB 發送請求。
- [ ] 程式**從未**透過 CDP 執行「點擊」「捲動」「輸入」等模擬使用者的動作。
- [ ] 程式只使用 CDP 的 **Network.enable** + **Network.requestWillBeSent**（讀 request）+ **Network.responseReceived** + **Network.getResponseBody**（唯讀）。
- [ ] 所有「展開留言」的動作皆由**使用者本人在瀏覽器內手動**完成。

符合上述即為「手動展開 + 攔截回應、純紀錄」、維持超低風險。此檢查表可轉成 CI / pre-commit 規則，自動掃描禁止 `requests.get`、`fetch`、`httpx` 等向 FB 發送請求的 pattern。

---

## 六、與現有 collector 的關係

- **現有 collector**：定時用 CDP 對頁面執行 `Runtime.evaluate(extract_posts.js)`，從 **DOM** 抓目前畫面上的貼文/留言；會受虛擬化與 modal 影響，展開的留言常抓不到。
- **本設計（攔截回應）**：不讀 DOM，改為**攔截 GraphQL 回應**；只要使用者有點開，該次回應就會被 FB 送進瀏覽器，我們從 Network 側複製一份來解析，故能完整紀錄「該次展開」回傳的所有留言，且不受虛擬化影響。

兩者可並存：**建議與現有 collector 共用同一個 CDP session**（同一條 WebSocket 連線），在同一 process 內同時訂閱 DOM 輪詢與 Network 事件，再依 post_id 合併；或只啟用「Network 攔截」作為留言的單一來源，依需求取捨。

---

## 七、實作項目摘要

1. **CDP 連線**：連到既有 Chrome（與 collector 相同 port），建議與 collector 共用同一 CDP session；只開 Network 監聽。
2. **Request/Response 配對**：訂閱 `requestWillBeSent` 暫存 requestId → postData；`responseReceived` 時查表篩選並取 body。
3. **篩選**：只處理 `POST /api/graphql/` 且 request 為 CommentsList / Depth1 / Depth2；未知 doc 寫入 `unused_graphql.jsonl`。
4. **取 body**：`Network.getResponseBody`；strip `for (;;);`、支援 NDJSON 逐行 parse；必要時驗證 gzip 已解壓。
5. **feedback_id → post_id**：從 CometFocusedStoryViewUFIQuery 或 base64 解碼建立/更新 mapping，Depth1/Depth2 依此歸屬貼文。
6. **解析**：依 fb-comments-api-from-har.md 抽出 `replies_connection.edges`、feedback id、body.text、author、created_time、expansion_token。
7. **合併**：以 post_id 為 key，queue 或 lock 處理並行，將多筆回應合併成樹狀留言結構。
8. **輸出**：raw 先寫 append-only JSONL；另跑 merge script 產出樹狀 JSON（依 post_id 或單檔多貼文）。
9. **斷線與遺漏**：CDP WebSocket 斷線時實作重連；斷線期間的 response 無法補救，須 log 讓使用者知悉。
10. **不實作**：不發請求、不自動點擊、不重放 HAR、不使用 cookie 發包。

此即「手動展開 + 攔截回應、依文件解析」的完整概念與邊界；實作時嚴格遵守第 10 點即可維持超低風險。
