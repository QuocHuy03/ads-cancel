"""
AdsCancel — Create MCC sub-account UI (PyQt5).

Separate from ui.py. Handles:
  - Cookie paste (raw or full cURL — same helper as ui.py)
  - Auto-discover session via auto.discover_session
  - Solve Google reCAPTCHA via NextCaptcha (https://nextcaptcha.com)
  - POST ClientCustomerSignupService/Mutate to create N child accounts
  - Auto-extract DRAPT from RAPT cookie for 2FA bypass

Run:    python create_mcc_ui.py
Deps:   pip install PyQt5 requests
"""

import json
import re
import sys
import time
from urllib.parse import urlencode

import requests
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDoubleSpinBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QSplitter, QStatusBar, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

import auto
import nextcaptcha

HERE = auto.data_dir()
COOKIE_FILE = HERE / "cookie.txt"
SESSION_EXTRAS = HERE / "session_extras.json"   # DRAPT + user-context sidecar

# Google Ads MCC create-account reCAPTCHA Enterprise sitekey (the same one
# the browser loads on /aw/accounts → "Create account" dialog). If Google
# changes it, the user can override via the UI field.
DEFAULT_SITEKEY = "6LfC8vEqAAAAALvQX15hR9GJ3V6jNKeMx6rBTcIr"  # reCAPTCHA v2 checkbox
DEFAULT_SITE_URL = "https://ads.google.com"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")


# ---------- network helpers ----------

def now_ms_id() -> str:
    return str(int(time.time() * 1000))


def shared_headers(cfg: dict, mcc_ocid: str, extras: dict = None) -> dict:
    """Headers that mirror what Chrome sends to the MCC Mutate endpoint.
    `extras` is a dict of any per-session header captured from the user's
    cURL (user-context, request-context, x-client-data, etc.) — they are
    merged in last so they win over our defaults."""
    h = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://ads.google.com",
        "referer": (
            f"https://ads.google.com/aw/accounts?ocid={mcc_ocid}"
            f"&workspaceId=0&euid={cfg['login_user_id']}"
            f"&__u={cfg['user_id']}&uscid={mcc_ocid}"
            f"&__c={cfg['customer_id']}&authuser={cfg['authuser']}"
        ),
        # Client hints that the browser always sends from Chrome 149 on Win.
        "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": UA,
        "x-framework-xsrf-token": cfg["xsrf_token"],
        "x-same-domain": "1",
    }
    if extras:
        for k, v in extras.items():
            if v:
                h[k] = v
    return h


def mutate_create_child(session, cookies, cfg, mcc_ocid, currency, tz,
                        country, recaptcha_token,
                        drapt: str = "", extras: dict = None) -> dict:
    url = (
        "https://ads.google.com/aw_mcc/_/rpc/ClientCustomerSignupService/Mutate"
        f"?authuser={cfg['authuser']}&xt=awn"
        "&rpcTrackingId=ClientCustomerSignupService.Mutate%3A1"
        f"&f.sid={cfg['f_sid']}"
    )
    ar = {
        "1": {"3": {"1": mcc_ocid}},
        "2": {
            "3": currency,
            "4": "",
            "5": tz,
            "7": 30,
            "8": False,
            "9": country,
            "10": 1,
            "11": {"1": ""},
        },
        "3": [{"1": "useUfoFlow"}, {"1": "managerUnqualified"}],
        "4": recaptcha_token,
    }
    body = {
        "hl": "en_US",
        "__lu": cfg["login_user_id"],
        "__u":  cfg["user_id"],
        "__c":  cfg["customer_id"],
        "f.sid": cfg["f_sid"],
        "ps": "aw",
        "__ar": json.dumps(ar, separators=(",", ":")),
        "activityContext": "MccAccountsTable.AccountRecaptchaDialog.NextButtonClicked",
        "requestPriority": "HIGH_LATENCY_SENSITIVE",
        "activityType": "INTERACTIVE",
        "activityId": now_ms_id(),
        "uniqueFingerprint": f"{cfg['f_sid']}_{now_ms_id()}_1",
        "previousPlace": "/aw/accounts",
        "activityName": "MccAccountsTable.AccountRecaptchaDialog.NextButtonClicked",
        "destinationPlace": "/aw/accounts",
    }
    # DRAPT is the proof-of-2FA token — sent as a body field, not a cookie.
    if drapt:
        body["drapt"] = drapt
    r = session.post(url,
                     headers=shared_headers(cfg, mcc_ocid, extras),
                     cookies=cookies, data=urlencode(body), timeout=60)
    return _wrap(r)


