"""
AdsCancel — one-shot bulk re-appeal for Google Ads MCC sub-accounts
that were suspended for "Circumventing systems: Multiple account abuse".

Workflow (single command):
    1. Read cookie.txt (paste from a logged-in browser).
    2. Auto-discover __u / __c / __lu / manager_customer_id / f.sid / xsrf
       by fetching https://ads.google.com/aw/accounts.
    3. List every sub-account under the user's default MCC.
    4. Filter to leaves with ui_account_status == 2 and a descriptive_name
       starting with "MCC_Child_" (the Multi-account-abuse cohort).
    5. POST AccountSuspensionAppealService.Submit for each one.
    6. Log results to appeal_results.json.

Usage:
    python auto.py                 # do everything, with prompt
    python auto.py --yes           # skip prompt
    python auto.py --dry-run       # show what would be sent, no POST
    python auto.py --limit 5       # process only first 5 (smoke test)
    python auto.py --ids ID1,ID2   # target specific customer IDs
"""

import argparse
import collections
import http.cookiejar
import json
import re
import sys
import time
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

# --- knobs ----------------------------------------------------------------

NAME_PREFIX = ""             # empty = no name filter (default tick everything)
STATUS_TARGET = 2            # ui_account_status: 2 = policy/abuse-suspended

APPEAL_ABUSE_TAG_IDS_PRIMARY   = [288]   # __ar.1.9
APPEAL_ABUSE_TAG_IDS_SECONDARY = [59]    # __ar.1.13

# activityId for the short 2-question re-appeal (older captured value —
# still accepted). The 13-question first-appeal form uses a different id
# captured from a fresh cURL: see APPEAL_ABUSE_FULL_ACTIVITY_ID below.
APPEAL_ABUSE_SIMPLE_ACTIVITY_ID = "475126922029082"
APPEAL_ABUSE_FULL_ACTIVITY_ID   = "3107931383697595"

# (field_id, question_text, default_answer) captured from a real browser
# submit of the "Multiple account abuse" *first* appeal (13 questions,
# from /aw/overview). question_text must match exactly what the Ads UI
# sends in __ar.2[*].2.
ABUSE_QUESTIONS = [
    ("inputCountries",                     "Which country will the business run ads in?",                              "united states"),
    ("inputBusinessModel",                 "What does your organization do?",                                          ""),
    ("isAdvertisingOwnBusiness",           "Are you the owner or a direct employee of your organization?",             "true"),
    ("inputDomain",                        "What's your organization's website?",                                      ""),
    ("isBusinessModelChanged",             "Has your organization changed in the last 3 days?",                        "false"),
    ("isUsingAffiliatedMarketing",         "Is your organization part of an affiliate program?",                       "false"),
    ("isHavingMultipleGoogleAccounts",     "Do you have multiple google accounts?",                                    "false"),
    ("isOrganizationOwningWebsite",        "Does your organization own the website?",                                  "true"),
    ("isDirectRelationshipWithOtherBrands","Does your business have a direct relationship with the other brands shown on its websites?", "false"),
    ("isManagedByDifferentOrganization",   "Is the business managed by a different organization?",                     "false"),
    ("inputAnyOtherInfoNeeded",            "Is there any other information we need to know about you or your organization?", "no"),
    ("inputSampleKeywords",                "What are some sample keywords from your campaigns?",                       ""),
    ("inputActiveWebsiteDuration",         "How long has your website been active?",                                   "now"),
]

DEFAULT_DELAY = 1.5
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
ACCOUNTS_URL_TMPL = "https://ads.google.com/aw/accounts?authuser={authuser}"


class MultipleMCCsError(Exception):
    """Raised by discover_session when the cookie owner has access to more
    than one MCC and the caller didn't force one. The UI can catch this and
    pop up a picker."""
    def __init__(self, mccs: list):
        super().__init__(f"Multiple MCCs available: {mccs}")
        self.mccs = mccs   # list[(ocid, optional_name)]


