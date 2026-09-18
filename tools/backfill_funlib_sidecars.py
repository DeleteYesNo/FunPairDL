"""Backfill ``funlib.json`` sidecars for works downloaded before FunPairDL
wrote them (docs/library-layout.md, "回填舊資料").

Phase 1 — offline (always):
  title / pair_id / downloaded_at from the pair that produced the folder
  (queue.json + queue_archive.jsonl, matched by folder name, else by
  title key); author from the "(Author)" prefix; the forum topic from the
  pair's source_url, else topic_index.json (pair id → topic), else the
  topic URLs in funpairdl.log (slug ↔ title key); variants[] from the
  files in the folder. Existing values are never overwritten; variants[]
  is refreshed.

Phase 2 — ``--forum``: for sidecars that know their topic id, fetch
  ``/t/<id>.json`` and fill tags / category / posted_at / OP (``posted_by``).
  ``author`` always stays the "(Author)" prefix; a sidecar whose author was
  once overwritten by the OP (has ``author_url``) is repaired in phase 1.

Phase 3 — ``--search``: for works with no topic, search the forum for the
  title and take the hit whose title matches; misses are remembered in
  ``_backfill_misses.json`` and not retried unless ``--retry-misses``.

Second pass for the leftovers (run after the first):
  * offline, always: a bundle's auto-split children inherit the parent
    pair's topic (from the "Auto-split" log lines); a pair sent from a
    topic page gets that topic when exactly one topic page was loaded in
    the foreground during the minute before and its slug overlaps the
    pair's name.
  * ``--search-loose``: for recorded misses whose name carries an
    "(Author)" prefix, search without the prefix and accept a hit only when
    the prefix-stripped titles match AND the author is confirmed (the hit's
    title names the author, or the topic's OP is the author). Every
    acceptance is listed in the report as LOOSE for review.
  * ``--local-tags``: tags derived offline, added only when absent —
    ``source-<site>`` (e621, iwara, …), ``pack-<bundle>`` for split
    children, ``len-…`` buckets (forum names) from the L0 script's length.

Forum requests go through the running app's embedded browser (CDP, port
9223) so the login cookies never leave it; without the app they use the
saved cookies directly (only safe while the app is NOT running).

DRY-RUN by default; ``--apply`` writes. Safe to re-run.

    python tools/backfill_funlib_sidecars.py --roots F:\\Lib H:\\Lib --apply
    python tools/backfill_funlib_sidecars.py --roots ... --apply --forum
    python tools/backfill_funlib_sidecars.py --roots ... --apply --forum --search --limit 500
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from funpairdl.constants import LOG_FILE, QUEUE_ARCHIVE_FILE, QUEUE_FILE  # noqa: E402
from funpairdl.core import library as lib  # noqa: E402
from funpairdl.core.queue_manager import QueueManager  # noqa: E402
from funpairdl.persistence.topic_index import TOPIC_INDEX_FILE  # noqa: E402
from funpairdl.utils import discourse  # noqa: E402

STAMP = time.strftime("%Y%m%d-%H%M%S")
FORUM = discourse.FORUM_BASE
LOOSE_WINDOW_S = 60
LEN_BUCKETS = [(2, "len-0-2"), (5, "len-2-5"), (10, "len-5-10"), (25, "len-10-25"),
               (60, "len-25-60"), (None, "len-60-plus")]      # forum tag names, minutes
SOURCE_SITES = {"e621.net": "e621", "e926.net": "e621", "iwara.tv": "iwara", "socigames.com": "socigames",
                "hmvmania.com": "hmvmania", "rule34video.com": "rule34video", "rule34.xxx": "rule34",
                "hanime1.me": "hanime"}
MISSES_FILE = ROOT / "_backfill_misses.json"
_LOG_TOPIC_RE = re.compile(r"discuss\.eroscripts\.com/t/([^/\s\"'?#]+)/(\d+)")
LOG_URL_TRUNCATED_AT = 100      # the browser logs url[:100]; a URL that long may have lost id digits


def _truncated(url: str) -> bool:
    return len(url) >= LOG_URL_TRUNCATED_AT


# ── offline sources ──────────────────────────────────────────────────────

def load_pairs() -> list[dict]:
    pairs: list[dict] = []
    try:
        pairs.extend(json.loads(Path(QUEUE_FILE).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    try:
        with open(QUEUE_ARCHIVE_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    pairs.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return [p for p in pairs if isinstance(p, dict) and p.get("id")]


def index_pairs(pairs: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    """by folder name (lower) and by title key; the newest pair wins."""
    by_folder: dict[str, dict] = {}
    by_title: dict[str, dict] = {}

    def newer(a: dict | None, b: dict) -> dict:
        if a is None:
            return b
        return b if (b.get("created_at") or "") >= (a.get("created_at") or "") else a

    for p in pairs:
        if p.get("state") not in (None, "completed"):
            continue
        od = p.get("output_dir") or ""
        if od:
            folder = Path(od).name.lower()
            by_folder[folder] = newer(by_folder.get(folder), p)
        k = QueueManager._title_key(p.get("name") or "")
        if len(k) >= 4:
            by_title[k] = newer(by_title.get(k), p)
    return by_folder, by_title


def load_topic_index() -> dict[str, tuple[str, str]]:
    """pair id → (topic id, url)."""
    out: dict[str, tuple[str, str]] = {}
    try:
        data = json.loads(Path(TOPIC_INDEX_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    for tid, e in data.items():
        for p in e.get("pairs") or []:
            if p.get("id"):
                out[p["id"]] = (str(tid), e.get("url") or "")
    return out


def load_log_topics(log_path: Path = LOG_FILE) -> dict[str, str]:
    """title key of a topic slug → topic id (only unambiguous ones)."""
    seen: dict[str, set[str]] = {}
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                for m in _LOG_TOPIC_RE.finditer(line):
                    slug, tid = m.group(1), m.group(2)
                    url = line[m.start():].split()[0].strip()
                    if _truncated(url[url.find("https://"):] if "https://" in url else "https://" + url):
                        continue
                    k = QueueManager._match_key(slug.replace("-", " "))
                    if len(k) >= 4:
                        seen.setdefault(k, set()).add(tid)
    except OSError:
        pass
    return {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}


_SPLIT_CHILD_RE = re.compile(r"Auto-split: created pair '(.*)' \(\d+ items\)$")
_SPLIT_PARENT_RE = re.compile(r"Auto-split: original pair '(.*)' split into (\d+) pairs$")
_ADDED_RE = re.compile(r"^(\S+ \S+) \[INFO\] funpairdl\.queue_manager: Added pair: (.*) \(\d+ items\)$")
_PAGE_RE = re.compile(r"^(\S+ \S+) \[INFO\] funpairdl\.gui\.browser: Page loaded in [\d.]+s \(foreground, ok=True\): "
                      r"https://discuss\.eroscripts\.com/t/([^/\s]+)/(\d+)")


def load_log_split_children(log_path: Path = LOG_FILE) -> dict[str, str]:
    """child pair name → parent pair name, from the auto-split log lines
    (the N "created pair" lines precede their "original pair … split into N")."""
    out: dict[str, str] = {}
    pending: list[str] = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _SPLIT_CHILD_RE.search(line)
                if m:
                    pending.append(m.group(1))
                    continue
                m = _SPLIT_PARENT_RE.search(line)
                if m:
                    n = int(m.group(2))
                    for child in pending[-n:]:
                        out[child] = m.group(1)
                    pending = []
    except OSError:
        pass
    return out


def _tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]+", (s or "").lower()) if len(t) >= 2}


def _overlaps(slug: str, name: str) -> bool:
    sk = QueueManager._match_key(slug.replace("-", " "))
    nk = QueueManager._title_key(name)
    if len(sk) >= 6 and len(nk) >= 6 and (sk in nk or nk in sk):
        return True
    a, b = _tokens(slug.replace("-", " ")), _tokens(name)
    return bool(a and b) and len(a & b) / len(a | b) >= 0.5


def load_log_sent_from_topic(log_path: Path = LOG_FILE) -> dict[str, str]:
    """title key of a pair name → topic id, for pairs added within a minute
    after exactly one topic page was loaded in the foreground, when that
    topic's slug overlaps the pair name. Ambiguous names are dropped."""
    seen: dict[str, set[str]] = {}
    recent: list[tuple[datetime, str, str]] = []      # (time, slug, topic id)
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                m = _PAGE_RE.match(line)
                if m:
                    at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                    if _truncated(line[line.find("https://"):].split()[0]):
                        continue
                    recent.append((at, m.group(2), m.group(3)))
                    recent = [r for r in recent if (at - r[0]).total_seconds() <= LOOSE_WINDOW_S]
                    continue
                m = _ADDED_RE.match(line)
                if not m:
                    continue
                at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                name = m.group(2)
                window = [r for r in recent if 0 <= (at - r[0]).total_seconds() <= LOOSE_WINDOW_S]
                topics = {r[2]: r[1] for r in window}
                if len(topics) != 1:
                    continue
                tid, slug = next(iter(topics.items()))
                if not _overlaps(slug, name):
                    continue
                k = QueueManager._title_key(name)
                if len(k) >= 4:
                    seen.setdefault(k, set()).add(tid)
    except OSError:
        pass
    return {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}


