"""Library layout helpers shared with FunLib: flat variant naming, sidecar
merge, variants inferred from disk, and FunLib's recycle-bin log."""
import json

from funpairdl.core import library as lib
from funpairdl.core.queue_manager import QueueManager
from funpairdl.persistence.topic_index import TopicIndex
from funpairdl.utils.discourse import normalize_tag, topic_meta_from_json


# ── naming ──

def test_sanitize_label_drops_brackets_and_path_chars():
    assert lib.sanitize_label("(Soft) [v2]") == "Soft v2"
    assert lib.sanitize_label("a/b\\c:d") == "a b c d"
    assert lib.sanitize_label("   ") == "Alt"
    assert lib.sanitize_label("巨乳(异域风情)") == "巨乳 异域风情"


def test_unique_label_numbers_from_two():
    assert lib.unique_label("Soft", set()) == "Soft"
    assert lib.unique_label("Soft", {"soft"}) == "Soft 2"
    assert lib.unique_label("Soft", {"Soft", "Soft 2"}) == "Soft 3"


def test_script_name_and_parse_roundtrip():
    assert lib.script_name("Work", "", "") == "Work.funscript"
    assert lib.script_name("Work", "", "pitch") == "Work.pitch.funscript"
    assert lib.script_name("Work", "Soft", "") == "Work (Soft).funscript"
    assert lib.script_name("Work", "Soft", "suckManual") == "Work (Soft).suckManual.funscript"
    assert lib.parse_script_name("Work.funscript", "Work") == ("", "")
    assert lib.parse_script_name("Work.pitch.funscript", "Work") == ("", "pitch")
    assert lib.parse_script_name("Work (Soft).funscript", "Work") == ("Soft", "")
    assert lib.parse_script_name("Work (Soft 2).roll.funscript", "Work") == ("Soft 2", "roll")
    assert lib.parse_script_name("Work Two.funscript", "Work") is None
    assert lib.parse_script_name("Other.funscript", "Work") is None
    assert lib.parse_script_name("Work.mp4", "Work") is None


def test_alt_dir_label():
    assert lib.alt_dir_label("Work.alt", "Work") == "Alt"
    assert lib.alt_dir_label("Work.alt1", "Work") == "Alt 1"
    assert lib.alt_dir_label("Work.alt12", "Work") == "Alt 12"
    assert lib.alt_dir_label("Nicole x Alice.alt", "[Keke] Nicole x Alice (+Part2)") == "Nicole x Alice"
    assert lib.alt_dir_label("巨乳(异域风情).alt", "[玲玉]巨乳") == "巨乳 异域风情"


def test_author_from_name_matches_funlib_rule():
    assert lib.author_from_name("(Theobrobine) Genshin - Lisa") == "Theobrobine"
    assert lib.author_from_name("[Wutboi] Anran [Multi-Axis]") == "Wutboi"
    assert lib.author_from_name("(CS-FREE-0118)(Crisisbeat)Wednesday") == "Crisisbeat"
    assert lib.author_from_name("(Multi-axis) Title") == ""
    assert lib.author_from_name("Plain Title") == ""


def test_topic_id_and_site():
    assert lib.topic_id_from_url("https://discuss.eroscripts.com/t/some-slug/12345") == 12345
    assert lib.topic_id_from_url("https://discuss.eroscripts.com/t/12345") == 12345
    assert lib.topic_id_from_url("https://discuss.eroscripts.com/t/slug/12345/7?u=x") == 12345
    assert lib.topic_id_from_url("https://e621.net/posts/1") is None
    assert lib.source_site("https://discuss.eroscripts.com/t/x/1") == "eroscripts"
    assert lib.source_site("https://e621.net/posts/1") == "e621"
    assert lib.source_site("") == ""


# ── roots / trash ──

