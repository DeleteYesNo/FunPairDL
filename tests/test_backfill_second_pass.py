"""Second pass of tools/backfill_funlib_sidecars.py: split children inherit
the parent's topic, sent-from-topic log correlation, loose keys and the
offline (local) tags."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("backfill_funlib_sidecars",
                                               ROOT / "tools" / "backfill_funlib_sidecars.py")
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)

L = "[INFO] funpairdl.queue_manager: "
B = "[INFO] funpairdl.gui.browser: "


def test_split_children_map_to_parent(tmp_path):
    log = tmp_path / "funpairdl.log"
    log.write_text(
        f"2026-05-22 17:12:57 {L}Auto-split: created pair '[a] One' (2 items)\n"
        f"2026-05-22 17:12:57 {L}Auto-split: created pair '[b] Two' (2 items)\n"
        f"2026-05-22 17:12:57 {L}Auto-split: original pair 'Pack Title' split into 2 pairs\n"
        f"2026-05-23 04:44:49 {L}Auto-split: created pair 'Solo' (2 items)\n"
        f"2026-05-23 04:44:49 {L}Auto-split: original pair 'Other Pack' split into 1 pairs\n",
        encoding="utf-8")
    assert bf.load_log_split_children(log) == {"[a] One": "Pack Title", "[b] Two": "Pack Title", "Solo": "Other Pack"}


def test_sent_from_topic_requires_one_topic_and_name_overlap(tmp_path):
    log = tmp_path / "funpairdl.log"
    log.write_text(
        f"2026-07-17 00:09:40 {B}Page loaded in 4.0s (foreground, ok=True): https://discuss.eroscripts.com/t/casey-sample-demo-load/111\n"
        f"2026-07-17 00:10:10 {L}Added pair: (Casey Sample) Demo Load (2 items)\n"
        # two topics in the window → ambiguous, skipped
        f"2026-07-17 00:20:00 {B}Page loaded in 4.0s (foreground, ok=True): https://discuss.eroscripts.com/t/alpha-beta/222\n"
        f"2026-07-17 00:20:20 {B}Page loaded in 4.0s (foreground, ok=True): https://discuss.eroscripts.com/t/gamma-delta/333\n"
        f"2026-07-17 00:20:40 {L}Added pair: Alpha Beta (2 items)\n"
        # slug does not overlap the name → skipped
        f"2026-07-17 00:30:00 {B}Page loaded in 4.0s (foreground, ok=True): https://discuss.eroscripts.com/t/something-else-entirely/444\n"
        f"2026-07-17 00:30:30 {L}Added pair: Unrelated Work Name (2 items)\n"
        # background loads do not count
        f"2026-07-17 00:40:00 {B}Page loaded in 4.0s (background, ok=True): https://discuss.eroscripts.com/t/quiet-work/555\n"
        f"2026-07-17 00:40:30 {L}Added pair: Quiet Work (2 items)\n"
        # too old
        f"2026-07-17 00:50:00 {B}Page loaded in 4.0s (foreground, ok=True): https://discuss.eroscripts.com/t/late-work/666\n"
        f"2026-07-17 00:52:00 {L}Added pair: Late Work (2 items)\n",
        encoding="utf-8")
    m = bf.load_log_sent_from_topic(log)
    k = bf.QueueManager._title_key
    assert m == {k("(Casey Sample) Demo Load"): "111"}


def test_child_inherits_parent_topic(tmp_path):
    root = tmp_path / "lib"
    child = root / "[a] One"
    child.mkdir(parents=True)
    (child / "[a] One.mp4").write_bytes(b"V")
    (child / "[a] One.funscript").write_bytes(b"S")
    pairs = [
        {"id": "parent", "name": "Pack Title", "state": "completed", "created_at": "2026-05-22T17:12:00",
         "output_dir": str(root / "Pack Title"), "source_url": "https://discuss.eroscripts.com/t/pack-title/999"},
        {"id": "kid", "name": "[a] One", "state": "completed", "created_at": "2026-05-22T17:12:57",
         "output_dir": str(child)},
    ]
    by_folder, by_title = bf.index_pairs(pairs)
    d = bf.offline_sidecar(child, by_folder, by_title, {}, {}, {"[a] One": "Pack Title"}, {})
    assert d["pair_id"] == "kid"
    assert d["source"] == {"site": "eroscripts", "url": "https://discuss.eroscripts.com/t/999", "topic_id": 999}
    # sent-from-topic wins over nothing, but an explicit source_url still wins over both
    d2 = bf.offline_sidecar(child, by_folder, by_title, {}, {}, {}, {bf.QueueManager._title_key("[a] One"): "123"})
    assert d2["source"]["topic_id"] == 123


def test_loose_key_and_prefix_strip():
    assert bf.strip_author_prefix("(Casey Sample) Demo Load [HQ]") == "Demo Load [HQ]"
    assert bf.strip_author_prefix("[CS-1](Casey) Demo") == "Demo"
    assert bf.loose_key("(Casey Sample) Demo Load (Requested, HQ Script)") == bf.loose_key("Demo Load")
    assert bf.loose_key("Demo Load [Casey Sample]") != bf.loose_key("Demo Load")   # trailing author stays


def test_local_tags(tmp_path):
    w = tmp_path / "Work"
    w.mkdir()
    actions = [{"at": 0, "pos": 0}, {"at": 7 * 60 * 1000, "pos": 100}]          # 7 minutes
    (w / "Work.funscript").write_text(json.dumps({"actions": actions}), encoding="utf-8")
    sc = {"tags": ["hmv"], "variants": [{"label": "Main", "primary": True, "files": {"L0": "Work.funscript"}}]}
    pair = {"items": [{"url": "https://e621.net/posts/1"}, {"url": "https://discuss.eroscripts.com/uploads/x"}]}
    assert bf.local_tags(w, sc, pair, "Casey Pack (Total 6m)") == ["source-e621", "pack-casey-pack-total-6m", "len-5-10"]
    # never duplicates, never adds a second len-/source- tag
    sc2 = {"tags": ["len-25-60", "source-iwara", "pack-x"], "variants": sc["variants"]}
    assert bf.local_tags(w, sc2, pair, "Casey Pack") == []
    assert bf.len_tag(30) == "len-0-2" and bf.len_tag(3600) == "len-60-plus" and bf.len_tag(None) == ""
    assert bf.pack_tag("(Author) Example Series Mockie 5 Packs! (Total 6m 13s)") == "pack-example-series-mockie-5-packs-total-6m"


def test_bundle_keys_from_item_urls():
    pair = {"items": [
        {"url": "https://mega.nz/folder/AbCdEf12#keykeykey/file/xyz"},
        {"url": "https://pixeldrain.com/api/filesystem/Root1234/All%20Demo%20Set/x.mp4"},
        {"url": "https://pixeldrain.com/l/List5678"},
        {"url": "https://pixeldrain.com/u/File9999"},          # a lone file is not a bundle
        {"url": "https://discuss.eroscripts.com/uploads/short-url/abc.funscript"},
    ]}
    assert bf.bundle_keys(pair) == {"AbCdEf12", "Root1234", "List5678"}
    assert bf.bundle_keys(None) == set()


def test_core_title_and_fuzzy_overlap():
    core, short = bf.core_title("(Tail Blazer) Legend of Mockda - Mommy Mockda's HJ")
    assert core == "Legend of Mockda - Mommy Mockda's HJ" and short == "Mommy Mockda's HJ"
    hcore, hshort = bf.core_title("(Tail blazer) Mommy Mockda's HJ (Requested, HQ script)")
    assert hcore == "Mommy Mockda's HJ" and hshort == "Mommy Mockda's HJ"
    assert bf.fuzzy_overlap(short, hshort) == 1.0
    assert bf.fuzzy_overlap("QRS Other PMV", "QRS - Other PMV") == 1.0
    assert bf.fuzzy_overlap("Mockenia HJ", "Mockenia BJ") == 0.5
    assert bf.fuzzy_overlap("HMV", "Bekscript 500 post party HMV") == 0.0     # one word proves nothing
    assert bf.fuzzy_overlap("Old Harbor BlobCG HMV (A Loop) Simple", "Old Harbor - BlobCG HMV (Suggested)") == 1.0
    assert bf.core_title("Hololive - Kurayami Mock HJ") == ("Hololive - Kurayami Mock HJ", "Kurayami Mock HJ")


def test_name_tags():
    assert bf.name_tags("bready-mock-goddess-of-samples-uncensored_1080p") == ["source-rule34video"]
    assert bf.name_tags("x9-loop2_1080p60FPS") == ["source-rule34video"]
    assert bf.name_tags("QWXYZ VAM HMV 4k") == ["hmv", "3d"]
    assert bf.name_tags("PixelFH-FH-Stamina-Stage-2") == ["fap-hero"]
    assert bf.name_tags("NoodleDude - Ultimate Clip PMV") == ["pmv"]
    assert bf.name_tags("(Author) Some Title") == []
    assert bf.name_tags("Shmv thing") == []          # not a whole word


def test_local_tags_len_from_any_script_when_names_do_not_match(tmp_path):
    w = tmp_path / "Pack Name"
    w.mkdir()
    actions = [{"at": 0, "pos": 0}, {"at": 3 * 60 * 1000, "pos": 100}]
    (w / "Other Title 1080P.funscript").write_text(json.dumps({"actions": actions}), encoding="utf-8")
    assert bf.local_tags(w, {"tags": [], "variants": []}, None, "") == ["len-2-5"]
