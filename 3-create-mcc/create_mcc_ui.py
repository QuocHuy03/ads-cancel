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
import random
import re
import sys
import threading
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
import ads_locale

HERE = auto.data_dir()
COOKIE_FILE = HERE / "cookie.txt"
SESSION_EXTRAS = HERE / "session_extras.json"   # DRAPT + user-context sidecar

# Google Ads MCC create-account reCAPTCHA Enterprise sitekey (the same one
# the browser loads on /aw/accounts → "Create account" dialog). If Google
# changes it, the user can override via the UI field.
DEFAULT_SITEKEY = "6LfC8vEqAAAAALvQX15hR9GJ3V6jNKeMx6rBTcIr"  # reCAPTCHA v2 checkbox
# Match the exact page where Google renders the recaptcha widget — solvers
# bind the token to this URL when verifying.
DEFAULT_SITE_URL = "https://ads.google.com/aw/account/new"
CAPTCHA_RETRIES = 3

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")


# ---------- network helpers ----------

def now_ms_id() -> str:
    return str(int(time.time() * 1000))


def shared_headers(cfg: dict, mcc_ocid: str, extras: dict = None,
                   referer_path: str = "/aw/account/new") -> dict:
    """Headers that mirror what Chrome sends to the MCC Mutate endpoint.
    `extras` is a dict of any per-session header captured from the user's
    cURL (user-context, request-context, x-client-data, etc.) — they are
    merged in last so they win over our defaults.
    `referer_path` defaults to /aw/account/new because that's the page the
    Save button is on — Google rejects creates if the referer is wrong."""
    h = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://ads.google.com",
        "referer": (
            f"https://ads.google.com{referer_path}?ocid={mcc_ocid}"
            f"&ascid={mcc_ocid}&euid={cfg['login_user_id']}"
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
                        country, recaptcha_token, descriptive_name: str = "",
                        drapt: str = "", extras: dict = None) -> dict:
    """Mutate via the /aw/account/new full-page flow — the variant Google
    accepts in production. Differs from the legacy dropdown dialog: no
    useUfoFlow flags, descriptive_name goes in __ar.2.4, activity* fields
    use the AccountCreationStepComponent.Save context."""
    url = (
        "https://ads.google.com/aw_mcc/_/rpc/ClientCustomerSignupService/Mutate"
        f"?authuser={cfg['authuser']}&xt=awn"
        "&rpcTrackingId=ClientCustomerSignupService.Mutate%3A4"
        f"&f.sid={cfg['f_sid']}"
    )
    ar = {
        "1": {"3": {"1": mcc_ocid}},
        "2": {
            "3": currency,
            "4": descriptive_name,        # the new account's display name
            "5": tz,
            "7": 30,
            "8": False,
            "9": country,
            "10": 1,
            "11": {"1": ""},
        },
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
        "activityContext": ".AccountCreationStepComponent.Save",
        "requestPriority": "HIGH_LATENCY_SENSITIVE",
        "activityType": "SAVE",
        "activityId": now_ms_id(),
        "uniqueFingerprint": f"{cfg['f_sid']}_{now_ms_id()}_1",
        "previousPlace": "/aw/account/new",
        "activityName": ".AccountCreationStepComponent.Save",
        "destinationPlace": "/aw/account/new",
    }
    # DRAPT is the proof-of-2FA token — sent as a body field, not a cookie.
    if drapt:
        body["drapt"] = drapt
    r = session.post(url,
                     headers=shared_headers(cfg, mcc_ocid, extras,
                                            referer_path="/aw/account/new"),
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

    # Success: the real shape is {"1":{"1":"<new_customer_id>"}}.
    new_cid = ""
    one = data.get("1")
    if isinstance(one, dict):
        inner = one.get("1")
        if isinstance(inner, str) and inner.isdigit():
            new_cid = inner
        else:
            # fall back to any digit string nested anywhere under "1"
            flat = json.dumps(one)
            m = re.search(r'"(\d{9,11})"', flat)
            if m:
                new_cid = m.group(1)
    return "OK", "", new_cid


# ---------- workers ----------

class SitekeyDetectWorker(QThread):
    """Downloads the awn_mcc dart bundle and greps the reCAPTCHA sitekey."""
    done = pyqtSignal(str)       # sitekey
    failed = pyqtSignal(str)

    def __init__(self, cookies, cfg):
        super().__init__()
        self.cookies = cookies
        self.cfg = cfg

    def run(self):
        try:
            session = requests.Session()
            sitekey = ads_locale.detect_recaptcha_sitekey(
                session, self.cookies, self.cfg)
            self.done.emit(sitekey)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class TimezoneFetchWorker(QThread):
    """Fetches the list of timezones Google Ads allows for a country,
    via /aw_mcc/_/rpc/TimeZoneConstantService/List."""
    done = pyqtSignal(str, list)   # (country_code, [(posix, display), ...])
    failed = pyqtSignal(str)

    def __init__(self, cookies, cfg, mcc_ocid, country_code, extras,
                 drapt: str = ""):
        super().__init__()
        self.cookies = cookies
        self.cfg = cfg
        self.mcc_ocid = mcc_ocid
        self.country_code = country_code
        self.extras = extras or {}
        self.drapt = drapt

    def run(self):
        try:
            session = requests.Session()
            tzs = ads_locale.list_timezones(
                session, self.cookies, self.cfg, self.mcc_ocid,
                self.country_code, self.extras, self.drapt,
            )
            self.done.emit(self.country_code, tzs)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class CreateWorker(QThread):
    log = pyqtSignal(str)
    progress = pyqtSignal(int, str, str, str)   # row, tag, detail, customer_id
    done = pyqtSignal()

    def __init__(self, cookies, cfg, mcc_ocid, params, count, api_key,
                 sitekey, delay_min: float = 2.0, delay_max: float = 3.0,
                 threads: int = 1, name_template: str = "Sub-{i}",
                 drapt: str = "", extras: dict = None):
        super().__init__()
        self.cookies = cookies
        self.cfg = cfg
        self.mcc_ocid = mcc_ocid
        self.params = params   # (currency, timezone, country)
        self.count = count
        self.api_key = api_key
        self.sitekey = sitekey
        self.delay_min = float(delay_min)
        self.delay_max = float(delay_max)
        self.threads = max(1, int(threads))
        self.name_template = name_template
        self.drapt = drapt
        self.extras = extras or {}
        self._stop = False

    def stop(self):
        self._stop = True

    def _format_name(self, i: int) -> str:
        """Render the descriptive_name for the i-th account.
        Placeholders: {i} (1-based), {n} (zero-padded 3-digit), {count}."""
        tpl = self.name_template or ""
        try:
            return tpl.format(i=i, n=f"{i:03d}", count=self.count)
        except (KeyError, IndexError):
            return tpl   # template had unknown placeholders — send as-is

    def _solve(self) -> str:
        last_err = None
        for attempt in range(1, CAPTCHA_RETRIES + 1):
            try:
                return nextcaptcha.solve_recaptcha_v2(
                    self.api_key,
                    website_url=DEFAULT_SITE_URL,
                    website_key=self.sitekey,
                    is_invisible=False,
                    on_progress=lambda s, a=attempt: self.log.emit(f"    captcha [{a}/{CAPTCHA_RETRIES}]: {s}"),
                )
            except Exception as e:
                last_err = e
                self.log.emit(f"    captcha attempt {attempt}/{CAPTCHA_RETRIES} failed: {e}")
                if attempt < CAPTCHA_RETRIES:
                    time.sleep(2.0)
        raise last_err or RuntimeError("captcha solve failed after retries")

    def run(self):
        currency, tz, country = self.params
        total = self.count

        queue = list(range(total))   # row indices
        queue_lock = threading.Lock()
        counter = [0]
        auth_expired = threading.Event()

        def worker_loop(tid: int):
            # Each worker thread gets its own requests.Session so connection
            # pools don't fight each other.
            session = requests.Session()
            while True:
                if self._stop or auth_expired.is_set():
                    return
                with queue_lock:
                    if not queue:
                        return
                    row = queue.pop(0)
                    counter[0] += 1
                    seq = counter[0]

                # 1) Solve reCAPTCHA
                self.log.emit(f"\n[t{tid}|{seq}/{total}] solving reCAPTCHA...")
                try:
                    token = self._solve()
                except Exception as e:
                    self.log.emit(f"    captcha FAILED: {e}")
                    self.progress.emit(row, "CAPTCHA_FAIL", str(e), "")
                    continue

                # 2) Mutate
                name = self._format_name(row + 1)
                self.log.emit(
                    f"[t{tid}|{seq}/{total}] POST Mutate name={name!r}  "
                    f"currency={currency}  tz={tz}  country={country}..."
                )
                res = mutate_create_child(session, self.cookies, self.cfg,
                                           self.mcc_ocid, currency, tz, country, token,
                                           descriptive_name=name,
                                           drapt=self.drapt, extras=self.extras)
                tag, detail, new_cid = classify_mutate(res["data"]) if res["data"] else (
                    f"HTTP{res['http']}", res["raw"][:200], "")

                # 3) Optional one-shot reauth ack + retry
                if tag == "REAUTH_REQUIRED":
                    drapt = self.drapt
                    if not drapt:
                        rapt = self.cookies.get("RAPT", "")
                        m = re.search(r"DRAPT:([^;+\s]+)", rapt)
                        drapt = m.group(1) if m else ""
                    if not drapt:
                        self.log.emit(
                            f"    [t{tid}] REAUTH_REQUIRED but no DRAPT — pass 2FA in Chrome and resave cookie."
                        )
                        self.progress.emit(row, "REAUTH_NEEDED", detail, "")
                        # Don't burn the whole batch on a missing DRAPT — let the
                        # other threads continue (maybe they have one) but stop
                        # taking new work on this thread if it's a session-wide
                        # auth fail.
                        auth_expired.set()
                        return
                    self.log.emit(f"    [t{tid}] REAUTH_REQUIRED — publishing reauth ack...")
                    pub = publish_reauth(session, self.cookies, self.cfg,
                                         self.mcc_ocid, drapt, detail, extras=self.extras)
                    if pub["data"] and pub["data"].get("1") == 2:
                        self.cookies["RAPT"] = f"AUTH:0+TYPE:6+DRAPT:{drapt}"
                        res2 = mutate_create_child(session, self.cookies, self.cfg,
                                                    self.mcc_ocid, currency, tz, country, token,
                                                    descriptive_name=name,
                                                    drapt=drapt, extras=self.extras)
                        tag, detail, new_cid = classify_mutate(res2["data"]) if res2["data"] else (
                            f"HTTP{res2['http']}", res2["raw"][:200], "")
                    else:
                        self.log.emit(f"    reauth ack FAILED: {pub['raw'][:200]}")
                        tag = "REAUTH_ACK_FAIL"

                label = f"{tag}" + (f" -> {new_cid}" if new_cid else "")
                self.log.emit(f"[t{tid}|{seq}/{total}] {label}  {detail[:120]}")
                self.progress.emit(row, tag, detail, new_cid)

                # Per-thread randomized cool-down so the N workers don't all
                # fire at the same instant.
                lo = max(0.0, self.delay_min)
                hi = max(lo, self.delay_max)
                wait = lo if hi <= lo else random.uniform(lo, hi)
                if wait > 0:
                    self.log.emit(f"    [t{tid}] sleep {wait:.2f}s before next pick...")
                    # Sleep in small chunks so Stop responds fast.
                    end = time.time() + wait
                    while time.time() < end:
                        if self._stop or auth_expired.is_set():
                            return
                        time.sleep(min(0.25, end - time.time()))

        self.log.emit(
            f"Spawning {self.threads} worker thread(s) — "
            f"per-thread delay {self.delay_min:.1f}–{self.delay_max:.1f}s"
        )
        pool = [threading.Thread(target=worker_loop, args=(t + 1,), daemon=True)
                for t in range(self.threads)]
        for t in pool:
            t.start()
        for t in pool:
            t.join()
        if self._stop:
            self.log.emit("Stopped by user.")
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
        rowB.addWidget(QLabel("Country:"))
        self.country_edit = QComboBox()
        self.country_edit.setEditable(True)
        for code, name in ads_locale.COUNTRIES:
            self.country_edit.addItem(f"{name} ({code})", userData=code)
        # default to Vietnam
        idx = next((i for i, (c, _) in enumerate(ads_locale.COUNTRIES) if c == "VN"), 0)
        self.country_edit.setCurrentIndex(idx)
        self.country_edit.currentIndexChanged.connect(self._on_country_changed)
        self.country_edit.setMinimumWidth(220)
        rowB.addWidget(self.country_edit)

        rowB.addWidget(QLabel("Timezone:"))
        self.tz_edit = QComboBox()
        self.tz_edit.setEditable(True)
        self.tz_edit.setMinimumWidth(240)
        self.tz_edit.setToolTip(
            "Populated automatically via TimeZoneConstantService.List when\n"
            "Country changes (after Discover). You can also type a custom\n"
            "posix name (e.g. Asia/Ho_Chi_Minh)."
        )
        rowB.addWidget(self.tz_edit)

        rowB.addWidget(QLabel("Currency:"))
        self.currency_edit = QComboBox()
        self.currency_edit.setEditable(True)
        for code, label in ads_locale.CURRENCIES:
            self.currency_edit.addItem(label, userData=code)
        idx = next((i for i, (c, _) in enumerate(ads_locale.CURRENCIES) if c == "USD"), 0)
        self.currency_edit.setCurrentIndex(idx)
        self.currency_edit.setMinimumWidth(220)
        rowB.addWidget(self.currency_edit)

        rowB.addWidget(QLabel("Account name:"))
        self.name_edit = QLineEdit("Sub-{i}")
        self.name_edit.setFixedWidth(180)
        self.name_edit.setToolTip(
            "Display name for new sub-accounts (__ar.2.4).\n"
            "Placeholders: {i} = 1,2,3...   {n} = 001,002,003   {count} = total\n"
            "Examples:  'Sub-{i}'   'Account {n}'   'hello'"
        )
        rowB.addWidget(self.name_edit)

        rowB.addWidget(QLabel("Count:"))
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 500)
        self.count_spin.setValue(1)
        self.count_spin.setFixedWidth(70)
        rowB.addWidget(self.count_spin)

        rowB.addWidget(QLabel("Delay (s):"))
        self.delay_min_spin = QDoubleSpinBox()
        self.delay_min_spin.setRange(0.0, 60.0)
        self.delay_min_spin.setValue(2.0)
        self.delay_min_spin.setSingleStep(0.5)
        self.delay_min_spin.setFixedWidth(60)
        self.delay_min_spin.setToolTip(
            "Lower bound of the per-thread randomized cooldown after each\n"
            "successful Mutate. Actual wait = random(min, max) seconds."
        )
        rowB.addWidget(self.delay_min_spin)
        rowB.addWidget(QLabel("–"))
        self.delay_max_spin = QDoubleSpinBox()
        self.delay_max_spin.setRange(0.0, 60.0)
        self.delay_max_spin.setValue(3.0)
        self.delay_max_spin.setSingleStep(0.5)
        self.delay_max_spin.setFixedWidth(60)
        self.delay_max_spin.setToolTip("Upper bound of the randomized cooldown.")
        rowB.addWidget(self.delay_max_spin)

        rowB.addWidget(QLabel("Threads:"))
        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, 10)
        self.threads_spin.setValue(1)
        self.threads_spin.setFixedWidth(60)
        self.threads_spin.setToolTip(
            "Parallel worker threads. Each thread picks the next pending\n"
            "account from the shared queue, solves its own captcha, posts\n"
            "Mutate, then sleeps random(min,max) seconds before the next pick.\n"
            "Higher = faster but more captcha-solve cost and higher chance of\n"
            "tripping Google's anti-bot heuristics. 1–3 is the sane range."
        )
        rowB.addWidget(self.threads_spin)
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
            "Click Detect to refresh from the live bundle, or paste a value manually."
        )
        rowC.addWidget(self.sitekey_edit, stretch=1)

        self.btn_detect_sitekey = QPushButton("Detect")
        self.btn_detect_sitekey.setToolTip(
            "Download the latest awn_mcc dart bundle from gstatic and grep for\n"
            "the current reCAPTCHA sitekey. Requires a discovered session."
        )
        self.btn_detect_sitekey.clicked.connect(self.detect_sitekey)
        rowC.addWidget(self.btn_detect_sitekey)
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

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels([
            "#", "name", "currency", "tz", "country",
            "result", "detail", "new customer_id",
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)   # #
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)   # name
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)   # currency
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)   # tz
        h.setSectionResizeMode(4, QHeaderView.ResizeToContents)   # country
        h.setSectionResizeMode(5, QHeaderView.ResizeToContents)   # result
        h.setSectionResizeMode(6, QHeaderView.Stretch)            # detail
        h.setSectionResizeMode(7, QHeaderView.ResizeToContents)   # new customer_id
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

    @staticmethod
    def _current_code(combo: QComboBox) -> str:
        """Pull the ISO code that was attached as userData on the dropdown
        item. Falls back to the visible text if the user typed something
        custom (editable combobox)."""
        idx = combo.currentIndex()
        if idx >= 0:
            code = combo.itemData(idx)
            if code:
                return code
        return combo.currentText().strip()

    def _on_country_changed(self):
        """When the country dropdown changes, fetch the timezones Google Ads
        allows for that country and repopulate the timezone dropdown.
        If we don't have a discovered session yet, kick off Discover first
        when cookies + MCC are already set — discover()'s success path will
        call this method again."""
        country = self._current_code(self.country_edit)
        if not country or len(country) != 2:
            return
        if not self.cfg:
            if self.cookies and self.mcc_edit.text().strip():
                self.append_log(
                    f"Country changed to {country} — auto-discovering session first..."
                )
                self.discover()
            else:
                self.append_log(
                    f"Country changed to {country} — paste cookie + MCC ID, then "
                    "Save cookie. Discovery will then run automatically."
                )
            return
        self.append_log(f"Loading timezones for country={country}...")
        worker = TimezoneFetchWorker(
            self.cookies, self.cfg, self.cfg["manager_customer_id"],
            country, dict(self.extra_headers),
            drapt=self.drapt_edit.text().strip(),
        )
        worker.done.connect(self._on_timezones_loaded)
        worker.failed.connect(self._on_timezone_fetch_failed)
        worker.start()
        self._tz_worker = worker   # keep ref so it isn't GC'd

    def _on_timezones_loaded(self, country: str, tzs: list):
        if not tzs:
            self.append_log(f"  no timezones returned for {country}")
            return
        # Remember whatever the user had typed so we can keep it if it's
        # still relevant after we swap the items.
        prior = self.tz_edit.currentText().strip()
        self.tz_edit.clear()
        for posix, display in tzs:
            self.tz_edit.addItem(f"{display}  [{posix}]", userData=posix)
        # Restore prior selection if it exists in the new list, else pick #0.
        idx = next((i for i, (p, _) in enumerate(tzs) if p == prior), 0)
        self.tz_edit.setCurrentIndex(idx)
        self.append_log(f"  loaded {len(tzs)} timezone(s) for {country}")

    def _on_timezone_fetch_failed(self, err: str):
        self.append_log(f"  timezone fetch failed: {err}")

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
        # If the MCC field is already filled, jump straight to Discover so the
        # user gets timezone auto-load without an extra button click.
        if self.mcc_edit.text().strip() and not self.cfg:
            self.discover()

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
        # Pre-populate timezone list for the currently selected country now
        # that we have a valid session.
        self._on_country_changed()

    def detect_sitekey(self):
        if not self.cfg:
            if not self.cookies or not self.mcc_edit.text().strip():
                QMessageBox.warning(
                    self, "Not ready",
                    "Save cookie + MCC ID first so we can fetch the dart bundle."
                ); return
            self.append_log("Auto-discovering session before detecting sitekey...")
            self.discover()
            # discover() is synchronous in our codepath, but bail if it didn't set cfg
            if not self.cfg:
                return

        self.append_log("Detecting reCAPTCHA sitekey from awn_mcc dart bundle...")
        self.btn_detect_sitekey.setEnabled(False)
        worker = SitekeyDetectWorker(self.cookies, self.cfg)
        worker.done.connect(self._on_sitekey_detected)
        worker.failed.connect(self._on_sitekey_detect_failed)
        worker.start()
        self._sitekey_worker = worker

    def _on_sitekey_detected(self, sitekey: str):
        self.btn_detect_sitekey.setEnabled(True)
        prev = self.sitekey_edit.text().strip()
        self.sitekey_edit.setText(sitekey)
        if sitekey == prev:
            self.append_log(f"  sitekey unchanged: {sitekey}")
        else:
            self.append_log(f"  sitekey updated: {prev} → {sitekey}")
        self.statusBar().showMessage(f"sitekey: {sitekey}")

    def _on_sitekey_detect_failed(self, err: str):
        self.btn_detect_sitekey.setEnabled(True)
        self.append_log(f"  sitekey detect failed: {err}")

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
        sitekey = self.sitekey_edit.text().strip()
        if not api_key:
            QMessageBox.warning(self, "No API key", "Paste NextCaptcha API key."); return
        if not sitekey:
            QMessageBox.warning(self, "No sitekey", "Sitekey is empty."); return

        n = self.count_spin.value()
        params = (self._current_code(self.currency_edit),
                  self._current_code(self.tz_edit),
                  self._current_code(self.country_edit))

        ans = QMessageBox.question(
            self, "Confirm",
            f"Create {n} sub-account(s) under MCC {self.cfg['manager_customer_id']}?\n"
            f"Currency={params[0]}  TZ={params[1]}  Country={params[2]}\n"
            f"NextCaptcha (V2 checkbox) cost ≈ ${n * 0.002:.4f}",
        )
        if ans != QMessageBox.Yes:
            return

        # Pre-fill the table with N rows so the user sees what's being created
        # before each Mutate even fires. name/currency/tz/country come from
        # the form values; result/detail/customer_id fill in as the worker runs.
        currency, tz, country = params
        name_tpl = self.name_edit.text().strip() or "Sub-{i}"
        def render(i: int) -> str:
            try:
                return name_tpl.format(i=i, n=f"{i:03d}", count=n)
            except (KeyError, IndexError):
                return name_tpl
        self.table.setRowCount(0)
        for i in range(n):
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(str(i + 1)))
            self.table.setItem(r, 1, QTableWidgetItem(render(i + 1)))
            self.table.setItem(r, 2, QTableWidgetItem(currency))
            self.table.setItem(r, 3, QTableWidgetItem(tz))
            self.table.setItem(r, 4, QTableWidgetItem(country))
            self.table.setItem(r, 5, QTableWidgetItem(""))
            self.table.setItem(r, 6, QTableWidgetItem(""))
            self.table.setItem(r, 7, QTableWidgetItem(""))

        self.append_log(f"\n=== Creating {n} sub-account(s) ===")
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._save_session_extras()
        self.worker = CreateWorker(
            self.cookies, self.cfg, self.cfg["manager_customer_id"],
            params, n, api_key, sitekey,
            delay_min=self.delay_min_spin.value(),
            delay_max=self.delay_max_spin.value(),
            threads=self.threads_spin.value(),
            name_template=self.name_edit.text().strip() or "Sub-{i}",
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
        self.table.item(row, 5).setText(tag)
        self.table.item(row, 6).setText(detail)
        self.table.item(row, 7).setText(cid)
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