def publish_reauth(session, cookies, cfg, mcc_ocid, drapt, challenge_id,
                   extras: dict = None) -> dict:
    url = (
        "https://ads.google.com/aw/_/rpc/PublishReauthMessageService/PublishReauthMessage"
        f"?authuser={cfg['authuser']}&xt=awn"
        "&rpcTrackingId=PublishReauthMessageService.PublishReauthMessage%3A2"
        f"&f.sid={cfg['f_sid']}"
    )
    now = time.time()
    ar = {
        "2": 3,
        "3": mcc_ocid,
        "4": cfg["login_user_id"],
        "8":  {"1": str(int(now)),      "2": int((now % 1) * 1e9)},
        "9":  {"1": str(int(now) + 60), "2": int((now % 1) * 1e9)},
        "12": challenge_id,
    }
    body = {
        "hl": "en_US",
        "__lu": cfg["login_user_id"], "__u": cfg["user_id"], "__c": cfg["customer_id"],
        "f.sid": cfg["f_sid"], "ps": "aw",
        "__ar": json.dumps(ar, separators=(",", ":")),
        "drapt": drapt,
        "activityContext": "Anonymous",
        "requestPriority": "HIGH_LATENCY_SENSITIVE",
        "activityType": "ANONYMOUS",
        "activityId": now_ms_id(),
        "uniqueFingerprint": f"{cfg['f_sid']}_{now_ms_id()}_1",
        "destinationPlace": "/aw/accounts",
    }
    r = session.post(url, headers=shared_headers(cfg, mcc_ocid, extras),
                     cookies=cookies, data=urlencode(body), timeout=30)
    return _wrap(r)


def _wrap(r) -> dict:
    raw = r.text
    cleaned = auto.strip_xssi(raw)
    try:
        data = json.loads(cleaned)
    except Exception:
        data = None
    return {"http": r.status_code, "raw": raw, "data": data}


def classify_mutate(data: dict) -> tuple[str, str, str]:
    """Return (tag, detail, new_customer_id). new_customer_id only set on OK."""
    if not isinstance(data, dict):
        return "BAD_JSON", "", ""
    errs = data.get("2", {}).get("2") if isinstance(data.get("2"), dict) else None
    if errs:
        e = errs[0]
        code = e.get("3", "ERROR")
        # detect reauth so caller can run the 2FA acknowledgement
        if code == "AUTH_ERROR_REAUTH_PROOF_TOKEN_REQUIRED":
            chid = (e.get("10", {}).get("7", {}) or {}).get("2", "")
            return "REAUTH_REQUIRED", chid, ""
        return code, e.get("1", ""), ""

    # Success path: the new customer id usually shows up under "1" somewhere.
    new_cid = ""
    if isinstance(data.get("1"), dict):
        # Walk the dict looking for any 10-digit number that's not 0.
        flat = json.dumps(data["1"])
        m = re.search(r'"(\d{9,11})"', flat)
        if m:
            new_cid = m.group(1)
    return "OK", "", new_cid


# ---------- workers ----------