def load_session_extras() -> tuple[str, dict]:
    """Read the DRAPT token + extra Chrome headers from session_extras.json
    next to cookie.txt. Returns (drapt, headers_dict). Empty defaults if the
    file doesn't exist yet — the appeal/create code degrades gracefully."""
    path = data_dir() / "session_extras.json"
    if not path.exists():
        return "", {}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return "", {}
    return d.get("drapt", "") or "", d.get("headers", {}) or {}


def list_available_mccs(session: requests.Session, cookies: dict,
                        authuser: str) -> list[tuple[str, str]]:
    """Hit /aw/accounts with a minimal UA and parse the resulting HTML for
    every ocid the user can switch to. Returns [(ocid, label), ...]. The
    label is best-effort — empty string when we couldn't find a name nearby."""
    r = session.get(ACCOUNTS_URL_TMPL.format(authuser=authuser),
                    headers={"user-agent": "Mozilla/5.0", "accept": "text/html"},
                    cookies=cookies, allow_redirects=True, timeout=30)
    if "accounts.google.com/" in r.url and "/signin/" in r.url:
        raise RuntimeError("Cookie expired — refresh cookie.txt.")
    text = r.text

    ocids = sorted(set(re.findall(r"ocid[=:%][^0-9]?(\d{6,12})", text)))
    pairs: list[tuple[str, str]] = []
    for ocid in ocids:
        # Best-effort name lookup — JSON blobs in the select-account page
        # often embed account names alongside the ocid value.
        name = ""
        for pat in (
            rf'"(?:name|descriptiveName|customerName)"\s*:\s*"([^"]{{1,80}})"[^{{}}]*"?ocid"?\s*:\s*"?{ocid}',
            rf'"?ocid"?\s*:\s*"?{ocid}"?[^{{}}]*"(?:name|descriptiveName|customerName)"\s*:\s*"([^"]{{1,80}})"',
            rf'aria-label="([^"]+?)"[^>]*ocid={ocid}',
        ):
            m = re.search(pat, text)
            if m:
                name = m.group(1)
                break
        pairs.append((ocid, name))
    return pairs


def data_dir() -> Path:
    """Where cookie.txt and appeal_results.json live.

    When running from source -> next to the script (familiar).
    When frozen by PyInstaller into a .app -> ~/.adscancel/ so the user can
    still edit cookie.txt and the app can write the results file."""
    if getattr(sys, "frozen", False):
        d = Path.home() / ".adscancel"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return Path(__file__).resolve().parent

# --- cookie / xssi helpers -----------------------------------------------

def strip_xssi(s: str) -> str:
    return s.lstrip(")]}\n' \r\t")


def load_cookies(path: Path) -> dict:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        sys.exit(f"cookie.txt is empty: {path}")
    if text.lstrip().startswith("#") or "\tTRUE\t" in text or "\tFALSE\t" in text:
        jar = http.cookiejar.MozillaCookieJar()
        jar.load(str(path), ignore_discard=True, ignore_expires=True)
        return {c.name: c.value for c in jar}
    cookies = {}
    for part in text.replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        cookies[k.strip()] = v.strip()
    return cookies


# --- session discovery ----------------------------------------------------

