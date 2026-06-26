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
from PyQt5.QtCore import QDate, Qt, QThread, pyqtSignal
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateEdit,
    QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QSplitter, QStatusBar, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
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
    mccs_needed = pyqtSignal(list)      # [(ocid, label), ...] — user must pick

    def __init__(self, cookies: dict, authuser: str = "0", forced_mcc: str = ""):
        super().__init__()
        self.cookies = cookies
        self.authuser = authuser
        self.forced_mcc = forced_mcc

    def run(self):
        try:
            session = requests.Session()
            self.log.emit("Discovering session from cookies...")
            try:
                cfg = auto.discover_session(session, self.cookies, self.authuser,
                                            self.forced_mcc)
            except auto.MultipleMCCsError as e:
                self.log.emit(f"Found {len(e.mccs)} MCCs — waiting for picker...")
                self.mccs_needed.emit(e.mccs)
                return
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
                 threads: int = 1,
                 tag_primary: list = None, tag_secondary: list = None,
                 payment_answers: dict = None):
        super().__init__()
        self.cookies = cookies
        self.cfg = cfg
        self.targets = targets   # list of (row, customer_id, name)
        self.delay = delay
        self.answer_changes = answer_changes
        self.answer_details = answer_details
        self.threads = max(1, int(threads))
        self.tag_primary = tag_primary
        self.tag_secondary = tag_secondary
        # When set, submit the "Suspicious Payment Activity" questionnaire
        # instead of the abuse re-appeal form.
        self.payment_answers = payment_answers
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
                    # Pull the DRAPT + Chrome session headers the create-MCC UI
                    # already captures into session_extras.json so this appeal
                    # path mirrors a real browser request as well.
                    _drapt, _extras = auto.load_session_extras()
                    code, tag, details, snippet = auto.submit_one(
                        session, self.cookies, self.cfg, cid,
                        self.answer_changes, self.answer_details,
                        self.tag_primary, self.tag_secondary,
                        drapt=_drapt, extras=_extras,
                        payment_answers=self.payment_answers)
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


