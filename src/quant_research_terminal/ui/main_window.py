from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

PAGE_NAMES = ("Dashboard", "Data", "Strategies", "Backtests", "Experiments", "Settings")


class PlaceholderPage(QWidget):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setObjectName("pageHeading")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(heading)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Quant Research Terminal")
        self.resize(1100, 700)

        root = QWidget(self)
        layout = QHBoxLayout(root)
        self.navigation = QListWidget(root)
        self.navigation.setObjectName("navigation")
        self.navigation.setFixedWidth(190)
        self.pages = QStackedWidget(root)
        self.pages.setObjectName("pages")

        for name in PAGE_NAMES:
            self.navigation.addItem(QListWidgetItem(name))
            self.pages.addWidget(PlaceholderPage(name, self.pages))

        self.navigation.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.navigation.setCurrentRow(0)
        layout.addWidget(self.navigation)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(root)
