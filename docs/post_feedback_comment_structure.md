分析完畢。以下是三者的關係：

## `post_id`、`feedback_id`、`comment_id` 的關係

**`feedback_id` 是 base64 編碼的字串**，解碼後可看出結構：

| 層級 | feedback_id 解碼結果 | 用途 |
|------|---------------------|------|
| 貼文 | `feedback:1967353666757786` | 作為 `CommentsListComponentsPaginationQuery` 的輸入，取得**頂層留言** |
| 留言 | `feedback:1967353666757786_<comment_id>` | 作為 `Depth1CommentsListPaginationQuery` 的輸入，取得該留言的**回覆** |

**核心概念：`feedback_id` 是「容器 ID」，用來取得它的子項目。**

### 具體對應

1. **貼文的 feedback_id** = `base64("feedback:" + post_id)`
   - `ZmVlZGJhY2s6MTk2NzM1MzY2Njc1Nzc4Ng==` → `feedback:1967353666757786`
   - 用於 lines 1-6 的 `CommentsListComponentsPaginationQuery`，分頁取得頂層留言

2. **留言的 feedback_id** = `base64("feedback:" + post_id + "_" + comment_id)`
   - 例：留言 `1967629016730251` 的 feedback_id 解碼 → `feedback:1967353666757786_1967629016730251`
   - 用於 `Depth1CommentsListPaginationQuery`，取得該留言底下的回覆

3. **回覆本身也有 feedback_id**，格式同樣是 `feedback:<post_id>_<reply_comment_id>`（不會巢狀父留言 ID），代表回覆也能繼續展開更深層回覆。

### 階層示意

```
Post (post_id: 1967353666757786)
  └─ feedback_id: feedback:1967353666757786  ← 用這個取頂層留言
       ├─ Comment (comment_id: 1967720600054426)
       │    └─ feedback_id: feedback:1967353666757786_1967720600054426  ← 用這個取回覆
       │         ├─ Reply (comment_id: 1967736883386131)  [line 7]
       │         └─ Reply (comment_id: 1967852436707909)  [line 7]
       ├─ Comment (comment_id: 1969877929838693)
       │    └─ feedback_id: ...1969877929838693  ← 用這個取回覆
       │         ├─ Reply (comment_id: 1969879199838566)  [line 8]
       │         └─ Reply (comment_id: 1969882776504875)  [line 8]
       ...
```

**總結**：`comment_id` 是留言的唯一 ID；`feedback_id` 是取得「該節點子項目」的 key，由 `post_id` + 可選的 `comment_id` 組成。想取頂層留言就用貼文的 feedback_id，想取回覆就用該留言的 feedback_id。