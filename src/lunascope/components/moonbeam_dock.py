
#  --------------------------------------------------------------------
#
#  This file is part of Luna.
#
#  LUNA is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Luna is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Luna. If not, see <http://www.gnu.org/licenses/>.
#
#  Please see LICENSE.txt for more details.
#
#  --------------------------------------------------------------------

"""Moonbeam dock – NSRR data browser powered by lunapi.moonbeam."""

import os
import pathlib

import lunapi as lp
import pandas as pd

from PySide6.QtCore import Qt, QObject, QRegularExpression, QSortFilterProxyModel, QThread, Signal, Slot
from PySide6.QtGui import QColor, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDockWidget,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableView,
    QTreeView,
    QVBoxLayout,
    QWidget,
)


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

class _MbConnectWorker(QObject):
    """Validates NSRR token and creates a moonbeam instance off the UI thread."""

    success = Signal(object)   # passes the moonbeam instance
    failure = Signal(str)

    def __init__(self, token):
        super().__init__()
        self._token = token  # may be None → use cached token

    @Slot()
    def run(self):
        try:
            mb = lp.moonbeam(self._token)
            self.success.emit(mb)
        except Exception as exc:
            self.failure.emit(str(exc))


class _MbDownloadWorker(QObject):
    """Downloads a list of individuals, emitting per-individual progress."""

    progress  = Signal(int, int)   # (current_index, total)
    item_done = Signal(str, bool)  # (iid, success)
    finished  = Signal()
    error     = Signal(str)        # non-fatal per-item errors

    def __init__(self, mb, iids, cohort, subcohort=None):
        super().__init__()
        self._mb        = mb
        self._iids      = list(iids)
        self._cohort    = cohort
        self._subcohort = subcohort
        self._stop      = False

    def stop(self):
        """Request cancellation between individuals (soft stop)."""
        self._stop = True

    @Slot()
    def run(self):
        total = len(self._iids)
        # Ensure the cohort context is set on the mb instance
        try:
            self._mb.cohort(self._cohort, self._subcohort)
        except Exception as exc:
            self.error.emit(f"Could not set cohort context: {exc}")
            self.finished.emit()
            return

        for i, iid in enumerate(self._iids):
            if self._stop:
                break
            self.progress.emit(i, total)
            try:
                self._mb.pull(iid, subcohort=self._subcohort)
                self.item_done.emit(str(iid), True)
            except Exception as exc:
                self.error.emit(f"{iid}: {exc}")
                self.item_done.emit(str(iid), False)

        self.progress.emit(total, total)
        self.finished.emit()


# ---------------------------------------------------------------------------
# Mixin
# ---------------------------------------------------------------------------