def discover_session(session: requests.Session, cookies: dict, authuser: str,
                     forced_ocid: str = "") -> dict:
    """Two-stage discovery:
       (a) GET /aw/accounts with a minimal UA. Google's browser-check
           redirects to /aw/browser_not_supported?ocid=...&__u=... — that
           URL exposes every per-user ID we need plus an f.sid.
       (b) GET /aw/accounts with a real Chrome UA + ocid. The HTML embeds
           the XSRF token used by all RPC calls.

    If the user has multiple MCCs Google redirects to /nav/selectaccount
    instead. In that case we parse the HTML for the available ocids and
    re-request with an explicit one (forced_ocid if given, else the first
    one found)."""

    # --- (a) IDs + f.sid from the browser-check redirect URL ---
    r1 = session.get(ACCOUNTS_URL_TMPL.format(authuser=authuser),
                     headers={"user-agent": "Mozilla/5.0", "accept": "text/html"},
                     cookies=cookies, allow_redirects=True, timeout=30)
    if "accounts.google.com/" in r1.url and "/signin/" in r1.url:
        sys.exit("Cookie expired — got redirected to sign-in. Refresh cookie.txt.")

    qs = parse_qs(urlparse(r1.url).query)
    def first(key): return qs.get(key, [""])[0]
    ocid, euid, uu, uc = first("ocid"), first("euid"), first("__u"), first("__c")
    fsid = first("f.sid")

    # Multi-account case: Google parks us at /nav/selectaccount until a pick.
    if "/nav/selectaccount" in r1.url or not ocid:
        ocids = sorted(set(re.findall(r"ocid[=:%][^0-9]?(\d{6,12})", r1.text)))
        if forced_ocid:
            pick = forced_ocid
        elif len(ocids) == 1:
            pick = ocids[0]
        elif len(ocids) > 1:
            # Hand control back to the caller so the UI can show a picker.
            raise MultipleMCCsError([(o, "") for o in ocids])
        else:
            # The account-chooser page is rendered by a Dart/JS bundle, so
            # the list of available MCCs isn't in the initial HTML. Tell the
            # user how to find their MCC id manually.
            sys.exit(
                "Your Google account has multiple MCCs and Google didn't "
                "auto-pick one. Open https://ads.google.com in your browser, "
                "click the MCC you want, then copy that page's URL (or just "
                "the ocid=... number) into the 'MCC ID or URL' field and try again."
            )
        # Retry with an explicit ocid to skip the picker.
        r1 = session.get(
            f"https://ads.google.com/aw/accounts?ocid={pick}&authuser={authuser}",
            headers={"user-agent": "Mozilla/5.0", "accept": "text/html"},
            cookies=cookies, allow_redirects=True, timeout=30,
        )
        qs = parse_qs(urlparse(r1.url).query)
        ocid = first("ocid") or pick
        euid = first("euid") or euid
        uu   = first("__u")  or uu
        uc   = first("__c")  or uc
        fsid = first("f.sid") or fsid

    if not (ocid and uu and uc):
        sys.exit(f"Could not extract IDs from redirect URL: {r1.url}")
    if not fsid:
        sys.exit(f"Could not extract f.sid from redirect URL: {r1.url}")

    # --- (b) XSRF token from the real page HTML ---
    real_url = (f"https://ads.google.com/aw/accounts?ocid={ocid}"
                f"&euid={euid}&__u={uu}&__c={uc}&authuser={authuser}")
    r2 = session.get(real_url, headers={"user-agent": UA, "accept": "text/html"},
                     cookies=cookies, allow_redirects=True, timeout=30)
    m_xsrf = re.search(r"(AA[A-Za-z0-9_\-]{24,}:\d{13})", r2.text)
    if not m_xsrf:
        sys.exit("Could not extract XSRF token from accounts page HTML.")

    return {
        "manager_customer_id": ocid,
        "login_user_id":       euid or uu,
        "user_id":              uu,
        "customer_id":          uc,
        "f_sid":                fsid,
        "xsrf_token":           m_xsrf.group(1),
        "authuser":             authuser,
    }


# --- list accounts --------------------------------------------------------

LIST_URL_TMPL = (
    "https://ads.google.com/aw_mcc/_/rpc/AccountService/List"
    "?authuser={authuser}&xt=awn"
    "&rpcTrackingId=AccountService.List%3A2"
    "&f.sid={fsid}"
)

LIST_FIELDS = [
    "customer_info.ui_account_status",
    "customer_info.is_hidden",
    "customer_info.descriptive_name",
    "customer_info.is_manager",
    "customer_id",
    "customer_info.external_customer_id",
    "customer_manager_info.manager_customer_id",
    "customer_manager_info.level",
    "customer_manager_info.in_authorized_customer_hierarchy",
    "currency_code",
]