class CreateWorker(QThread):
    log = pyqtSignal(str)
    progress = pyqtSignal(int, str, str, str)   # row, tag, detail, customer_id
    done = pyqtSignal()

    def __init__(self, cookies, cfg, mcc_ocid, params, count, api_key,
                 sitekey, delay, drapt: str = "", extras: dict = None):
        super().__init__()
        self.cookies = cookies
        self.cfg = cfg
        self.mcc_ocid = mcc_ocid
        self.params = params   # (currency, timezone, country)
        self.count = count
        self.api_key = api_key
        self.sitekey = sitekey
        self.delay = delay
        self.drapt = drapt
        self.extras = extras or {}
        self._stop = False

    def stop(self):
        self._stop = True

    def _solve(self) -> str:
        return nextcaptcha.solve_recaptcha_v2(
            self.api_key,
            website_url=DEFAULT_SITE_URL,
            website_key=self.sitekey,
            is_invisible=False,
            on_progress=lambda s: self.log.emit(f"    captcha: {s}"),
        )

    def run(self):
        currency, tz, country = self.params
        session = requests.Session()

        for i in range(self.count):
            if self._stop:
                self.log.emit("Stopped by user.")
                break

            self.log.emit(f"\n[{i+1}/{self.count}] solving reCAPTCHA via NextCaptcha...")
            try:
                token = self._solve()
            except Exception as e:
                self.log.emit(f"    captcha FAILED: {e}")
                self.progress.emit(i, "CAPTCHA_FAIL", str(e), "")
                continue

            self.log.emit(f"[{i+1}/{self.count}] POST Mutate ({currency}, {tz}, {country})...")
            res = mutate_create_child(session, self.cookies, self.cfg,
                                       self.mcc_ocid, currency, tz, country, token,
                                       drapt=self.drapt, extras=self.extras)
            tag, detail, new_cid = classify_mutate(res["data"]) if res["data"] else (
                f"HTTP{res['http']}", res["raw"][:200], "")

            # If reauth needed: prefer DRAPT from the body-field source the
            # browser uses; fall back to RAPT cookie if user pasted it that way.
            if tag == "REAUTH_REQUIRED":
                drapt = self.drapt
                if not drapt:
                    rapt = self.cookies.get("RAPT", "")
                    m = re.search(r"DRAPT:([^;+\s]+)", rapt)
                    drapt = m.group(1) if m else ""
                if not drapt:
                    self.log.emit(
                        "\n    ┌──────────────── 2FA NEEDED ────────────────\n"
                        f"    │ challenge_id: {detail}\n"
                        "    │\n"
                        "    │ Do this ONCE in Chrome (same Google account):\n"
                        "    │  1. Open https://ads.google.com/aw/accounts\n"
                        f"    │     ?ocid={self.mcc_ocid}\n"
                        "    │  2. Click '+ Create account' → currency/timezone/country\n"
                        "    │  3. Google shows '2-Step Verification' → pass it\n"
                        "    │  4. CANCEL the create dialog (don't actually create)\n"
                        "    │  5. F12 → Network → any request → Copy as cURL\n"
                        "    │  6. Paste into this UI's cookie box → Save cookie\n"
                        "    │  7. Click 'Create accounts' here again\n"
                        "    │\n"
                        "    │ After step 5 the cookie includes RAPT=DRAPT:<token>\n"
                        "    │ which this tool auto-extracts and reuses for every\n"
                        "    │ subsequent Mutate in the same browser session.\n"
                        "    └────────────────────────────────────────────"
                    )
                    self.progress.emit(i, "REAUTH_NEEDED", detail, "")
                    continue
                self.log.emit("    REAUTH_REQUIRED — calling PublishReauthMessage...")
                pub = publish_reauth(session, self.cookies, self.cfg,
                                     self.mcc_ocid, drapt, detail, extras=self.extras)
                if pub["data"] and pub["data"].get("1") == 2:
                    self.log.emit("    reauth ack OK, retrying Mutate...")
                    self.cookies["RAPT"] = f"AUTH:0+TYPE:6+DRAPT:{drapt}"
                    res2 = mutate_create_child(session, self.cookies, self.cfg,
                                                self.mcc_ocid, currency, tz, country, token,
                                                drapt=drapt, extras=self.extras)
                    tag, detail, new_cid = classify_mutate(res2["data"]) if res2["data"] else (
                        f"HTTP{res2['http']}", res2["raw"][:200], "")
                else:
                    self.log.emit(f"    reauth ack FAILED: {pub['raw'][:200]}")
                    tag = "REAUTH_ACK_FAIL"

            label = f"{tag}" + (f" -> {new_cid}" if new_cid else "")
            self.log.emit(f"[{i+1}/{self.count}] {label}  {detail[:120]}")
            self.progress.emit(i, tag, detail, new_cid)

            if i + 1 < self.count and self.delay > 0:
                time.sleep(self.delay)

        self.done.emit()