# field_ids in the Suspicious-Payment form whose answer is a yes/no boolean.
PAYMENT_BOOL_FIELDS = {
    "isAdvertisingOwnBusiness", "isBusinessModelChanged",
    "isUsingAffiliatedMarketing", "isHavingMultipleGoogleAccounts",
    "isManagedByDifferentOrganization",
}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Ads Cancel - Suspicious Payment - @huyit32")
        self.resize(1100, 760)

        self.cookies = {}
        self.cfg = None
        self.accounts = []
        self.submit_worker = None
        # Guards itemChanged spam while we bulk-fill / bulk-toggle the table.
        self._populating = False
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
        v.addWidget(QLabel("Cookie (paste full cURL OR raw Cookie header value):"))
        self.cookie_edit = QPlainTextEdit()
        self.cookie_edit.setPlaceholderText(
            "Either:\n"
            "  • Full cURL command from DevTools (right-click request → Copy as cURL)\n"
            "  • Or raw cookie string: SID=...; HSID=...; SAPISID=...; __Secure-1PSID=...; ..."
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

        row1.addWidget(QLabel("MCC ID or URL:"))
        self.mcc_edit = QLineEdit()
        self.mcc_edit.setPlaceholderText(
            "Paste full Ads URL (https://ads.google.com/aw/accounts?ocid=...) "
            "or raw customer_id, or leave empty to auto-pick"
        )
        self.mcc_edit.setMinimumWidth(360)
        self.mcc_edit.setToolTip(
            "If your Google account owns multiple MCCs, paste the URL of the\n"
            "MCC's overview/accounts page from your browser address bar.\n"
            "We'll extract the ocid (= manager customer_id) automatically.\n"
            "You can also paste just the bare numeric ID."
        )
        row1.addWidget(self.mcc_edit, stretch=1)

        row1.addStretch()
        v.addLayout(row1)

        # Suspicious-Payment questionnaire — inline, collapsible. One shared set
        # of answers is submitted for every checked account.
        self._build_payment_inputs(v)

        # Filter row
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Filter prefix:"))
        self.prefix_edit = QLineEdit()   # empty by default — name-prefix filtering off
        self.prefix_edit.setPlaceholderText("e.g. MCC_Child_  (leave empty for all)")
        self.prefix_edit.setFixedWidth(180)
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
        # Click the "✓" header cell to toggle every row's checkbox.
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        # Row checkboxes are checkable items now — one signal keeps the count live.
        self.table.itemChanged.connect(self._on_item_changed)
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

        self.btn_invert = QPushButton("Invert")
        self.btn_invert.setToolTip("Flip every row's tick state.")
        self.btn_invert.clicked.connect(self._invert_checked)
        row3.addWidget(self.btn_invert)

        row3.addStretch()
        self.summary_lbl = QLabel("0 / 0 selected")
        row3.addWidget(self.summary_lbl)

        self.btn_submit_payment = QPushButton("Submit appeal for checked")
        self.btn_submit_payment.setStyleSheet("background-color:#1a73e8; color:white; padding:8px 16px;")
        self.btn_submit_payment.setToolTip(
            "Suspicious Payment Activity questionnaire (11 questions, one\n"
            "shared set of answers applied to every checked account)."
        )
        self.btn_submit_payment.clicked.connect(self.submit_checked_payment)
        row3.addWidget(self.btn_submit_payment)

        self.btn_stop = QPushButton("Stop")
        self.btn_stop.clicked.connect(self.stop_submit)
        self.btn_stop.setEnabled(False)
        row3.addWidget(self.btn_stop)
        v.addLayout(row3)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Paste cookie -> Save -> Scan")

    def _build_payment_inputs(self, parent_layout):
        """Inline, collapsible group of the 11 Suspicious-Payment answers."""
        box = QGroupBox("Suspicious-Payment appeal answers  (one set, applied to every checked account)")
        box.setCheckable(True)
        box.setChecked(False)   # collapsed by default to keep the UI compact
        form = QFormLayout(box)
        self.payment_widgets: dict[str, object] = {}
        for fid, qtext, default in auto.PAYMENT_QUESTIONS:
            if fid in PAYMENT_BOOL_FIELDS:
                w = QComboBox(); w.addItems(["true", "false"])
                w.setCurrentText(default if default in ("true", "false") else "false")
            elif fid == "inputWhoOwnsPaymentInstrument":
                w = QComboBox(); w.addItems(["me", "someone_else"])
                w.setCurrentText(default or "me")
            elif fid == "inputPaymentOption":
                w = QComboBox(); w.addItems(["card", "bank", "other"])
                w.setCurrentText(default or "card")
            elif fid == "lastPaymentDate":
                w = QDateEdit()
                w.setCalendarPopup(True)          # full month calendar to pick from
                w.setDisplayFormat("yyyy-MM-dd")
                w.setDate(QDate.currentDate())    # default to today
                w.setMaximumDate(QDate.currentDate())  # a payment can't be in the future
            else:
                w = QLineEdit(default)
                if fid == "inputDomain":
                    w.setPlaceholderText("https://example.com/")
                elif fid == "inputBusinessModel":
                    w.setPlaceholderText("What the business does")
            self.payment_widgets[fid] = w
            form.addRow(qtext, w)
        # Collapse the body when unchecked so it doesn't eat vertical space.
        box.toggled.connect(
            lambda on: [w.setVisible(on) for w in self.payment_widgets.values()]
        )
        for w in self.payment_widgets.values():
            w.setVisible(False)
        parent_layout.addWidget(box)

    def _payment_answers_from_ui(self) -> dict:
        out = {}
        for fid, w in self.payment_widgets.items():
            if isinstance(w, QComboBox):
                out[fid] = w.currentText()
            elif isinstance(w, QDateEdit):
                d = w.date()
                # Match the browser's "2026-6-26" shape — no zero padding.
                out[fid] = f"{d.year()}-{d.month()}-{d.day()}"
            else:
                out[fid] = w.text().strip()
        return out

    # ---- helpers ----
    def append_log(self, msg: str):
        self.log.appendPlainText(msg)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    @staticmethod
    def _extract_cookie_from_curl(text: str) -> str:
        """If `text` looks like a cURL command, pull out the cookie value from
        the -b / --cookie flag or a -H 'Cookie: …' header. Return None when
        the input is plain key=value;... so the caller can pass it through."""
        if "curl " not in text and " -b " not in text and "Cookie:" not in text:
            return None

        import re as _re
        # Mac/Linux cURL: -b 'value' or -b $'value' or -b "value"
        # Windows cmd cURL: -b ^"value^"
        patterns = [
            r"-b\s+\$?'([^']{20,})'",
            r'-b\s+"([^"]{20,})"',
            r'-b\s+\^"(.+?)\^"',
            r"--cookie\s+\$?'([^']{20,})'",
            r'--cookie\s+"([^"]{20,})"',
            r"-H\s+\$?'[Cc]ookie:\s*([^']{20,})'",
            r'-H\s+"[Cc]ookie:\s*([^"]{20,})"',
            r'-H\s+\^"[Cc]ookie:\s*(.+?)\^"',
        ]
        for pat in patterns:
            m = _re.search(pat, text, _re.DOTALL)
            if m:
                value = m.group(1)
                # Strip Windows ^ escape characters that survived inside the match.
                value = value.replace("^", "")
                # Unescape ANSI-C backslash sequences cURL sometimes emits ($'...').
                value = value.replace(r"\!", "!").replace(r"\\", "\\")
                return value.strip()
        return None

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
        """Save the pasted cookies, merging with what's already on disk
        WHEN it's the same Google account (only rotating bits changed) and
        REPLACING when the user has switched to a different account.
        Also accepts a full cURL command: the cookie value is auto-extracted
        from the -b / --cookie flag or from a -H 'Cookie: ...' header."""
        text = self.cookie_edit.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Empty", "Cookie textarea is empty.")
            return

        text = self._extract_cookie_from_curl(text) or text

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

        # Identity fingerprint: __Secure-1PSID (or fallback SID) encodes the
        # Google account. If it changed, user switched accounts and we must
        # replace — merging would mix cookies from two accounts and break auth.
        def fp(d): return d.get("__Secure-1PSID") or d.get("SID") or ""
        same_account = (not existing) or (fp(new) and fp(existing) and fp(new) == fp(existing))

        if same_account:
            merged = {**existing, **new}
            mode = "merged (same account, rotating cookies refreshed)"
        else:
            merged = new
            mode = "REPLACED (different account detected — replaced wholesale)"

        COOKIE_FILE.write_text("; ".join(f"{k}={v}" for k, v in merged.items()),
                               encoding="utf-8")
        self.cookies = merged
        # Reflect the canonical state in the textarea too.
        self.cookie_edit.setPlainText(COOKIE_FILE.read_text(encoding="utf-8").strip())

        added = sorted(set(new) - set(existing))
        updated = sorted(set(new) & set(existing))
        self.append_log(f"Cookie {mode}: {len(merged)} total  (+{len(added)} new, {len(updated)} refreshed)")
        self.statusBar().showMessage(
            f"Cookie ready ({len(merged)} keys)  —  {mode}"
        )

        critical = ["SID", "HSID", "APISID", "SAPISID",
                    "__Secure-1PSID", "__Secure-3PSID"]
        missing = [c for c in critical if c not in merged]
        if missing:
            QMessageBox.warning(
                self, "Missing cookies",
                "These critical cookies are missing — auth will likely fail:\n\n"
                + ", ".join(missing) +
                "\n\nPaste the FULL Cookie header value from DevTools "
                "(F12 → Network → click a request → Headers → Cookie:)."
            )

    def scan(self):
        if not self.cookies:
            QMessageBox.warning(self, "No cookie", "Save cookie first.")
            return
        self.btn_scan.setEnabled(False)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self.statusBar().showMessage(
            "Scanning… large MCCs take a while to download + parse; please wait."
        )
        self.append_log("--- Scanning ---")

        # Accept either a raw customer_id or a full Ads URL containing ocid=…
        raw_mcc = self.mcc_edit.text().strip()
        import re as _re
        m = _re.search(r"ocid=(\d{6,12})", raw_mcc)
        forced_mcc = m.group(1) if m else (raw_mcc if raw_mcc.isdigit() else "")
        if raw_mcc and not forced_mcc:
            self.append_log(f"WARN: couldn't parse MCC from {raw_mcc!r}; auto-picking.")
        elif forced_mcc and forced_mcc != raw_mcc:
            self.append_log(f"Extracted MCC {forced_mcc} from URL.")

        self.scan_worker = ScanWorker(
            self.cookies,
            self.authuser_edit.text().strip() or "0",
            forced_mcc,
        )
        self.scan_worker.log.connect(self.append_log)
        self.scan_worker.done.connect(self.on_scan_done)
        self.scan_worker.failed.connect(self.on_scan_failed)
        self.scan_worker.mccs_needed.connect(self.on_mccs_needed)
        self.scan_worker.start()

    def on_mccs_needed(self, mccs: list):
        """Multiple MCCs available — let the user pick one, then re-scan."""
        QApplication.restoreOverrideCursor()
        self.btn_scan.setEnabled(True)
        items = [f"{ocid}  {label}".strip() for ocid, label in mccs]
        choice, ok = QInputDialog.getItem(
            self,
            "Pick an MCC",
            f"Your Google account can access {len(mccs)} MCCs.\n"
            "Choose which one to scan:",
            items,
            0,
            False,  # not editable
        )
        if not ok:
            self.append_log("Scan cancelled — no MCC picked.")
            return
        # The chosen item starts with the ocid; remember it in the field
        # so subsequent scans skip the picker.
        picked_ocid = choice.split()[0]
        self.mcc_edit.setText(picked_ocid)
        self.append_log(f"Picked MCC: {picked_ocid} — re-scanning...")
        self.scan()

    def on_scan_failed(self, msg: str):
        QApplication.restoreOverrideCursor()
        self.btn_scan.setEnabled(True)
        self.append_log(f"SCAN FAILED: {msg}")
        QMessageBox.critical(self, "Scan failed", msg)

    def on_scan_done(self, cfg: dict, accounts: list):
        QApplication.restoreOverrideCursor()
        self.btn_scan.setEnabled(True)
        self.cfg = cfg
        self.accounts = accounts
        self._populate_table(accounts)

        # Single-account fallback: synthetic "(your account)" entries from
        # auto.list_accounts. Tick the primary one (and optionally the alt id).
        single_rows = [
            i for i, a in enumerate(accounts)
            if (a.get("descriptive_name") or "").startswith("(your account)")
        ]
        if single_rows and len(single_rows) == len(accounts):
            self._set_row_checked(single_rows[0], True)
            msg = (
                "Single-account mode: no MCC sub-accounts found. "
                "Primary customer (__c) added as target."
            )
            if len(single_rows) > 1:
                msg += (
                    f" Alt ID (ocid) also listed — if the first one returns "
                    f"ENTITY_DOES_NOT_EXIST, untick row 1 and tick row 2."
                )
            self.append_log(msg)
            self.statusBar().showMessage(
                "Single account ready. Click Submit to re-appeal."
            )
        else:
            self.apply_filter()
            self.statusBar().showMessage(
                f"Scanned: {len(accounts)} accounts. Apply filter and click Submit."
            )

    def _populate_table(self, accounts: list):
        # Building thousands of rows is the slowest part of a scan. Freeze
        # painting + signals while we fill the model so the GUI doesn't lock
        # up ("Not Responding") on large MCCs, then re-enable once at the end.
        self._populating = True
        self.table.setUpdatesEnabled(False)
        self.table.setSortingEnabled(False)
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(len(accounts))
            for r, a in enumerate(accounts):
                # Checkable item instead of a per-row QCheckBox widget — an
                # order of magnitude cheaper to create and keeps scrolling smooth.
                chk = QTableWidgetItem()
                chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled
                             | Qt.ItemIsSelectable)
                chk.setCheckState(Qt.Unchecked)
                self.table.setItem(r, 0, chk)

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
        finally:
            self.table.blockSignals(False)
            self.table.setUpdatesEnabled(True)
            self._populating = False
        self._update_summary()

    def apply_filter(self):
        prefix = self.prefix_edit.text()
        status = self.status_spin.value()
        skip_done = self.skip_done_chk.isChecked()
        skip_set = {"OK", "PENDING"} if skip_done else set()
        n = skipped = 0
        self._populating = True
        self.table.blockSignals(True)
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
            self._set_row_checked(r, match)
            if match:
                n += 1
        self.table.blockSignals(False)
        self._populating = False
        self._update_summary()
        msg = f"Filter: status={status}, prefix={prefix!r} -> {n} matches"
        if skip_done and skipped:
            msg += f"  (skipped {skipped} already OK/PENDING)"
        self.append_log(msg)

    def _is_row_checked(self, r: int) -> bool:
        it = self.table.item(r, 0)
        return it is not None and it.checkState() == Qt.Checked

    def _set_row_checked(self, r: int, v: bool):
        it = self.table.item(r, 0)
        if it is not None:
            it.setCheckState(Qt.Checked if v else Qt.Unchecked)

    def _set_all_checked(self, v: bool):
        self._populating = True
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            self._set_row_checked(r, v)
        self.table.blockSignals(False)
        self._populating = False
        self._update_summary()

    def _invert_checked(self):
        self._populating = True
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            self._set_row_checked(r, not self._is_row_checked(r))
        self.table.blockSignals(False)
        self._populating = False
        self._update_summary()

    def _on_header_clicked(self, col: int):
        # Click the "✓" column header to toggle every row.
        if col != 0:
            return
        total = self.table.rowCount()
        if total == 0:
            return
        checked = sum(1 for r in range(total) if self._is_row_checked(r))
        # All ticked already → untick all; otherwise tick all.
        self._set_all_checked(checked != total)

    def _on_item_changed(self, item):
        # A row checkbox toggled — refresh the count (skip during bulk fills).
        if not getattr(self, "_populating", False) and item.column() == 0:
            self._update_summary()

    def _update_summary(self):
        total = self.table.rowCount()
        n = sum(1 for r in range(total) if self._is_row_checked(r))
        self.summary_lbl.setText(f"{n} / {total} selected")

    def submit_checked_payment(self):
        """Submit the 'Suspicious Payment Activity' questionnaire for every
        checked row, using one shared set of answers."""
        if not self.cfg:
            QMessageBox.warning(self, "Not scanned", "Scan first.")
            return
        targets = []
        for r in range(self.table.rowCount()):
            if self._is_row_checked(r):
                cid = self.table.item(r, 1).text()
                name = self.table.item(r, 2).text()
                self.table.item(r, 6).setText("")
                self.table.item(r, 7).setText("")
                targets.append((r, cid, name))
        if not targets:
            QMessageBox.information(self, "Empty", "No rows checked.")
            return

        answers = self._payment_answers_from_ui()

        delay = self.delay_spin.value()
        threads = self.threads_spin.value()
        eta = len(targets) * delay / 60 / max(1, threads)
        ans = QMessageBox.question(
            self, "Confirm Suspicious-Payment appeal",
            f"Submit the payment questionnaire for {len(targets)} accounts "
            f"with {threads} thread(s)?  (~{eta:.1f} min)\n\n"
            f"website={answers.get('inputDomain') or '(empty)'}  "
            f"country={answers.get('inputCountries')}",
        )
        if ans != QMessageBox.Yes:
            return

        self.append_log(
            f"--- Submitting {len(targets)} SUSPICIOUS-PAYMENT appeals "
            f"({threads} threads) ---"
        )
        self.append_log(f"Answers: {answers}")
        self.btn_submit_payment.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_scan.setEnabled(False)

        self.submit_worker = SubmitWorker(
            self.cookies, self.cfg, targets, delay,
            threads=threads, payment_answers=answers,
        )
        self.submit_worker.log.connect(self.append_log)
        self.submit_worker.progress.connect(self.on_submit_progress)
        self.submit_worker.done.connect(self.on_submit_done)
        self.submit_worker.start()
        self._results = []

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
        # For failures, also dump the body into the log so the user can read
        # the specific error text without inspecting the JSON file.
        if tag not in ("OK", "PENDING", "dry-run"):
            self.append_log(f"  └─ {cid}: {body[:300]}")
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
        self.btn_submit_payment.setEnabled(True)
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
