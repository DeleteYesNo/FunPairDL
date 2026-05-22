from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
)


class AddPairDialog(QDialog):
    """Dialog for manually adding a video+script pair."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Download Pair")
        self.setMinimumWidth(500)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # Name
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Pair name (e.g. video title)")
        form.addRow("Name:", self.name_edit)
        layout.addLayout(form)

        # Video URLs
        layout.addWidget(QLabel("Video URLs:"))
        self.video_list = QListWidget()
        layout.addWidget(self.video_list)

        video_btns = QHBoxLayout()
        self.video_url_edit = QLineEdit()
        self.video_url_edit.setPlaceholderText("Paste video URL here")
        video_btns.addWidget(self.video_url_edit)
        btn_add_video = QPushButton("Add")
        btn_add_video.clicked.connect(self._add_video_url)
        video_btns.addWidget(btn_add_video)
        btn_rm_video = QPushButton("Remove")
        btn_rm_video.clicked.connect(lambda: self._remove_selected(self.video_list))
        video_btns.addWidget(btn_rm_video)
        layout.addLayout(video_btns)

        # Script URLs
        layout.addWidget(QLabel("Script URLs (.funscript):"))
        self.script_list = QListWidget()
        layout.addWidget(self.script_list)

        script_btns = QHBoxLayout()
        self.script_url_edit = QLineEdit()
        self.script_url_edit.setPlaceholderText("Paste .funscript URL here")
        script_btns.addWidget(self.script_url_edit)
        btn_add_script = QPushButton("Add")
        btn_add_script.clicked.connect(self._add_script_url)
        script_btns.addWidget(btn_add_script)
        btn_rm_script = QPushButton("Remove")
        btn_rm_script.clicked.connect(lambda: self._remove_selected(self.script_list))
        script_btns.addWidget(btn_rm_script)
        layout.addLayout(script_btns)

        # Dialog buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add_video_url(self):
        url = self.video_url_edit.text().strip()
        if url:
            self.video_list.addItem(url)
            self.video_url_edit.clear()

    def _add_script_url(self):
        url = self.script_url_edit.text().strip()
        if url:
            self.script_list.addItem(url)
            self.script_url_edit.clear()

    def _remove_selected(self, list_widget: QListWidget):
        for item in list_widget.selectedItems():
            list_widget.takeItem(list_widget.row(item))

    def get_data(self) -> dict:
        return {
            "name": self.name_edit.text().strip() or "Untitled",
            "video_urls": [
                self.video_list.item(i).text()
                for i in range(self.video_list.count())
            ],
            "script_urls": [
                self.script_list.item(i).text()
                for i in range(self.script_list.count())
            ],
        }
