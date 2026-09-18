# 媒體庫版面與 `funlib.json` 約定（v2，2026-09-18 修訂）

FunPairDL（下載、整理）與 FunLib（索引、播放）之間唯一的介面就是磁碟上的資料夾版面與這份約定。兩個 repo 各放一份相同內容；改動時**兩邊一起改**並把 `version` 加一。

- 寫入者：FunPairDL（整理完成時寫 `funlib.json`；回填工具補舊資料夾）。FunPairDL 是版面的唯一寫入者。
- 讀取者：FunLib（每次掃描讀，缺檔案就退回「從檔名推導」）。
- FunLib 不寫 `funlib.json`，不移動、不改名、不重排 FunPairDL 產生的檔案。**唯一的例外是刪除**：使用者明確刪除一個作品或一組腳本時，FunLib 把檔案搬進 `_trash/`（見下），並記錄在 `_trash/deleted.jsonl`。

## 資料夾版面（平放版面為標準）

一個作品一個資料夾，一支影片，幾套腳本就是幾個「變體」，全部平放：

```
<媒體庫根目錄>/
  <作品>/                                  一個作品一個資料夾
    <作品>.mp4                              唯一的影片
    <作品>.funscript                        主版本（Main）的 L0
    <作品>.<axis>.funscript                 主版本的其他軸：surge / sway / twist / roll / pitch / vibe / suck / valve …
    <作品>.<axis><修飾詞>.funscript         軸字後可接大寫、數字、_、- 開頭的修飾詞（suckManual、vib2、twist_v2）
    <作品> (<Label>).funscript              變體 Label 的 L0，例如 (Soft)、(Hard)、(Filler)
    <作品> (<Label>).<axis>.funscript       變體自己的其他軸（可省略：缺的軸從主版本繼承）
    <作品> (<Label>).mp4                    只在該變體有自己的影片時存在（另一個編碼、另一段影片）
    funlib.json                             本約定的 sidecar
  No Video/
    <作品>/                                 只有腳本、沒有影片的作品，版面同上
  _trash/                                   FunLib 刪除的東西（見「刪除與垃圾桶」）
    deleted.jsonl
    <stamp>/<原本的相對路徑>
```

變體命名規則（FunPairDL 在整理時保證）：
- Label 來自帖子的分組名稱或面板上的分組；輸出前正規化為 `<作品> (<Label>)`，作者原本的檔名不落地。
- 同一作品的 Label 不可重複；重複時加編號 `(Soft 2)`。
- Label 不含括號與路徑字元；大小寫保留原樣（FunLib 顯示時照用，標籤化時轉小寫）。
- Label 的來源依序：面板上替分組取的名稱 → 該組腳本的作者（與主版本作者不同時）→ `Alt`。
- 分組自帶的影片若與主影片位元相同就丟掉（同一支影片）；若是**不同的影片**，仍是同一個作品的變體，影片存成 `<作品> (<Label>).<ext>`，sidecar 的 `variants[].video` 指到它。FunLib 在媒體庫與播放器切換變體時要跟著換影片與縮圖（卡片預設顯示主版本）。

**舊版面（相容，不再產生）**：`<作品>.alt/`、`.alt1/` 子夾內放 `<作品>.alt.mp4`（hardlink）與 `<作品>.alt[.axis].funscript`。FunLib 會把它們當成同一作品的變體（Label = Alt、Alt 1…）。FunPairDL 的遷移工具會把它們改回平放版面：hardlink 或位元相同的影片刪掉、腳本改名為 `<作品> (Alt).funscript` 等；`.alt/` 內的影片若是另一個編碼版本，搬成 `<作品> (Alt).<ext>` 當該變體自己的影片。

## `funlib.json`

放在作品資料夾內，UTF-8，無 BOM。所有欄位都可省略，缺的欄位 FunLib 用推導值；未知欄位 FunLib 忽略。