# ---------- UI ----------

TAG_COLORS = {
    "OK":             QColor("#cfe9c9"),
    "CAPTCHA_FAIL":   QColor("#ffd9b3"),
    "REAUTH_NEEDED":  QColor("#ffe8a3"),
    "REAUTH_ACK_FAIL":QColor("#ffc9c9"),
}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AdsCancel — Create MCC sub-accounts")
        self.resize(1100, 760)

        self.cookies = {}
        self.cfg = None
        self.worker = None
        self._build_ui()
        self._load_existing_cookie()

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)

        # ---- Cookie ----
        v.addWidget(QLabel("Cookie (paste full cURL OR raw Cookie header):"))
        self.cookie_edit = QPlainTextEdit()
        self.cookie_edit.setPlaceholderText(
            "Either full cURL from DevTools or raw 'SID=...; HSID=...; ...'"
        )
        self.cookie_edit.setMaximumHeight(80)
        self.cookie_edit.setFont(QFont("Consolas", 9))
        v.addWidget(self.cookie_edit)

        rowA = QHBoxLayout()
        self.btn_save_cookie = QPushButton("Save cookie")
        self.btn_save_cookie.clicked.connect(self.save_cookie)
        rowA.addWidget(self.btn_save_cookie)

        rowA.addWidget(QLabel("MCC ID or URL:"))
        self.mcc_edit = QLineEdit()
        self.mcc_edit.setPlaceholderText("ocid=... or full URL or bare digits")
        self.mcc_edit.setMinimumWidth(280)
        rowA.addWidget(self.mcc_edit, stretch=1)

        rowA.addWidget(QLabel("authuser:"))
        self.authuser_edit = QLineEdit("0")
        self.authuser_edit.setFixedWidth(40)
        rowA.addWidget(self.authuser_edit)

        self.btn_discover = QPushButton("Discover session")
        self.btn_discover.clicked.connect(self.discover)
        rowA.addWidget(self.btn_discover)
        v.addLayout(rowA)

        # ---- Account params ----
        rowB = QHBoxLayout()
        rowB.addWidget(QLabel("Currency:"))
        self.currency_edit = QComboBox()
        self.currency_edit.setEditable(True)
        self.currency_edit.addItems(["USD", "VND", "EUR", "GBP", "JPY", "ILS",
                                     "INR", "AUD", "CAD", "SGD", "THB"])
        self.currency_edit.setCurrentText("USD")
        rowB.addWidget(self.currency_edit)

        rowB.addWidget(QLabel("Timezone:"))
        self.tz_edit = QComboBox()
        self.tz_edit.setEditable(True)
        self.tz_edit.addItems([
            "Asia/Ho_Chi_Minh", "Asia/Bangkok", "Asia/Singapore", "Asia/Tokyo",
            "Asia/Jerusalem", "Europe/Moscow", "Europe/London", "America/New_York",
            "America/Los_Angeles", "Australia/Sydney",
        ])
        self.tz_edit.setCurrentText("Asia/Ho_Chi_Minh")
        rowB.addWidget(self.tz_edit)

        rowB.addWidget(QLabel("Country:"))
        self.country_edit = QComboBox()
        self.country_edit.setEditable(True)
        self.country_edit.addItems(["VN", "US", "GB", "TH", "SG", "JP", "IL", "RU",
                                    "IN", "AU", "CA"])
        self.country_edit.setCurrentText("VN")
        rowB.addWidget(self.country_edit)

        rowB.addWidget(QLabel("Count:"))
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 500)
        self.count_spin.setValue(1)
        self.count_spin.setFixedWidth(70)
        rowB.addWidget(self.count_spin)

        rowB.addWidget(QLabel("Delay (s):"))
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(0.0, 60.0)
        self.delay_spin.setValue(2.0)
        self.delay_spin.setSingleStep(0.5)
        self.delay_spin.setFixedWidth(70)
        rowB.addWidget(self.delay_spin)
        rowB.addStretch()
        v.addLayout(rowB)

        # ---- NextCaptcha ----
        rowC = QHBoxLayout()
        rowC.addWidget(QLabel("NextCaptcha API key:"))
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setPlaceholderText("get from https://dashboard.nextcaptcha.com")
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        rowC.addWidget(self.api_key_edit, stretch=1)

        self.btn_balance = QPushButton("Check balance")
        self.btn_balance.clicked.connect(self.check_balance)
        rowC.addWidget(self.btn_balance)

        rowC.addWidget(QLabel("sitekey (reCAPTCHA v2):"))
        self.sitekey_edit = QLineEdit(DEFAULT_SITEKEY)
        self.sitekey_edit.setMinimumWidth(320)
        self.sitekey_edit.setToolTip(
            "reCAPTCHA v2 checkbox data-sitekey for the MCC verification dialog.\n"
            "Default is what Google currently uses (extracted from aw_mcc dart bundle).\n"
            "If Google rotates it: open the verification dialog → DevTools Elements →\n"
            "find <div class='g-recaptcha' data-sitekey='...'> and copy the value."
        )
        rowC.addWidget(self.sitekey_edit, stretch=1)
        v.addLayout(rowC)

        # DRAPT + session-bound headers — captured from a real cURL AFTER 2FA.
        # We hold the headers (user-context, request-context, x-client-data,
        # x-browser-validation, x-browser-*) inside self.extra_headers and
        # show a single status label so the row stays compact.
        self.extra_headers: dict = {}
        rowD = QHBoxLayout()
        rowD.addWidget(QLabel("DRAPT:"))
        self.drapt_edit = QLineEdit()
        self.drapt_edit.setPlaceholderText("AEjHL4N... (auto-extracted from cURL body's drapt= param)")
        self.drapt_edit.setEchoMode(QLineEdit.Password)
        self.drapt_edit.setToolTip(
            "2FA proof token. After you pass 2-Step Verification in Chrome\n"
            "and capture a Mutate cURL, the body has 'drapt=AEjHL4N...'.\n"
            "Paste that value here (or paste full cURL and it auto-fills)."
        )
        rowD.addWidget(self.drapt_edit, stretch=1)

        self.extras_lbl = QLabel("session headers: none")
        self.extras_lbl.setToolTip(
            "Session-bound headers Google checks (user-context, request-context,\n"
            "x-client-data, x-browser-validation, ...). Auto-captured when you\n"
            "paste a full cURL into the cookie box."
        )
        rowD.addWidget(self.extras_lbl)

        self.btn_save_extras = QPushButton("Save extras")
        self.btn_save_extras.setToolTip("Persist DRAPT + extra headers to session_extras.json")
        self.btn_save_extras.clicked.connect(self._save_session_extras)
        rowD.addWidget(self.btn_save_extras)
        v.addLayout(rowD)

        # ---- Splitter: table + log ----
        split = QSplitter(Qt.Vertical)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["#", "result", "detail", "new customer_id"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        split.addWidget(self.table)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Consolas", 9))
        split.addWidget(self.log)
        split.setSizes([400, 320])
        v.addWidget(split, stretch=1)

        # ---- Run/Stop ----
        rowD = QHBoxLayout()
        rowD.addStretch()
        self.btn_run = QPushButton("Create accounts")
        self.btn_run.setStyleSheet("background-color:#1a73e8; color:white; padding:8px 16px;")
        self.btn_run.clicked.connect(self.run_create)
        rowD.addWidget(self.btn_run)
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.clicked.connect(self.stop_run)
        self.btn_stop.setEnabled(False)
        rowD.addWidget(self.btn_stop)
        v.addLayout(rowD)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("1) Paste cookie → 2) Discover → 3) Set params + API key → 4) Create")

    # ---- helpers ----
    def append_log(self, msg: str):
        self.log.appendPlainText(msg)
        self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    # Browser headers Google checks alongside the regular auth ones. The
    # values are session-bound (or per-page-load) — captured from a real cURL.
    EXTRA_HEADERS = [
        "user-context",
        "request-context",
        "x-client-data",
        "x-browser-validation",
        "x-browser-channel",
        "x-browser-copyright",
        "x-browser-year",
    ]

    @staticmethod
    def _grep_header(text: str, name: str) -> str:
        pats = [
            rf"-H\s+\$?'{name}:\s*([^']{{1,4000}})'",
            rf'-H\s+"{name}:\s*([^"]{{1,4000}})"',
            rf'-H\s+\^"{name}:\s*(.+?)\^"',
        ]
        for p in pats:
            m = re.search(p, text, re.DOTALL | re.IGNORECASE)
            if m:
                return m.group(1).replace("^", "").strip()
        return ""

    @classmethod
    def _extract_from_curl(cls, text: str) -> dict:
        """Return cookie + drapt + every extra header we know Google checks."""
        out = {"cookie": "", "drapt": "", "headers": {}}
        if "curl " not in text and " -b " not in text and "Cookie:" not in text:
            return out

        # Cookie (-b / --cookie / -H 'Cookie:')
        for p in (
            r"-b\s+\$?'([^']{20,})'",
            r'-b\s+"([^"]{20,})"',
            r'-b\s+\^"(.+?)\^"',
            r"--cookie\s+\$?'([^']{20,})'",
            r'--cookie\s+"([^"]{20,})"',
            r"-H\s+\$?'[Cc]ookie:\s*([^']{20,})'",
            r'-H\s+"[Cc]ookie:\s*([^"]{20,})"',
            r'-H\s+\^"[Cc]ookie:\s*(.+?)\^"',
        ):
            m = re.search(p, text, re.DOTALL)
            if m:
                out["cookie"] = m.group(1).replace("^", "").strip()
                break

        # DRAPT in body
        m = re.search(r"[?&]drapt=([A-Za-z0-9_\-]+)", text)
        if m:
            out["drapt"] = m.group(1)

        # Every extra header we know to mirror.
        for name in cls.EXTRA_HEADERS:
            val = cls._grep_header(text, name)
            if val:
                out["headers"][name] = val
        return out

    # Backwards-compat shim — used to be a separate method.
    @classmethod
    def _extract_cookie_from_curl(cls, text):
        return cls._extract_from_curl(text)["cookie"] or None

    def _load_existing_cookie(self):
        if COOKIE_FILE.exists():
            try:
                self.cookie_edit.setPlainText(COOKIE_FILE.read_text(encoding="utf-8").strip())
                self.cookies = auto.load_cookies(COOKIE_FILE)
                self.statusBar().showMessage(f"Loaded cookie.txt ({len(self.cookies)} keys)")
            except Exception as e:
                self.append_log(f"Failed to load cookie.txt: {e}")
        # Restore DRAPT + extra session headers from sidecar.
        if SESSION_EXTRAS.exists():
            try:
                d = json.loads(SESSION_EXTRAS.read_text(encoding="utf-8"))
                self.drapt_edit.setText(d.get("drapt", ""))
                self.extra_headers = d.get("headers", {}) or {}
                self._refresh_extras_label()
            except Exception as e:
                self.append_log(f"Failed to read session_extras.json: {e}")

    def _refresh_extras_label(self):
        if not self.extra_headers:
            self.extras_lbl.setText("session headers: none")
        else:
            names = ", ".join(sorted(self.extra_headers.keys()))
            self.extras_lbl.setText(f"session headers: {len(self.extra_headers)} ({names})")

    def _save_session_extras(self):
        d = {
            "drapt": self.drapt_edit.text().strip(),
            "headers": self.extra_headers,
        }
        try:
            SESSION_EXTRAS.write_text(json.dumps(d, indent=2), encoding="utf-8")
            self.append_log(
                f"Saved session_extras.json  (DRAPT={'yes' if d['drapt'] else 'no'}, "
                f"headers={len(self.extra_headers)})"
            )
        except Exception as e:
            self.append_log(f"Save extras failed: {e}")

    def save_cookie(self):
        text = self.cookie_edit.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "Empty", "Cookie textarea is empty.")
            return

        # Pull cookie + drapt + user-context all at once if the user pasted a cURL.
        extracted = self._extract_from_curl(text)
        if extracted["cookie"]:
            text = extracted["cookie"]

        # Update the DRAPT field + session-bound headers, persist to sidecar.
        if extracted["drapt"]:
            self.drapt_edit.setText(extracted["drapt"])
        if extracted["headers"]:
            self.extra_headers.update(extracted["headers"])
            self._refresh_extras_label()
        self._save_session_extras()

        def parse(s):
            out = {}
            for part in s.replace("\n", ";").split(";"):
                part = part.strip()
                if part and "=" in part:
                    k, _, v = part.partition("=")
                    out[k.strip()] = v.strip()
            return out

        existing = parse(COOKIE_FILE.read_text(encoding="utf-8")) if COOKIE_FILE.exists() else {}
        new = parse(text)
        def fp(d): return d.get("__Secure-1PSID") or d.get("SID") or ""
        same = not existing or (fp(new) and fp(existing) and fp(new) == fp(existing))
        merged = {**existing, **new} if same else new
        COOKIE_FILE.write_text("; ".join(f"{k}={v}" for k, v in merged.items()),
                               encoding="utf-8")
        self.cookies = merged
        self.cookie_edit.setPlainText(COOKIE_FILE.read_text(encoding="utf-8").strip())
        mode = "merged" if same else "REPLACED"
        self.append_log(f"Cookie {mode}: {len(merged)} total keys "
                        f"(RAPT={'yes' if 'RAPT' in merged else 'no'})")
        self.statusBar().showMessage(f"Cookie ready ({len(merged)} keys; mode={mode})")

    def discover(self):
        if not self.cookies:
            QMessageBox.warning(self, "No cookie", "Save cookie first."); return
        raw_mcc = self.mcc_edit.text().strip()
        m = re.search(r"ocid=(\d{6,12})", raw_mcc)
        forced = m.group(1) if m else (raw_mcc if raw_mcc.isdigit() else "")
        self.append_log(f"Discovering session (forced_mcc={forced or 'auto'})...")
        try:
            session = requests.Session()
            self.cfg = auto.discover_session(
                session, self.cookies,
                self.authuser_edit.text().strip() or "0",
                forced,
            )
        except auto.MultipleMCCsError as e:
            ocids = [o for o, _ in e.mccs]
            QMessageBox.warning(
                self, "Multiple MCCs",
                "Pick one and paste into 'MCC ID or URL':\n\n" + "\n".join(ocids),
            )
            return
        except SystemExit as e:
            QMessageBox.critical(self, "Discovery failed", str(e)); return

        self.append_log(
            f"  MCC={self.cfg['manager_customer_id']}  __u={self.cfg['user_id']}  "
            f"__c={self.cfg['customer_id']}  f.sid={self.cfg['f_sid']}"
        )
        self.statusBar().showMessage(
            f"Discovered MCC {self.cfg['manager_customer_id']} — ready to create."
        )
        if forced != self.cfg["manager_customer_id"]:
            self.mcc_edit.setText(self.cfg["manager_customer_id"])

    def check_balance(self):
        api_key = self.api_key_edit.text().strip()
        if not api_key:
            QMessageBox.warning(self, "No key", "Paste NextCaptcha API key first."); return
        try:
            bal = nextcaptcha.get_balance(api_key)
            self.append_log(f"NextCaptcha balance: ${bal:.4f}")
            self.statusBar().showMessage(f"NextCaptcha balance: ${bal:.4f}")
        except Exception as e:
            QMessageBox.critical(self, "Balance error", str(e))

    def run_create(self):
        if not self.cfg:
            QMessageBox.warning(self, "Not discovered", "Click 'Discover session' first."); return
        api_key = self.api_key_edit.text().strip()
        if not api_key:
            QMessageBox.warning(self, "No API key", "Paste NextCaptcha API key."); return
        sitekey = self.sitekey_edit.text().strip()
        if not sitekey:
            QMessageBox.warning(self, "No sitekey", "Sitekey is empty."); return

        n = self.count_spin.value()
        params = (self.currency_edit.currentText().strip(),
                  self.tz_edit.currentText().strip(),
                  self.country_edit.currentText().strip())

        ans = QMessageBox.question(
            self, "Confirm",
            f"Create {n} sub-account(s) under MCC {self.cfg['manager_customer_id']}?\n"
            f"Currency={params[0]}  TZ={params[1]}  Country={params[2]}\n"
            f"NextCaptcha (V2 checkbox) cost ≈ ${n * 0.002:.4f}",
        )
        if ans != QMessageBox.Yes:
            return

        # Pre-fill the table with N empty rows.
        self.table.setRowCount(0)
        for i in range(n):
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(str(i + 1)))
            self.table.setItem(r, 1, QTableWidgetItem(""))
            self.table.setItem(r, 2, QTableWidgetItem(""))
            self.table.setItem(r, 3, QTableWidgetItem(""))

        self.append_log(f"\n=== Creating {n} sub-account(s) ===")
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._save_session_extras()
        self.worker = CreateWorker(
            self.cookies, self.cfg, self.cfg["manager_customer_id"],
            params, n, api_key, sitekey,
            self.delay_spin.value(),
            drapt=self.drapt_edit.text().strip(),
            extras=dict(self.extra_headers),
        )
        self.worker.log.connect(self.append_log)
        self.worker.progress.connect(self.on_progress)
        self.worker.done.connect(self.on_done)
        self.worker.start()

    def stop_run(self):
        if self.worker:
            self.worker.stop()

    def on_progress(self, row, tag, detail, cid):
        self.table.item(row, 1).setText(tag)
        self.table.item(row, 2).setText(detail)
        self.table.item(row, 3).setText(cid)
        color = TAG_COLORS.get(tag)
        if not color and tag and tag != "OK":
            color = QColor("#ffc9c9")
        if color:
            for c in range(self.table.columnCount()):
                self.table.item(row, c).setBackground(color)

        # Convenience: when 2FA is needed, offer to open the MCC URL in browser
        # so the user can pass the challenge with one click.
        if tag == "REAUTH_NEEDED" and self.cfg:
            mcc_url = (
                "https://ads.google.com/aw/accounts"
                f"?ocid={self.cfg['manager_customer_id']}"
                f"&euid={self.cfg['login_user_id']}"
                f"&__u={self.cfg['user_id']}"
                f"&__c={self.cfg['customer_id']}"
                "&authuser=0"
            )
            ans = QMessageBox.question(
                self, "2FA needed",
                "Google asked for 2-step verification.\n\n"
                "Open the MCC in your default browser now, click '+ Create account',\n"
                "pass the 2FA challenge, cancel the dialog, then recopy + Save your\n"
                "cookie here. Re-run Create afterwards.\n\n"
                "Open MCC URL now?",
            )
            if ans == QMessageBox.Yes:
                import webbrowser
                webbrowser.open(mcc_url)

    def on_done(self):
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.append_log("=== Done ===")


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
