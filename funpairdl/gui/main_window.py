from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStatusBar,
    QSystemTrayIcon,
    QTabWidget,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from funpairdl.core.pair import FileType, ItemState, Pair, PairItem, PairState
from funpairdl.core.progress import format_eta, format_size, format_speed
from funpairdl.core.queue_manager import QueueManager
from funpairdl.gui.pair_dialog import AddPairDialog
from funpairdl.gui.pixeldrain_picker_dialog import PixeldrainPickerDialog
from funpairdl.persistence.settings import Settings
from funpairdl.utils.clipboard_watcher import ClipboardWatcher

logger = logging.getLogger("funpairdl.gui.main_window")


class MainWindow(QMainWindow):
    # Signals for thread-safe GUI updates from async callbacks
    sig_pair_added = Signal(str)
    sig_pair_updated = Signal(str)
    sig_item_updated = Signal(str)
    sig_queue_changed = Signal()
    sig_quota_updated = Signal(str)

    def __init__(self, queue_manager: QueueManager, settings: Settings):
        super().__init__()
        self.qm = queue_manager
        self.settings = settings
        self._pair_items: dict[str, QTreeWidgetItem] = {}
        self._item_lookup: dict[str, tuple[str, int]] = {}  # item_id → (pair_id, child_index)
        self._dirty_pairs: set[str] = set()
        self._dirty_items: set[str] = set()

        self.setWindowTitle("FunPairDL - Download Manager")
        self.setMinimumSize(900, 500)
        self.resize(1000, 600)

        # Set custom icon
        self._app_icon = self._load_app_icon()
        if self._app_icon:
            self.setWindowIcon(self._app_icon)
            QApplication.setWindowIcon(self._app_icon)

        self._setup_ui()
        self._setup_tray()
        self._connect_signals()
        self._setup_refresh_timer()
        self._setup_clipboard_watcher()

        # Connect QueueManager callbacks
        self.qm.on_pair_added = self._on_pair_added
        self.qm.on_pair_updated = self._on_pair_updated
        self.qm.on_item_updated = self._on_item_updated
        self.qm.on_queue_changed = self._on_queue_changed

        # Populate tree with any pairs already loaded from queue store
        if self.qm.pairs:
            self._refresh_all()

    def _setup_ui(self):
        # Central widget with tabs
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        # ── Tab 1: Browser ──
        try:
            from funpairdl.gui.browser_widget import BrowserWidget
            self.browser = BrowserWidget()
            self.tabs.addTab(self.browser, "Browser")
        except Exception as e:
            logger.warning("Failed to load browser tab: %s", e)
            self.browser = None
            placeholder = QWidget()
            lbl = QLabel(f"Browser unavailable: {e}")
            lbl.setAlignment(Qt.AlignCenter)
            lo = QVBoxLayout(placeholder)
            lo.addWidget(lbl)
            self.tabs.addTab(placeholder, "Browser")

        # ── Tab 2: Downloads ──
        downloads_widget = QWidget()
        dl_layout = QVBoxLayout(downloads_widget)
        dl_layout.setContentsMargins(8, 8, 8, 8)

        # Toolbar
        toolbar = QToolBar()
        toolbar.setMovable(False)

        self.btn_add = QAction("Add Pair", self)
        self.btn_add.triggered.connect(self._on_add_pair)
        toolbar.addAction(self.btn_add)

        self.btn_batch_pixeldrain = QAction("Batch Pixeldrain", self)
        self.btn_batch_pixeldrain.triggered.connect(self._on_batch_pixeldrain)
        toolbar.addAction(self.btn_batch_pixeldrain)

        toolbar.addSeparator()

        self.btn_pause_all = QAction("Pause All", self)
        self.btn_pause_all.triggered.connect(self._on_pause_all)
        toolbar.addAction(self.btn_pause_all)

        self.btn_resume_all = QAction("Resume All", self)
        self.btn_resume_all.triggered.connect(self._on_resume_all)
        toolbar.addAction(self.btn_resume_all)

        toolbar.addSeparator()

        self.btn_clear_done = QAction("Clear Completed", self)
        self.btn_clear_done.triggered.connect(self._on_clear_completed)
        toolbar.addAction(self.btn_clear_done)

        self.btn_restart_pump = QAction("Restart Downloads", self)
        self.btn_restart_pump.triggered.connect(self._on_restart_pump)
        toolbar.addAction(self.btn_restart_pump)

        toolbar.addSeparator()

        self.btn_settings = QAction("Settings", self)
        self.btn_settings.triggered.connect(self._on_settings)
        toolbar.addAction(self.btn_settings)

        dl_layout.addWidget(toolbar)

        # Queue tree
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([
            "Name", "Status", "Progress", "Size", "Speed", "ETA",
        ])
        self.tree.setColumnCount(6)
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)

        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        header.resizeSection(2, 150)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)

        dl_layout.addWidget(self.tree)

        self.tabs.addTab(downloads_widget, "Downloads")

        # Status bar
        self.status_label = QLabel("Ready")
        self.statusBar().addWidget(self.status_label, 1)
        self.quota_label = QLabel("")
        self.quota_label.setStyleSheet("color: #888; margin-right: 12px;")
        self.statusBar().addPermanentWidget(self.quota_label)
        self.speed_label = QLabel("")
        self.statusBar().addPermanentWidget(self.speed_label)

    @staticmethod
    def _load_app_icon() -> QIcon | None:
        """Load the custom app icon from assets/."""
        import os
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        ico_path = os.path.join(base, "assets", "funpairdl.ico")
        png_path = os.path.join(base, "assets", "funpairdl.png")

        if os.path.exists(ico_path):
            return QIcon(ico_path)
        if os.path.exists(png_path):
            return QIcon(png_path)
        return None

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(self)
        if self._app_icon:
            self.tray.setIcon(self._app_icon)
        self.tray.setToolTip("FunPairDL - Download Manager")

        tray_menu = QMenu()

        # Show / Hide window
        self._tray_show_action = tray_menu.addAction("Show Window")
        self._tray_show_action.triggered.connect(self._toggle_window)

        tray_menu.addSeparator()

        # Show / Hide console (only relevant when launched with console)
        self._console_visible = True
        self._tray_console_action = tray_menu.addAction("Hide Console")
        self._tray_console_action.triggered.connect(self._toggle_console)

        tray_menu.addSeparator()

        # Clipboard watcher controls
        self._tray_clipboard_now = tray_menu.addAction("Detect from Clipboard Now")
        self._tray_clipboard_now.triggered.connect(self._on_clipboard_manual_trigger)

        self._tray_clipboard_toggle = tray_menu.addAction("")  # text set in _refresh_clipboard_tray
        self._tray_clipboard_toggle.triggered.connect(self._on_clipboard_toggle_enabled)

        self._tray_clipboard_dnd = tray_menu.addAction("")  # text set in _refresh_clipboard_tray
        self._tray_clipboard_dnd.triggered.connect(self._on_clipboard_toggle_dnd)

        tray_menu.addSeparator()

        quit_action = tray_menu.addAction("Quit")
        quit_action.triggered.connect(self._on_quit)

        self.tray.setContextMenu(tray_menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.messageClicked.connect(self._on_tray_message_clicked)
        self.tray.show()

        # URLs queued by the clipboard watcher waiting for the user to click
        # the tray notification.
        self._pending_clipboard_urls: list[str] = []

        # Auto-hide console if launched with .pyw (no console exists)
        self._has_console = self._detect_console()
        if not self._has_console:
            self._tray_console_action.setVisible(False)

    @staticmethod
    def _detect_console() -> bool:
        """Check if a console window is attached to this process."""
        try:
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            return hwnd != 0
        except Exception:
            return False

    def _toggle_window(self):
        if self.isVisible():
            self.hide()
            self._tray_show_action.setText("Show Window")
        else:
            self.show()
            self.activateWindow()
            self._tray_show_action.setText("Hide Window")

    def _toggle_console(self):
        try:
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd == 0:
                return
            SW_HIDE = 0
            SW_SHOW = 5
            if self._console_visible:
                ctypes.windll.user32.ShowWindow(hwnd, SW_HIDE)
                self._console_visible = False
                self._tray_console_action.setText("Show Console")
            else:
                ctypes.windll.user32.ShowWindow(hwnd, SW_SHOW)
                self._console_visible = True
                self._tray_console_action.setText("Hide Console")
        except Exception as e:
            logger.warning("Failed to toggle console: %s", e)

    def _connect_signals(self):
        self.sig_pair_added.connect(self._refresh_pair)
        self.sig_pair_updated.connect(self._mark_pair_dirty)
        self.sig_item_updated.connect(self._mark_item_dirty)
        self.sig_queue_changed.connect(self._refresh_all)
        self.sig_quota_updated.connect(self._on_quota_text)
        # Flush pending updates immediately when switching to Downloads tab
        self.tabs.currentChanged.connect(self._on_main_tab_changed)

    @Slot(int)
    def _on_main_tab_changed(self, index: int):
        if index == 1:  # Downloads tab
            self._flush_dirty()

    def _setup_refresh_timer(self):
        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self._update_status_bar)
        self.refresh_timer.start(1000)

        # Batched GUI refresh: flush dirty pairs/items every 500ms
        self._gui_flush_timer = QTimer()
        self._gui_flush_timer.timeout.connect(self._flush_dirty)
        self._gui_flush_timer.start(500)

        # Quota refresh: on startup + every 10 minutes
        self._refresh_quotas()
        self.quota_timer = QTimer()
        self.quota_timer.timeout.connect(self._refresh_quotas)
        self.quota_timer.start(600_000)  # 10 min

    # === Batched GUI updates ===

    @Slot(str)
    def _mark_pair_dirty(self, pair_id: str):
        self._dirty_pairs.add(pair_id)

    @Slot(str)
    def _mark_item_dirty(self, item_id: str):
        self._dirty_items.add(item_id)

    @Slot()
    def _flush_dirty(self):
        """Flush accumulated dirty pairs/items in one batch."""
        if not self._dirty_pairs and not self._dirty_items:
            return

        # Skip expensive tree updates when Downloads tab is not visible.
        # Dirty items accumulate and flush when the user switches to Downloads.
        if self.tabs.currentIndex() != 1:
            return

        # Process dirty items first (cheaper per-row updates)
        for item_id in self._dirty_items:
            loc = self._item_lookup.get(item_id)
            if not loc:
                continue
            pair_id, child_idx = loc
            tree_item = self._pair_items.get(pair_id)
            if not tree_item or child_idx >= tree_item.childCount():
                continue
            pair = self.qm._find_pair(pair_id)
            if not pair:
                continue
            for pi in pair.items:
                if pi.id == item_id:
                    self._update_item_row(tree_item.child(child_idx), pi)
                    break

        # Process dirty pairs (full row + children refresh)
        for pair_id in self._dirty_pairs:
            self._refresh_pair(pair_id)

        self._dirty_items.clear()
        self._dirty_pairs.clear()

    # === QueueManager callbacks (called from async context) ===

    def _on_pair_added(self, pair: Pair):
        self.sig_pair_added.emit(pair.id)

    def _on_pair_updated(self, pair: Pair):
        self.sig_pair_updated.emit(pair.id)

    def _on_item_updated(self, item: PairItem):
        self.sig_item_updated.emit(item.id)

    def _on_queue_changed(self):
        self.sig_queue_changed.emit()

    # === GUI slots ===

    @Slot(str)
    def _refresh_pair(self, pair_id: str):
        pair = self.qm._find_pair(pair_id)
        if not pair:
            return

        if pair_id not in self._pair_items:
            # Create new tree item at top (newest first)
            item = QTreeWidgetItem()
            self.tree.insertTopLevelItem(0, item)
            self._pair_items[pair_id] = item
            # Add child items for each file
            for pi in pair.items:
                child = QTreeWidgetItem(item)
                child.setData(0, Qt.UserRole, pi.id)

        tree_item = self._pair_items[pair_id]
        tree_item.setData(0, Qt.UserRole, pair_id)

        # Update pair row
        tree_item.setText(0, pair.name)
        tree_item.setText(1, pair.state.value.upper())
        if pair.total_bytes > 0:
            tree_item.setText(2, f"{pair.progress:.1f}%")
            tree_item.setText(3, format_size(pair.total_bytes))
            remaining = pair.total_bytes - pair.downloaded_bytes
            tree_item.setText(5, format_eta(remaining, pair.speed_bps))
        elif pair.downloaded_bytes > 0:
            tree_item.setText(2, format_size(pair.downloaded_bytes))
            tree_item.setText(3, "??")
            tree_item.setText(5, "--")
        else:
            tree_item.setText(2, "")
            tree_item.setText(3, "")
            tree_item.setText(5, "")
        tree_item.setText(4, format_speed(pair.speed_bps) if pair.speed_bps else "")

        # Color by state
        from PySide6.QtGui import QColor
        state_colors = {
            PairState.QUEUED: "#888",
            PairState.DOWNLOADING: "#4a90d9",
            PairState.PAUSED: "#f0ad4e",
            PairState.COMPLETED: "#28a745",
            PairState.FAILED: "#dc3545",
        }
        color = state_colors.get(pair.state, "#888")
        for col in range(6):
            tree_item.setForeground(col, QColor(color))

        # Update children and build lookup index
        for idx, pi in enumerate(pair.items):
            if idx < tree_item.childCount():
                child = tree_item.child(idx)
            else:
                child = QTreeWidgetItem(tree_item)
                child.setData(0, Qt.UserRole, pi.id)
            self._item_lookup[pi.id] = (pair_id, idx)
            self._update_item_row(child, pi)

    def _update_item_row(self, tree_item: QTreeWidgetItem, item: PairItem):
        type_icon = "V" if item.file_type == FileType.VIDEO else "S"
        tree_item.setText(0, f"[{type_icon}] {item.filename}")
        tree_item.setText(1, item.state.value)
        if item.total_bytes > 0:
            tree_item.setText(2, f"{item.progress:.1f}%")
            tree_item.setText(3, format_size(item.total_bytes))
            remaining = item.total_bytes - item.downloaded_bytes
            tree_item.setText(5, format_eta(remaining, item.speed_bps))
        elif item.downloaded_bytes > 0:
            tree_item.setText(2, format_size(item.downloaded_bytes))
            tree_item.setText(3, "??")
            tree_item.setText(5, "--")
        else:
            tree_item.setText(2, "")
            tree_item.setText(3, "")
            tree_item.setText(5, "")
        tree_item.setText(4, format_speed(item.speed_bps) if item.speed_bps else "")

    @Slot()
    def _refresh_all(self):
        self.tree.clear()
        self._pair_items.clear()
        self._item_lookup.clear()
        # pairs list is oldest-first; inserting each at position 0 reverses the order
        for pair in self.qm.pairs:
            self._refresh_pair(pair.id)

    def _update_status_bar(self):
        total_pairs = len(self.qm.pairs)
        active = sum(1 for p in self.qm.pairs if p.state == PairState.DOWNLOADING)
        completed = sum(1 for p in self.qm.pairs if p.state == PairState.COMPLETED)

        self.status_label.setText(
            f"Queue: {total_pairs} pairs | Active: {active} | Completed: {completed}"
        )

        total_speed = sum(p.speed_bps for p in self.qm.pairs if p.state == PairState.DOWNLOADING)
        if total_speed > 0:
            self.speed_label.setText(format_speed(total_speed))
        else:
            self.speed_label.setText("")

    # === Actions ===

    def _on_add_pair(self):
        dialog = AddPairDialog(self)
        if dialog.exec():
            data = dialog.get_data()
            self.qm.add_pair(
                name=data["name"],
                video_urls=data["video_urls"],
                script_urls=data["script_urls"],
            )

    def _on_batch_pixeldrain(self):
        self._open_pixeldrain_picker(initial_urls=None)

    # === Clipboard Watcher integration ===

    def _setup_clipboard_watcher(self):
        # Build the per-call set of URLs already known to the queue. The
        # watcher uses this to avoid re-prompting for in-flight downloads.
        def queue_urls() -> set[str]:
            seen: set[str] = set()
            for pair in self.qm.pairs:
                for it in pair.items:
                    if it.url:
                        seen.add(it.url)
            return seen

        self.clipboard_watcher = ClipboardWatcher(self.settings, queue_urls, self)
        self.clipboard_watcher.urls_detected.connect(self._on_clipboard_urls)
        self.clipboard_watcher.duplicate_in_queue.connect(self._on_clipboard_duplicates)
        self.clipboard_watcher.start()
        self._refresh_clipboard_tray()

    def _refresh_clipboard_tray(self):
        s = self.settings
        self._tray_clipboard_toggle.setText(
            "Disable Clipboard Watcher" if s.clipboard_watch_enabled
            else "Enable Clipboard Watcher"
        )
        self._tray_clipboard_dnd.setText(
            "Disable Do-Not-Disturb" if s.clipboard_dnd_enabled
            else "Enable Do-Not-Disturb"
        )
        self._tray_clipboard_now.setEnabled(s.clipboard_watch_enabled)

    def _on_clipboard_toggle_enabled(self):
        self.settings.clipboard_watch_enabled = not self.settings.clipboard_watch_enabled
        self.settings.save()
        self.clipboard_watcher.refresh_settings(self.settings)
        self._refresh_clipboard_tray()

    def _on_clipboard_toggle_dnd(self):
        self.settings.clipboard_dnd_enabled = not self.settings.clipboard_dnd_enabled
        self.settings.save()
        self.clipboard_watcher.refresh_settings(self.settings)
        self._refresh_clipboard_tray()

    def _on_clipboard_manual_trigger(self):
        self.clipboard_watcher.trigger_manual_check()

    @Slot(list)
    def _on_clipboard_urls(self, urls: list):
        if not urls:
            return
        # Tray notification
        if self.settings.clipboard_notify_tray and self.tray.isVisible():
            label = "Pixeldrain link detected" if len(urls) == 1 \
                else f"{len(urls)} Pixeldrain links detected"
            self.tray.showMessage(label, "Click to open the picker.",
                                  QSystemTrayIcon.Information, 5000)
            # Cache so the next tray click opens with these URLs
            self._pending_clipboard_urls = list(urls)
        else:
            self._pending_clipboard_urls = list(urls)

        # Flash taskbar (non-stealing)
        if self.settings.clipboard_notify_flash and not self.isActiveWindow():
            QApplication.alert(self, 0)

        # If neither notification path is enabled, open the picker directly
        # to avoid the watcher being silently useless.
        if not self.settings.clipboard_notify_tray and not self.settings.clipboard_notify_flash:
            self._open_pixeldrain_picker(initial_urls=urls)

    @Slot(list)
    def _on_clipboard_duplicates(self, urls: list):
        if not self.tray.isVisible() or not self.settings.clipboard_notify_tray:
            return
        self.tray.showMessage(
            "Pixeldrain link skipped",
            f"{len(urls)} link(s) already in the queue.",
            QSystemTrayIcon.Information, 3000,
        )

    def _open_pixeldrain_picker(self, initial_urls=None):
        dlg = PixeldrainPickerDialog(self.settings, initial_urls=initial_urls, parent=self)
        accepted = dlg.exec()
        logger.info("Picker closed (accepted=%s)", bool(accepted))
        if accepted:
            results = dlg.picker_results()
            logger.info("Picker returned %d PickerResult(s)", len(results))
            for result in results:
                logger.info("Adding pair %r: %d videos, %d scripts, auto_rename=%s",
                            result.name, len(result.video_urls), len(result.script_urls),
                            result.auto_rename)
                self.qm.add_pair(
                    name=result.name,
                    video_urls=result.video_urls,
                    script_urls=result.script_urls,
                    auto_rename=result.auto_rename,
                    output_dir_override=result.output_dir_override,
                )
        self._pending_clipboard_urls = []

    def _on_pause_all(self):
        # Pause everything that could otherwise pick up next:
        # in-flight downloads AND queued ones waiting in line.
        for pair in self.qm.pairs:
            if pair.state in (PairState.DOWNLOADING, PairState.QUEUED):
                self.qm.pause_pair(pair.id)

    def _on_resume_all(self):
        for pair in self.qm.pairs:
            if pair.state in (PairState.PAUSED, PairState.FAILED):
                self.qm.resume_pair(pair.id)

    def _on_clear_completed(self):
        completed_ids = [p.id for p in self.qm.pairs if p.state == PairState.COMPLETED]
        for pid in completed_ids:
            self.qm.remove_pair(pid)
            if pid in self._pair_items:
                idx = self.tree.indexOfTopLevelItem(self._pair_items[pid])
                if idx >= 0:
                    self.tree.takeTopLevelItem(idx)
                del self._pair_items[pid]

    def _on_restart_pump(self):
        """Force-restart the download pump when downloads appear stuck."""
        asyncio.ensure_future(self.qm.force_restart_pump())

    def _on_settings(self):
        from funpairdl.gui.settings_dialog import SettingsDialog
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec():
            self.settings = dialog.get_settings()
            self.settings.save()
            # Invalidate cached registry so new credentials take effect
            self.qm._registry = None
            # Apply clipboard-watcher changes immediately
            if hasattr(self, "clipboard_watcher"):
                self.clipboard_watcher.refresh_settings(self.settings)
                self._refresh_clipboard_tray()

    def _on_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if not item:
            return

        pair_id = item.data(0, Qt.UserRole)
        pair = self.qm._find_pair(pair_id)
        if not pair:
            # Might be a child item, get parent
            parent = item.parent()
            if parent:
                pair_id = parent.data(0, Qt.UserRole)
                pair = self.qm._find_pair(pair_id)
        if not pair:
            return

        menu = QMenu(self.tree)

        if pair.state == PairState.DOWNLOADING:
            pause_action = menu.addAction("Pause")
            pause_action.triggered.connect(lambda: self.qm.pause_pair(pair_id))
        elif pair.state in (PairState.PAUSED, PairState.FAILED):
            resume_action = menu.addAction("Resume")
            resume_action.triggered.connect(lambda: self.qm.resume_pair(pair_id))
        elif pair.state == PairState.QUEUED:
            move_up = menu.addAction("Move Up")
            move_up.triggered.connect(lambda: self.qm.move_pair(pair_id, -1))
            move_down = menu.addAction("Move Down")
            move_down.triggered.connect(lambda: self.qm.move_pair(pair_id, 1))
        elif pair.state == PairState.COMPLETED:
            if pair.organized:
                reorg_action = menu.addAction("Re-organize")
                reorg_action.setToolTip(
                    "Undo current organize then run it again with the latest "
                    "logic (e.g. to add sibling-funscript hardlinks)."
                )
                reorg_action.triggered.connect(lambda: self._reorganize_pair(pair_id))
                undo_action = menu.addAction("Undo Rename")
                undo_action.triggered.connect(lambda: self.qm.undo_organize_pair(pair_id))
            else:
                org_action = menu.addAction("Rename Files")
                org_action.triggered.connect(lambda: self.qm.organize_pair(pair_id))

        menu.addSeparator()

        # Open output folder
        if pair.output_dir:
            open_folder = menu.addAction("Open Folder")
            open_folder.triggered.connect(lambda: self._open_folder(pair.output_dir))

        remove_action = menu.addAction("Remove")
        remove_action.triggered.connect(lambda: self._remove_pair(pair_id))

        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _reorganize_pair(self, pair_id: str):
        """Undo + re-run organize so newer organize logic (e.g. sibling
        funscript hardlinks for extra videos) gets applied to a Pair
        that was organized before the upgrade."""
        ok = self.qm.undo_organize_pair(pair_id)
        if not ok:
            logger.warning("Re-organize: undo failed for %s", pair_id)
            return
        self.qm.organize_pair(pair_id)

    def _open_folder(self, path: str):
        import subprocess
        try:
            subprocess.Popen(["explorer", path.replace("/", "\\")])
        except Exception:
            pass

    def _remove_pair(self, pair_id: str):
        self.qm.remove_pair(pair_id)
        if pair_id in self._pair_items:
            idx = self.tree.indexOfTopLevelItem(self._pair_items[pair_id])
            if idx >= 0:
                self.tree.takeTopLevelItem(idx)
            del self._pair_items[pair_id]

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show()
            self.activateWindow()

    def _on_tray_message_clicked(self):
        # Triggered when the user clicks the balloon notification body
        if self._pending_clipboard_urls:
            urls = self._pending_clipboard_urls
            self._pending_clipboard_urls = []
            self.show()
            self.activateWindow()
            self._open_pixeldrain_picker(initial_urls=urls)

    def _on_quit(self):
        """Quit gracefully, with a force-kill fallback if Qt event loop is stuck."""
        import os
        # Save browser session before quitting
        try:
            if hasattr(self, "browser"):
                self.browser.save_session()
                # Close all tabs so QWebEnginePage destructors flush cookies to disk
                self.browser.close_all_tabs()
        except Exception as e:
            logger.warning("Failed to save browser session: %s", e)
        QApplication.quit()
        # Give Chromium enough time to flush cookies before force exit
        QTimer.singleShot(5000, lambda: os._exit(0))

    # === Account quota ===

    def _refresh_quotas(self):
        """Kick off async quota queries for all configured providers."""
        asyncio.ensure_future(self._fetch_quotas())

    async def _fetch_quotas(self):
        """Query all provider quotas and update the status bar."""
        from funpairdl.core.progress import format_size
        from funpairdl.utils.account_quota import (
            query_gofile_quota,
            query_mega_quota,
            query_pixeldrain_quota,
        )

        settings = Settings.load()
        parts = []

        # Run queries in parallel
        import asyncio as _aio
        mega_task = _aio.create_task(query_mega_quota(settings.mega_sid))
        pd_task = _aio.create_task(query_pixeldrain_quota(settings.pixeldrain_api_key))
        gf_task = _aio.create_task(query_gofile_quota(settings.gofile_token))
        mega, pd, gf = await _aio.gather(mega_task, pd_task, gf_task)

        if mega:
            used = format_size(mega["transfer_used"])
            total = format_size(mega["transfer_total"])
            parts.append(f"MEGA {mega['tier']}: {used}/{total}")

        if pd:
            if pd["bandwidth_total"] > 0:
                used = format_size(pd["bandwidth_used"])
                total = format_size(pd["bandwidth_total"])
                parts.append(f"PD {pd['tier']}: {used}/{total}")
            else:
                parts.append(f"PD: {pd['tier']}")

        if gf:
            parts.append(f"GoFile: {gf['tier']}")

        text = "  |  ".join(parts) if parts else ""
        self.sig_quota_updated.emit(text)

    @Slot(str)
    def _on_quota_text(self, text: str):
        self.quota_label.setText(text)
        self.quota_label.setToolTip(text.replace("  |  ", "\n") if text else "No premium accounts configured")

    def closeEvent(self, event):
        if self.settings.minimize_to_tray:
            event.ignore()
            self.hide()
            self.tray.showMessage(
                "FunPairDL",
                "Minimized to tray. Downloads continue in background.",
                QSystemTrayIcon.Information,
                2000,
            )
        else:
            event.accept()