def test_iter_work_dirs_skips_trash_and_enters_no_video(tmp_path):
    (tmp_path / "Work A").mkdir()
    (tmp_path / "_trash" / "20260918-100000" / "Work B").mkdir(parents=True)
    (tmp_path / "_dup_quarantine_20260916" / "Work C").mkdir(parents=True)
    (tmp_path / "No Video" / "Work D").mkdir(parents=True)
    (tmp_path / "loose.mp4").write_bytes(b"x")
    (tmp_path / ".claude").mkdir()
    names = sorted(d.name for d in lib.iter_work_dirs(tmp_path))
    assert names == ["Work A", "Work D"]
    (tmp_path / "Pack" / "Sub").mkdir(parents=True)
    (tmp_path / "Work A" / "Work A.funscript").write_bytes(b"x")
    assert lib.has_media(tmp_path / "Work A") and not lib.has_media(tmp_path / "Pack")
    assert lib.in_trash(tmp_path / "_trash" / "x" / "Work B")
    assert not lib.in_trash(tmp_path / "Work A")


def test_reconcile_never_finds_a_trashed_copy(tmp_path, monkeypatch):
    import funpairdl.persistence.settings as settings_mod
    from funpairdl.core.pair import FileType, Pair, PairItem
    s = settings_mod.Settings(reconcile_on_redownload=True)
    monkeypatch.setattr(settings_mod.Settings, "load", lambda *a, **k: s)
    qm = QueueManager(download_dir=tmp_path)
    binned = tmp_path / "_trash" / "20260918-100000" / "Work"
    binned.mkdir(parents=True)
    (binned / "Work.mp4").write_bytes(b"VIDEO")
    (binned / "Work.funscript").write_text("OLD")
    temp = tmp_path / "Work__dl"
    temp.mkdir()
    (temp / "Work.mp4").write_bytes(b"VIDEO")
    (temp / "Work.funscript").write_text("NEW")
    pair = Pair(name="Work", items=[
        PairItem(url="u/v", filename="Work.mp4", file_type=FileType.VIDEO),
        PairItem(url="u/s", filename="Work.funscript", file_type=FileType.FUNSCRIPT)])
    pair.output_dir = str(temp)
    assert qm._reconcile_with_library(pair) is False
    assert (temp / "Work.funscript").read_text() == "NEW"
    assert (binned / "Work.funscript").read_text() == "OLD"


# ── variants from disk / sidecar ──

def test_scan_variants_flat_and_legacy(tmp_path):
    w = tmp_path / "Work"
    (w / "Work.alt").mkdir(parents=True)
    for n in ("Work.mp4", "Work.funscript", "Work.pitch.funscript", "Work (Soft).funscript",
              "Work (Soft).roll.funscript", "Work (Hard 2).funscript", "Work (Hard 2).mkv", "Other.funscript",
              "Work.alt/Work.alt.funscript", "Work.alt/Work.alt.surge.funscript", "Work.alt/Work.alt.mp4"):
        (w / n).write_bytes(b"x")
    v = lib.scan_variants(w, overrides={"Soft": {"inherit_axes": False}, "Main": {"author": "A"}})
    assert v[0] == {"label": "Main", "primary": True, "video": "Work.mp4", "author": "A",
                    "files": {"L0": "Work.funscript", "pitch": "Work.pitch.funscript"}}
    by = {x["label"]: x for x in v}
    assert by["Soft"] == {"label": "Soft", "inherit_axes": False,
                          "files": {"L0": "Work (Soft).funscript", "roll": "Work (Soft).roll.funscript"}}
    assert by["Hard 2"] == {"label": "Hard 2", "video": "Work (Hard 2).mkv",
                            "files": {"L0": "Work (Hard 2).funscript"}}
    assert by["Alt"]["files"] == {"L0": "Work.alt/Work.alt.funscript", "surge": "Work.alt/Work.alt.surge.funscript"}
    assert by["Alt"]["video"] == "Work.alt/Work.alt.mp4"
    assert lib.existing_labels(w) == {"Soft", "Hard 2", "Alt"}


