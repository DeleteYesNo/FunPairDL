from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from funpairdl.persistence.settings import Settings


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Settings")
        self.setMinimumWidth(450)
        self._setup_ui()

    def _setup_ui(self):
        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)

        # ── General tab ─────────────────────────────────────────────
        general = QWidget()
        layout = QVBoxLayout(general)
        form = QFormLayout()

        # Download directory
        dir_layout = QHBoxLayout()
        self.dir_edit = QLineEdit(self.settings.download_dir)
        dir_layout.addWidget(self.dir_edit)
        btn_browse = QPushButton("Browse")
        btn_browse.clicked.connect(self._browse_dir)
        dir_layout.addWidget(btn_browse)
        form.addRow("Download directory:", dir_layout)

        # Max segments
        self.segments_spin = QSpinBox()
        self.segments_spin.setRange(1, 32)
        self.segments_spin.setValue(self.settings.max_segments)
        self.segments_spin.setToolTip(
            "Parallel chunks per file. MEGA throttles each connection, so more "
            "segments = proportionally faster (≈0.25 MB/s per segment). 32 is a "
            "good MEGA value; MEGA files download one at a time to stay under "
            "its connection-reset threshold."
        )
        form.addRow("Max segments per file:", self.segments_spin)

        # Max concurrent pairs
        self.concurrent_pairs_spin = QSpinBox()
        self.concurrent_pairs_spin.setRange(1, 8)
        self.concurrent_pairs_spin.setValue(self.settings.max_concurrent_pairs)
        self.concurrent_pairs_spin.setToolTip("How many posts download simultaneously")
        form.addRow("Max concurrent downloads:", self.concurrent_pairs_spin)

        # API port
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(self.settings.api_port)
        form.addRow("API server port:", self.port_spin)

        # Minimize to tray
        self.tray_check = QCheckBox()
        self.tray_check.setChecked(self.settings.minimize_to_tray)
        form.addRow("Minimize to tray:", self.tray_check)

        # Browser for cookies
        self.browser_edit = QLineEdit(self.settings.cookies_from_browser)
        form.addRow("Browser for cookies:", self.browser_edit)

        # Default resolution
        self.resolution_combo = QComboBox()
        resolutions = ["best", "2160", "1080", "720", "480", "360"]
        self.resolution_combo.addItems(resolutions)
        idx = self.resolution_combo.findText(self.settings.default_resolution)
        if idx >= 0:
            self.resolution_combo.setCurrentIndex(idx)
        form.addRow("Default resolution:", self.resolution_combo)

        # Pixeldrain API key
        self.pd_key_edit = QLineEdit(self.settings.pixeldrain_api_key)
        self.pd_key_edit.setEchoMode(QLineEdit.Password)
        form.addRow("Pixeldrain API key:", self.pd_key_edit)

        # GoFile token
        self.gofile_token_edit = QLineEdit(self.settings.gofile_token)
        self.gofile_token_edit.setEchoMode(QLineEdit.Password)
        form.addRow("GoFile API token:", self.gofile_token_edit)

        # EroScripts auto-login
        self.ero_user_edit = QLineEdit(self.settings.eroscripts_username)
        self.ero_user_edit.setPlaceholderText("(optional — for auto re-login)")
        form.addRow("EroScripts username:", self.ero_user_edit)

        self.ero_pass_edit = QLineEdit(self.settings.eroscripts_password)
        self.ero_pass_edit.setEchoMode(QLineEdit.Password)
        self.ero_pass_edit.setPlaceholderText("(optional — for auto re-login)")
        form.addRow("EroScripts password:", self.ero_pass_edit)

        # MEGA credentials
        self.mega_email_edit = QLineEdit(self.settings.mega_email)
        form.addRow("MEGA email:", self.mega_email_edit)

        self.mega_pass_edit = QLineEdit(self.settings.mega_password)
        self.mega_pass_edit.setEchoMode(QLineEdit.Password)
        form.addRow("MEGA password:", self.mega_pass_edit)

        layout.addLayout(form)
        self.tabs.addTab(general, "General")

        # ── Clipboard tab ───────────────────────────────────────────
        self.tabs.addTab(self._build_clipboard_tab(), "Clipboard")

        # Dialog buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _build_clipboard_tab(self) -> QWidget:
        s = self.settings
        w = QWidget()
        v = QVBoxLayout(w)

        self.clip_enabled = QCheckBox("Watch clipboard for download links")
        self.clip_enabled.setChecked(s.clipboard_watch_enabled)
        v.addWidget(self.clip_enabled)

        self.clip_dnd = QCheckBox("Do-Not-Disturb (suppress all clipboard prompts)")
        self.clip_dnd.setChecked(s.clipboard_dnd_enabled)
        v.addWidget(self.clip_dnd)

        # Domains
        domains_box = QGroupBox("Watched domains")
        dv = QVBoxLayout(domains_box)
        self.clip_domain_pixeldrain = QCheckBox("pixeldrain.com")
        self.clip_domain_pixeldrain.setChecked(
            "pixeldrain.com" in (s.clipboard_watch_domains or [])
        )
        dv.addWidget(self.clip_domain_pixeldrain)
        # Future domains can be added here as additional checkboxes.
        v.addWidget(domains_box)

        # Notification mode (independent toggles)
        notify_box = QGroupBox("Notification")
        nv = QVBoxLayout(notify_box)
        self.clip_notify_tray = QCheckBox("Show tray notification (click to open picker)")
        self.clip_notify_tray.setChecked(s.clipboard_notify_tray)
        nv.addWidget(self.clip_notify_tray)
        self.clip_notify_flash = QCheckBox("Flash taskbar icon (does not steal focus)")
        self.clip_notify_flash.setChecked(s.clipboard_notify_flash)
        nv.addWidget(self.clip_notify_flash)
        nv.addWidget(QLabel(
            "If both are off, the picker opens immediately when a link is detected."
        ))
        v.addWidget(notify_box)

        # Behaviour
        beh = QFormLayout()
        self.clip_dedupe = QSpinBox()
        self.clip_dedupe.setRange(0, 600)
        self.clip_dedupe.setSuffix(" s")
        self.clip_dedupe.setValue(s.clipboard_dedupe_seconds)
        self.clip_dedupe.setToolTip("Same URL within this window is ignored. 0 = always prompt.")
        beh.addRow("Ignore repeats within:", self.clip_dedupe)

        self.clip_skip_in_queue = QCheckBox()
        self.clip_skip_in_queue.setChecked(s.clipboard_skip_in_queue)
        beh.addRow("Skip URLs already in queue:", self.clip_skip_in_queue)
        v.addLayout(beh)

        v.addStretch(1)
        return w

    def _browse_dir(self):
        dir_path = QFileDialog.getExistingDirectory(
            self, "Select Download Directory", self.dir_edit.text()
        )
        if dir_path:
            self.dir_edit.setText(dir_path)

    def get_settings(self) -> Settings:
        # Mutate the existing settings object instead of constructing a new
        # one so unrelated fields (cookies, browser tabs, picker prefs, etc.)
        # are preserved.
        s = self.settings
        s.download_dir = self.dir_edit.text()
        s.max_segments = self.segments_spin.value()
        s.max_concurrent_pairs = self.concurrent_pairs_spin.value()
        s.api_port = self.port_spin.value()
        s.minimize_to_tray = self.tray_check.isChecked()
        s.cookies_from_browser = self.browser_edit.text()
        s.default_resolution = self.resolution_combo.currentText()
        s.pixeldrain_api_key = self.pd_key_edit.text()
        s.gofile_token = self.gofile_token_edit.text()
        s.eroscripts_username = self.ero_user_edit.text()
        s.eroscripts_password = self.ero_pass_edit.text()
        s.mega_email = self.mega_email_edit.text()
        s.mega_password = self.mega_pass_edit.text()

        # Clipboard tab
        s.clipboard_watch_enabled = self.clip_enabled.isChecked()
        s.clipboard_dnd_enabled = self.clip_dnd.isChecked()
        domains = []
        if self.clip_domain_pixeldrain.isChecked():
            domains.append("pixeldrain.com")
        s.clipboard_watch_domains = domains
        s.clipboard_notify_tray = self.clip_notify_tray.isChecked()
        s.clipboard_notify_flash = self.clip_notify_flash.isChecked()
        s.clipboard_dedupe_seconds = self.clip_dedupe.value()
        s.clipboard_skip_in_queue = self.clip_skip_in_queue.isChecked()
        return s
