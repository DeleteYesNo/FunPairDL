# FunPairDL

A local download manager that keeps a **video** and its matching **funscript(s)** together as a single unit — built around the EroScripts ecosystem. It resolves links from multiple hosts, downloads them, tracks progress in a queue, and renames the results into a tidy structure that players and devices can recognize.

> ⚠️ **NSFW / personal project.** This is a hobby tool I built for my own use and share for others with the same need. It comes with no warranty and may contain bugs. Use it at your own risk and respect the terms of service of every site you download from.

---

## English

### What it does

The core unit is a **Pair**: one post / work = one video + one or more `.funscript` files. FunPairDL:

- Resolves links from several providers: **Pixeldrain, MEGA, GoFile, EroScripts, Iwara, yt-dlp** sources, and direct URLs.
- Expands "bundles" (a Pixeldrain list or a MEGA folder) into individual files.
- Pairs videos with funscripts automatically (with a preview before downloading), handling multi-axis scripts (`.roll`, `.pitch`, …).
- Downloads with multi-segment / HLS support and a persistent queue.
- Organizes output: unifies file names, preserves multi-axis suffixes, and creates `.alt` / `.alt1` variant subfolders for alternates or multiple authors.

It is a Windows desktop **GUI** (PySide6) with a local **FastAPI** backend and an embedded tabbed browser.

### Requirements

- Python 3.11+
- Windows (developed and used on Windows 11; other platforms untested)
- Dependencies in `requirements.txt` (PySide6, aiohttp, yt-dlp, mega.py, fastapi, uvicorn, pydantic, qasync)

### Install & run

**Easiest (Windows):** just double-click **`run.bat`**. On first run it
checks for the required packages and installs them if needed, creates
`config.json` from the template, and then starts the app (no console
window). Later runs skip straight to launching. The only prerequisite is
[Python 3.11+](https://www.python.org/downloads/) on your PATH.

**Manual (any platform):**

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create your config from the template
#    (copy config.example.json to config.json and fill in only the
#     services you actually use)
copy config.example.json config.json   # Windows
# cp config.example.json config.json    # macOS/Linux

# 3. Launch the GUI
python run.py
```

If the app doesn't appear when launched via `run.bat`, run `python run.py`
in a terminal to see the error.

**Optional — desktop shortcut with icon (Windows):** run

```bash
python create_shortcut.py
```

This creates a `FunPairDL` shortcut on your desktop that launches the app
(via `pythonw.exe`, no console window) using the bundled icon. It will
auto-install `winshell` and `pywin32` if they are missing.

A headless **server mode** is available via `python run_server.py`.

### Configuration

Settings live in `config.json` (gitignored — it holds your API keys, passwords and session cookies, so it must never be committed). Start from `config.example.json` and fill in credentials only for the services you use.

### License

Released under the **PolyForm Noncommercial License 1.0.0** — free for any noncommercial use (personal, hobby, research), commercial use is not permitted. See [`LICENSE`](LICENSE).

---

## 中文

### 這是什麼

核心單位是 **Pair**:一篇貼文 / 一個作品 = 一支影片 + 一個或多個 `.funscript`。FunPairDL 會:

- 解析多來源連結:**Pixeldrain、MEGA、GoFile、EroScripts、Iwara、yt-dlp** 支援站點,以及直接 URL。
- 把「bundle」(Pixeldrain list、MEGA 資料夾)展開成個別檔案。
- 自動配對影片與 funscript(下載前會顯示預覽),並處理多軸腳本(`.roll`、`.pitch`…)。
- 支援多分段 / HLS 下載,並用佇列持久化保存進度。
- 整理輸出:統一檔名、保留多軸 suffix,遇到變體或多作者時建立 `.alt` / `.alt1` 子資料夾。

它是一個 Windows 桌面 **GUI**(PySide6),搭配本機 **FastAPI** 後端與內建分頁瀏覽器。

### 系統需求

- Python 3.11 以上
- Windows(在 Windows 11 上開發與使用;其他平台未測試)
- 相依套件見 `requirements.txt`(PySide6、aiohttp、yt-dlp、mega.py、fastapi、uvicorn、pydantic、qasync)

### 安裝與執行

**最簡單(Windows):** 直接雙擊 **`run.bat`**。首次執行時它會檢查所需套件、缺少就自動安裝,從範本建立 `config.json`,然後啟動 app(無主控台視窗);之後再執行就直接開啟。唯一前提是系統 PATH 上有 [Python 3.11 以上](https://www.python.org/downloads/)。

**手動(任何平台):**

```bash
# 1. 安裝相依套件
pip install -r requirements.txt

# 2. 從範本建立你的設定檔
#    (把 config.example.json 複製成 config.json,只填你實際會用的服務)
copy config.example.json config.json   # Windows

# 3. 啟動 GUI
python run.py
```

若用 `run.bat` 啟動後視窗沒出現,可在終端機執行 `python run.py` 查看錯誤訊息。

**選用——建立帶圖示的桌面捷徑(Windows):** 執行

```bash
python create_shortcut.py
```

會在桌面建立一個 `FunPairDL` 捷徑,用內附的 icon、以 `pythonw.exe` 啟動(無主控台視窗)。若缺少 `winshell`、`pywin32` 會自動安裝。

另有無介面的 **server 模式**:`python run_server.py`。

### 設定

設定存在 `config.json`(已被 gitignore——裡面有你的 API key、密碼與 session cookie,**絕對不能 commit**)。請從 `config.example.json` 開始,只填你會用到的服務憑證。

### 授權

採用 **PolyForm Noncommercial License 1.0.0**——任何非商業用途(個人、興趣、研究)皆可自由使用,**不允許商業用途**。詳見 [`LICENSE`](LICENSE)。