def test_merge_sidecar_fills_only_blanks_and_replaces_variants():
    old = {"version": 1, "title": "T", "author": "A", "tags": [], "source": {"url": "u1"},
           "variants": [{"label": "Main", "primary": True, "files": {"L0": "old"}},
                        {"label": "Soft", "inherit_axes": False, "files": {"L0": "s"}}]}
    new = {"title": "T2", "author": "", "tags": ["a"], "source": {"url": "u2", "topic_id": 5},
           "posted_at": "2026-01-01T00:00:00Z",
           "variants": [{"label": "Main", "primary": True, "files": {"L0": "new"}},
                        {"label": "Soft", "files": {"L0": "s"}}]}
    m = lib.merge_sidecar(old, new)
    assert m["title"] == "T" and m["author"] == "A" and m["tags"] == ["a"]
    # tags are unioned, never replaced: local tags survive a forum fill and vice versa
    u = lib.merge_sidecar({"tags": ["len-2-5", "HMV"]}, {"tags": ["hmv", "riding"]})
    assert u["tags"] == ["len-2-5", "HMV", "riding"]
    assert m["source"] == {"url": "u1", "topic_id": 5}
    assert m["posted_at"] == "2026-01-01T00:00:00Z"
    assert m["variants"][0]["files"] == {"L0": "new"}
    assert m["variants"][1]["inherit_axes"] is False          # kept from the old entry
    m2 = lib.merge_sidecar(old, new, overwrite=True)
    assert m2["title"] == "T2" and m2["author"] == "A"       # empty never overwrites


def test_write_and_read_sidecar_utf8_no_bom(tmp_path):
    p = lib.write_sidecar(tmp_path, {"title": "巨乳"})
    raw = p.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert lib.read_sidecar(tmp_path) == {"title": "巨乳", "version": 2}
    (tmp_path / "funlib.json").write_text("not json", encoding="utf-8")
    assert lib.read_sidecar(tmp_path) is None


# ── deleted.jsonl ──

def _bin(root, lines):
    d = root / "_trash"
    d.mkdir(parents=True, exist_ok=True)
    (d / "deleted.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")


def test_deleted_log_last_state_wins(tmp_path):
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    r1.mkdir(); r2.mkdir()
    _bin(r1, [
        {"id": "a", "ts": "1", "action": "trash", "scope": "work", "work": "Work A",
         "paths": ["Work A/Work A.mp4", "Work A/Work A.funscript"], "pair_id": "p1", "source_url": "https://discuss.eroscripts.com/t/x/11"},
        {"id": "b", "ts": "2", "action": "trash", "scope": "work", "work": "Work B", "paths": ["Work B/Work B.mp4"]},
        {"id": "c", "ts": "3", "action": "trash", "scope": "variant", "work": "Work C", "label": "Soft",
         "paths": ["Work C/Work C (Soft).funscript"], "pair_id": "p3"},
        {"id": "x", "ts": "4", "action": "restore", "ref": "b", "paths": ["Work B/Work B.mp4"]},
        {"id": "d", "ts": "5", "action": "trash", "scope": "work", "paths": ["No Video/Work D/Work D.funscript"]},
        {"id": "y", "ts": "6", "action": "purge", "ref": "d"},
        "garbage",
    ])
    _bin(r2, [{"id": "e", "ts": "7", "action": "trash", "scope": "work", "paths": ["Work E/Work E.mp4"], "pair_id": "p5"}])
    log = lib.DeletedLog(lambda: [r1, r2])
    idx = log.index(QueueManager._title_key)
    assert idx["work"]["pair_ids"] == {"p1", "p5"}
    assert idx["work"]["topic_ids"] == {"11"}
    assert idx["work"]["folders"] == {"work a", "work e"}
    assert idx["variant"]["pair_ids"] == {"p3"}
    assert idx["variant"]["folders"] == {"work c"}
    # re-read when the file changes
    _bin(r1, [{"id": "a", "ts": "1", "action": "trash", "scope": "work", "paths": ["Work A/x"]},
              {"id": "z", "ts": "9", "action": "restore", "ref": "a"}])
    log._checked = 0
    assert log.index(QueueManager._title_key)["work"]["folders"] == {"work e"}


