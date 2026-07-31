from PySide6.QtWidgets import QApplication

from quant_research_terminal.app import create_application
from quant_research_terminal.ui.main_window import PAGE_NAMES, MainWindow


def test_application_can_be_created() -> None:
    application = create_application(["quant-research-terminal-test"])
    assert isinstance(application, QApplication)
    assert create_application([]) is application


def test_main_window_contains_all_phase_zero_pages() -> None:
    create_application(["quant-research-terminal-test"])
    window = MainWindow()
    labels = tuple(window.navigation.item(index).text() for index in range(window.navigation.count()))
    assert labels == PAGE_NAMES
    assert window.pages.count() == len(PAGE_NAMES)
    assert window.pages.currentIndex() == 0
