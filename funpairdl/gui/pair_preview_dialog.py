from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from funpairdl.core.pairing import Confidence, Group
from funpairdl.core.progress import format_size

CONFIDENCE_BADGE = {
    Confidence.HIGH: ("OK", "#2a8"),
    Confidence.MEDIUM: ("CHECK", "#c80"),
    Confidence.LOW: ("ORPHAN", "#a55"),
}


class PairPreviewDialog(QDialog):
    """Shows the user how their picker selection will be split into Pairs.

    Only confirms or cancels. The 'Edit groups' button is a placeholder
    for a future drag-and-drop reorganisation step (see Step B in the
    development plan)."""

    def __init__(self, groups: list[Group], rename_mode: str, parent=None):
        super().__init__(parent)
        self.groups = groups
        self.rename_mode = rename_mode
        self._never_show_again = False

        self.setWindowTitle("Confirm Pair grouping")
        self.setMinimumSize(720, 480)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        n_total = len(self.groups)
        n_high = sum(1 for g in self.groups if g.confidence == Confidence.HIGH)
        n_med = sum(1 for g in self.groups if g.confidence == Confidence.MEDIUM)
        n_low = sum(1 for g in self.groups if g.confidence == Confidence.LOW)

        header = QLabel(
            f"Will create <b>{n_total}</b> Pair(s):  "
            f"<span style='color:#2a8'>{n_high} confident</span>, "
            f"<span style='color:#c80'>{n_med} need review</span>, "
            f"<span style='color:#a55'>{n_low} orphan</span>."
        )
        layout.addWidget(header)

        rename_label = {
            "off": "Auto-rename: <b>Off</b>",
            "video_first": "Auto-rename: <b>Video name → Script</b> (orphans skipped)",
            "script_first": "Auto-rename: <b>Script name → Video</b> (orphans skipped)",
        }.get(self.rename_mode, f"Auto-rename: {self.rename_mode}")
        layout.addWidget(QLabel(rename_label))

        # Tree of groups
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Pair / file", "Kind", "Size", "Note"])
        self.tree.setColumnWidth(0, 380)
        self.tree.setColumnWidth(1, 70)
        self.tree.setColumnWidth(2, 100)
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self._populate_tree()
        self.tree.expandAll()
        layout.addWidget(self.tree, 1)

        # Footer
        bottom_row = QHBoxLayout()
        self.cb_skip_next = QCheckBox(
            "Skip this preview next time when every group is confident"
        )
        bottom_row.addWidget(self.cb_skip_next)
        bottom_row.addStretch(1)

        self.btn_edit = QPushButton("Edit groups…")
        self.btn_edit.setEnabled(False)
        self.btn_edit.setToolTip("Manual regroup is not implemented yet — coming in a future update.")
        bottom_row.addWidget(self.btn_edit)
        layout.addLayout(bottom_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Confirm & Add to Queue")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _populate_tree(self):
        for g in self.groups:
            badge, color = CONFIDENCE_BADGE[g.confidence]
            total_size = sum(c.size for c in (*g.videos, *g.scripts, *g.others))
            summary = (
                f"{len(g.videos)} video, {len(g.scripts)} script"
                + (f", {len(g.others)} other" if g.others else "")
            )
            head = QTreeWidgetItem([
                f"[{badge}]  {g.name}",
                summary,
                format_size(total_size) if total_size else "",
                g.note,
            ])
            head.setForeground(0, QBrush(QColor(color)))
            font = head.font(0)
            font.setBold(True)
            head.setFont(0, font)
            self.tree.addTopLevelItem(head)

            for c in g.videos:
                head.addChild(self._file_item(c.name, "video", c.size))
            for c in g.scripts:
                head.addChild(self._file_item(c.name, "script", c.size))
            for c in g.others:
                head.addChild(self._file_item(c.name, "other", c.size))

    @staticmethod
    def _file_item(name: str, kind: str, size: int) -> QTreeWidgetItem:
        return QTreeWidgetItem([
            f"  {name}",
            kind,
            format_size(size) if size else "",
            "",
        ])

    def accept(self):
        self._never_show_again = self.cb_skip_next.isChecked()
        super().accept()

    @property
    def remember_skip(self) -> bool:
        return self._never_show_again