def test_topic_status_shows_deleted_for_binned_work(tmp_path):
    root = tmp_path / "lib"
    root.mkdir()
    _bin(root, [
        {"id": "a", "action": "trash", "scope": "work", "paths": ["Work A/Work A.mp4"], "pair_id": "pa"},
        {"id": "b", "action": "trash", "scope": "work", "paths": ["Work B/Work B.mp4"]},
        {"id": "c", "action": "trash", "scope": "work", "paths": ["Work C/Work C.mp4"],
         "source_url": "https://discuss.eroscripts.com/t/work-c/303"},
        {"id": "v", "action": "trash", "scope": "variant", "paths": ["Work V/Work V (Soft).funscript"], "pair_id": "pv"},
    ])
    arch = tmp_path / "queue_archive.jsonl"
    arch.write_text("".join(json.dumps(d) + "\n" for d in [
        {"id": "pa", "name": "Work A", "state": "completed"},
        {"id": "pb", "name": "Work B", "state": "completed"},
        {"id": "pc", "name": "Work C", "state": "completed"},
        {"id": "pv", "name": "Work V", "state": "completed"},
        {"id": "pk", "name": "Work Kept", "state": "completed"},
    ]), encoding="utf-8")
    idx = TopicIndex(tmp_path / "topic_index.json", arch, deleted_log=lib.DeletedLog(lambda: [root]))
    idx.record_pair("101", "", "Work A", "pa", "Work A")     # by pair id
    idx.record_pair("303", "", "Work C", "pc", "Work C")     # by topic id
    idx.record_pair("505", "", "Work V", "pv", "Work V")     # only a variant binned
    idx.record_pair("707", "", "Work Kept", "pk", "Work Kept")
    st = idx.status([{"id": "101", "title": "Work A"}, {"id": "202", "title": "Work B"},
                     {"id": "303", "title": "Work C"}, {"id": "505", "title": "Work V"},
                     {"id": "707", "title": "Work Kept"}], [], QueueManager._title_key)
    assert st["101"]["state"] == "deleted" and st["101"]["deleted"] is True
    assert st["202"]["state"] == "deleted" and st["202"]["by_title"] is True   # folder name only
    assert st["303"]["state"] == "deleted"
    assert st["505"]["state"] == "completed"
    assert st["707"]["state"] == "completed" and st["707"]["deleted"] is False


def test_topic_status_live_redownload_outranks_deleted(tmp_path):
    from funpairdl.core.pair import Pair, PairState
    root = tmp_path / "lib"
    root.mkdir()
    _bin(root, [{"id": "a", "action": "trash", "scope": "work", "paths": ["Work A/Work A.mp4"], "pair_id": "pa"}])
    arch = tmp_path / "queue_archive.jsonl"
    arch.write_text(json.dumps({"id": "pa", "name": "Work A", "state": "completed"}) + "\n", encoding="utf-8")
    idx = TopicIndex(tmp_path / "topic_index.json", arch, deleted_log=lib.DeletedLog(lambda: [root]))
    idx.record_pair("101", "", "Work A", "pa", "Work A")
    again = Pair(name="Work A")
    again.state = PairState.QUEUED
    idx.record_pair("101", "", "Work A", again.id, again.name)
    st = idx.status([{"id": "101", "title": "Work A"}], [again], QueueManager._title_key)
    assert st["101"]["state"] == "queued"


# ── discourse ──

def test_topic_meta_from_json():
    topic = {"id": 327994, "slug": "fap-hero-demo", "title": "Fap Hero Demo [Quantoz]",
             "tags": [{"name": "Fap Hero", "slug": "fap-hero"}, {"name": "HMV"}, "Multi Axis"],
             "category_id": 14, "created_at": "2026-07-30T01:14:38.186Z",
             "details": {"created_by": {"username": "opname"}},
             "post_stream": {"posts": [{"post_number": 1, "username": "opname"}]}}
    m = topic_meta_from_json(topic, {14: "Free Scripts"})
    assert m["source"] == {"site": "eroscripts", "topic_id": 327994,
                           "url": "https://discuss.eroscripts.com/t/fap-hero-demo/327994"}
    assert m["tags"] == ["fap-hero", "hmv", "multi-axis"]
    assert m["category"] == {"id": 14, "name": "Free Scripts"}
    assert m["posted_at"] == "2026-07-30T01:14:38.186Z"
    assert "author" not in m
    assert m["posted_by"] == "opname" and m["posted_by_url"].endswith("/u/opname")
    assert normalize_tag("Len 25 60") == "len-25-60"
