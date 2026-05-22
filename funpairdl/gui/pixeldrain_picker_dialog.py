from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field

import aiohttp
from PySide6.QtCore import Qt, QObject, QThread, Signal
from PySide6.QtGui import QBrush, QColor, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QStyle,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from funpairdl.core.pairing import (
    Candidate,
    Confidence,
    FileKind,
    Group,
    pair_files,
)
from funpairdl.core.progress import format_size
from funpairdl.gui.pair_preview_dialog import PairPreviewDialog
from funpairdl.persistence.settings import Settings
from funpairdl.providers.pixeldrain import FsNode, PixeldrainProvider

logger = logging.getLogger("funpairdl.gui.pixeldrain_picker_dialog")

SCRIPT_EXTS = {"funscript", "syncscript"}

# Internal Pixeldrain metadata files we should never offer for download.
PIXELDRAIN_METADATA_FILENAMES = {".search_index.gz"}


def _is_pixeldrain_metadata(node) -> bool:
    """Pixeldrain's filesystem API surfaces internal metadata files
    (.search_index.gz today; possibly more in future) alongside real
    user content. Drop them so users never accidentally download them."""
    if node.is_dir:
        return False
    name = (node.name or "").lower()
    if name in PIXELDRAIN_METADATA_FILENAMES:
        return True
    if name.startswith(".search_index"):
        return True
    return False
VIDEO_EXTS = {
    "mp4", "mkv", "webm", "avi", "mov", "wmv", "flv", "m4v", "ts", "mpg", "mpeg",
}

# Tree columns: 0 = name (with checkbox + expand), 1 = ext, 2 = size, 3 = "as type", 4 = modified
COL_NAME = 0
COL_EXT = 1
COL_SIZE = 2
COL_AS_TYPE = 3
COL_MODIFIED = 4

# QTreeWidgetItem user-data roles
ROLE_NODE = Qt.UserRole + 1
ROLE_LOADED = Qt.UserRole + 2  # bool: children fetched
ROLE_AS_TYPE = Qt.UserRole + 3  # str: "video"|"script"|"other" (file leaves only)


@dataclass
class PickerResult:
    name: str
    video_urls: list[str]
    script_urls: list[str]
    rename_direction: str  # "off" | "video_first" | "script_first"
    auto_rename: bool = True   # False => skip rename for this Pair (orphans)
    output_dir_override: str = ""  # empty -> use global download_dir


# ── Async worker that owns its own event loop ───────────────────────────
class _AsyncWorker(QObject):
    """Holds a single asyncio event loop on a QThread so the picker can
    fire many concurrent fetches without blocking the GUI."""

    def __init__(self):
        super().__init__()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread = QThread()
        self._thread.run = self._run
        self._ready = asyncio.Event() if False else None  # placeholder

    def start(self):
        self._thread.start()
        # Spin until loop is ready
        import time
        while self.loop is None:
            time.sleep(0.005)

    def stop(self):
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.quit()
        self._thread.wait(2000)

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        finally:
            self.loop.close()

    def submit(self, coro, on_done=None, on_error=None):
        """Schedule a coroutine on the worker loop. Callbacks fire on
        whatever thread the loop dispatches; UI work must be marshalled
        via Qt signals."""
        if not self.loop:
            raise RuntimeError("Worker not started")
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def _done(fut):
            try:
                result = fut.result()
            except Exception as e:
                logger.exception("Async task failed")
                if on_error:
                    on_error(e)
                return
            if on_done:
                on_done(result)

        future.add_done_callback(_done)
        return future