def list_accounts(session: requests.Session, cookies: dict, cfg: dict) -> list[dict]:
    url = LIST_URL_TMPL.format(authuser=cfg["authuser"], fsid=cfg["f_sid"])
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://ads.google.com",
        "referer": f"https://ads.google.com/aw/accounts?ocid={cfg['manager_customer_id']}&authuser={cfg['authuser']}",
        "user-agent": UA,
        "x-framework-xsrf-token": cfg["xsrf_token"],
        "x-same-domain": "1",
    }
    ar = {
        "1": {
            "3": {"1": cfg["manager_customer_id"]},
            "5": "TABLE_TWO_STAGE_RPC",
            "6": "32400000",
        },
        "2": {
            "1": LIST_FIELDS,
            "2": [
                {"1": "customer_info.status", "2": 3,
                 "4": [{"3": str(s)} for s in (1, 2, 3, 4, 5)]},
                {"1": "customer_info.is_hidden", "2": 1, "4": [{"1": False}]},
                {"1": "customer_manager_info.manager_customer_id", "2": 1,
                 "4": [{"3": cfg["manager_customer_id"]}]},
                {"1": "customer_manager_info.level", "2": 1, "4": [{"3": "1"}]},
                {"1": "customer_manager_info.in_authorized_customer_hierarchy", "2": 1,
                 "4": [{"1": True}]},
            ],
            "3": [
                {"1": "customer_info.is_manager", "2": 2},
                {"1": "customer_info.descriptive_name", "2": 1},
                {"1": "customer_info.external_customer_id", "2": 1},
            ],
            "14": True,
        },
    }
    body = {
        "hl": "en_US",
        "__lu": cfg["login_user_id"],
        "__u":  cfg["user_id"],
        "__c":  cfg["customer_id"],
        "f.sid": cfg["f_sid"],
        "ps": "aw",
        "__ar": json.dumps(ar, separators=(",", ":")),
        "activityContext": "AccountSecondaryRpc",
        "requestPriority":  "HIGH_LATENCY_SENSITIVE",
        "activityType":     "USER_NON_BLOCKING",
        "activityId":       "747129126721226",
        "uniqueFingerprint": f"{cfg['f_sid']}_747129126721226_1",
        "destinationPlace": "/aw/accounts",
    }
    r = session.post(url, headers=headers, cookies=cookies,
                     data=urlencode(body), timeout=60)
    # For non-MCC (single-account) users the manager-scoped List call can
    # return a 4xx/5xx or an authorization error. In that case we fall through
    # to the empty-list path and the single-account fallback below kicks in.
    if r.status_code != 200:
        data = {}
    else:
        try:
            data = json.loads(strip_xssi(r.text))
        except json.JSONDecodeError:
            data = {}
        errs = data.get("5", {}).get("2") if isinstance(data.get("5"), dict) else None
        if errs and "1" not in data:
            data = {}   # treat as "no rows" — single-account fallback handles it
    out = []
    for raw in data.get("1", []):
        info = raw.get("3", {})
        out.append({
            "customer_id":          raw.get("1"),
            "descriptive_name":     info.get("2"),
            "external_customer_id": info.get("5"),
            "is_manager":           info.get("7", False),
            "is_hidden":            info.get("8", False),
            "ui_account_status":    info.get("32"),
        })

    # Single-account fallback: user isn't an MCC owner. Use ocid as the
    # customer_id (it's the real Google Ads customer for this session). __c
    # is a workspace/billing scope marker, not a customer the appeal endpoint
    # can target — it rejects with AUTH_ERROR_CUSTOMER_NOT_FOUND.
    if not out:
        out.append({
            "customer_id":          cfg["manager_customer_id"],
            "descriptive_name":     "(your account)",
            "external_customer_id": "",
            "is_manager":           False,
            "is_hidden":            False,
            "ui_account_status":    None,
        })
    return out


# --- submit appeal --------------------------------------------------------

APPEAL_URL_TMPL = (
    "https://ads.google.com/ga/_/AwSupportPlatform/_/rpc/"
    "AccountSuspensionAppealService/Submit"
    "?authuser={authuser}&xt=awn"
    "&rpcTrackingId=AccountSuspensionAppealService.Submit%3A1"
    "&f.sid={fsid}"
)


