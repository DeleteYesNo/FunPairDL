"""Library de-duplication: same video in two work folders, or copied (not
hardlinked) inside one.

Input is the verified duplicate scan (groups with full sha256 of every video
and every funscript in the folders involved). Every decision below is
hash-based — nothing is "the same" by name.

Per group of folders sharing a byte-identical video:
  * a folder whose every file (video + scripts, recursively) exists by hash
    in the other one is redundant -> moved to <root>/_dup_quarantine_<stamp>/
  * both hold scripts the other lacks (same video, different scripts) ->
    the loser's scripts become .alt variants of the keeper (video
    hardlinked in, .linkinfo appended), then the loser is redundant
  * folders that also hold OTHER videos the keeper lacks (a collection) are
    left alone and reported
Inside one folder, copies of the same video become hardlinks of the
top-level one.

Keeper preference: "(Author) Work" title > plain title > URL slug; then the
main library root (first --roots entry); then the folder holding more
files; then the longer name.

    python tools/dedupe_library.py --verified dups_verified.json            # dry run
    python tools/dedupe_library.py --verified dups_verified.json --apply
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from funpairdl.core.library import (  # noqa: E402
    existing_labels, in_trash, is_meta_dir, sanitize_label, script_name, unique_label, update_sidecar,
)
from funpairdl.core.queue_manager import QueueManager  # noqa: E402

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".wmv", ".ts", ".flv"}
STAMP = date.today().isoformat().replace("-", "")


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class HashCache:
    def __init__(self, path: Path):
        self.path = path
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def get(self, p: Path) -> str:
        st = p.stat()
        key = f"{p}|{st.st_size}|{int(st.st_mtime)}"
        if key not in self.data:
            self.data[key] = sha256(p)
        return self.data[key]

    def save(self):
        self.path.write_text(json.dumps(self.data), encoding="utf-8")


def inventory(folder: Path, cache: HashCache) -> dict[Path, str]:
    """{relative path: sha256} for every video/funscript under folder."""
    out = {}
    for dp, dn, fn in os.walk(folder):
        dn[:] = [d for d in dn if not is_meta_dir(d)]
        for f in fn:
            p = Path(dp) / f
            if p.suffix.lower() in VIDEO_EXTS or f.lower().endswith(".funscript"):
                out[p.relative_to(folder)] = cache.get(p)
    return out


def name_quality(name: str) -> int:
    """0 for a URL slug / opaque id / mis-named folder, 1 for a plain title,
    2 for the library's canonical "(Author) Work" / "[Author] Work" form."""
    if name.lower().endswith(".funscript"):
        return 0
    if " " not in name and (("-" in name or "_" in name) or re.fullmatch(r"[A-Za-z0-9]{4,12}", name)):
        return 0
    if re.match(r"^[\(\[（【][^\)\]）】]{2,40}[\)\]）】]\s*\S", name):
        return 2
    return 1


def keeper_rank(root_index: int, name: str, nfiles: int) -> tuple:
    # a real title beats a slug; then the main library root; then the folder
    # holding more files; then the more descriptive (longer) name
    return (name_quality(name), -root_index, nfiles, len(name), name)


def next_alt_slot(folder: Path, base: str) -> str:
    used = set()
    for sub in folder.iterdir():
        if sub.is_dir():
            m = re.search(r"\.alt(\d*)$", sub.name)
            if m:
                used.add(m.group(1))
    if "" not in used:
        return "alt"
    n = 1
    while str(n) in used:
        n += 1
    return f"alt{n}"