```json
{
  "version": 2,
  "title": "(Theobrobine) Genshin Impact - Lisa X Aether",
  "author": "Theobrobine",
  "posted_by": "scripterName",
  "posted_by_url": "https://discuss.eroscripts.com/u/scriptername",
  "source": {
    "site": "eroscripts",
    "url": "https://discuss.eroscripts.com/t/some-slug/12345",
    "topic_id": 12345,
    "post_number": 1
  },
  "category": { "id": 14, "name": "Free Scripts" },
  "tags": ["multi-axis", "genshin-impact", "3d"],
  "posted_at": "2026-03-09T03:42:55Z",
  "downloaded_at": "2026-03-10T01:02:03Z",
  "pair_id": "59fb9fada842",
  "variants": [
    { "label": "Main", "primary": true, "author": "scripterName",
      "files": { "L0": "Work.funscript", "surge": "Work.surge.funscript", "sway": "Work.sway.funscript" } },
    { "label": "Soft", "inherit_axes": true, "author": "scripterName",
      "files": { "L0": "Work (Soft).funscript" } },
    { "label": "Remake", "author": "otherScripter", "video": "Work (Remake).mp4",
      "files": { "L0": "Work (Remake).funscript" } }
  ],
  "notes": ""
}
```

| 欄位 | 型別 | 寫入者從哪來 | FunLib 對應 |
|---|---|---|---|
| `version` | 整數 | 目前 2 | 不認識的版本照最新已知版本解讀並記 log |
| `title` | 字串 | 帖子標題 | 只作參考，列名仍用檔名 |
| `author` | 字串 | 標題的 `(Author)` 前綴（作品／動畫作者），**論壇資料不會覆蓋** | `threadOPName` |
| `author_url` | 字串 | 保留，目前不寫 | 目前不使用 |
| `posted_by` / `posted_by_url` | 字串 | 帖子 OP 的 username 與個人頁（通常是腳本作者） | 顯示為發帖者；與 `author` 並列標示 |
| `source.site` | 字串 | `eroscripts`、`e621`、`socigames`… | — |
| `source.url` | 字串 | 帖子網址 | `sourceURL`（`threadURL` 仍是列型別 `local://…`） |
| `source.topic_id` | 整數 | Discourse topic id | `threadID` |
| `category.id` / `.name` | 整數／字串 | Discourse `category_id`；名稱備援 | `threadCategory`（id；沒有 id 時 0） |
| `tags` | 字串陣列 | 帖子標籤，小寫、以 `-` 連字 | 併入 `threadTags` |
| `posted_at` | ISO 8601 | 帖子發文時間 | `threadPostDate` |
| `downloaded_at` | ISO 8601 | pair 完成時間 | 目前不使用 |
| `pair_id` | 字串 | FunPairDL pair id | 寫進刪除紀錄，讓 FunPairDL 對回 pair |
| `variants[]` | 陣列 | 帖子分組／面板分組 | 見下 |
| `variants[].label` | 字串 | 分組名稱；主版本用 `Main` | `variantLabel`；標籤 `variant:<label 小寫>` |
| `variants[].primary` | 布林 | 主版本設 true（沒有時 label=`Main` 者為主） | `isPrimary` |
| `variants[].files` | 物件 `{軸: 相對路徑}` | 鍵是軸名（`L0`、`surge`、`suckManual`…），值是相對作品資料夾的檔名 | 用 `L0` 檔名把資料庫列對到變體；軸名照鍵取，不再解析檔名 |
| `variants[].inherit_axes` | 布林，預設 true | 只想單軸播的變體設 false | `inheritAxes`：播放時缺的軸從主版本補 |
| `variants[].author` | 字串 | 該套腳本的作者（面板／帖子上的腳本作者；可省略） | 變體選單標示「哪個作者是哪個」 |
| `variants[].video` | 字串 | 該變體自己的影片檔名（相對作品資料夾；省略＝用作品的影片） | 該變體的列用這支影片與它的縮圖；媒體庫與播放器切換變體時一併切換 |

## FunLib 的資料模型與推導規則