def appeal_headers(cfg: dict, customer_id: str, extras: dict = None) -> dict:
    """Mirror what Chrome sends to the appeal endpoint. `extras` carries the
    session-bound headers Google now checks on authenticated mutations:
    user-context, request-context, x-client-data, x-browser-validation,
    x-browser-channel/copyright/year. Capture them from a real cURL once and
    persist via the UI's session_extras.json — every appeal then mirrors a
    valid browser request."""
    h = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://ads.google.com",
        "referer": (
            f"https://ads.google.com/aw/overview?ocid={customer_id}"
            f"&euid={cfg['login_user_id']}&__u={cfg['user_id']}"
            f"&uscid={cfg['manager_customer_id']}&__c={cfg['customer_id']}"
            f"&authuser={cfg['authuser']}"
        ),
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


def _appeal_body(cfg: dict, customer_id: str, questions: list,
                 tag_primary: list, tag_secondary: list,
                 drapt: str = "",
                 activity_id: str = APPEAL_ABUSE_SIMPLE_ACTIVITY_ID) -> str:
    """Build the urlencoded Submit body for any appeal form.

    `questions` is a list of {"1": field_id, "2": question_text, "3": answer}.
    `tag_primary`/`tag_secondary` are the __ar.1.9 / __ar.1.13 tag-id lists that
    tell Google which suspension reason this appeal is for.
    `activity_id` is the client-side activity token — captured per-flow from
    a real browser cURL. Short re-appeal and long first-appeal use different ids."""
    ar = {
        "1": {
            "1": str(customer_id),
            "2": "-1",
            "4": 2,
            "9": tag_primary,
            "13": tag_secondary,
        },
        "2": questions,
    }
    body = {
        "hl": "en_US",
        "__lu": cfg["login_user_id"],
        "__u":  cfg["user_id"],
        "__c":  cfg["customer_id"],
        "f.sid": cfg["f_sid"],
        "ps": "aw",
        "__ar": json.dumps(ar, separators=(",", ":")),
        "activityContext": "AccountAppealFormSlidealog.AccountAppealFormStepper.Submit",
        "requestPriority":  "HIGH_LATENCY_SENSITIVE",
        "activityType":     "INTERACTIVE",
        "activityId":       activity_id,
        "uniqueFingerprint": f"{cfg['f_sid']}_{activity_id}_1",
        "previousPlace":    "/aw/overview",
        "activityName":     "AccountAppealFormSlidealog.AccountAppealFormStepper.Submit",
        "destinationPlace": "/aw/overview",
    }
    # DRAPT is the 2FA proof token Google now requires for authenticated
    # mutations — sent as a body field, not a cookie.
    if drapt:
        body["drapt"] = drapt
    return urlencode(body)


def appeal_body(cfg: dict, customer_id: str,
                answer_changes: str = "yes",
                answer_details: str = "yes",
                tag_primary: list = None,
                tag_secondary: list = None,
                drapt: str = "") -> str:
    """"Multiple account abuse" re-appeal form (2 yes/no questions)."""
    questions = [
        {
            "1": "inputChangesFromLastAppeal",
            "2": "What changes have you made to your account or payments since the last appeal?",
            "3": answer_changes,
        },
        {
            "1": "inputFurtherDetailsSinceLastAppeal",
            "2": "Is there any other info that wasn't included in the last appeal?",
            "3": answer_details,
        },
    ]
    return _appeal_body(
        cfg, customer_id, questions,
        tag_primary if tag_primary is not None else APPEAL_ABUSE_TAG_IDS_PRIMARY,
        tag_secondary if tag_secondary is not None else APPEAL_ABUSE_TAG_IDS_SECONDARY,
        drapt=drapt,
    )


def appeal_body_full(cfg: dict, customer_id: str, answers: dict = None,
                     tag_primary: list = None, tag_secondary: list = None,
                     drapt: str = "") -> str:
    """"Multiple account abuse" *first* appeal (13-question business form).

    Submitted from /aw/overview when the account is first suspended, before
    a re-appeal is available. `answers` maps field_id -> answer; any field
    omitted falls back to the default captured in ABUSE_QUESTIONS."""
    answers = answers or {}
    questions = [
        {"1": fid, "2": qtext, "3": str(answers.get(fid, default))}
        for fid, qtext, default in ABUSE_QUESTIONS
    ]
    return _appeal_body(
        cfg, customer_id, questions,
        tag_primary if tag_primary is not None else APPEAL_ABUSE_TAG_IDS_PRIMARY,
        tag_secondary if tag_secondary is not None else APPEAL_ABUSE_TAG_IDS_SECONDARY,
        drapt=drapt, activity_id=APPEAL_ABUSE_FULL_ACTIVITY_ID,
    )