def append_linkinfo(folder: Path, pairs: list[tuple[Path, Path]], apply: bool):
    if not pairs or not apply:
        return
    p = folder / ".linkinfo"
    old = p.read_text(encoding="utf-8").rstrip("\n") if p.exists() else ""
    blocks = [f"[hardlink]\noriginal={o}\nlinked={l}\ncreated={date.today().isoformat()}" for o, l in pairs]
    p.write_text((old + "\n\n" if old else "") + "\n\n".join(blocks) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verified", required=True, help="dups_verified.json from the scan")
    ap.add_argument("--roots", nargs="+", default=[r"F:\Funscript_DATA", r"H:\Funscript_Data"])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--busy", default="", help="JSON list of folders a running pair still writes (skipped)")
    ap.add_argument("--report", default=f"_dedupe_report_{STAMP}.md")
    args = ap.parse_args()

    roots = [Path(r) for r in args.roots]
    groups = json.loads(Path(args.verified).read_text(encoding="utf-8"))
    cache = HashCache(Path(args.verified).with_name("hash_cache.json"))
    busy = {Path(b).resolve() for b in json.loads(args.busy)} if args.busy else set()

    def work_dir(p: str) -> Path | None:
        if in_trash(Path(p)):
            return None          # FunLib's recycle bin does not exist for us
        for r in roots:
            try:
                rel = Path(p).relative_to(r)
            except ValueError:
                continue
            if is_meta_dir(rel.parts[0]):
                return None
            return r / rel.parts[0]
        return None

    def root_index(folder: Path) -> int:
        for i, r in enumerate(roots):
            if folder.parent == r:
                return i
        return 99

    lines = [f"# Library de-dup {STAMP} ({'APPLY' if args.apply else 'DRY RUN'})\n"]
    freed = 0
    actions = {"quarantine": 0, "alt_merge": 0, "hardlink": 0, "skipped": 0}
    handled: set[Path] = set()
    keepers: set[Path] = set()

    # ── cross-folder groups ──
    for g in groups:
        if g["kind"] != "cross" or not g.get("video_identical"):
            continue
        folders = sorted({work_dir(p) for p in g["paths"]} - {None})
        folders = [f for f in folders if f.exists() and f not in handled]
        if len(folders) < 2:
            continue
        if any(f.resolve() in busy for f in folders):
            lines.append(f"- SKIP (busy): {[f.name for f in folders]}")
            actions["skipped"] += 1
            continue
        inv = {f: inventory(f, cache) for f in folders}
        hashes = {f: set(v.values()) for f, v in inv.items()}

        # keeper = the folder covering the most, by preference
        keeper = max(folders, key=lambda f: keeper_rank(root_index(f), f.name, len(hashes[f])))
        if any(f in keepers for f in folders if f is not keeper):
            lines.append(f"- SKIP (folder already kept in another group): {[f.name for f in folders]}")
            actions["skipped"] += 1
            continue
        keepers.add(keeper)
        base_video = next((f for f in sorted(inv[keeper]) if len(f.parts) == 1
                           and f.suffix.lower() in VIDEO_EXTS and f.stem == keeper.name), None)
        if base_video is None:
            base_video = next((f for f in sorted(inv[keeper]) if len(f.parts) == 1
                               and f.suffix.lower() in VIDEO_EXTS), None)
        for loser in folders:
            if loser is keeper:
                continue
            uncovered = {rel: h for rel, h in inv[loser].items() if h not in hashes[keeper]}
            unc_videos = [r for r in uncovered if r.suffix.lower() in VIDEO_EXTS]
            if unc_videos:
                lines.append(f"- SKIP (loser has other videos): keep `{keeper.name}` vs `{loser.name}` "
                             f"— {len(unc_videos)} video(s) only there")
                actions["skipped"] += 1
                continue
            links: list[tuple[Path, Path]] = []
            # The keeper video this loser's scripts belong to: the one that is
            # byte-identical to the loser's own top-level video (a keeper may
            # hold several videos), else the keeper's main video.
            loser_vid_hash = next((h for rel, h in sorted(inv[loser].items())
                                   if len(rel.parts) == 1 and rel.suffix.lower() in VIDEO_EXTS), None)
            alt_src = next((rel for rel, h in sorted(inv[keeper].items())
                            if h == loser_vid_hash and rel.suffix.lower() in VIDEO_EXTS), base_video)
            if uncovered:
                # same video, scripts the keeper lacks -> "(Label)" variants
                # laid flat next to the keeper's set (docs/library-layout.md);
                # the video is shared, nothing is linked
                if alt_src is None:
                    lines.append(f"- SKIP (keeper has no top-level video): `{keeper.name}`")
                    actions["skipped"] += 1
                    continue
                keeper_base = alt_src.stem if len(alt_src.parts) == 1 else keeper.name
                used = existing_labels(keeper, keeper_base)
                by_dir: dict[Path, list[Path]] = {}
                for rel in sorted(uncovered):
                    by_dir.setdefault(rel.parent, []).append(rel)
                for _d, rels in by_dir.items():
                    l0 = [r for r in rels if QueueManager._parse_axis(r.name)[0] == "L0"]
                    axes = [r for r in rels if QueueManager._parse_axis(r.name)[0] != "L0"]
                    slots = l0 or [None]
                    for main_rel in slots:
                        label = unique_label(sanitize_label(loser.name, "Alt"), used)
                        used.add(label)
                        moves = []
                        if main_rel is not None:
                            moves.append((loser / main_rel, keeper / script_name(keeper_base, label, "")))
                        for ax in axes:
                            suffix = QueueManager._parse_axis(ax.name)[1]
                            moves.append((loser / ax, keeper / script_name(keeper_base, label, suffix)))
                        lines.append(f"- VARIANT: `{loser.name}` -> `{keeper.name}` as ({label}) "
                                     f"({len(moves)} script(s))")
                        if args.apply:
                            for src, dst in moves:
                                if dst.exists():
                                    dst = dst.with_name(dst.stem + "-2" + dst.suffix)
                                shutil.move(str(src), str(dst))
                        actions["alt_merge"] += 1
                        axes = []   # axes go with the first slot only
                if args.apply:
                    from funpairdl.core.library import scan_variants
                    update_sidecar(keeper, {"variants": scan_variants(keeper, keeper_base)})
            # loser is now fully covered -> quarantine
            qdir = loser.parent / f"_dup_quarantine_{STAMP}"
            size = sum((loser / r).stat().st_size for r in inv[loser]
                       if (loser / r).exists() and (loser / r).stat().st_nlink == 1)
            freed += size
            lines.append(f"- QUARANTINE: `{loser.name}` ({size / 2**30:.2f} GB) — covered by `{keeper.name}`")
            if args.apply:
                qdir.mkdir(exist_ok=True)
                dst = qdir / loser.name
                if dst.exists():
                    dst = qdir / f"{loser.name} ({int(time.time())})"
                shutil.move(str(loser), str(dst))
            handled.add(loser)
            actions["quarantine"] += 1

    # ── within-folder copies -> hardlinks ──
    for g in groups:
        if g["kind"] != "within" or not g.get("video_identical"):
            continue
        paths = [Path(p) for p in g["paths"] if Path(p).exists()]
        if len(paths) < 2:
            continue
        folder = work_dir(str(paths[0]))
        if folder is None or folder in handled or (folder.resolve() in busy):
            continue
        paths.sort(key=lambda p: (len(p.relative_to(folder).parts), len(str(p))))
        primary = paths[0]
        links = []
        for other in paths[1:]:
            try:
                if os.stat(other).st_ino == os.stat(primary).st_ino:
                    continue
            except OSError:
                continue
            freed += other.stat().st_size
            lines.append(f"- HARDLINK: `{folder.name}`: {other.relative_to(folder)} -> {primary.relative_to(folder)}")
            if args.apply:
                other.unlink()
                os.link(primary, other)
                links.append((primary, other))
            actions["hardlink"] += 1
        append_linkinfo(folder, links, args.apply)

    cache.save()
    lines.append(f"\n**Summary**: {actions}; space freed once quarantine is emptied ≈ {freed / 2**30:.1f} GB")
    Path(args.report).write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[-8:]))
    print(f"report: {args.report}")


if __name__ == "__main__":
    main()