- 資料庫一列 = 一套腳本。同一作品的列共用 `workKey`，恰好一列是 `isPrimary`。媒體庫預設一個作品一張卡片（只列主版本），播放器與手機頁有變體選單可切換；Shuffle／Play All 預設每個作品只取一個變體。
- 分組規則：有 `variants[]` 時以它為準——`files.L0` 對到的列都屬於這個資料夾的作品，**即使該列的影片是 `<作品> (<Label>).mp4`**（v2 起，FunLib 不能再只靠影片主幹推作品鍵）。沒有 `variants[]` 時：影片主幹是腳本主幹的前綴且下一字元非英數 → 同一作品；`.alt*/` 子夾 → 上層作品；`No Video/<作品>/` 內以資料夾名分組；媒體庫根目錄直接放的檔案不分組。
- 變體播放：變體自己的檔案優先，缺的軸從主版本補（`inherit_axes` 預設 true）。
- `author`：sidecar 優先；否則列名開頭的 `(Author)` 或 `[Author]` 前綴（跳過 `(CS-FREE-0118)` 這類包碼、排除 `(Multi-axis)` `(Unknown)` 等描述性前綴）；沒有就留 `Local Scan`。
- 自動標籤（固定字彙，掃描時重算，不會蓋掉使用者手動加的其他標籤）：`multi-axis` / `single-axis`、`script-only`、`variant`、`variant:<label>`。
- 合併規則：`threadTags` = 使用者既有標籤 ∪ sidecar `tags` ∪ 自動標籤，再移除「這次沒算出來」的自動標籤（只動固定字彙與 `variant:*`）。

## 刪除與垃圾桶（FunLib 寫、FunPairDL 讀）

FunLib 的刪除永遠是「搬進垃圾桶」，不直接刪檔：

- 目的地：`<媒體庫根目錄>/_trash/<stamp>/<原本的相對路徑>`，`stamp` 為 `YYYYMMDD-HHMMSS`。
- 範圍：`variant` = 只搬該套腳本自己的檔案（共用的影片留下給其他變體）；`work` = 整個作品。整個資料夾只在「裡面每個媒體檔都屬於被刪的列」時才整夾搬走，否則只搬個別檔案。
- 名稱永遠不改：主版本被刪後剩下的變體原名留在原位，FunLib 只是把它當成該作品現在的主版本顯示。
- 復原：把同樣的相對路徑搬回去；原位置已有檔案就中止並回報，不覆蓋。
- 清空（purge）：真正刪除 `_trash/<stamp>/` 下的那些路徑。

每次動作追加一行到 `<媒體庫根目錄>/_trash/deleted.jsonl`：

```json
{"id":"k3f9…","ts":"2026-09-18T10:00:00.000Z","action":"trash","scope":"variant","folderIdx":1,"stamp":"20260918-100000",
 "work":"Work","title":"Work (Soft)","label":"Soft","rowIds":["…"],"titles":["Work (Soft)"],
 "paths":["Work/Work (Soft).funscript"],"pair_id":"59fb9fada842","source_url":"https://discuss.eroscripts.com/t/some-slug/12345"}
{"id":"…","ts":"…","action":"restore","ref":"k3f9…","paths":["Work/Work (Soft).funscript"]}
{"id":"…","ts":"…","action":"purge","ref":"k3f9…"}
```

FunPairDL 的責任：
- 去重、「已在磁碟」判斷、媒體庫整理一律**忽略 `_trash/`**（視為不存在）。
- 讀 `deleted.jsonl`：以每個 `trash` 事件的 `id` 為鍵，後面的 `restore`／`purge` 以 `ref` 指回它，取最後狀態。`trash` 狀態下：帖子清單顯示「已刪除」而非 ✓；使用者重送同一帖子時照常下載（不視為已存在）。`restore` 後回到 ✓。
- `pair_id`／`source_url` 可能為空（沒有 sidecar 的舊資料夾），此時用 `paths` 對回作品資料夾名稱。

## 回填舊資料（FunPairDL 側）

1. 作者、下載時間、pair id：從 `queue_archive.jsonl` 與資料夾名就有，全部可補。
2. 帖子網址：`queue_archive.jsonl` 的 `source_url`（2026-09-17 起才有）、`topic_index.json`、`funpairdl.log` 裡的帖子網址（約 550 個帖子）可對回 pair。
3. 標籤、分類、發文時間、OP（`posted_by`）：有帖子網址的，抓 Discourse 的 `/t/<id>.json`；沒有的，用標題到論壇搜尋（`(Author) 標題` 幾乎唯一），找到再抓。`author` 一律維持前綴推得的值。
4. `variants[]`：從資料夾內的檔案反推（主版本＝與資料夾同名的檔案；`(Label)` 後綴＝變體；`.alt*/` 子夾＝變體，遷移為平放時一併改寫）。
5. 寫入順序：先寫能離線算出的欄位，之後補齊論壇欄位時**只更新缺的欄位**，不覆蓋既有值。
