from __future__ import annotations

import sys
from collections.abc import Sequence

from PySide6.QtWidgets import QApplication

from quant_research_terminal.ui.main_window import MainWindow


def create_application(argv: Sequence[str] | None = None) -> QApplication:
    """Return the process-wide Qt application, creating it when necessary."""
    existing = QApplication.instance()
    if existing is not None:
        return existing
    return QApplication(list(argv) if argv is not None else sys.argv)


def main(argv: Sequence[str] | None = None) -> int:
    application = create_application(argv)
    window = MainWindow()
    window.show()
    return application.exec()

