"""
AdsCancel UI — paste cookie, scan accounts, bulk re-appeal.

Run:    python ui.py
Deps:   pip install PyQt5 requests
"""

import json
import sys
import threading
import time

import requests
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDoubleSpinBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QSplitter, QStatusBar, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

import auto   # reuse all the networking logic

HERE = auto.data_dir()
COOKIE_FILE = HERE / "cookie.txt"
RESULTS_FILE = HERE / "appeal_results.json"


# ---------- background workers ----------

class ScanWorker(QThread):
    """Discover session + list accounts."""
    log = pyqtSignal(str)
    done = pyqtSignal(object, object)   # (config_dict, accounts_list)
    failed = pyqtSignal(str)

    def __init__(self, cookies: dict, authuser: str = "0", forced_mcc: str = ""):
        super().__init__()
        self.cookies = cookies
        self.authuser = authuser
        self.forced_mcc = forced_mcc

    def run(self):
        try:
            session = requests.Session()
            self.log.emit("Discovering session from cookies...")
            cfg = auto.discover_session(session, self.cookies, self.authuser,
                                        self.forced_mcc)
            self.log.emit(
                f"  MCC={cfg['manager_customer_id']}  __u={cfg['user_id']}  "
                f"__c={cfg['customer_id']}  f.sid={cfg['f_sid']}"
            )
            self.log.emit("Listing accounts...")
            accounts = auto.list_accounts(session, self.cookies, cfg)
            self.log.emit(f"  Got {len(accounts)} accounts.")
            self.done.emit(cfg, accounts)
        except SystemExit as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class SubmitWorker(QThread):
    """Submit appeals for a list of customer IDs."""
    log = pyqtSignal(str)
    # row, customer_id, http, tag, details, body
    progress = pyqtSignal(int, str, int, str, str, str)
    done = pyqtSignal()

    def __init__(self, cookies: dict, cfg: dict, targets: list, delay: float,
                 answer_changes: str = "yes", answer_details: str = "yes",
                 threads: int = 1):
        super().__init__()
        self.cookies = cookies
        self.cfg = cfg
        self.targets = targets   # list of (row, customer_id, name)
        self.delay = delay
        self.answer_changes = answer_changes
        self.answer_details = answer_details
        self.threads = max(1, int(threads))
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        total = len(self.targets)
        queue = list(self.targets)
        queue_lock = threading.Lock()
        counter = [0]   # mutable shared counter for "[i/N]" log labels
        auth_expired = threading.Event()

        def worker_loop():
            # Each worker owns its own requests.Session so connection
            # pools don't get contested between threads.
            session = requests.Session()
            while True:
                if self._stop or auth_expired.is_set():
                    return
                with queue_lock:
                    if not queue:
                        return
                    row, cid, name = queue.pop(0)
                    counter[0] += 1
                    i = counter[0]

                try:
                    code, tag, details, snippet = auto.submit_one(
                        session, self.cookies, self.cfg, cid,
                        self.answer_changes, self.answer_details)
                    label = f"{tag} ({details})" if details else tag
                    self.log.emit(
                        f"[{i}/{total}] {label:<32} {cid} ({name}) http={code}"
                    )
                    self.progress.emit(row, cid, code, tag, details, snippet)
                    if tag == "AUTH_ERROR":
                        self.log.emit("AUTH expired — stopping all threads. Refresh cookie and rescan.")
                        auth_expired.set()
                        return
                except Exception as e:
                    self.log.emit(f"[{i}/{total}] EXC {cid} ({name}): {e}")
                    self.progress.emit(row, cid, 0, "EXC", "", str(e))

                if self.delay > 0:
                    time.sleep(self.delay)

        self.log.emit(f"Spawning {self.threads} worker thread(s)...")
        pool = [threading.Thread(target=worker_loop, daemon=True)
                for _ in range(self.threads)]
        for t in pool:
            t.start()
        for t in pool:
            t.join()
        if self._stop:
            self.log.emit("Stopped by user.")
        self.done.emit()


# ---------- main window ----------

TAG_COLORS = {
    "OK":          QColor("#cfe9c9"),
    "PENDING":     QColor("#ffe8a3"),
    "BLACKLISTED": QColor("#ffc9c9"),
    "AUTH_ERROR":  QColor("#ffc9c9"),
    "ERROR":       QColor("#ffd9b3"),
    "EXC":         QColor("#ffd9b3"),
}

