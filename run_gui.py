# -*- coding: utf-8 -*-
"""GUI 入口：python run_gui.py"""
import sys

from PySide6.QtWidgets import QApplication

from galtools.gui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
