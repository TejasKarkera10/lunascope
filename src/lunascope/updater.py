import sys
import subprocess
import urllib.request
import urllib.error
import json

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout,
)

_PYPI_URL = "https://pypi.org/pypi/lunascope/json"


class _VersionCheckWorker(QThread):
    """Fetches the latest PyPI version silently in the background."""
    update_available = Signal(str)  # emits latest version string if newer

    def __init__(self, current_version: str, parent=None):
        super().__init__(parent)
        self._current = current_version

    def run(self):
        try:
            latest = _fetch_latest_version()
            is_newer = (
                tuple(int(x) for x in latest.split("."))
                > tuple(int(x) for x in self._current.split("."))
            )
            if is_newer:
                self.update_available.emit(latest)
        except Exception:
            pass


def start_background_check(current_version: str, on_update_available) -> _VersionCheckWorker:
    """Start a background PyPI check; calls on_update_available(latest) if newer."""
    worker = _VersionCheckWorker(current_version)
    worker.update_available.connect(on_update_available)
    worker.start()
    return worker


def _fetch_latest_version() -> str:
    """Return the latest lunascope version string from PyPI, or raise."""
    req = urllib.request.Request(_PYPI_URL, headers={"User-Agent": "lunascope-updater"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    return data["info"]["version"]


class _PipWorker(QThread):
    output = Signal(str)
    finished = Signal(bool, str)  # success, message

    def run(self):
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "pip", "install", "--upgrade", "lunascope"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for line in proc.stdout:
                self.output.emit(line.rstrip())
            proc.wait()
            if proc.returncode == 0:
                self.finished.emit(True, "Update complete.")
            else:
                self.finished.emit(False, f"pip exited with code {proc.returncode}.")
        except Exception as exc:
            self.finished.emit(False, str(exc))


class _UpdateDialog(QDialog):
    def __init__(self, current: str, latest: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Update Lunascope")
        self.setMinimumWidth(480)
        self._worker = None

        layout = QVBoxLayout(self)

        self._label = QLabel(
            f"<b>v{latest}</b> is available &nbsp;(you have v{current}).<br>"
            "Do you want to update now?"
        )
        self._label.setTextFormat(Qt.RichText)
        layout.addWidget(self._label)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(2000)
        self._log.hide()
        layout.addWidget(self._log)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # indeterminate
        self._progress.hide()
        layout.addWidget(self._progress)

        self._buttons = QDialogButtonBox()
        self._update_btn = self._buttons.addButton("Update", QDialogButtonBox.AcceptRole)
        self._cancel_btn = self._buttons.addButton("Cancel", QDialogButtonBox.RejectRole)
        self._update_btn.clicked.connect(self._run_update)
        self._cancel_btn.clicked.connect(self.reject)
        layout.addWidget(self._buttons)

    def _run_update(self):
        self._update_btn.setEnabled(False)
        self._cancel_btn.setEnabled(False)
        self._log.show()
        self._progress.show()
        self.adjustSize()

        self._worker = _PipWorker()
        self._worker.output.connect(self._append_log)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _append_log(self, line: str):
        self._log.appendPlainText(line)
        self._log.verticalScrollBar().setValue(self._log.verticalScrollBar().maximum())

    def _on_finished(self, success: bool, message: str):
        self._progress.hide()
        if success:
            self._label.setText(
                f"<b>{message}</b><br>Please restart Lunascope to use the new version."
            )
            restart_btn = QPushButton("Restart Now")
            restart_btn.clicked.connect(self._restart)
            self._buttons.addButton(restart_btn, QDialogButtonBox.ActionRole)
            self._cancel_btn.setText("Later")
            self._cancel_btn.setEnabled(True)
        else:
            self._label.setText(f"<b>Update failed:</b> {message}")
            self._cancel_btn.setText("Close")
            self._cancel_btn.setEnabled(True)

    def _restart(self):
        self.accept()
        subprocess.Popen([sys.executable] + sys.argv)
        sys.exit(0)


def check_and_prompt(current_version: str, parent=None) -> None:
    """Fetch latest version from PyPI and show update dialog if one is available."""
    try:
        latest = _fetch_latest_version()
    except urllib.error.URLError:
        QMessageBox.warning(
            parent,
            "Update Check Failed",
            "Could not reach PyPI. Please check your internet connection.",
        )
        return
    except Exception as exc:
        QMessageBox.warning(parent, "Update Check Failed", str(exc))
        return

    try:
        is_newer = tuple(int(x) for x in latest.split(".")) > tuple(int(x) for x in current_version.split("."))
    except Exception:
        is_newer = latest != current_version

    if not is_newer:
        QMessageBox.information(
            parent,
            "Up to Date",
            f"You are already on the latest version (v{current_version}).",
        )
        return

    dlg = _UpdateDialog(current_version, latest, parent)
    dlg.exec()