# Human-readable label for ui_account_status returned by AccountService.List.
STATUS_LABEL = {
    1: "1 · Billing",
    2: "2 · Policy-suspended",
    3: "3 · Canceled",
    4: "4 · Closed",
    5: "5 · Other",
}

# Color tag for the inferred "type" cell.
TYPE_COLORS = {
    "Multi-account abuse":  QColor("#d6e8ff"),   # the target cohort — light blue
    "Suspicious payment":   QColor("#ffe1e1"),   # different tags needed — red-ish
    "Billing / unpaid":     QColor("#fff3cd"),   # different appeal flow
    "Manager":              QColor("#eeeeee"),
}


def infer_type(acc: dict) -> str:
    """Classify an account based on its descriptive_name + ui_account_status."""
    name = acc.get("descriptive_name") or ""
    status = acc.get("ui_account_status")
    if acc.get("is_manager"):
        return "Manager"
    if status == 1:
        return "Billing / unpaid"
    if status == 2:
        if name.startswith("MCC_Child_"):
            return "Multi-account abuse"
        if name.startswith("Live-"):
            return "Suspicious payment"
        return "Policy (unknown subtype)"
    return "—"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AdsCancel — @huyit32")
        self.resize(1100, 760)

        self.cookies = {}
        self.cfg = None
        self.accounts = []
        self.submit_worker = None
        # customer_id -> last tag from previous runs (loaded from appeal_results.json).
        self.last_results: dict[str, str] = {}

        self._build_ui()
        self._load_existing_cookie()
        self._load_last_results()

    # ---- UI layout ----
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)

        # Cookie row
        v.addWidget(QLabel("Cookie (paste from browser DevTools — Cookie header value):"))
        self.cookie_edit = QPlainTextEdit()
        self.cookie_edit.setPlaceholderText(
            "SID=...; HSID=...; SAPISID=...; __Secure-1PSID=...; __Secure-1PSIDCC=...; ..."
        )
        self.cookie_edit.setMaximumHeight(80)
        self.cookie_edit.setFont(QFont("Consolas", 9))
        v.addWidget(self.cookie_edit)

        row1 = QHBoxLayout()
        self.btn_save_cookie = QPushButton("Save cookie")
        self.btn_save_cookie.clicked.connect(self.save_cookie)
        row1.addWidget(self.btn_save_cookie)

        self.btn_scan = QPushButton("Scan accounts")
        self.btn_scan.clicked.connect(self.scan)
        row1.addWidget(self.btn_scan)

        row1.addWidget(QLabel("authuser:"))
        self.authuser_edit = QLineEdit("0")
        self.authuser_edit.setFixedWidth(40)
        row1.addWidget(self.authuser_edit)

        row1.addWidget(QLabel("force MCC (optional):"))
        self.mcc_edit = QLineEdit()
        self.mcc_edit.setPlaceholderText("e.g. 6793056170 — leave empty to auto-pick")
        self.mcc_edit.setFixedWidth(220)
        self.mcc_edit.setToolTip(
            "If your Google account owns multiple MCCs, the discovery picks "
            "the first one. Paste a specific MCC customer_id here to force it."
        )
        row1.addWidget(self.mcc_edit)

        row1.addStretch()
        v.addLayout(row1)

        # Appeal answers row — the two free-text replies the form expects.
        row_ans = QHBoxLayout()
        row_ans.addWidget(QLabel("Changes since last appeal:"))
        self.answer_changes_edit = QLineEdit("yes")
        self.answer_changes_edit.setMinimumWidth(220)
        row_ans.addWidget(self.answer_changes_edit, stretch=1)
        row_ans.addWidget(QLabel("Further details:"))
        self.answer_details_edit = QLineEdit("yes")
        self.answer_details_edit.setMinimumWidth(220)
        row_ans.addWidget(self.answer_details_edit, stretch=1)
        v.addLayout(row_ans)

        # Filter row
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Filter prefix:"))
        self.prefix_edit = QLineEdit("MCC_Child_")
        self.prefix_edit.setFixedWidth(140)
        row2.addWidget(self.prefix_edit)

        row2.addWidget(QLabel("ui_account_status:"))
        self.status_spin = QSpinBox()
        self.status_spin.setRange(0, 9)
        self.status_spin.setValue(2)
        self.status_spin.setFixedWidth(50)
        row2.addWidget(self.status_spin)

        row2.addWidget(QLabel("delay (s):"))
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(0.0, 30.0)
        self.delay_spin.setSingleStep(0.5)
        self.delay_spin.setValue(1.5)
        self.delay_spin.setFixedWidth(70)
        row2.addWidget(self.delay_spin)

        row2.addWidget(QLabel("threads:"))
        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, 20)
        self.threads_spin.setValue(1)
        self.threads_spin.setFixedWidth(50)
        self.threads_spin.setToolTip(
            "Parallel workers. Throughput ≈ threads / delay submits/sec.\n"
            "Higher values risk Google rate-limiting; 3–5 is usually safe."
        )
        row2.addWidget(self.threads_spin)

        self.skip_done_chk = QCheckBox("Skip already submitted (OK/PENDING)")
        self.skip_done_chk.setChecked(True)
        row2.addWidget(self.skip_done_chk)

        self.btn_apply_filter = QPushButton("Apply filter (check matching rows)")
        self.btn_apply_filter.clicked.connect(self.apply_filter)
        row2.addWidget(self.btn_apply_filter)

        row2.addStretch()
        v.addLayout(row2)

        # Splitter: table on top, log on bottom
        split = QSplitter(Qt.Vertical)

        # Table
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["✓", "Customer ID", "Name", "status", "Type", "manager?", "tag", "http"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(True)
        self.table.verticalHeader().setDefaultSectionSize(36)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(7, QHeaderView.ResizeToContents)
        split.addWidget(self.table)

        # Log
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Consolas", 9))
        split.addWidget(self.log)
        split.setSizes([520, 220])
        v.addWidget(split, stretch=1)

        # Bottom row: submit / stop
        row3 = QHBoxLayout()
        self.btn_check_all = QPushButton("Check all")
        self.btn_check_all.clicked.connect(lambda: self._set_all_checked(True))
        row3.addWidget(self.btn_check_all)

        self.btn_uncheck_all = QPushButton("Uncheck all")
        self.btn_uncheck_all.clicked.connect(lambda: self._set_all_checked(False))
        row3.addWidget(self.btn_uncheck_all)

        row3.addStretch()
        self.summary_lbl = QLabel("0 selected")
        row3.addWidget(self.summary_lbl)

        self.btn_submit = QPushButton("Submit appeals for checked")
        self.btn_submit.setStyleSheet("background-color:#1a73e8; color:white; padding:8px 16px;")
        self.btn_submit.clicked.connect(self.submit_checked)
        row3.addWidget(self.btn_submit)

        self.btn_stop = QPushButton("Stop")
        self.btn_stop.clicked.connect(self.stop_submit)
        self.btn_stop.setEnabled(False)
        row3.addWidget(self.btn_stop)
        v.addLayout(row3)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Paste cookie -> Save -> Scan")

    # ---- helpers ----
    def append_log(self, msg: str):
        self.log.appendPlainText(msg)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def _load_existing_cookie(self):
        if COOKIE_FILE.exists():
            try:
                self.cookie_edit.setPlainText(COOKIE_FILE.read_text(encoding="utf-8").strip())
                self.cookies = auto.load_cookies(COOKIE_FILE)
                self.statusBar().showMessage(f"Loaded cookie.txt ({len(self.cookies)} keys)")
            except Exception as e:
                self.append_log(f"Failed to load cookie.txt: {e}")

    def _load_last_results(self):
        """Read appeal_results.json so we can show 'already-submitted' status
        in the table before the next run starts."""
        if not RESULTS_FILE.exists():
            return
        try:
            entries = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
            for e in entries:
                cid = str(e.get("customer_id", "")).strip()
                tag = e.get("tag", "")
                if cid and tag:
                    self.last_results[cid] = tag   # later entries win (latest run)
            self.append_log(f"Loaded {len(self.last_results)} prior results from appeal_results.json")
        except Exception as e:
            self.append_log(f"Failed to read appeal_results.json: {e}")

    def save_cookie(self):
        """Merge pasted cookies with whatever's already in cookie.txt so that
        persistent values (SID, HSID, APISID, SAPISID, SSID, __Secure-*PSID...)
        survive when the user only pastes the rotating bits (SIDCC, SIDTS)."""
        text = self.cookie_edit.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Empty", "Cookie textarea is empty.")
            return

        def parse(s: str) -> dict:
            out = {}
            for part in s.replace("\n", ";").split(";"):
                part = part.strip()
                if part and "=" in part:
                    k, _, v = part.partition("=")
                    out[k.strip()] = v.strip()
            return out

        existing = parse(COOKIE_FILE.read_text(encoding="utf-8")) if COOKIE_FILE.exists() else {}
        new = parse(text)
        merged = {**existing, **new}   # new values override stale ones for same key

        # Persist as single-line "k=v; k=v" (auto.load_cookies handles it).
        COOKIE_FILE.write_text("; ".join(f"{k}={v}" for k, v in merged.items()),
                               encoding="utf-8")
        self.cookies = merged

        added = sorted(set(new) - set(existing))
        updated = sorted(set(new) & set(existing))
        self.append_log(
            f"Cookie merged: {len(merged)} total "
            f"(+{len(added)} new, {len(updated)} refreshed)"
        )
        self.statusBar().showMessage(
            f"Cookie ready ({len(merged)} keys; refreshed: {len(updated)})"
        )

        critical = ["SID", "HSID", "APISID", "SAPISID",
                    "__Secure-1PSID", "__Secure-3PSID"]
        missing = [c for c in critical if c not in merged]
        if missing:
            QMessageBox.warning(
                self, "Missing cookies",
                "These critical cookies are missing — auth will likely fail:\n\n"
                + ", ".join(missing) +
                "\n\nPaste the full Cookie header value from DevTools."
            )

    def scan(self):
        if not self.cookies:
            QMessageBox.warning(self, "No cookie", "Save cookie first.")
            return
        self.btn_scan.setEnabled(False)
        self.append_log("--- Scanning ---")
        self.scan_worker = ScanWorker(
            self.cookies,
            self.authuser_edit.text().strip() or "0",
            self.mcc_edit.text().strip(),
        )
        self.scan_worker.log.connect(self.append_log)
        self.scan_worker.done.connect(self.on_scan_done)
        self.scan_worker.failed.connect(self.on_scan_failed)
        self.scan_worker.start()

    def on_scan_failed(self, msg: str):
        self.btn_scan.setEnabled(True)
        self.append_log(f"SCAN FAILED: {msg}")
        QMessageBox.critical(self, "Scan failed", msg)

    def on_scan_done(self, cfg: dict, accounts: list):
        self.btn_scan.setEnabled(True)
        self.cfg = cfg
        self.accounts = accounts
        self._populate_table(accounts)
        self.apply_filter()
        self.statusBar().showMessage(
            f"Scanned: {len(accounts)} accounts. Apply filter and click Submit."
        )

    def _populate_table(self, accounts: list):
        self.table.setRowCount(0)
        for a in accounts:
            r = self.table.rowCount()
            self.table.insertRow(r)

            chk = QCheckBox()
            chk.stateChanged.connect(self._update_summary)
            self.table.setCellWidget(r, 0, chk)

            status = a.get("ui_account_status")
            status_label = STATUS_LABEL.get(status, str(status) if status else "")
            kind = infer_type(a)

            self.table.setItem(r, 1, QTableWidgetItem(str(a.get("customer_id") or "")))
            self.table.setItem(r, 2, QTableWidgetItem(a.get("descriptive_name") or ""))
            self.table.setItem(r, 3, QTableWidgetItem(status_label))
            type_item = QTableWidgetItem(kind)
            color = TYPE_COLORS.get(kind)
            if color:
                type_item.setBackground(color)
            self.table.setItem(r, 4, type_item)
            self.table.setItem(r, 5, QTableWidgetItem("yes" if a.get("is_manager") else ""))

            # Pre-fill the "tag" column from prior runs so the user can see
            # at a glance which accounts have already been re-appealed.
            cid_str = str(a.get("customer_id") or "")
            prior_tag = self.last_results.get(cid_str, "")
            tag_item = QTableWidgetItem(prior_tag)
            tcolor = TAG_COLORS.get(prior_tag)
            if tcolor:
                tag_item.setBackground(tcolor)
            self.table.setItem(r, 6, tag_item)
            self.table.setItem(r, 7, QTableWidgetItem(""))

    def apply_filter(self):
        prefix = self.prefix_edit.text()
        status = self.status_spin.value()
        skip_done = self.skip_done_chk.isChecked()
        skip_set = {"OK", "PENDING"} if skip_done else set()
        n = skipped = 0
        for r in range(self.table.rowCount()):
            a = self.accounts[r]
            cid = str(a.get("customer_id") or "")
            prior = self.last_results.get(cid, "")
            match = (not a.get("is_manager")
                     and a.get("ui_account_status") == status
                     and (a.get("descriptive_name") or "").startswith(prefix))
            if match and prior in skip_set:
                match = False
                skipped += 1
            chk: QCheckBox = self.table.cellWidget(r, 0)
            chk.setChecked(match)
            if match:
                n += 1
        msg = f"Filter: status={status}, prefix={prefix!r} -> {n} matches"
        if skip_done and skipped:
            msg += f"  (skipped {skipped} already OK/PENDING)"
        self.append_log(msg)

    def _set_all_checked(self, v: bool):
        for r in range(self.table.rowCount()):
            self.table.cellWidget(r, 0).setChecked(v)

    def _update_summary(self):
        n = sum(1 for r in range(self.table.rowCount())
                if self.table.cellWidget(r, 0).isChecked())
        self.summary_lbl.setText(f"{n} selected")

    def submit_checked(self):
        if not self.cfg:
            QMessageBox.warning(self, "Not scanned", "Scan first.")
            return
        targets = []
        for r in range(self.table.rowCount()):
            if self.table.cellWidget(r, 0).isChecked():
                cid = self.table.item(r, 1).text()
                name = self.table.item(r, 2).text()
                # clear previous result columns
                self.table.item(r, 6).setText("")
                self.table.item(r, 7).setText("")
                targets.append((r, cid, name))
        if not targets:
            QMessageBox.information(self, "Empty", "No rows checked.")
            return
        delay = self.delay_spin.value()
        threads = self.threads_spin.value()
        eta = len(targets) * delay / 60 / max(1, threads)
        ans = QMessageBox.question(
            self, "Confirm",
            f"Submit appeals for {len(targets)} accounts "
            f"with {threads} thread(s)?  (~{eta:.1f} min)",
        )
        if ans != QMessageBox.Yes:
            return

        self.append_log(f"--- Submitting {len(targets)} appeals ({threads} threads) ---")
        self.btn_submit.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_scan.setEnabled(False)

        ans_changes = self.answer_changes_edit.text().strip() or "yes"
        ans_details = self.answer_details_edit.text().strip() or "yes"
        self.append_log(f"Answers — changes={ans_changes!r}  details={ans_details!r}")
        self.submit_worker = SubmitWorker(
            self.cookies, self.cfg, targets, delay,
            ans_changes, ans_details, threads,
        )
        self.submit_worker.log.connect(self.append_log)
        self.submit_worker.progress.connect(self.on_submit_progress)
        self.submit_worker.done.connect(self.on_submit_done)
        self.submit_worker.start()
        self._results = []   # capture for json dump

    def stop_submit(self):
        if self.submit_worker:
            self.submit_worker.stop()

    def on_submit_progress(self, row: int, cid: str, code: int, tag: str,
                           details: str, body: str):
        # Show all error codes in the tag cell when multiple errors fire on
        # the same submit (e.g. "BLACKLISTED + PENDING").
        cell_text = f"{tag}\n{details}" if details else tag
        tag_item = self.table.item(row, 6)
        tag_item.setText(cell_text)
        tag_item.setToolTip(body)
        self.table.item(row, 7).setText(str(code) if code else "-")
        color = TAG_COLORS.get(tag)
        if color:
            # Tint everything except the Type column (col 4) so the inferred
            # type label stays visible.
            for col in (1, 2, 3, 5, 6, 7):
                item = self.table.item(row, col)
                if item:
                    item.setBackground(color)
        self.last_results[cid] = tag
        self._results.append({"customer_id": cid, "http": code, "tag": tag,
                              "details": details, "body": body[:200]})

    def on_submit_done(self):
        self.btn_submit.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_scan.setEnabled(True)
        # Append to appeal_results.json so prior history isn't lost.
        try:
            existing = []
            if RESULTS_FILE.exists():
                try:
                    existing = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
                    if not isinstance(existing, list):
                        existing = []
                except Exception:
                    existing = []
            merged = existing + self._results
            RESULTS_FILE.write_text(
                json.dumps(merged, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.append_log(
                f"Wrote {RESULTS_FILE.name}  "
                f"(+{len(self._results)} new, {len(merged)} total)"
            )
        except Exception as e:
            self.append_log(f"Write log failed: {e}")
        # summary
        from collections import Counter
        tags = Counter(r["tag"] for r in self._results)
        self.append_log(f"Done. {dict(tags)}")
        self.statusBar().showMessage(f"Done: {dict(tags)}")


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