def strip_author_prefix(name: str) -> str:
    return re.sub(r"^(\s*[\(\[（【][^\)\]）】]*[\)\]）】]\s*)+", "", name or "").strip()


def loose_key(name: str) -> str:
    """Title key with the leading "(Author)" groups and trailing qualifier
    tags removed — what a loose forum match compares."""
    s = strip_author_prefix(name)
    while True:
        t = QueueManager._QUALIFIER_RE.sub("", s)
        if t == s:
            break
        s = t
    return QueueManager._match_key(s)


def pack_tag(parent_name: str) -> str:
    slug = re.sub(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]+", "-", strip_author_prefix(parent_name).lower()).strip("-")
    if len(slug) > 40:
        slug = slug[:40].rsplit("-", 1)[0] if "-" in slug[:40] else slug[:40]
    return f"pack-{slug.strip('-')}" if slug else ""


def len_tag(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return ""
    minutes = seconds / 60.0
    for limit, tag in LEN_BUCKETS:
        if limit is None or minutes < limit:
            return tag
    return ""


def source_tags(pair: dict | None) -> set[str]:
    out: set[str] = set()
    from urllib.parse import urlparse
    for it in (pair or {}).get("items") or []:
        host = (urlparse(it.get("url") or "").hostname or "").lower()
        for h, site in SOURCE_SITES.items():
            if host == h or host.endswith("." + h):
                out.add(f"source-{site}")
    return out


def local_tags(work: Path, sidecar: dict, pair: dict | None, parent_name: str) -> list[str]:
    """Tags to add (absent ones only): source site, pack, length bucket."""
    have = {str(t).lower() for t in (sidecar.get("tags") or [])}
    add: list[str] = []
    if not any(t.startswith("source-") for t in have):
        add.extend(sorted(source_tags(pair)))
    if parent_name and not any(t.startswith("pack-") for t in have):
        pt = pack_tag(parent_name)
        if pt:
            add.append(pt)
    if not any(t.startswith("len-") for t in have):
        from funpairdl.utils.media_duration import funscript_info
        main = next((v for v in sidecar.get("variants") or [] if v.get("primary")), None)
        l0 = (main or {}).get("files", {}).get("L0") if main else None
        if l0:
            try:
                info = funscript_info((work / l0).read_bytes())
                lt = len_tag(info.get("duration"))
                if lt:
                    add.append(lt)
            except OSError:
                pass
    return [t for t in add if t.lower() not in have]


def to_utc_iso(local_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(local_iso)
    except (TypeError, ValueError):
        return ""
    if dt.tzinfo is None:
        dt = dt.astimezone()          # naive = local clock
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def repair_op_author(existing: dict | None, title: str, folder: str) -> dict | None:
    """An earlier run stored the OP's username as ``author`` (with
    ``author_url``): move it to ``posted_by`` and restore the prefix author."""
    if not existing or not existing.get("author_url") or existing.get("posted_by"):
        return existing
    fixed = dict(existing)
    fixed["posted_by"] = existing.get("author", "")
    fixed["posted_by_url"] = existing["author_url"]
    fixed.pop("author_url", None)
    prefix = lib.author_from_name(title) or lib.author_from_name(folder)
    if prefix:
        fixed["author"] = prefix
    else:
        fixed.pop("author", None)
    return fixed


def find_pair(work: Path, by_folder: dict, by_title: dict) -> dict | None:
    return by_folder.get(work.name.lower()) or by_title.get(QueueManager._title_key(work.name))


def offline_sidecar(work: Path, by_folder: dict, by_title: dict,
                    topic_by_pair: dict, log_topics: dict,
                    split_parents: dict | None = None, sent_from: dict | None = None) -> dict:
    folder = work.name
    pair = find_pair(work, by_folder, by_title)
    title = (pair.get("name") if pair else "") or folder
    data: dict = {"version": lib.SIDECAR_VERSION, "title": title}
    author = lib.author_from_name(title) or lib.author_from_name(folder)
    if not author and pair:
        for it in pair.get("items") or []:
            if it.get("file_type") == "funscript" and (it.get("group") or "Main") == "Main" and it.get("author"):
                author = it["author"]
                break
    if author:
        data["author"] = author
    tid, url = None, ""
    if pair:
        data["pair_id"] = pair["id"]
        dl = to_utc_iso(pair.get("created_at") or "")
        if dl:
            data["downloaded_at"] = dl
        url = (pair.get("source_url") or "").strip()
        if url:
            tid = lib.topic_id_from_url(url) if lib.source_site(url) == "eroscripts" else None
        if not url and pair["id"] in topic_by_pair:
            t, u = topic_by_pair[pair["id"]]
            tid = int(t)
            url = u or f"{FORUM}/t/{t}"
    if not url:
        for key in (QueueManager._title_key(title), QueueManager._title_key(folder)):
            if key in log_topics:
                tid = int(log_topics[key])
                url = f"{FORUM}/t/{tid}"
                break
    # second pass: a topic the pair was sent from (foreground page + name overlap)
    if not url and sent_from:
        for key in (QueueManager._title_key(title), QueueManager._title_key(folder)):
            if key in sent_from:
                tid = int(sent_from[key])
                url = f"{FORUM}/t/{tid}"
                break
    # second pass: a bundle's child inherits the parent pair's topic
    if not url and split_parents:
        parent_name = split_parents.get(title) or split_parents.get(folder)
        parent = by_title.get(QueueManager._title_key(parent_name)) if parent_name else None
        if parent:
            purl = (parent.get("source_url") or "").strip()
            ptid = lib.topic_id_from_url(purl) if purl and lib.source_site(purl) == "eroscripts" else None
            if not ptid and parent["id"] in topic_by_pair:
                ptid = int(topic_by_pair[parent["id"]][0])
            if ptid:
                tid, url = ptid, f"{FORUM}/t/{ptid}"
    if url:
        src = {"site": lib.source_site(url), "url": url}
        if tid:
            src["topic_id"] = tid
        data["source"] = src
    data["variants"] = lib.scan_variants(work, folder)
    return data


# ── forum access ─────────────────────────────────────────────────────────

def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


class CdpFetcher:
    """GET JSON through the app's embedded browser (logged-in tab)."""

    def __init__(self, port: int):
        self.port = port
        self._ws = None
        self._session = None
        self._id = 0

    async def open(self):
        import aiohttp
        self._session = aiohttp.ClientSession()
        async with self._session.get(f"http://127.0.0.1:{self.port}/json") as r:
            tabs = await r.json()
        tab = next((t for t in tabs if t.get("type") == "page" and "discuss.eroscripts.com" in (t.get("url") or "")), None)
        if tab is None:
            raise RuntimeError("no EroScripts tab open in the embedded browser")
        self._ws = await self._session.ws_connect(tab["webSocketDebuggerUrl"], max_msg_size=64 * 1024 * 1024)

    async def close(self):
        if self._ws is not None:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()

    async def get_json(self, url: str):
        self._id += 1
        expr = (f"fetch({json.dumps(url)},{{credentials:'include'}})"
                ".then(async r=>r.status+'\\n'+(r.status===200?await r.text():''))")
        await self._ws.send_json({"id": self._id, "method": "Runtime.evaluate",
                                  "params": {"expression": expr, "awaitPromise": True, "returnByValue": True}})
        while True:
            m = await self._ws.receive_json()
            if m.get("id") == self._id:
                break
        val = ((m.get("result") or {}).get("result") or {}).get("value") or ""
        status, _, body = val.partition("\n")
        if status == "429":
            raise RuntimeError("rate limited (429)")
        if status != "200":
            return None
        return json.loads(body) if body else None


class CookieFetcher:
    """GET JSON with the saved cookies (app not running)."""

    def __init__(self):
        self._session = None

    async def open(self):
        import aiohttp
        self._session = aiohttp.ClientSession()

    async def close(self):
        if self._session is not None:
            await self._session.close()

    async def get_json(self, url: str):
        return await discourse._get_json(self._session, url)


async def forum_phase(works: list[tuple[Path, dict]], args, report: list[str]) -> dict:
    """Fill forum fields; returns counters."""
    counts = {"enriched": 0, "searched": 0, "found": 0, "missed": 0, "errors": 0, "skipped_miss": 0,
              "loose_found": 0, "loose_rejected": 0}
    if _port_open(args.cdp_port):
        fetcher = CdpFetcher(args.cdp_port)
        how = f"CDP :{args.cdp_port}"
    elif _port_open(args.api_port):
        report.append("- forum phase aborted: the app is running but its browser has no CDP port / "
                      "EroScripts tab; refusing to use cookies directly while the app runs")
        return counts
    else:
        fetcher = CookieFetcher()
        how = "saved cookies"
    report.append(f"- forum access: {how}")
    misses: dict[str, str] = {}
    try:
        misses = json.loads(MISSES_FILE.read_text(encoding="utf-8")) if MISSES_FILE.exists() else {}
    except (OSError, ValueError):
        misses = {}
    await fetcher.open()
    try:
        cats: dict[int, str] = {}
        site = await _guarded(fetcher, f"{FORUM}/site.json", args)
        for c in (site or {}).get("categories") or []:
            try:
                cats[int(c["id"])] = str(c.get("name") or "")
            except (KeyError, TypeError, ValueError):
                pass
        done = 0
        for work, sc in works:
            if args.limit and done >= args.limit:
                break
            src = sc.get("source") or {}
            tid = src.get("topic_id") if src.get("site") == "eroscripts" else None
            needs = not (sc.get("tags") and sc.get("category") and sc.get("posted_at") and sc.get("posted_by"))
            if tid and not needs:
                continue
            loose_author = ""
            if not tid:
                title = sc.get("title") or work.name
                is_miss = str(work) in misses
                if args.search_loose and is_miss:
                    # second pass: prefix-stripped title, author must be confirmed
                    loose_author = lib.author_from_name(title) or lib.author_from_name(work.name)
                    key = loose_key(title)
                    if not loose_author or len(key) < 4:
                        continue
                    q = strip_author_prefix(title)
                elif args.search:
                    key = QueueManager._title_key(title)
                    if len(key) < 4:
                        continue
                    if is_miss and not args.retry_misses:
                        counts["skipped_miss"] += 1
                        continue
                    q = title
                else:
                    continue
                done += 1
                counts["searched"] += 1
                from urllib.parse import quote
                res = await _guarded(fetcher, f"{FORUM}/search.json?q={quote(q)}", args)
                if res is None:
                    counts["errors"] += 1
                    continue
                hit = None
                for t in res.get("topics") or []:
                    if not isinstance(t, dict):
                        continue
                    ht = str(t.get("title") or "")
                    if loose_author:
                        if loose_key(ht) == key:
                            hit = t
                            break
                    elif QueueManager._title_key(ht) == key:
                        hit = t
                        break
                if hit is None:
                    counts["missed"] += 1
                    misses[str(work)] = q
                    if not loose_author:
                        report.append(f"- MISS `{work.name}`")
                    continue
                tid = int(hit["id"])
                if loose_author:
                    # confirm the author: named in the hit's title, or the OP
                    ht = str(hit.get("title") or "")
                    op = next((str(pp.get("username") or "") for pp in res.get("posts") or []
                               if isinstance(pp, dict) and pp.get("topic_id") == tid and pp.get("post_number") == 1), "")
                    confirmed = (loose_author.lower() in ht.lower()
                                 or (op and op.lower() == loose_author.lower()))
                    if not confirmed:
                        topic_doc = await _guarded(fetcher, f"{FORUM}/t/{tid}.json", args)
                        opname = ((topic_doc or {}).get("details") or {}).get("created_by", {}).get("username", "")
                        confirmed = bool(opname) and opname.lower() == loose_author.lower()
                    if not confirmed:
                        counts["loose_rejected"] += 1
                        report.append(f"- LOOSE-REJECT `{work.name}` ~ topic {tid} \"{ht}\" (author {loose_author} not confirmed)")
                        continue
                    counts["loose_found"] += 1
                    report.append(f"- LOOSE `{work.name}` → topic {tid} \"{ht}\"")
                    misses.pop(str(work), None)
                else:
                    counts["found"] += 1
                src = {"site": "eroscripts", "topic_id": tid,
                       "url": f"{FORUM}/t/{hit.get('slug') or 'topic'}/{tid}"}
                if args.apply:
                    lib.update_sidecar(work, {"source": src})
                sc = lib.read_sidecar(work) or sc
            else:
                done += 1
            topic = await _guarded(fetcher, f"{FORUM}/t/{tid}.json", args)
            if topic is None:
                counts["errors"] += 1
                report.append(f"- NOTOPIC `{work.name}` (topic {tid})")
                continue
            meta = discourse.topic_meta_from_json(topic, cats)
            if args.apply:
                lib.update_sidecar(work, meta, overwrite=False)
            counts["enriched"] += 1
            if counts["enriched"] % 25 == 0:
                print(f"  forum: {counts}", flush=True)
                if args.apply:
                    MISSES_FILE.write_text(json.dumps(misses, ensure_ascii=False, indent=1), encoding="utf-8")
    finally:
        await fetcher.close()
        if args.apply:
            MISSES_FILE.write_text(json.dumps(misses, ensure_ascii=False, indent=1), encoding="utf-8")
    return counts


async def _guarded(fetcher, url: str, args):
    """One request with the polite delay; a 429 waits a minute and retries once."""
    for attempt in range(2):
        try:
            await asyncio.sleep(args.delay)
            return await fetcher.get_json(url)
        except RuntimeError as e:
            if "429" in str(e) and attempt == 0:
                print("  rate limited — sleeping 60s", flush=True)
                await asyncio.sleep(60)
                continue
            print(f"  {url}: {e}", flush=True)
            return None
        except Exception as e:
            print(f"  {url}: {e}", flush=True)
            return None
    return None


# ── main ─────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--roots", nargs="*", help="library roots (default: download_dir + library_paths)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--forum", action="store_true", help="fetch topic JSON for works with a topic id")
    ap.add_argument("--search", action="store_true", help="search the forum for works without a topic")
    ap.add_argument("--retry-misses", action="store_true")
    ap.add_argument("--search-loose", action="store_true",
                    help="second pass over recorded misses: prefix-stripped title + author confirmation")
    ap.add_argument("--local-tags", action="store_true",
                    help="add source-<site> / pack-<bundle> / len-* tags derived offline")
    ap.add_argument("--limit", type=int, default=0, help="max forum lookups this run")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds between forum requests")
    ap.add_argument("--only", default="", help="only work folders whose name contains this")
    ap.add_argument("--cdp-port", type=int, default=9223)
    ap.add_argument("--api-port", type=int, default=9172)
    ap.add_argument("--report", default=str(ROOT / f"_backfill_sidecars_{STAMP}.md"))
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    roots = [Path(r) for r in args.roots] if args.roots else lib.library_roots()
    pairs = load_pairs()
    by_folder, by_title = index_pairs(pairs)
    topic_by_pair = load_topic_index()
    log_topics = load_log_topics()
    split_parents = load_log_split_children()
    sent_from = load_log_sent_from_topic()
    report = [f"# funlib.json backfill {STAMP} ({'APPLY' if args.apply else 'DRY RUN'})",
              f"roots: {', '.join(str(r) for r in roots)}; pairs known: {len(pairs)}; "
              f"topic index links: {len(topic_by_pair)}; log topic slugs: {len(log_topics)}; "
              f"split children: {len(split_parents)}; sent-from-topic names: {len(sent_from)}", ""]
    counts = {"works": 0, "written": 0, "unchanged": 0, "with_pair": 0, "with_topic": 0, "no_pair": 0,
              "local_tagged": 0}
    works: list[tuple[Path, dict]] = []
    for root in roots:
        for work in lib.iter_work_dirs(root):
            if args.only and args.only.lower() not in work.name.lower():
                continue
            counts["works"] += 1
            data = offline_sidecar(work, by_folder, by_title, topic_by_pair, log_topics,
                                   split_parents, sent_from)
            if data.get("pair_id"):
                counts["with_pair"] += 1
            else:
                counts["no_pair"] += 1
            if (data.get("source") or {}).get("topic_id"):
                counts["with_topic"] += 1
            original = lib.read_sidecar(work)
            existing = repair_op_author(original, data.get("title") or work.name, work.name)
            merged = lib.merge_sidecar(existing, data)
            if args.local_tags:
                pair = find_pair(work, by_folder, by_title)
                pname = split_parents.get(data.get("title") or "") or split_parents.get(work.name) or ""
                extra = local_tags(work, merged, pair, pname)
                if extra:
                    merged["tags"] = list(merged.get("tags") or []) + extra
                    counts["local_tagged"] += 1
            if merged != (original or {}):
                counts["written"] += 1
                if args.apply:
                    lib.write_sidecar(work, merged)
            else:
                counts["unchanged"] += 1
            works.append((work, merged))
    report.append(f"offline: {counts}")
    print(f"offline: {counts}", flush=True)
    if args.forum or args.search or args.search_loose:
        fc = asyncio.run(forum_phase(works, args, report))
        report.append(f"forum: {fc}")
        print(f"forum: {fc}", flush=True)
    Path(args.report).write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"report: {args.report}")


if __name__ == "__main__":
    main()