class MoonbeamMixin:
    """Adds the Moonbeam floating dock to the Lunascope controller."""

    _MB_FLOAT_SIZE = (1060, 660)

    # -----------------------------------------------------------------------
    # Initialisation
    # -----------------------------------------------------------------------

    def _init_moonbeam(self):
        self._mb              = None   # live moonbeam instance
        self._mb_curr_cohort  = None
        self._mb_curr_subcohort = None
        self._mb_df2          = None   # current individual manifest DataFrame
        self._mb_thread       = None
        self._mb_connect_worker = None
        self._mb_dl_worker    = None

        # ----------------------------------------------------------------
        # Build the dock widget
        # ----------------------------------------------------------------
        dock = QDockWidget("Moonbeam (NSRR)", self.ui)
        dock.setObjectName("dock_moonbeam")
        dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea
        )
        dock.setFeatures(
            QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
            | QDockWidget.DockWidgetClosable
        )
        dock.setWindowFlag(Qt.WindowMinimizeButtonHint, True)
        dock.setWindowFlag(Qt.WindowMaximizeButtonHint, True)

        root = QWidget(dock)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        # ---- Token / connect row ----------------------------------------
        tok_frame = QFrame(root)
        tok_layout = QHBoxLayout(tok_frame)
        tok_layout.setContentsMargins(0, 0, 0, 0)
        tok_layout.setSpacing(6)

        tok_lbl  = QLabel("NSRR Token:")
        tok_edit = QLineEdit()
        tok_edit.setObjectName("mb_token_edit")
        tok_edit.setEchoMode(QLineEdit.Password)
        tok_edit.setPlaceholderText("Paste token (or leave blank to use cached)")
        tok_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        tok_edit.returnPressed.connect(self._mb_connect)

        connect_btn = QPushButton("Connect")
        connect_btn.setObjectName("mb_connect_btn")
        connect_btn.setFixedWidth(90)

        status_lbl = QLabel("Not connected")
        status_lbl.setObjectName("mb_status_lbl")
        status_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        tok_layout.addWidget(tok_lbl)
        tok_layout.addWidget(tok_edit)
        tok_layout.addWidget(connect_btn)
        tok_layout.addWidget(status_lbl, 1)
        outer.addWidget(tok_frame)

        # ---- Summary bar ------------------------------------------------
        summ_frame = QFrame(root)
        summ_layout = QHBoxLayout(summ_frame)
        summ_layout.setContentsMargins(0, 0, 0, 0)
        summ_layout.setSpacing(8)

        def _stat(txt, min_w=100):
            lab = QLabel(txt)
            lab.setFrameStyle(QFrame.Panel | QFrame.Sunken)
            lab.setMinimumWidth(min_w)
            lab.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            return lab

        lbl_ncohorts = _stat("Cohorts: -")
        lbl_ncohorts.setObjectName("mb_lbl_ncohorts")
        lbl_n = _stat("N: -", 80)
        lbl_n.setObjectName("mb_lbl_n")
        lbl_ncached = _stat("Cached: -", 90)
        lbl_ncached.setObjectName("mb_lbl_ncached")
        lbl_cdir = QLabel("Cache: -")
        lbl_cdir.setObjectName("mb_lbl_cdir")
        lbl_cdir.setFrameStyle(QFrame.Panel | QFrame.Sunken)
        lbl_cdir.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        for w in (lbl_ncohorts, lbl_n, lbl_ncached, lbl_cdir):
            summ_layout.addWidget(w)
        outer.addWidget(summ_frame)

        # ---- Main splitter: tree (left) + table (right) -----------------
        splitter = QSplitter(Qt.Horizontal, root)

        # Left: cohort / subcohort tree
        tree = QTreeView(splitter)
        tree.setObjectName("mb_tree")
        tree.setHeaderHidden(False)
        tree.setSelectionMode(QAbstractItemView.SingleSelection)
        tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        tree.setMinimumWidth(160)
        tree.setMaximumWidth(340)
        tree.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        tree.setEditTriggers(QAbstractItemView.NoEditTriggers)

        # Right: filter bar + individuals table
        right_frame = QFrame(splitter)
        right_layout = QVBoxLayout(right_frame)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(3)

        flt_bar = QFrame(right_frame)
        flt_bar_layout = QHBoxLayout(flt_bar)
        flt_bar_layout.setContentsMargins(0, 2, 0, 2)
        flt_bar_layout.setSpacing(4)
        flt_lbl = QLabel("Filter ID:")
        flt_edit = QLineEdit()
        flt_edit.setObjectName("mb_flt")
        flt_edit.setPlaceholderText("comma-separated IDs…")
        flt_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        flt_clear = QPushButton("✕")
        flt_clear.setObjectName("mb_flt_clear")
        flt_clear.setFixedWidth(26)
        flt_clear.setToolTip("Clear filter")
        flt_clear.clicked.connect(flt_edit.clear)
        flt_bar_layout.addWidget(flt_lbl)
        flt_bar_layout.addWidget(flt_edit)
        flt_bar_layout.addWidget(flt_clear)
        right_layout.addWidget(flt_bar)

        table = QTableView(right_frame)
        table.setObjectName("mb_table")
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        table.setSortingEnabled(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        table.horizontalHeader().setStretchLastSection(False)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)

        # Persistent proxy – filters on column 1 (ID); source model swapped in
        # each time _mb_populate_table is called.
        mb_proxy = QSortFilterProxyModel(table)
        mb_proxy.setFilterKeyColumn(1)
        mb_proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        table.setModel(mb_proxy)

        def _on_flt_changed(text):
            parts = [s.strip() for s in text.split(',') if s.strip()]
            if not parts:
                mb_proxy.setFilterRegularExpression(QRegularExpression())
                return
            esc = [QRegularExpression.escape(p) for p in parts]
            rx = QRegularExpression("(" + "|".join(esc) + ")",
                                    QRegularExpression.CaseInsensitiveOption)
            mb_proxy.setFilterRegularExpression(rx)

        flt_edit.textChanged.connect(_on_flt_changed)
        right_layout.addWidget(table)

        splitter.addWidget(tree)
        splitter.addWidget(right_frame)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([210, 730])
        outer.addWidget(splitter, 1)

        # ---- Bottom bar: action buttons + progress ----------------------
        bot_frame = QFrame(root)
        bot_layout = QHBoxLayout(bot_frame)
        bot_layout.setContentsMargins(0, 0, 0, 0)
        bot_layout.setSpacing(6)

        dl_sel_btn  = QPushButton("Download Selected")
        dl_sel_btn.setObjectName("mb_dl_sel_btn")
        dl_all_btn  = QPushButton("Download All")
        dl_all_btn.setObjectName("mb_dl_all_btn")
        cancel_btn  = QPushButton("Cancel")
        cancel_btn.setObjectName("mb_cancel_btn")
        cancel_btn.setVisible(False)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.setObjectName("mb_refresh_btn")
        refresh_btn.setToolTip("Refresh cached-file status")
        pop_slist_btn = QPushButton("Populate S-List")
        pop_slist_btn.setObjectName("mb_pop_slist_btn")

        progress     = QProgressBar()
        progress.setObjectName("mb_progress")
        progress.setRange(0, 100)
        progress.setValue(0)
        progress.setVisible(False)
        progress.setFixedHeight(18)
        progress.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        progress_lbl = QLabel("")
        progress_lbl.setObjectName("mb_progress_lbl")
        progress_lbl.setMinimumWidth(70)

        bot_layout.addWidget(dl_sel_btn)
        bot_layout.addWidget(dl_all_btn)
        bot_layout.addWidget(cancel_btn)
        bot_layout.addWidget(refresh_btn)
        bot_layout.addStretch(1)
        bot_layout.addWidget(progress_lbl)
        bot_layout.addWidget(progress)
        bot_layout.addWidget(pop_slist_btn)
        outer.addWidget(bot_frame)

        # ---- Attach dock to main window ---------------------------------
        dock.setWidget(root)
        self.ui.addDockWidget(Qt.RightDockWidgetArea, dock)
        dock.setFloating(True)
        dock.resize(*self._MB_FLOAT_SIZE)
        dock.hide()

        # ---- Store references on ui object (actigraphy-style) -----------
        self.ui.dock_moonbeam    = dock
        self.ui.mb_token_edit    = tok_edit
        self.ui.mb_connect_btn   = connect_btn
        self.ui.mb_status_lbl    = status_lbl
        self.ui.mb_tree          = tree
        self.ui.mb_table         = table
        self.ui.mb_flt           = flt_edit
        self._mb_proxy           = mb_proxy
        self.ui.mb_lbl_ncohorts  = lbl_ncohorts
        self.ui.mb_lbl_n         = lbl_n
        self.ui.mb_lbl_ncached   = lbl_ncached
        self.ui.mb_lbl_cdir      = lbl_cdir
        self.ui.mb_dl_sel_btn    = dl_sel_btn
        self.ui.mb_dl_all_btn    = dl_all_btn
        self.ui.mb_cancel_btn    = cancel_btn
        self.ui.mb_refresh_btn   = refresh_btn
        self.ui.mb_pop_slist_btn = pop_slist_btn
        self.ui.mb_progress      = progress
        self.ui.mb_progress_lbl  = progress_lbl

        # ---- Wire signals -----------------------------------------------
        connect_btn.clicked.connect(self._mb_connect)
        tree.clicked.connect(self._mb_on_tree_click)
        dl_sel_btn.clicked.connect(self._mb_download_selected)
        dl_all_btn.clicked.connect(self._mb_download_all)
        cancel_btn.clicked.connect(self._mb_cancel_download)
        refresh_btn.clicked.connect(self._mb_refresh_cache_status)
        pop_slist_btn.clicked.connect(self._mb_populate_slist)

        # ---- Apply dark-panel styling -----------------------------------
        root.setStyleSheet("""
            QFrame {
                color: #d7e3f4;
            }
            QLabel {
                color: #d7e3f4;
            }
        """)

        self._mb_set_action_enabled(False)

    # -----------------------------------------------------------------------
    # Thread lifecycle helpers
    # -----------------------------------------------------------------------

    def _mb_clear_thread(self):
        """Called via finished signal so self._mb_thread is never a stale C++ object."""
        self._mb_thread    = None
        self._mb_dl_worker = None

    def _mb_thread_running(self) -> bool:
        return self._mb_thread is not None and self._mb_thread.isRunning()

    # -----------------------------------------------------------------------
    # Helper: enable / disable action buttons
    # -----------------------------------------------------------------------

    def _mb_set_action_enabled(self, enabled: bool):
        for name in ("mb_dl_sel_btn", "mb_dl_all_btn",
                     "mb_refresh_btn", "mb_pop_slist_btn"):
            w = getattr(self.ui, name, None)
            if w:
                w.setEnabled(enabled)

    # -----------------------------------------------------------------------
    # Connect / authenticate
    # -----------------------------------------------------------------------

    def _mb_connect(self):
        # Guard against double-connect while a thread is running
        if self._mb_thread_running():
            return

        token = self.ui.mb_token_edit.text().strip() or None
        self.ui.mb_status_lbl.setText("Connecting…")
        self.ui.mb_connect_btn.setEnabled(False)

        thread = QThread(self)
        worker = _MbConnectWorker(token)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.success.connect(self._mb_on_connect_success)
        worker.failure.connect(self._mb_on_connect_failure)
        worker.success.connect(thread.quit)
        worker.failure.connect(thread.quit)
        worker.success.connect(worker.deleteLater)
        worker.failure.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._mb_clear_thread)

        self._mb_thread         = thread
        self._mb_connect_worker = worker
        thread.start()

    def _mb_on_connect_success(self, mb):
        self._mb = mb
        self.ui.mb_connect_btn.setEnabled(True)
        self.ui.mb_status_lbl.setText("Connected")
        self._mb_populate_tree()
        self._mb_set_action_enabled(True)

    def _mb_on_connect_failure(self, msg):
        self._mb = None
        self.ui.mb_connect_btn.setEnabled(True)
        self.ui.mb_status_lbl.setText(f"Error: {msg[:100]}")
        QMessageBox.critical(self.ui, "Moonbeam – Connection Error", msg)

    # -----------------------------------------------------------------------
    # Populate cohort/subcohort tree
    # -----------------------------------------------------------------------

    def _mb_populate_tree(self):
        if self._mb is None:
            return

        df1 = self._mb.cohorts()
        total_n      = int(df1['N'].sum())      if 'N'      in df1.columns else 0
        total_cached = int(df1['Cached'].sum()) if 'Cached' in df1.columns else 0

        self.ui.mb_lbl_ncohorts.setText(f"Cohorts: {len(df1)}")
        self.ui.mb_lbl_n.setText(f"N: {total_n:,}")
        self.ui.mb_lbl_ncached.setText(f"Cached: {total_cached:,}")
        self.ui.mb_lbl_cdir.setText(f"Cache: {self._mb.cdir}")

        model = QStandardItemModel()
        model.setHorizontalHeaderLabels(["Cohort / Subcohort", "N", "Cached"])

        for _, row in df1.iterrows():
            cohort         = str(row['Cohort'])
            n              = row.get('N', '')
            cached         = row.get('Cached', '')
            subcohorts_str = str(row.get('Subcohorts', ''))

            coh_item = QStandardItem(cohort)
            coh_item.setData(('cohort', cohort, None), Qt.UserRole)
            coh_item.setEditable(False)
            n_item   = _right_item(str(n))
            c_item   = _right_item(str(cached))

            subcohorts = [s.strip() for s in subcohorts_str.split(',') if s.strip()]
            for sc in subcohorts:
                sc_n      = 0
                sc_cached = 0
                if cohort in self._mb._mf and sc in self._mb._mf[cohort]:
                    subs = self._mb._mf[cohort][sc]
                    sc_n = len(subs)
                    sc_cached = sum(
                        1 for info in subs.values()
                        if (pathlib.Path(self._mb.cdir) / cohort
                            / info['edf'].lstrip('/')).exists()
                    )

                sc_item = QStandardItem(f"  {sc}")
                sc_item.setData(('subcohort', cohort, sc), Qt.UserRole)
                sc_item.setEditable(False)
                coh_item.appendRow([sc_item, _right_item(str(sc_n)),
                                    _right_item(str(sc_cached))])

            model.appendRow([coh_item, n_item, c_item])

        tree = self.ui.mb_tree
        tree.setModel(model)
        h = tree.header()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        h.setSectionResizeMode(1, QHeaderView.Fixed)
        h.setSectionResizeMode(2, QHeaderView.Fixed)
        tree.setColumnWidth(1, 52)
        tree.setColumnWidth(2, 62)
        tree.expandAll()

    # -----------------------------------------------------------------------
    # Tree click → populate right table
    # -----------------------------------------------------------------------

    def _mb_on_tree_click(self, index):
        if self._mb is None:
            return
        item = self.ui.mb_tree.model().itemFromIndex(index.siblingAtColumn(0))
        if item is None:
            return
        data = item.data(Qt.UserRole)
        if data is None:
            return
        kind, cohort, subcohort = data
        self._mb_curr_cohort    = cohort
        self._mb_curr_subcohort = subcohort  # None for cohort-level click
        self._mb_populate_table(cohort, subcohort)

    # -----------------------------------------------------------------------
    # Populate individual table
    # -----------------------------------------------------------------------

    def _mb_populate_table(self, cohort, subcohort=None):
        if self._mb is None:
            return

        df2 = self._mb.cohort(cohort, subcohort)
        if df2 is None or df2.empty:
            self._mb_proxy.setSourceModel(QStandardItemModel())
            self._mb_df2 = pd.DataFrame()
            return

        self._mb_df2 = df2.copy()

        cols  = ["", "ID", "Subcohort", "EDF", "Annot", "Cached"]
        model = QStandardItemModel(len(df2), len(cols))
        model.setHorizontalHeaderLabels(cols)

        for r, (_, row) in enumerate(df2.iterrows()):
            iid   = str(row['ID'])
            sc    = str(row.get('Subcohort', ''))
            edf   = str(row.get('EDF', ''))
            annot = str(row.get('Annot', '.'))
            is_cached = self._mb.cached(f"{cohort}/{edf}")

            chk = QStandardItem()
            chk.setCheckable(True)
            chk.setCheckState(Qt.Unchecked)
            chk.setEditable(False)
            model.setItem(r, 0, chk)
            model.setItem(r, 1, _plain_item(iid))
            model.setItem(r, 2, _plain_item(sc))
            model.setItem(r, 3, _plain_item(os.path.basename(edf)))
            model.setItem(r, 4, _plain_item(
                os.path.basename(annot) if annot not in ('.', '') else '-'
            ))
            model.setItem(r, 5, _cached_item(is_cached))

        tbl = self.ui.mb_table
        self._mb_proxy.setSourceModel(model)
        h = tbl.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Fixed)
        for c in (1, 2, 3, 4):
            h.setSectionResizeMode(c, QHeaderView.Interactive)
        h.setSectionResizeMode(5, QHeaderView.Fixed)
        h.setStretchLastSection(False)
        tbl.setColumnWidth(0, 28)
        tbl.resizeColumnsToContents()
        tbl.setColumnWidth(0, 28)   # restore after resize
        tbl.setColumnWidth(5, 58)

    # -----------------------------------------------------------------------
    # Refresh cache status (tree + table)
    # -----------------------------------------------------------------------

    def _mb_refresh_cache_status(self):
        if self._mb is None:
            return
        if self._mb_curr_cohort:
            self._mb_populate_table(self._mb_curr_cohort, self._mb_curr_subcohort)
        self._mb_populate_tree()

    # -----------------------------------------------------------------------
    # Get IIDs from table
    # -----------------------------------------------------------------------

    def _mb_src_model(self):
        """Return the source QStandardItemModel behind the proxy."""
        return self._mb_proxy.sourceModel()

    def _mb_get_checked_iids(self):
        src = self._mb_src_model()
        if src is None:
            return []
        iids = []
        for r in range(src.rowCount()):
            chk = src.item(r, 0)
            if chk and chk.checkState() == Qt.Checked:
                it = src.item(r, 1)
                if it:
                    iids.append(it.text())
        return iids

    def _mb_get_all_iids(self):
        src = self._mb_src_model()
        if src is None:
            return []
        return [src.item(r, 1).text()
                for r in range(src.rowCount())
                if src.item(r, 1)]

    # -----------------------------------------------------------------------
    # Download: selected
    # -----------------------------------------------------------------------

    def _mb_download_selected(self):
        iids = self._mb_get_checked_iids()
        if not iids:
            QMessageBox.information(
                self.ui, "Moonbeam",
                "No individuals selected.\n"
                "Use the checkboxes in the first column to select individuals."
            )
            return
        self._mb_start_download(iids)

    # -----------------------------------------------------------------------
    # Download: all in current view
    # -----------------------------------------------------------------------

    def _mb_download_all(self):
        if self._mb_curr_cohort is None:
            return
        iids = self._mb_get_all_iids()
        if not iids:
            return
        label = self._mb_curr_subcohort or self._mb_curr_cohort
        reply = QMessageBox.question(
            self.ui, "Download All",
            f"Download all {len(iids):,} individuals from '{label}'?\n"
            "Already-cached files will be skipped automatically.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
        self._mb_start_download(iids)

    # -----------------------------------------------------------------------
    # Download: start worker thread
    # -----------------------------------------------------------------------

    def _mb_start_download(self, iids):
        if self._mb is None or self._mb_curr_cohort is None:
            return
        if self._mb_thread_running():
            QMessageBox.information(
                self.ui, "Moonbeam", "A download is already in progress."
            )
            return

        total = len(iids)
        self.ui.mb_progress.setRange(0, total)
        self.ui.mb_progress.setValue(0)
        self.ui.mb_progress.setVisible(True)
        self.ui.mb_progress_lbl.setText(f"0 / {total}")
        self.ui.mb_cancel_btn.setVisible(True)
        self._mb_set_action_enabled(False)
        self.ui.mb_connect_btn.setEnabled(False)
        self.ui.mb_status_lbl.setText(f"Downloading 0 / {total}…")

        thread = QThread(self)
        worker = _MbDownloadWorker(
            self._mb, iids,
            self._mb_curr_cohort,
            self._mb_curr_subcohort
        )
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.progress.connect(self._mb_on_dl_progress)
        worker.item_done.connect(self._mb_on_item_done)
        worker.error.connect(self._mb_on_dl_error)
        worker.finished.connect(self._mb_on_dl_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._mb_clear_thread)

        self._mb_thread    = thread
        self._mb_dl_worker = worker
        self._mb_dl_total  = total
        thread.start()

    # -----------------------------------------------------------------------
    # Cancel download (soft – between individuals)
    # -----------------------------------------------------------------------

    def _mb_cancel_download(self):
        w = getattr(self, "_mb_dl_worker", None)
        if w is not None:
            w.stop()
            self.ui.mb_status_lbl.setText("Cancelling…")

    # -----------------------------------------------------------------------
    # Download progress slots
    # -----------------------------------------------------------------------

    def _mb_on_dl_progress(self, current, total):
        self.ui.mb_progress.setValue(current)
        self.ui.mb_progress_lbl.setText(f"{current} / {total}")
        self.ui.mb_status_lbl.setText(f"Downloading {current} / {total}…")

    def _mb_on_item_done(self, iid, success):
        """Update the Cached column in the table for the finished individual."""
        if not success or self._mb is None or self._mb_curr_cohort is None:
            return
        if self._mb_df2 is None or self._mb_df2.empty:
            return
        rows = self._mb_df2[self._mb_df2['ID'] == iid]
        if rows.empty:
            return
        edf       = str(rows.iloc[0]['EDF'])
        is_cached = self._mb.cached(f"{self._mb_curr_cohort}/{edf}")
        src = self._mb_src_model()
        if src is None:
            return
        for r in range(src.rowCount()):
            id_it = src.item(r, 1)
            if id_it and id_it.text() == iid:
                src.setItem(r, 5, _cached_item(is_cached))
                break

    def _mb_on_dl_finished(self):
        self.ui.mb_progress.setVisible(False)
        self.ui.mb_cancel_btn.setVisible(False)
        self.ui.mb_progress_lbl.setText("")
        self._mb_set_action_enabled(True)
        self.ui.mb_connect_btn.setEnabled(True)
        self.ui.mb_status_lbl.setText("Connected")
        # Full refresh: tree counts + table cached flags
        self._mb_refresh_cache_status()

    def _mb_on_dl_error(self, msg):
        # Non-fatal; show briefly in status bar
        self.ui.mb_status_lbl.setText(f"Warning: {msg[:100]}")

    # -----------------------------------------------------------------------
    # Populate S-List from cached individuals in current view
    # -----------------------------------------------------------------------

    def _mb_populate_slist(self):
        if self._mb is None or self._mb_curr_cohort is None:
            QMessageBox.information(
                self.ui, "Moonbeam", "No cohort selected."
            )
            return
        if self._mb_df2 is None or self._mb_df2.empty:
            QMessageBox.information(
                self.ui, "Moonbeam",
                "The current cohort view is empty."
            )
            return

        cohort  = self._mb_curr_cohort
        cdir    = pathlib.Path(self._mb.cdir)
        rows    = []

        for _, row in self._mb_df2.iterrows():
            iid   = str(row['ID'])
            edf   = str(row['EDF'])
            annot = str(row.get('Annot', '.'))

            if not self._mb.cached(f"{cohort}/{edf}"):
                continue

            local_edf = str(cdir / cohort / edf.lstrip('/'))
            if annot in ('.', ''):
                local_annot = '.'
            else:
                candidate = str(cdir / cohort / annot.lstrip('/'))
                local_annot = candidate if os.path.exists(candidate) else '.'

            rows.append([iid, local_edf, local_annot])

        if not rows:
            QMessageBox.information(
                self.ui, "Moonbeam",
                "No cached individuals found in the current view.\n"
                "Download some files first, then click Populate S-List."
            )
            return

        label = self._mb_curr_subcohort or cohort
        reply = QMessageBox.question(
            self.ui, "Populate S-List",
            f"Load {len(rows):,} cached individual(s) from '{label}' "
            f"into the S-List?\nThis will replace the current S-List.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        try:
            from PySide6.QtWidgets import (
                QHeaderView as _QHV,
                QAbstractItemView as _QAV,
            )
            self.proj.clear()
            self.proj.eng.set_sample_list(rows)
            df = self.proj.sample_list()
            model = self.df_to_model(df)
            self._proxy.setSourceModel(model)
            view = self.ui.tbl_slist
            h = view.horizontalHeader()
            h.setSectionResizeMode(_QHV.Interactive)
            h.setStretchLastSection(False)
            view.resizeColumnsToContents()
            view.setSelectionBehavior(_QAV.SelectRows)
            view.setSelectionMode(_QAV.SingleSelection)
            view.verticalHeader().setVisible(True)
            self.ui.lbl_slist.setText(f"<moonbeam:{label}>")
        except Exception as exc:
            QMessageBox.critical(
                self.ui, "Moonbeam – S-List Error", str(exc)
            )


# ---------------------------------------------------------------------------
# Item helpers
# ---------------------------------------------------------------------------

def _plain_item(text: str) -> QStandardItem:
    it = QStandardItem(text)
    it.setEditable(False)
    return it


def _right_item(text: str) -> QStandardItem:
    it = QStandardItem(text)
    it.setEditable(False)
    it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return it


def _cached_item(is_cached: bool) -> QStandardItem:
    it = QStandardItem("✓" if is_cached else "-")
    it.setEditable(False)
    it.setTextAlignment(Qt.AlignCenter | Qt.AlignVCenter)
    if is_cached:
        it.setForeground(QColor("#5dca7a"))
    return it