_ERROR_CODE_RX = re.compile(
    r'"3":"([A-Z][A-Z0-9_]{4,}(?:_ERROR_[A-Z0-9_]+|ERROR[A-Z0-9_]*))"'
)


def classify(http_status: int, body: str) -> tuple[str, str]:
    """Return (primary_tag, full_details).

    primary_tag picks the most informative single label for coloring.
    full_details lists every error code found in the response so the UI can
    show all of them when one submit triggers multiple errors at once."""
    if http_status != 200:
        return f"HTTP{http_status}", ""
    cleaned = strip_xssi(body)

    codes = sorted(set(_ERROR_CODE_RX.findall(cleaned)))
    if not codes:
        if "An error occurred" in cleaned:
            return "ERROR", ""
        return "OK", ""

    # Short-name each code so the tag cell stays scannable. Example:
    #   FIELD_ERROR_VALUE_BLACKLISTED -> BLACKLISTED
    #   ACCOUNT_APPEAL_ERROR_PENDING  -> PENDING
    #   AUTH_ERROR_AUTHENTICATION_FAILED -> AUTH_ERROR
    def short(code: str) -> str:
        if "BLACKLISTED" in code: return "BLACKLISTED"
        if "PENDING"     in code: return "PENDING"
        if "AUTH"        in code: return "AUTH_ERROR"
        return code

    short_tags = []
    for c in codes:
        s = short(c)
        if s not in short_tags:
            short_tags.append(s)

    # Pick a single primary tag for the row's color (priority order).
    priority = ["AUTH_ERROR", "BLACKLISTED", "PENDING", "ERROR"]
    primary = next((p for p in priority if p in short_tags), short_tags[0])
    details = " + ".join(short_tags) if len(short_tags) > 1 else ""
    return primary, details


def submit_one(session: requests.Session, cookies: dict, cfg: dict, customer_id: str,
               answer_changes: str = "yes", answer_details: str = "yes",
               tag_primary: list = None, tag_secondary: list = None,
               drapt: str = "", extras: dict = None,
               full_answers: dict = None):
    """Submit the 'Multiple account abuse' appeal.

    Pass `full_answers` to send the 13-question first-appeal form; otherwise
    the short 2-question re-appeal (yes/yes by default) is submitted."""
    url = APPEAL_URL_TMPL.format(authuser=cfg["authuser"], fsid=cfg["f_sid"])
    if full_answers is not None:
        data = appeal_body_full(cfg, customer_id, full_answers,
                                tag_primary, tag_secondary, drapt=drapt)
    else:
        data = appeal_body(cfg, customer_id, answer_changes, answer_details,
                           tag_primary, tag_secondary, drapt=drapt)
    r = session.post(url, headers=appeal_headers(cfg, customer_id, extras),
                     cookies=cookies, data=data, timeout=30)
    tag, details = classify(r.status_code, r.text)
    return r.status_code, tag, details, r.text[:400].replace("\n", " ")