class PixeldrainPickerDialog(QDialog):
    """Tree-based picker for Pixeldrain filesystem URLs.

    Each user-supplied URL becomes a top-level node. Directories are
    loaded lazily when expanded; tristate checkboxes on directories
    indicate which descendants are selected. On accept, every checked
    directory is recursively walked to enumerate its file leaves.
    """

    sig_root_loaded = Signal(object, object, object, str)  # url, root, children, error
    sig_children_loaded = Signal(object, object, object, str)  # parent_item, root_node, children, error

    def __init__(self, settings: Settings, initial_urls: list[str] | None = None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.provider = PixeldrainProvider(api_key=settings.pixeldrain_api_key or "")
        self._aiohttp_session: aiohttp.ClientSession | None = None
        self._worker = _AsyncWorker()
        self._worker.start()
        self._pending_root_loads: set[str] = set()
        # Re-entrancy guard for tristate propagation
        self._propagating_check = False

        self.setWindowTitle("Pixeldrain Picker")
        self.setMinimumSize(960, 640)
        self._setup_ui()

        # Worker-side aiohttp session (created on first use)
        self._worker.submit(self._init_session(), on_error=lambda e: logger.error(e))

        self.sig_root_loaded.connect(self._on_root_loaded)
        self.sig_children_loaded.connect(self._on_children_loaded)

        if initial_urls:
            self.url_input.setPlainText("\n".join(initial_urls))
            self._on_parse_clicked()

    # ── UI ───────────────────────────────────────────────────────────
    def _setup_ui(self):
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Paste Pixeldrain URLs (any text is fine — URLs are auto-extracted):"))
        self.url_input = QPlainTextEdit()
        self.url_input.setPlaceholderText(
            "https://pixeldrain.com/d/...\nhttps://pixeldrain.com/u/...\nhttps://pixeldrain.com/l/..."
        )
        self.url_input.setMaximumHeight(110)
        layout.addWidget(self.url_input)

        input_btns = QHBoxLayout()
        self.btn_parse = QPushButton("Parse / Add to tree")
        self.btn_parse.clicked.connect(self._on_parse_clicked)
        input_btns.addWidget(self.btn_parse)
        self.fetch_progress = QProgressBar()
        self.fetch_progress.setRange(0, 0)
        self.fetch_progress.setVisible(False)
        input_btns.addWidget(self.fetch_progress, 1)
        layout.addLayout(input_btns)

        # Filter row — search & ext filter operate on already-loaded nodes
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter (loaded nodes only):"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search filename...")
        self.search_edit.textChanged.connect(self._apply_filters)
        filter_row.addWidget(self.search_edit, 2)

        filter_row.addWidget(QLabel("Ext:"))
        self.ext_combo = QComboBox()
        self.ext_combo.addItem("All", "")
        self.ext_combo.currentIndexChanged.connect(self._apply_filters)
        filter_row.addWidget(self.ext_combo, 1)

        self.btn_expand_all = QPushButton("Expand All")
        self.btn_expand_all.setToolTip(
            "Recursively load every folder under all top-level URLs."
        )
        self.btn_expand_all.clicked.connect(self._on_expand_all_clicked)
        filter_row.addWidget(self.btn_expand_all)

        self.btn_collapse_all = QPushButton("Collapse All")
        self.btn_collapse_all.clicked.connect(lambda: self.tree.collapseAll())
        filter_row.addWidget(self.btn_collapse_all)
        layout.addLayout(filter_row)

        # Top-level master checkbox row
        master_row = QHBoxLayout()
        self.cb_select_all_top = QCheckBox("Select all top-level")
        self.cb_select_all_top.setTristate(True)
        self.cb_select_all_top.setChecked(True)
        self.cb_select_all_top.clicked.connect(self._on_select_all_top_clicked)
        master_row.addWidget(self.cb_select_all_top)
        master_row.addStretch(1)
        layout.addLayout(master_row)

        # Tree
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Ext", "Size", "As", "Modified"])
        self.tree.setColumnWidth(COL_NAME, 460)
        self.tree.setColumnWidth(COL_EXT, 70)
        self.tree.setColumnWidth(COL_SIZE, 100)
        self.tree.setColumnWidth(COL_AS_TYPE, 90)
        self.tree.setColumnWidth(COL_MODIFIED, 140)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        header = self.tree.header()
        header.setSectionResizeMode(QHeaderView.Interactive)
        self.tree.itemExpanded.connect(self._on_item_expanded)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        layout.addWidget(self.tree, 1)

        # Footer
        footer = QVBoxLayout()
        rename_row = QHBoxLayout()
        rename_row.addWidget(QLabel("Auto-rename:"))
        self.rb_rename_off = QRadioButton("Off")
        self.rb_video_first = QRadioButton("Video name → Script")
        self.rb_script_first = QRadioButton("Script name → Video")
        mode = self.settings.default_rename_direction
        if mode == "off":
            self.rb_rename_off.setChecked(True)
        elif mode == "script_first":
            self.rb_script_first.setChecked(True)
        else:
            self.rb_video_first.setChecked(True)
        rename_row.addWidget(self.rb_rename_off)
        rename_row.addWidget(self.rb_video_first)
        rename_row.addWidget(self.rb_script_first)
        rename_row.addStretch(1)
        footer.addLayout(rename_row)

        merge_row = QHBoxLayout()
        self.cb_merge = QCheckBox("Merge all selections into one Pair")
        self.cb_merge.setToolTip(
            "Off: each top-level URL becomes its own Pair (named after its bucket).\n"
            "On: every selected file goes into a single Pair using the name below."
        )
        self.cb_merge.toggled.connect(self._on_merge_toggled)
        merge_row.addWidget(self.cb_merge)

        merge_row.addWidget(QLabel("Pair name:"))
        self.merged_name_edit = QLineEdit()
        self.merged_name_edit.setPlaceholderText("Used only when 'Merge' is checked")
        self.merged_name_edit.setEnabled(False)
        merge_row.addWidget(self.merged_name_edit, 2)
        footer.addLayout(merge_row)

        # Download destination — applies to every Pair this picker
        # produces. Defaults to the global download_dir but can be
        # overridden for this batch only (e.g. when the default volume
        # is full).
        dest_row = QHBoxLayout()
        dest_row.addWidget(QLabel("Download to:"))
        self.dest_edit = QLineEdit(self.settings.download_dir)
        self.dest_edit.textChanged.connect(self._update_disk_free)
        dest_row.addWidget(self.dest_edit, 3)
        self.btn_dest_browse = QPushButton("Browse...")
        self.btn_dest_browse.clicked.connect(self._browse_destination)
        dest_row.addWidget(self.btn_dest_browse)
        self.cb_save_dest_default = QCheckBox("Save as default")
        self.cb_save_dest_default.setToolTip(
            "When checked, the chosen folder also becomes the global default."
        )
        dest_row.addWidget(self.cb_save_dest_default)
        footer.addLayout(dest_row)

        self.disk_free_label = QLabel("")
        self.disk_free_label.setStyleSheet("color: #888;")
        footer.addWidget(self.disk_free_label)
        self._update_disk_free()

        self.summary_label = QLabel("No URLs loaded.")
        footer.addWidget(self.summary_label)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Add to Queue")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        footer.addWidget(self.buttons)
        layout.addLayout(footer)

    # ── Worker session lifecycle ─────────────────────────────────────
    async def _init_session(self):
        if self._aiohttp_session is None:
            self._aiohttp_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers=self.provider._auth_headers(),
            )

    async def _close_session(self):
        if self._aiohttp_session:
            await self._aiohttp_session.close()
            self._aiohttp_session = None

    # ── Parse top-level URLs ─────────────────────────────────────────
    def _on_parse_clicked(self):
        from funpairdl.utils.url_parser import extract_pixeldrain_urls
        urls = extract_pixeldrain_urls(self.url_input.toPlainText())
        if not urls:
            QMessageBox.information(self, "No URLs", "No Pixeldrain URLs found.")
            return

        existing = {self.tree.topLevelItem(i).data(0, Qt.UserRole)
                    for i in range(self.tree.topLevelItemCount())}
        new = [u for u in urls if u not in existing]
        if not new:
            QMessageBox.information(self, "Already loaded",
                                    "All URLs are already in the tree.")
            return

        self._pending_root_loads.update(new)
        self.fetch_progress.setVisible(True)
        self.btn_parse.setEnabled(False)
        self.summary_label.setText(f"Loading {len(new)} URL(s)...")

        for url in new:
            # Add a placeholder row immediately so users see progress
            placeholder = QTreeWidgetItem([url, "", "", "", ""])
            placeholder.setForeground(0, QBrush(QColor("#888")))
            placeholder.setData(0, Qt.UserRole, url)  # stash URL
            placeholder.setFlags(placeholder.flags() & ~Qt.ItemIsUserCheckable)
            self.tree.addTopLevelItem(placeholder)
            self._worker.submit(
                self._fetch_root(url),
                on_error=lambda e, u=url: self.sig_root_loaded.emit(u, None, [], str(e)),
            )

    async def _fetch_root(self, url: str):
        if not self._aiohttp_session:
            await self._init_session()
        root, children, err = await self.provider.fetch_root_node(
            url, session=self._aiohttp_session,
        )
        self.sig_root_loaded.emit(url, root, children, err)

    def _on_root_loaded(self, url: str, root, children, error: str):
        # Replace placeholder for this URL
        placeholder = None
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.data(0, Qt.UserRole) == url and item.data(0, ROLE_NODE) is None:
                placeholder = item
                break
        idx = self.tree.indexOfTopLevelItem(placeholder) if placeholder else -1
        if placeholder is not None:
            self.tree.takeTopLevelItem(idx)

        if error or root is None:
            err_item = QTreeWidgetItem([f"❌ {url}", "", "", "", ""])
            err_item.setForeground(0, QBrush(QColor("#c33")))
            err_item.setToolTip(0, error or "unknown error")
            err_item.setData(0, Qt.UserRole, url)
            err_item.setFlags(err_item.flags() & ~Qt.ItemIsUserCheckable)
            self.tree.addTopLevelItem(err_item)
        else:
            root_item = self._make_item(root, url)
            self.tree.addTopLevelItem(root_item)
            self._populate_children(root_item, children)
            root_item.setData(0, ROLE_LOADED, True)
            # Default-check the new top-level. Setting on a tristate-auto
            # parent propagates to its already-loaded children automatically.
            self._propagating_check = True
            try:
                root_item.setCheckState(0, Qt.Checked)
            finally:
                self._propagating_check = False
            if children:
                root_item.setExpanded(False)  # lazy: don't auto-expand

        self._pending_root_loads.discard(url)
        if not self._pending_root_loads:
            self.fetch_progress.setVisible(False)
            self.btn_parse.setEnabled(True)
        self._refresh_ext_combo()
        self._update_summary()
        self._refresh_master_checkbox()

    # ── Tree population ──────────────────────────────────────────────
    def _make_item(self, node: FsNode, source_url: str = "") -> QTreeWidgetItem:
        item = QTreeWidgetItem()
        item.setText(COL_NAME, node.name)
        item.setText(COL_EXT, node.ext)
        item.setText(COL_SIZE, format_size(node.size) if node.size else "")
        item.setData(COL_SIZE, Qt.UserRole, node.size or 0)
        item.setText(COL_MODIFIED,
                     node.date_modified[:19].replace("T", " ") if node.date_modified else "")
        item.setData(0, ROLE_NODE, node)
        if source_url:
            item.setData(0, Qt.UserRole, source_url)

        flags = item.flags() | Qt.ItemIsUserCheckable
        # Qt's AutoTristate would auto-recompute a directory's check from
        # its children, which fights with our lazy-load model: an unloaded
        # subdir would always pull its parent back to Unchecked. We do
        # the propagation ourselves in _on_item_changed instead.
        if node.is_dir:
            item.setIcon(COL_NAME,
                         self.style().standardIcon(QStyle.SP_DirIcon))
            # Add a dummy child so the expand arrow shows up. The dummy is
            # checkable purely so Qt's AutoTristate propagates the parent's
            # check mark visually — _selected_leaves_under ignores any
            # node without a real FsNode payload, so this never affects
            # what actually gets downloaded.
            if node.path.startswith("/"):
                dummy = QTreeWidgetItem(["(not yet loaded — expand to see)", "", "", "", ""])
                dummy.setFlags(
                    Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable
                )
                dummy.setCheckState(0, Qt.Unchecked)
                dummy.setForeground(0, QBrush(QColor("#888")))
                item.addChild(dummy)
                item.setData(0, ROLE_LOADED, False)
            else:
                # Synthetic wrapper (legacy single-file or list) — children
                # are added by caller; treat as already loaded.
                item.setData(0, ROLE_LOADED, True)
        else:
            item.setIcon(COL_NAME,
                         self.style().standardIcon(QStyle.SP_FileIcon))
            # Auto-detect "as type"
            if node.ext in SCRIPT_EXTS:
                as_type = "script"
            elif node.ext in VIDEO_EXTS:
                as_type = "video"
            else:
                as_type = "other"
            item.setData(0, ROLE_AS_TYPE, as_type)
            item.setText(COL_AS_TYPE, as_type)

        item.setFlags(flags)
        if node.error:
            item.setText(COL_NAME, f"❌ {node.name}")
            item.setForeground(COL_NAME, QBrush(QColor("#c33")))
            item.setToolTip(COL_NAME, node.error)
            item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
        else:
            item.setCheckState(0, Qt.Unchecked)
        return item

    def _populate_children(self, parent_item: QTreeWidgetItem, children: list[FsNode]):
        # Remove any "Loading..." placeholder
        for i in reversed(range(parent_item.childCount())):
            child = parent_item.child(i)
            if child.data(0, ROLE_NODE) is None:
                parent_item.removeChild(child)
        # Filter out Pixeldrain's auto-generated metadata files — these
        # are gzipped search indexes the API surfaces, not user content.
        children = [c for c in children if not _is_pixeldrain_metadata(c)]
        # Sort: directories first, then by name
        children = sorted(children, key=lambda n: (not n.is_dir, n.name.lower()))
        for c in children:
            parent_item.addChild(self._make_item(c))

    # ── Lazy expansion ───────────────────────────────────────────────
    def _on_item_expanded(self, item: QTreeWidgetItem):
        node: FsNode | None = item.data(0, ROLE_NODE)
        if not node or not node.is_dir:
            return
        if item.data(0, ROLE_LOADED):
            return
        # Mark loaded immediately to avoid duplicate fetches
        item.setData(0, ROLE_LOADED, True)
        # Replace placeholder text while loading
        if item.childCount() == 1 and item.child(0).data(0, ROLE_NODE) is None:
            item.child(0).setText(0, "Loading...")

        self._worker.submit(
            self._fetch_dir(item, node),
            on_error=lambda e, it=item: self.sig_children_loaded.emit(it, None, [], str(e)),
        )

    async def _fetch_dir(self, parent_item: QTreeWidgetItem, node: FsNode):
        if not self._aiohttp_session:
            await self._init_session()
        try:
            root, children, err = await self.provider.fetch_node_children(
                node.path, self._aiohttp_session,
            )
            self.sig_children_loaded.emit(parent_item, root, children, err)
        except aiohttp.ClientResponseError as e:
            self.sig_children_loaded.emit(parent_item, None, [], f"HTTP {e.status}")
        except Exception as e:
            self.sig_children_loaded.emit(parent_item, None, [], f"{type(e).__name__}: {e}")

    def _on_children_loaded(self, parent_item, _root, children, error: str):
        if error:
            # Replace placeholder with an error row
            for i in reversed(range(parent_item.childCount())):
                child = parent_item.child(i)
                if child.data(0, ROLE_NODE) is None:
                    child.setText(0, f"❌ {error}")
                    child.setForeground(0, QBrush(QColor("#c33")))
            parent_item.setData(0, ROLE_LOADED, False)  # allow retry
            return
        was_checked = parent_item.checkState(0) == Qt.Checked
        self._populate_children(parent_item, children)
        # If parent was fully checked before expansion, propagate to new children
        if was_checked:
            self._propagating_check = True
            try:
                for i in range(parent_item.childCount()):
                    c = parent_item.child(i)
                    if c.flags() & Qt.ItemIsUserCheckable:
                        c.setCheckState(0, Qt.Checked)
            finally:
                self._propagating_check = False
        self._refresh_ext_combo()
        self._apply_filters()
        self._update_summary()

    # ── Selection / propagation ──────────────────────────────────────
    def _on_item_changed(self, item: QTreeWidgetItem, col: int):
        if col != 0 or self._propagating_check:
            self._update_summary()
            return
        # Propagate check state down to children (also unloaded ones —
        # _set_subtree_check is a no-op past the dummy, but the dummy
        # itself stays in sync) and recompute every ancestor's tristate.
        node: FsNode | None = item.data(0, ROLE_NODE)
        state = item.checkState(0)
        self._propagating_check = True
        try:
            if node and node.is_dir and state in (Qt.Checked, Qt.Unchecked):
                self._set_subtree_check(item, state)
            # Roll up to ancestors so directories accurately reflect
            # whether all/some/none of their checkable descendants are on.
            parent = item.parent()
            while parent is not None:
                self._recompute_parent_state(parent)
                parent = parent.parent()
        finally:
            self._propagating_check = False
        self._update_summary()
        self._refresh_master_checkbox()

    def _recompute_parent_state(self, parent: QTreeWidgetItem):
        """Set `parent`'s check state based on its visible children:
        all checked → Checked, none → Unchecked, mix → PartiallyChecked.
        Ignores non-checkable / non-FsNode placeholder rows."""
        if not (parent.flags() & Qt.ItemIsUserCheckable):
            return
        node: FsNode | None = parent.data(0, ROLE_NODE)
        if not (node and node.is_dir):
            return
        n_checked = n_partial = n_unchecked = 0
        for i in range(parent.childCount()):
            c = parent.child(i)
            cn: FsNode | None = c.data(0, ROLE_NODE)
            if cn is None:
                # Skip dummy placeholder — it tracks the parent, not the other way
                continue
            if not (c.flags() & Qt.ItemIsUserCheckable):
                continue
            s = c.checkState(0)
            if s == Qt.Checked:
                n_checked += 1
            elif s == Qt.PartiallyChecked:
                n_partial += 1
            else:
                n_unchecked += 1
        total = n_checked + n_partial + n_unchecked
        if total == 0:
            return
        if n_partial or (n_checked and n_unchecked):
            parent.setCheckState(0, Qt.PartiallyChecked)
        elif n_checked == total:
            parent.setCheckState(0, Qt.Checked)
        else:
            parent.setCheckState(0, Qt.Unchecked)

    def _set_subtree_check(self, item: QTreeWidgetItem, state):
        for i in range(item.childCount()):
            c = item.child(i)
            if not (c.flags() & Qt.ItemIsUserCheckable):
                continue
            c.setCheckState(0, state)
            child_node: FsNode | None = c.data(0, ROLE_NODE)
            # Recurse into directories regardless of load state. For
            # unloaded dirs the recursion only touches the dummy
            # placeholder (which we made checkable), keeping its visual
            # state in sync with the parent. _selected_leaves_under uses
            # ROLE_LOADED to decide what to do with it on accept.
            if child_node and child_node.is_dir:
                self._set_subtree_check(c, state)

    def _refresh_master_checkbox(self):
        """Sync the 'Select all top-level' checkbox to reflect actual top-
        level selection state without re-propagating."""
        states = []
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if not (it.flags() & Qt.ItemIsUserCheckable):
                continue
            states.append(it.checkState(0))
        self.cb_select_all_top.blockSignals(True)
        try:
            if not states:
                self.cb_select_all_top.setCheckState(Qt.Unchecked)
            elif all(s == Qt.Checked for s in states):
                self.cb_select_all_top.setCheckState(Qt.Checked)
            elif all(s == Qt.Unchecked for s in states):
                self.cb_select_all_top.setCheckState(Qt.Unchecked)
            else:
                self.cb_select_all_top.setCheckState(Qt.PartiallyChecked)
        finally:
            self.cb_select_all_top.blockSignals(False)

    def _on_select_all_top_clicked(self, checked: bool):
        """Master checkbox toggles every top-level URL (and propagates to
        loaded descendants via the existing change handler)."""
        target = Qt.Checked if checked else Qt.Unchecked
        # Force a deterministic two-state cycle: the tristate visual is
        # only used as an indicator, never a click target.
        self.cb_select_all_top.setCheckState(target)
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            if it.flags() & Qt.ItemIsUserCheckable:
                it.setCheckState(0, target)

    # ── Bulk expansion ───────────────────────────────────────────────
    def _on_expand_all_clicked(self):
        """Recursively walk every top-level URL, populating the tree as
        we go. Uses the existing fetch_node_children path so already-
        loaded directories are not re-fetched."""
        roots = []
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            n: FsNode | None = it.data(0, ROLE_NODE)
            logger.info("Expand All: top-level %d: node=%s path=%r is_dir=%s",
                        i, n is not None,
                        n.path if n else None,
                        n.is_dir if n else None)
            if n and n.is_dir and (n.path.startswith("/") or n.path.startswith("list:")):
                roots.append(it)
        logger.info("Expand All clicked: %d expandable root(s) found", len(roots))
        if not roots:
            QMessageBox.information(self, "Nothing to expand",
                                    "Load some URLs first.")
            return
        self._run_recursive_expansion(roots)

    def _on_tree_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        node: FsNode | None = item.data(0, ROLE_NODE)
        menu = QMenu(self)
        if node and node.is_dir and node.path.startswith("/"):
            act_expand = menu.addAction("Expand recursively from here")
            act_expand.triggered.connect(lambda: self._run_recursive_expansion([item]))
        if menu.actions():
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _run_recursive_expansion(self, items: list[QTreeWidgetItem]):
        """Walk subtree(s) using PixeldrainProvider, batching results
        back to the GUI thread for population. Lazy-load aware: already
        loaded subtrees are recursed in-place without re-fetching."""
        progress = QProgressDialog(
            "Loading... 0 folders walked, 0 files found.",
            "Cancel", 0, 0, self,
        )
        progress.setWindowTitle("Recursive expansion")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)

        cancel_event = asyncio.Event()
        progress.canceled.connect(
            lambda: self._worker.loop.call_soon_threadsafe(cancel_event.set)
        )

        from PySide6.QtCore import QTimer

        # Counters live on GUI thread; worker writes via call_soon_threadsafe-friendly
        # primitives — but here we just stash them in a mutable dict.
        stats = {"dirs": 0, "files": 0}

        def report():
            progress.setLabelText(
                f"Loading... {stats['dirs']} folder(s) walked, "
                f"{stats['files']} file(s) found."
            )

        # Worker collects (parent_path, children) tuples and emits them
        # in batches so the GUI thread can populate without being on the
        # async-walk's hot path.
        result_queue: list[tuple[str, list]] = []  # (parent_id_path, children_FsNode)

        async def walk(node_path: str):
            """Fetch children for `node_path` and recurse into subdirs.
            Pure data-fetch; no Qt mutation."""
            if cancel_event.is_set():
                return
            try:
                _root, children, err = await self.provider.fetch_node_children(
                    node_path, self._aiohttp_session,
                )
            except Exception as e:
                logger.warning("Walk failed at %s: %s", node_path, e)
                return
            if err:
                logger.warning("Walk error at %s: %s", node_path, err)
                return
            logger.debug("Walked %s: %d children", node_path, len(children))
            result_queue.append((node_path, children))
            # Recurse into nested filesystem dirs concurrently
            tasks = [walk(c.path) for c in children
                     if c.is_dir and c.path.startswith("/")]
            if tasks:
                # Cap concurrency via semaphore-bound walk
                await asyncio.gather(*tasks)

        async def driver(start_paths: list[str]):
            sem = asyncio.Semaphore(4)
            async def bounded(p):
                async with sem:
                    await walk(p)
            await asyncio.gather(*(bounded(p) for p in start_paths))

        # Map node-path → QTreeWidgetItem so we can find where to attach
        # children once they come back. Built before kicking off worker.
        path_to_item: dict[str, QTreeWidgetItem] = {}
        start_paths: list[str] = []

        def index(item: QTreeWidgetItem):
            n: FsNode | None = item.data(0, ROLE_NODE)
            if n and n.is_dir and n.path.startswith("/"):
                path_to_item[n.path] = item
            for i in range(item.childCount()):
                index(item.child(i))

        for it in items:
            index(it)
            n: FsNode | None = it.data(0, ROLE_NODE)
            if n and n.is_dir and n.path.startswith("/"):
                start_paths.append(n.path)

        logger.info("Recursive expansion: %d start path(s), index=%d",
                    len(start_paths), len(path_to_item))
        if not start_paths:
            progress.close()
            return

        # Make sure the aiohttp session exists before submitting the walk
        # (the picker normally lazy-creates it on first parse).
        if not self._aiohttp_session:
            init_future = self._worker.submit(self._init_session())
            try:
                init_future.result(timeout=5)
            except Exception as e:
                logger.warning("session init failed: %s", e)
        logger.info("Submitting driver to worker loop")
        future = self._worker.submit(driver(start_paths))

        # Drive GUI: drain result_queue while the walker runs, populate
        # tree, refresh progress.
        loops = 0
        while not future.done():
            self._drain_expansion_results(result_queue, path_to_item, stats)
            report()
            QApplication.processEvents()
            # Tiny sleep keeps CPU sane without breaking responsiveness
            self._sleep_ms(20)
            loops += 1
            if loops % 50 == 0:  # every ~1s
                logger.info("Expansion loop alive: queue=%d dirs_done=%d files=%d done=%s",
                            len(result_queue), stats["dirs"], stats["files"], future.done())
        logger.info("Driver finished. Queue size at end=%d, dirs=%d, files=%d",
                    len(result_queue), stats["dirs"], stats["files"])
        try:
            future.result(timeout=1)
            logger.info("Driver returned cleanly")
        except Exception as e:
            logger.exception("Driver raised: %s", e)
        # Final drain after walker finishes — keep draining until empty
        # in case the worker beat us to a final batch.
        while result_queue:
            self._drain_expansion_results(result_queue, path_to_item, stats)
        report()
        logger.info("Final stats: dirs=%d, files=%d", stats["dirs"], stats["files"])
        progress.close()

        # Force-expand top-level + first-level dirs only (no recursion).
        # We DO NOT re-walk the entire subtree to set check states: drain
        # already inherits the parent's state to brand-new children, so
        # 5k+ leaves are correct already. Touching them again here would
        # call setCheckState on every leaf — which freezes the GUI thread
        # for minutes on big vaults. The summary uses the leaves directly
        # via _selected_leaves_under, so it is accurate without further
        # propagation here.
        self._propagating_check = True
        try:
            for it in items:
                node = it.data(0, ROLE_NODE)
                logger.info("Force-expand: top-level path=%r checkState=%s",
                            node.path if node else None,
                            it.checkState(0).name
                            if hasattr(it.checkState(0), "name")
                            else str(it.checkState(0)))
                it.setExpanded(True)
                # Re-assert top-level check (cheap, single call) so any
                # tristate roll-up wobble during drain is corrected.
                if it.checkState(0) == Qt.PartiallyChecked:
                    it.setCheckState(0, Qt.Checked)
                # Expand only the immediate children; deeper levels
                # remain collapsed for readability and performance.
                for i in range(it.childCount()):
                    child = it.child(i)
                    cn = child.data(0, ROLE_NODE)
                    if cn and cn.is_dir:
                        child.setExpanded(True)
        finally:
            self._propagating_check = False
        # Refresh selection summary now that the tree is final.
        self._refresh_ext_combo()
        self._apply_filters()
        self._refresh_master_checkbox()
        self._update_summary()
        logger.info("Expand all complete: tree expanded, summary refreshed")

    @staticmethod
    def _sleep_ms(ms: int):
        from PySide6.QtCore import QEventLoop, QTimer
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def _drain_expansion_results(
        self,
        queue: list,
        path_to_item: dict,
        stats: dict,
    ):
        """Pop pending (parent_path, children) results and attach them
        to the tree on the GUI thread."""
        # Take a local copy so the worker can keep appending while we work.
        pending = queue[:]
        del queue[:len(pending)]
        if not pending:
            return
        logger.info("Drain: %d pending result(s)", len(pending))
        # Briefly mute change handler while we mass-attach.
        self._propagating_check = True
        try:
            for parent_path, children in pending:
                parent_item = path_to_item.get(parent_path)
                if parent_item is None:
                    logger.warning("Drain: no item for path %r in index (size=%d)",
                                   parent_path, len(path_to_item))
                    continue
                if parent_item.data(0, ROLE_LOADED):
                    continue  # already populated by another path
                self._populate_children(parent_item, children)
                parent_item.setData(0, ROLE_LOADED, True)
                # Auto-expand the top two levels so users immediately see
                # what was loaded; deeper levels stay collapsed for
                # readability (still expandable on click).
                depth = 0
                p = parent_item.parent()
                while p is not None:
                    depth += 1
                    p = p.parent()
                if depth <= 1:
                    parent_item.setExpanded(True)
                stats["dirs"] += 1
                # Inherit parent check state to brand new children. We
                # treat PartiallyChecked as Checked here so a vault
                # checked en-masse keeps that intent as deeper folders
                # stream in (otherwise tristate roll-up flips the parent
                # to Partial mid-populate and new descendants would be
                # left unchecked).
                state = parent_item.checkState(0)
                inherit = Qt.Checked if state in (Qt.Checked, Qt.PartiallyChecked) else Qt.Unchecked
                for i in range(parent_item.childCount()):
                    c = parent_item.child(i)
                    if c.flags() & Qt.ItemIsUserCheckable:
                        c.setCheckState(0, inherit)
                # Index the new directory children so the worker's results
                # for them can find their attachment points
                for i in range(parent_item.childCount()):
                    c = parent_item.child(i)
                    cn: FsNode | None = c.data(0, ROLE_NODE)
                    if cn:
                        if cn.is_dir and cn.path.startswith("/"):
                            path_to_item[cn.path] = c
                        else:
                            stats["files"] += 1
        finally:
            self._propagating_check = False

    def _count_loaded_dirs(self, item) -> int:
        n = 0
        for i in range(item.childCount()):
            c = item.child(i)
            cn: FsNode | None = c.data(0, ROLE_NODE)
            if cn and cn.is_dir and c.data(0, ROLE_LOADED):
                n += 1
            n += self._count_loaded_dirs(c)
        return n

    def _count_loaded_files(self, item) -> int:
        n = 0
        for i in range(item.childCount()):
            c = item.child(i)
            cn: FsNode | None = c.data(0, ROLE_NODE)
            if cn and not cn.is_dir:
                n += 1
            n += self._count_loaded_files(c)
        return n

    # ── Filters ──────────────────────────────────────────────────────
    def _refresh_ext_combo(self):
        seen = set()
        self._collect_loaded_exts(self.tree.invisibleRootItem(), seen)
        current = self.ext_combo.currentData()
        self.ext_combo.blockSignals(True)
        self.ext_combo.clear()
        self.ext_combo.addItem("All", "")
        for e in sorted(seen):
            self.ext_combo.addItem(f".{e}", e)
        if current:
            idx = self.ext_combo.findData(current)
            if idx >= 0:
                self.ext_combo.setCurrentIndex(idx)
        self.ext_combo.blockSignals(False)

    def _collect_loaded_exts(self, item: QTreeWidgetItem, out: set):
        for i in range(item.childCount()):
            c = item.child(i)
            n: FsNode | None = c.data(0, ROLE_NODE)
            if n and not n.is_dir and n.ext:
                out.add(n.ext)
            if c.childCount():
                self._collect_loaded_exts(c, out)

    def _apply_filters(self):
        needle = self.search_edit.text().strip().lower()
        ext = self.ext_combo.currentData() or ""
        self._apply_filter_rec(self.tree.invisibleRootItem(), needle, ext)

    def _apply_filter_rec(self, item: QTreeWidgetItem, needle: str, ext: str) -> bool:
        any_visible = False
        n: FsNode | None = item.data(0, ROLE_NODE)
        for i in range(item.childCount()):
            c = item.child(i)
            child_visible = self._apply_filter_rec(c, needle, ext)
            any_visible = any_visible or child_visible
        if n:
            if n.is_dir:
                # Directories visible if any descendant matches, or if no
                # filter is active
                visible = any_visible or (not needle and not ext)
            else:
                visible = True
                if needle and needle not in n.name.lower():
                    visible = False
                if ext and n.ext != ext:
                    visible = False
            item.setHidden(not visible)
            return visible
        return any_visible

    # ── Summary ──────────────────────────────────────────────────────
    def _on_merge_toggled(self, checked):
        self.merged_name_edit.setEnabled(checked)
        self._update_summary()

    def _selected_leaves_under(self, item: QTreeWidgetItem) -> tuple[list[QTreeWidgetItem], int]:
        """Count files (loaded leaves) currently checked under `item` and any
        unloaded directories that are checked (they contribute unknown counts)."""
        checked_leaves: list[QTreeWidgetItem] = []
        pending_dirs = 0

        def walk(it):
            nonlocal pending_dirs
            for i in range(it.childCount()):
                c = it.child(i)
                n: FsNode | None = c.data(0, ROLE_NODE)
                if not n:
                    continue
                if n.is_dir:
                    if c.checkState(0) == Qt.Checked and not c.data(0, ROLE_LOADED):
                        pending_dirs += 1
                    walk(c)
                else:
                    if c.checkState(0) == Qt.Checked:
                        checked_leaves.append(c)

        walk(item)
        return checked_leaves, pending_dirs

    def _update_summary(self):
        leaves, pending = self._selected_leaves_under(self.tree.invisibleRootItem())
        size = sum((it.data(COL_SIZE, Qt.UserRole) or 0) for it in leaves)
        n = len(leaves)
        if pending:
            extra = f" + {pending} unexpanded folder(s) (will be enumerated on Add)"
        else:
            extra = ""
        if n == 0 and pending == 0:
            self.summary_label.setText("Nothing selected.")
            self.buttons.button(QDialogButtonBox.Ok).setEnabled(False)
            return
        self.summary_label.setText(
            f"{n} file(s) selected ({format_size(size)}){extra}"
        )
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(True)

    # ── Accept: recursively expand checked dirs, then build PickerResults ─
    def _current_rename_mode(self) -> str:
        if self.rb_rename_off.isChecked():
            return "off"
        if self.rb_script_first.isChecked():
            return "script_first"
        return "video_first"

    def accept(self):
        leaves, pending = self._selected_leaves_under(self.tree.invisibleRootItem())
        logger.info("Picker accept: %d leaves, %d pending unexpanded dirs",
                    len(leaves), pending)
        if not leaves and not pending:
            logger.info("Picker accept: nothing selected, returning")
            return
        if self.cb_merge.isChecked() and not self.merged_name_edit.text().strip():
            QMessageBox.warning(self, "Pair name required",
                                "Enter a Pair name before merging selections.")
            return

        # Validate the destination folder before going further so the
        # user can fix it without losing their selection.
        dest = self.dest_edit.text().strip()
        if not dest:
            QMessageBox.warning(self, "Destination required",
                                "Choose a download folder.")
            return

        # Persist default rename direction
        self.settings.default_rename_direction = self._current_rename_mode()
        # Optionally persist the destination as the new global default
        if self.cb_save_dest_default.isChecked():
            self.settings.download_dir = dest
        self.settings.save()

        if pending:
            logger.info("Picker accept: enumerating pending dirs...")
            collected = self._enumerate_pending_dirs()
            if collected is None:
                logger.info("Picker accept: enumeration cancelled")
                return  # cancelled
            logger.info("Picker accept: enumerated %d additional leaves", len(collected))
            leaves.extend(collected)

        logger.info("Picker accept: grouping %d leaves into Pairs...", len(leaves))
        results = self._group_results(leaves)
        if results is None:
            logger.info("Picker accept: preview cancelled")
            return
        logger.info("Picker accept: produced %d PickerResult(s); accepting dialog", len(results))
        self._build_results = results
        super().accept()

    def _enumerate_pending_dirs(self) -> list[QTreeWidgetItem] | None:
        # Build cancellation event in worker loop
        def collect_pending(item, out):
            for i in range(item.childCount()):
                c = item.child(i)
                n: FsNode | None = c.data(0, ROLE_NODE)
                if not n or not n.is_dir:
                    if c.childCount():
                        collect_pending(c, out)
                    continue
                if c.checkState(0) == Qt.Checked and not c.data(0, ROLE_LOADED):
                    out.append(c)
                else:
                    collect_pending(c, out)

        pending_items: list[QTreeWidgetItem] = []
        collect_pending(self.tree.invisibleRootItem(), pending_items)
        if not pending_items:
            return []

        progress = QProgressDialog(
            "Enumerating folders...\n0 files found.", "Cancel", 0, 0, self,
        )
        progress.setWindowTitle("Expanding selection")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)

        cancel_event = asyncio.Event()
        progress.canceled.connect(
            lambda: self._worker.loop.call_soon_threadsafe(cancel_event.set)
        )

        all_leaves: list[FsNode] = []
        # progress_cb runs on worker thread; marshal to GUI via QTimer.singleShot
        from PySide6.QtCore import QMetaObject, Q_ARG, QTimer

        def progress_cb(visited, found, current_path):
            text = f"Enumerating... {visited} folder(s) walked, {found} file(s) found."
            QTimer.singleShot(
                0,
                lambda t=text: progress.setLabelText(t),
            )

        async def expand_all():
            for item in pending_items:
                if cancel_event.is_set():
                    break
                node: FsNode = item.data(0, ROLE_NODE)
                files = await self.provider.expand_directory_recursive(
                    node, self._aiohttp_session,
                    progress_cb=progress_cb, cancel_event=cancel_event,
                )
                # Tag each leaf with its source dir item for "as type"
                # inheritance. Drop Pixeldrain metadata files here so they
                # don't slip through the bulk-expansion path either.
                for fn in files:
                    if _is_pixeldrain_metadata(fn):
                        continue
                    all_leaves.append((item, fn))

        future = self._worker.submit(expand_all())
        # Spin on Qt event loop until done or cancelled
        while not future.done():
            QApplication.processEvents()
        progress.close()

        if cancel_event.is_set():
            return None

        # Convert to QTreeWidgetItem-like adapters carrying enough info
        adapters: list[QTreeWidgetItem] = []
        for parent_item, fn in all_leaves:
            stub = QTreeWidgetItem()
            stub.setData(0, ROLE_NODE, fn)
            stub.setData(COL_SIZE, Qt.UserRole, fn.size or 0)
            # Inherit "as type" from parent dir if it has one set, else auto-detect
            if fn.ext in SCRIPT_EXTS:
                stub.setData(0, ROLE_AS_TYPE, "script")
            elif fn.ext in VIDEO_EXTS:
                stub.setData(0, ROLE_AS_TYPE, "video")
            else:
                stub.setData(0, ROLE_AS_TYPE, "other")
            # Stash containing top-level URL for grouping
            top = parent_item
            while top.parent():
                top = top.parent()
            stub.setData(0, Qt.UserRole, top.data(0, Qt.UserRole) or top.text(COL_NAME))
            adapters.append(stub)
        return adapters

    def _top_level_for(self, item: QTreeWidgetItem) -> QTreeWidgetItem:
        cur = item
        while cur.parent():
            cur = cur.parent()
        return cur

    def _current_output_override(self) -> str:
        """Destination chosen for this batch. Returns '' if it equals
        the global default (so add_pair falls through to the global)."""
        dest = self.dest_edit.text().strip()
        if not dest or dest == self.settings.download_dir:
            return ""
        return dest

    def _group_results(self, leaves: list[QTreeWidgetItem]) -> list[PickerResult] | None:
        """Run the pairing heuristic, show the preview (when warranted),
        and translate the confirmed Groups into PickerResults.

        Returns None if the user cancelled at the preview step."""
        direction = self._current_rename_mode()

        def url_for(item):
            n: FsNode = item.data(0, ROLE_NODE)
            if n.path.startswith("file:"):
                fid = n.path.split(":", 1)[1]
                return f"https://pixeldrain.com/u/{fid}"
            return n.download_url

        # ── Merge mode: one Pair, no auto-grouping ──
        if self.cb_merge.isChecked():
            name = self.merged_name_edit.text().strip() or "Pixeldrain Pair"
            videos, scripts = [], []
            for it in leaves:
                t = it.data(0, ROLE_AS_TYPE) or "other"
                u = url_for(it)
                if t == "script":
                    scripts.append(u)
                else:
                    videos.append(u)
            # Merge mode treats whatever the user gave as a single intentional
            # Pair — no orphan check, auto_rename follows the user's choice
            # but stays off if they only ticked one kind of file.
            auto_rename = direction != "off" and bool(videos and scripts)
            return [PickerResult(name, videos, scripts, direction,
                                 auto_rename=auto_rename,
                                 output_dir_override=self._current_output_override())]

        # ── Normal mode: heuristic-based grouping with preview ──
        candidates: list[Candidate] = []
        leaf_to_url: dict[int, str] = {}
        for it in leaves:
            n: FsNode = it.data(0, ROLE_NODE)
            t = it.data(0, ROLE_AS_TYPE) or "other"
            kind = (FileKind.SCRIPT if t == "script"
                    else FileKind.VIDEO if t == "video"
                    else FileKind.OTHER)
            # Derive parent path so the pairing algorithm can prefer
            # same-folder matches. For filesystem paths
            # ("/bucket/sub/foo.mp4") the parent is everything before
            # the last "/".
            #
            # For synthetic markers (file:<id>, list:<id>) the per-file id
            # is UNIQUE, so using the leaf's own path as the bucket would
            # scatter every file into its own "folder" — then a video and
            # its funscripts from the same list never match in-folder and
            # all get flagged as cross-folder matches. Instead bucket by the
            # parent tree item (the list/file root) so siblings from the
            # same source share a folder scope.
            if n.path.startswith("/"):
                parent_path = n.path.rsplit("/", 1)[0] or "/"
            else:
                parent = it.parent()
                pnode: FsNode | None = (
                    parent.data(0, ROLE_NODE) if parent is not None else None
                )
                parent_path = (
                    pnode.path if pnode is not None and pnode.path else n.path
                )
            cand = Candidate(
                key=id(it),
                name=n.name,
                kind=kind,
                size=n.size,
                parent_path=parent_path,
            )
            candidates.append(cand)
            leaf_to_url[id(it)] = url_for(it)

        groups = pair_files(candidates)

        # Preview policy: skip when every group is high-confidence AND user
        # ticked the "skip when confident" option in a previous run.
        all_confident = all(g.confidence == Confidence.HIGH for g in groups)
        skip = self.settings.pixeldrain_skip_preview_when_confident and all_confident
        if not skip:
            dlg = PairPreviewDialog(groups, direction, self)
            if not dlg.exec():
                return None
            if dlg.remember_skip:
                self.settings.pixeldrain_skip_preview_when_confident = True
                self.settings.save()

        # Translate Groups to PickerResults
        results: list[PickerResult] = []
        for g in groups:
            videos = [leaf_to_url[c.key] for c in g.videos]
            scripts = [leaf_to_url[c.key] for c in g.scripts]
            # `others` go into video bucket so they download as files
            videos.extend(leaf_to_url[c.key] for c in g.others)
            # Orphans (no video OR no script) skip auto-rename regardless of
            # the user's selection — there is nothing to rename against.
            auto_rename = direction != "off" and not g.is_orphan
            results.append(PickerResult(
                name=g.name,
                video_urls=videos,
                script_urls=scripts,
                rename_direction=direction,
                auto_rename=auto_rename,
                output_dir_override=self._current_output_override(),
            ))
        return results

    # ── Download destination helpers ─────────────────────────────────
    def _browse_destination(self):
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose download folder", self.dest_edit.text(),
        )
        if chosen:
            self.dest_edit.setText(chosen)

    def _update_disk_free(self):
        from pathlib import Path
        import shutil
        path = self.dest_edit.text().strip()
        if not path:
            self.disk_free_label.setText("")
            return
        # Walk up to an existing parent so shutil.disk_usage works even
        # for not-yet-created subfolders.
        p = Path(path)
        while not p.exists() and p.parent != p:
            p = p.parent
        try:
            usage = shutil.disk_usage(p)
        except (OSError, FileNotFoundError):
            self.disk_free_label.setText("(could not read disk space)")
            self.disk_free_label.setStyleSheet("color: #c33;")
            return
        free_gb = usage.free / (1024 ** 3)
        if free_gb < 5:
            color = "#c33"
        elif free_gb < 50:
            color = "#c80"
        else:
            color = "#2a8"
        self.disk_free_label.setText(f"Free space: {free_gb:.1f} GB")
        self.disk_free_label.setStyleSheet(f"color: {color};")

    def picker_results(self) -> list[PickerResult]:
        return getattr(self, "_build_results", [])

    def closeEvent(self, event):
        # Wait for the session-close coroutine to actually run before
        # tearing down the worker loop, otherwise asyncio emits a
        # "coroutine was never awaited" warning.
        try:
            future = self._worker.submit(self._close_session())
            future.result(timeout=2)
        except Exception:
            pass
        self._worker.stop()
        super().closeEvent(event)
