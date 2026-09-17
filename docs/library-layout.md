# 媒體庫版面與 `funlib.json` 約定（v1）

FunPairDL（下載、整理）與 FunLib（索引、播放）之間唯一的介面就是磁碟上的資料夾版面與這份約定。兩個 repo 各放一份相同內容；改動時**兩邊一起改**並把 `version` 加一。

- 寫入者：FunPairDL（整理完成時寫 `funlib.json`；回填工具補舊資料夾）。
- 讀取者：FunLib（每次掃描讀，缺檔案就退回「從檔名推導」）。
- FunLib 不寫這個檔案，也不移動 FunPairDL 產生的檔案。

## 資料夾版面

```
<媒體庫根目錄>/
  <作品>/                                  一個作品一個資料夾
    <作品>.mp4                              主影片
    <作品>.funscript                        主腳本（L0）
    <作品>.<axis>.funscript                 多軸：surge / sway / twist / roll / pitch / vibe / suck / valve …
    <作品>.<axis><修飾詞>.funscript         軸字後可接大寫、數字、_、- 開頭的修飾詞（suckManual、vib2、twist_v2）
    <作品> (Soft).funscript                 2026-05 之前的舊版面：變體平放在主資料夾（FunLib 視為變體列）
    funlib.json                             本約定的 sidecar（可選）
    <作品>.alt/                             變體：同影片、另一組腳本
      <作品>.alt.mp4                        影片以 hardlink 帶入
      <作品>.alt.funscript
      <作品>.alt.<axis>.funscript
      funlib.json                           可選；沒有時沿用上層的
    <作品>.alt1/ …                          第二個以後的變體
  No Video/
    <作品>/                                 只有腳本、沒有影片的作品，版面同上
```

## `funlib.json`

放在作品資料夾內，UTF-8，無 BOM。所有欄位都可省略，缺的欄位 FunLib 用推導值；未知欄位 FunLib 忽略。

```json
{
  "version": 1,
  "title": "(Theobrobine) Mockgame - Lina X Demo",
  "author": "Theobrobine",
  "author_url": "https://discuss.eroscripts.com/u/theobrobine",
  "source": {
    "site": "eroscripts",
    "url": "https://discuss.eroscripts.com/t/some-slug/12345",
    "topic_id": 12345,
    "post_number": 1
  },
  "category": { "id": 14, "name": "Free Scripts" },
  "tags": ["multi-axis", "genshin-impact", "3d"],
  "posted_at": "2026-01-01T00:00:00Z",
  "downloaded_at": "2026-03-10T01:02:03Z",
  "pair_id": "a1b2c3d4e5f6",
  "variant": { "kind": "alt", "index": 1, "label": "Soft" },
  "notes": ""
}
```

| 欄位 | 型別 | 寫入者從哪來 | FunLib 對應 |
|---|---|---|---|
| `version` | 整數 | 固定 1 | 不認識的版本照 v1 解讀並記 log |
| `title` | 字串 | 帖子標題 | 只作參考，列名仍用檔名 |
| `author` | 字串 | 帖子作者（OP）；或 `(Author)` 前綴 | `threadOPName` |
| `author_url` | 字串 | 論壇個人頁 | 暫存於 `threadOPAvatarURL` 欄位之外，目前不使用 |
| `source.site` | 字串 | `eroscripts`、`e621`、`socigames`… | — |
| `source.url` | 字串 | 帖子網址 | `sourceURL`（新欄位；`threadURL` 仍是列型別 `local://…`） |
| `source.topic_id` | 整數 | Discourse topic id | `threadID`（僅當列尚無 id 時） |
| `category.id` / `.name` | 整數／字串 | Discourse `category_id`；名稱備援 | `threadCategory`（id；沒有 id 時 0） |
| `tags` | 字串陣列 | 帖子標籤，小寫、以 `-` 連字 | 併入 `threadTags` |
| `posted_at` | ISO 8601 | 帖子發文時間 | `threadPostDate` |
| `downloaded_at` | ISO 8601 | pair 完成時間 | 目前不使用（列的 `importDate` 是掃描時間） |
| `pair_id` | 字串 | FunPairDL pair id | 目前不使用 |
| `variant.kind` | `alt` | 只在 `.alt*/` 內 | 資訊性 |
| `variant.index` | 整數 | `.alt`=0、`.alt1`=1… | 資訊性 |
| `variant.label` | 字串 | 作者給的名稱（Soft、Hard、Filler…） | 併入 `threadTags` 為 `variant:<label 小寫>` |

## FunLib 的推導規則（沒有 sidecar 或欄位缺漏時）

- `author`：列名開頭的 `(Author)` 或 `[Author]` 前綴；沒有就留 `Local Scan`。
- 自動標籤（固定字彙，掃描時重算，不會蓋掉使用者手動加的其他標籤）：
  - `multi-axis` / `single-axis`：依 `axisCount`。
  - `script-only`：沒有影片的列。
  - `variant`：`local://variant` 列（同夾變體）與 `.alt*` 資料夾內的列。
  - `variant:<label>`：變體名稱小寫，例如 `variant:soft`、`variant:simple`、`variant:vibration`、`variant:hardcore`；來自 sidecar 的 `variant.label` 或檔名尾端的 `(Label)`。
- 合併規則：`threadTags` = 使用者既有標籤 ∪ sidecar `tags` ∪ 自動標籤，再移除「這次沒算出來」的自動標籤（只動固定字彙與 `variant:*`）。

## 回填舊資料（FunPairDL 側）

1. 作者、下載時間、pair id：從 `queue_archive.jsonl` 與資料夾名就有，全部可補。
2. 帖子網址：`queue_archive.jsonl` 的 `source_url`（2026-09-17 起才有）、`topic_index.json`、`funpairdl.log` 裡的帖子網址（約 550 個帖子）可對回 pair。
3. 標籤、分類、發文時間、OP：有帖子網址的，抓 Discourse 的 `/t/<id>.json`；沒有的，用標題到論壇搜尋（`(Author) 標題` 幾乎唯一），找到再抓。
4. 寫入順序：先寫能離線算出的欄位，之後補齊論壇欄位時**只更新缺的欄位**，不覆蓋既有值。