# --- main -----------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    p.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    p.add_argument("--name-prefix", default=NAME_PREFIX)
    p.add_argument("--status", type=int, default=STATUS_TARGET)
    p.add_argument("--ids", default="", help="Comma-separated customer IDs (skip listing)")
    p.add_argument("--authuser", default="0")
    p.add_argument("--mcc", default="",
                   help="Force a specific MCC customer_id (when the account has many).")
    p.add_argument("--answer-changes", default="yes",
                   help="Reply to 'What changes have you made...?'")
    p.add_argument("--answer-details", default="yes",
                   help="Reply to 'Is there any other info...?'")
    args = p.parse_args()

    here = data_dir()
    cookies = load_cookies(here / "cookie.txt")
    session = requests.Session()

    # Pick up DRAPT + extra Chrome headers that the UI captured from a real
    # cURL. Empty when the user hasn't passed 2FA in this session yet.
    _session_drapt, _session_extras_headers = load_session_extras()
    if _session_drapt or _session_extras_headers:
        print(f"  session extras: DRAPT={'yes' if _session_drapt else 'no'}, "
              f"headers={len(_session_extras_headers)}")

    print("Step 1/3  Discovering session from cookies...")
    cfg = discover_session(session, cookies, args.authuser, args.mcc)
    print(f"  MCC={cfg['manager_customer_id']}  __u={cfg['user_id']}  "
          f"__c={cfg['customer_id']}  f.sid={cfg['f_sid']}")

    if args.ids:
        targets = [{"customer_id": x.strip(), "descriptive_name": "(from --ids)"}
                   for x in args.ids.split(",") if x.strip()]
        print(f"\nStep 2/3  Using {len(targets)} explicit IDs (skipping list)")
    else:
        print("\nStep 2/3  Listing accounts + filtering...")
        accounts = list_accounts(session, cookies, cfg)
        by_status = collections.Counter(a["ui_account_status"] for a in accounts)
        print(f"  Got {len(accounts)} accounts. status distribution: {dict(by_status)}")

        # Single-account fallback: list_accounts added synthetic "(your account)"
        # rows when the user isn't an MCC owner. Skip the MCC_Child cohort
        # filter and target the first one (__c). The second row (ocid) is a
        # backup for when __c returns ENTITY_DOES_NOT_EXIST.
        single = [a for a in accounts
                  if (a.get("descriptive_name") or "").startswith("(your account)")]
        if single and len(single) == len(accounts):
            targets = single[:1]
            print(f"  Single-account mode: targeting {targets[0]['customer_id']}")
            if len(single) > 1:
                print(f"  (alt ID {single[1]['customer_id']} also available — "
                      "use --ids to try if the first fails)")
        else:
            targets = [a for a in accounts
                       if not a["is_manager"]
                       and a["ui_account_status"] == args.status
                       and (a["descriptive_name"] or "").startswith(args.name_prefix)]
            print(f"  {len(targets)} target accounts "
                  f"(status={args.status}, name prefix {args.name_prefix!r})")

    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        sys.exit("Nothing to do.")

    if not args.dry_run and not args.yes:
        eta = len(targets) * args.delay / 60
        ans = input(f"\nSubmit appeals for {len(targets)} accounts? "
                    f"(~{eta:.1f} min) [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            sys.exit("Aborted.")

    print(f"\nStep 3/3  Submitting (delay={args.delay}s)...")
    results = []
    for i, acc in enumerate(targets, 1):
        cid = str(acc["customer_id"])
        name = acc.get("descriptive_name", "")
        if args.dry_run:
            print(f"[{i}/{len(targets)}] DRY {cid}  ({name})")
            results.append({"customer_id": cid, "name": name, "tag": "dry-run"})
            continue
        try:
            code, tag, details, snippet = submit_one(
                session, cookies, cfg, cid,
                args.answer_changes, args.answer_details,
                drapt=_session_drapt, extras=_session_extras_headers)
            label = f"{tag} ({details})" if details else tag
            print(f"[{i}/{len(targets)}] {label:<32} {cid}  ({name})  http={code}")
            results.append({"customer_id": cid, "name": name,
                            "http": code, "tag": tag, "details": details,
                            "body": snippet})
            if tag == "AUTH_ERROR":
                print("\nAUTH expired mid-run — refresh cookie.txt and rerun.")
                break
        except Exception as e:
            print(f"[{i}/{len(targets)}] EXC         {cid}  ({name})  {e}")
            results.append({"customer_id": cid, "name": name, "error": str(e)})
        if i < len(targets):
            time.sleep(args.delay)

    out = data_dir() / "appeal_results.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    counts = collections.Counter(r.get("tag", "?") for r in results)
    print(f"\nDone. Log -> {out}\nSummary: {dict(counts)}")


if __name__ == "__main__":
    main()
