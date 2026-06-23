"""
Standalone test: create a child account under an MCC via Google Ads internal
RPC. This is NOT wired into ui.py — pure experiment.

What it does:
    1. POST /aw_mcc/_/rpc/ClientCustomerSignupService/Mutate
       with currency/timezone/country + a recaptcha token.
    2. If Google returns AUTH_ERROR_REAUTH_PROOF_TOKEN_REQUIRED, print the
       challenge_id and stop — the user needs to complete 2FA in Chrome.
    3. After 2FA, the browser receives a `RAPT` cookie containing
       `DRAPT:<token>`. Paste that into RAPT_DRAPT below and rerun.
    4. The script then calls PublishReauthMessageService/PublishReauthMessage
       to acknowledge, then retries Mutate.

Usage:
    1. Refresh cookie.txt (full Cookie header from a logged-in Chrome).
    2. Edit the CONFIG block below — point MCC_OCID at the parent MCC, set
       currency/timezone/country, paste the captured RECAPTCHA_TOKEN, and
       (after 2FA) paste RAPT_DRAPT + CHALLENGE_ID.
    3. python test_create_mcc.py
"""

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import requests

import auto   # for load_cookies / data_dir / strip_xssi / discover_session

# ----- CONFIG ------------------------------------------------------------

MCC_OCID = "6598905101"   # parent MCC ocid; leave empty to auto-pick from /nav/selectaccount

CURRENCY  = "ILS"            # ISO 4217 currency code (e.g. USD, VND, ILS)
TIMEZONE  = "Europe/Moscow"  # IANA timezone
COUNTRY   = "RU"             # ISO country code

# Captured from a real "Create account" flow in the browser. We can't generate
# this — it's a reCAPTCHA / DoubleClick anti-fraud token. Re-capture it from
# DevTools each time:
#   F12 -> Network -> click "Create" in MCC -> find the Mutate request ->
#   Payload -> __ar -> field "4" is the token. Paste it as one long string.
RECAPTCHA_TOKEN = "0cAFcWeA6O0VRHEFoC_27qhC5KTMghpEqGsCqBa9DGoes-BgxtfftBJlnpmHcASSVLufQYMga_png6ID_IkkP6nj4f15SbEgvufrEh3oQP8UGWORks79pC0MCYS028hb6K1NBVh_1wAAe8LbrtkwiKdhQi5gG4vT6v4LR3Ipay3Fp5ceNdp-4M-WXsVuFb0KDHAltACwCODkiDg_niirQ5wXqMkknmkoZchKu3YqcchaejuHgKPMr9dc0vZ1EkdoR-9PNj9bfnHKMPd_VDNI_VoR9ZqsZdJ5g-Nfyi7Mx8Zb_971OoTHMCt3Edkoz2yd--qMXXypu1d9DsicG-9eOIlsG9bwL28h-PZsw7FS1nZzaF3VKbIXNn07Fm6pRp5KtKQUXXgQK21FOxlPjAhdN7ZymWjaAHghAzW_WK2cK45NBbMIy9UIG9u0XotTVNwsFBCfQUnw8C6cXuJM6BL_QvcpUTruL8qSYCAa4LOCsVAwaLpkWFVUrf5-2XC0Reu5v78YUFUHZeSVeywmNO3J4g7DrwiPY6TjQoxsbeHhnQ1iIq8UPoy94TrCwP7gwxExVah6l8EN5H1ejbD7yT2GlVqAKuzFfWFpWmUARtySj5JKs_q90loEBS1tmk0RIAEMlk1rUr2NVkXAOWIKdnvpJnwme3Qv9wlaVAA8qVMy6Q_BEss6qFoHloOuGTxtfBHu3YHXVY30eLd24LhPaVPKkDeBwhKhEfQzbWmc5rNFgIkugjS_5qLFSTVAXZmzHCciLOMU4QARNkxJoXcqAPFAKmraLx1DrVHR33thL-ZjyxpLa83vTrkeze3OpFPV4MwRH8sF0r7KXQ8R5X9SOO028lKs41JjClZWtDDt6JEGUVAONXNYPOXsxoOlavnW4D0bVGK_DoCuvEfEiN_em3XNLD0T98wylktLRk86GXRM2CKt_h8jZgjagFW97GNUSRXdsYmFYsmvaWbQCqf7b-B8Td7S-tRoZFszjcFTnjXstYQM-S6Szg7FWMkjED73rG76EX0e3FheyxMV-V2neTa39new-FCby5mfkjNWEKIkg40xylEO0BQ5EfJXFJk-INe4zqAjDn05HDyVCXzVkwOKera6KiqHGvCxDiBKWj-VpceTvnVBBTCOnBt2ZSRmWUBAdboXIclE2hfUB7rJZq0D9JlfYGlG0dHfq_KhGlKSYmhgRAQIsAkdbfgkmL5umuOZ_a4NCTvUeeaFDrONe-4he2aRm8s0EfqbBytaHo1qR98qaKgLLjfLfX99aiU2pgXybCFr3TrG82lsMoU_h9wWfriiUlYdNJCi19SuTdW36xiv7d4R2e1If0iJdKlU_gMtzzl2Gyf0A23aZDWFg7Ydq21UDtHAmX_iyK6lFQm4pfDYs2uH_DUksm44SBuFTZE0S2qvqXCdl1FizadpxuMO3NQRA63XR0tkc0DraOaSiXSwGaX14y09Rshp5LFc-pudl32plZ43IDfAUy0BwbnlvMTalrwhcEJdqO-8ksiZWFDXpClB6gKfB6K1rMGpuuqRrNfm61yasEt_c97lPv-OxV0jHvLelkQbNLZe7Fn3aqsa5WQ7r24tb3hwK_HlWwDo9-ZOnuq8cjB5QI471RbecxYZRqq1jCnGUssxVFaWamL0O_4U_jTaetwMGGb7aAdv7t8pVvRtqorw_jvMdUi74ogNKPuLMZaJQhbDqLaqiOCC70Z9Au7AMbWmxWQN_G58bwCHCHl5g6wpVNqVE05CVZ02fbckfIWDgCeNQmTwprlNBGtvzxuABpCCyDXFIsC7Ax8P5L8YJjFwLbSWNpuUsEGw7VRcTIwUSfaN6WXFuX3LcoWzFR6EEiupxtolO6flshQbOL-ECH4wsXCBreSfBJzI2AAQv6ymBTFkqPg8Z9R6yBoa3MR-ZhzWHjh-KlVcTGWJJwdBTK58Mql2Shw8j8oiP_cDIixmcFOTvv7-Saei91TUEU2DF7ACCg-R2Qtbv_gXDLTCd4xj7gbAKjIpPeraYI4QKuhhLtGRb56cDTPIa0tHDZnRgQ1zI-Ey_ORDuJjX3XrpikKCVuj_PJXMKg7bUpoa8kS11ZRgnzCiH0LjFid77v4NvAogKCT6TLmtmDMzhkKRtNzSz1Y5ikBbzyOxEEeo2Rh_17YR4Jbl5E_TdshS1GEwuBUGfEW1vyvNjG6o1FKEV_C2Vgp7xvCdo5gYAmkV73K2gPjA"

# After Google challenges with AUTH_ERROR_REAUTH_PROOF_TOKEN_REQUIRED:
#   - The response payload's field 10.7.2 carries a UUID — put it in CHALLENGE_ID
#   - Complete 2FA in Chrome
#   - The fresh Cookie header now has RAPT=AUTH:0+TYPE:6+DRAPT:<token>
#   - Extract everything after "DRAPT:" and paste it as RAPT_DRAPT
CHALLENGE_ID = ""
RAPT_DRAPT   = ""

# Session identifiers — leave blank to auto-discover via auto.discover_session.
FORCED_LU = ""    # __lu (login user id)
FORCED_U  = ""    # __u  (user id)
FORCED_C  = ""    # __c  (customer id)
FORCED_XSRF = ""  # x-framework-xsrf-token

# -------------------------------------------------------------------------

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")


def now_ms_id() -> str:
    import time
    return str(int(time.time() * 1000))


def shared_headers(cfg: dict) -> dict:
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://ads.google.com",
        "referer": (
            f"https://ads.google.com/aw/accounts?ocid={MCC_OCID}"
            f"&workspaceId=0&euid={cfg['login_user_id']}"
            f"&__u={cfg['user_id']}&uscid={MCC_OCID}"
            f"&__c={cfg['customer_id']}&authuser={cfg['authuser']}"
        ),
        "user-agent": UA,
        "x-framework-xsrf-token": cfg["xsrf_token"],
        "x-same-domain": "1",
    }


def mutate_create_child(session: requests.Session, cookies: dict, cfg: dict) -> dict:
    url = (
        "https://ads.google.com/aw_mcc/_/rpc/ClientCustomerSignupService/Mutate"
        f"?authuser={cfg['authuser']}&xt=awn"
        "&rpcTrackingId=ClientCustomerSignupService.Mutate%3A1"
        f"&f.sid={cfg['f_sid']}"
    )
    ar = {
        "1": {"3": {"1": MCC_OCID}},
        "2": {
            "3": CURRENCY,
            "4": "",
            "5": TIMEZONE,
            "7": 30,
            "8": False,
            "9": COUNTRY,
            "10": 1,
            "11": {"1": ""},
        },
        "3": [{"1": "useUfoFlow"}, {"1": "managerUnqualified"}],
        "4": RECAPTCHA_TOKEN,
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
        "requestPriority":  "HIGH_LATENCY_SENSITIVE",
        "activityType":     "INTERACTIVE",
        "activityId":       now_ms_id(),
        "uniqueFingerprint": f"{cfg['f_sid']}_{now_ms_id()}_1",
        "previousPlace":    "/aw/accounts",
        "activityName":     "MccAccountsTable.AccountRecaptchaDialog.NextButtonClicked",
        "destinationPlace": "/aw/accounts",
    }
    r = session.post(url, headers=shared_headers(cfg), cookies=cookies,
                     data=urlencode(body), timeout=60)
    return {"http": r.status_code, "raw": r.text,
            "data": _safe_json(r.text)}


def publish_reauth(session: requests.Session, cookies: dict, cfg: dict,
                   drapt: str, challenge_id: str) -> dict:
    """Tell Google: 'the 2FA challenge has been answered, here's the proof.'"""
    url = (
        "https://ads.google.com/aw/_/rpc/PublishReauthMessageService/PublishReauthMessage"
        f"?authuser={cfg['authuser']}&xt=awn"
        "&rpcTrackingId=PublishReauthMessageService.PublishReauthMessage%3A2"
        f"&f.sid={cfg['f_sid']}"
    )
    import time
    now = time.time()
    ar = {
        "2": 3,
        "3": MCC_OCID,
        "4": cfg["login_user_id"],
        "8": {"1": str(int(now)),       "2": int((now % 1) * 1e9)},
        "9": {"1": str(int(now) + 60),  "2": int((now % 1) * 1e9)},
        "12": challenge_id,
    }
    body = {
        "hl": "en_US",
        "__lu": cfg["login_user_id"],
        "__u":  cfg["user_id"],
        "__c":  cfg["customer_id"],
        "f.sid": cfg["f_sid"],
        "ps": "aw",
        "__ar": json.dumps(ar, separators=(",", ":")),
        "drapt": drapt,
        "activityContext": "Anonymous",
        "requestPriority": "HIGH_LATENCY_SENSITIVE",
        "activityType":    "ANONYMOUS",
        "activityId":      now_ms_id(),
        "uniqueFingerprint": f"{cfg['f_sid']}_{now_ms_id()}_1",
        "destinationPlace": "/aw/accounts",
    }
    r = session.post(url, headers=shared_headers(cfg), cookies=cookies,
                     data=urlencode(body), timeout=30)
    return {"http": r.status_code, "raw": r.text,
            "data": _safe_json(r.text)}


def _safe_json(text: str):
    try:
        return json.loads(auto.strip_xssi(text))
    except Exception:
        return None


def parse_reauth_error(data: dict) -> tuple[bool, str]:
    """Detect 'needs 2FA' and pull out the challenge UUID."""
    if not isinstance(data, dict):
        return False, ""
    errs = data.get("2", {}).get("2") if isinstance(data.get("2"), dict) else None
    if not errs:
        return False, ""
    for e in errs:
        if e.get("3") == "AUTH_ERROR_REAUTH_PROOF_TOKEN_REQUIRED":
            cid = (e.get("10", {}).get("7", {}) or {}).get("2", "")
            return True, cid
    return False, ""


def main():
    global MCC_OCID
    here = auto.data_dir()
    cookies = auto.load_cookies(here / "cookie.txt")

    session = requests.Session()

    # Auto-discover the session params unless the user pinned them above.
    try:
        cfg = auto.discover_session(session, cookies, "0", MCC_OCID)
    except auto.MultipleMCCsError as e:
        ocids = [o for o, _ in e.mccs]
        print("Multiple MCCs available — pick one and rerun with MCC_OCID set:")
        for o in ocids:
            print(f"  {o}")
        sys.exit(1)

    # Sync our copy of MCC_OCID with the one the discoverer ended up using
    # (it may have auto-picked when there was just one).
    MCC_OCID = cfg["manager_customer_id"]
    if FORCED_LU: cfg["login_user_id"] = FORCED_LU
    if FORCED_U:  cfg["user_id"]       = FORCED_U
    if FORCED_C:  cfg["customer_id"]   = FORCED_C
    if FORCED_XSRF: cfg["xsrf_token"]  = FORCED_XSRF
    print("Session:", {k: cfg[k] for k in
          ("manager_customer_id","login_user_id","user_id","customer_id","f_sid")})

    if not RECAPTCHA_TOKEN:
        sys.exit(
            "RECAPTCHA_TOKEN is empty. Capture it from a real browser flow:\n"
            "  Chrome -> open the MCC -> 'Create sub-account' -> solve reCAPTCHA\n"
            "  -> DevTools Network -> the Mutate request -> Payload -> __ar -> field '4'."
        )

    # Step 1: try the mutation.
    print("\n[1] POST ClientCustomerSignupService/Mutate ...")
    out = mutate_create_child(session, cookies, cfg)
    print(f"    http={out['http']}")
    print(f"    raw : {out['raw'][:300]}")
    if out["data"] is None:
        sys.exit("Non-JSON response, bailing.")

    needs_2fa, chid = parse_reauth_error(out["data"])
    if not needs_2fa:
        # Probably success or some other error — print and stop.
        print("\n[done] Response:")
        print(json.dumps(out["data"], indent=2, ensure_ascii=False)[:1200])
        return

    print(f"\n[2] Reauth required. challenge_id (auto-extracted) = {chid}")

    # Auto-pull DRAPT from the cookies if the user already did 2FA in Chrome
    # and refreshed cookie.txt. RAPT looks like  AUTH:0+TYPE:6+DRAPT:<token>
    rapt_cookie = cookies.get("RAPT", "")
    auto_drapt = ""
    m = re.search(r"DRAPT:([^;+\s]+)", rapt_cookie)
    if m:
        auto_drapt = m.group(1)
        print(f"    Found DRAPT in cookie.txt's RAPT cookie ({len(auto_drapt)} chars).")

    drapt = RAPT_DRAPT or auto_drapt
    challenge = CHALLENGE_ID or chid   # use auto-extracted if user didn't set

    if not drapt:
        sys.exit(
            "\nNo DRAPT available. Do this:\n"
            "  1. Open https://ads.google.com/aw/accounts in Chrome (same Google account).\n"
            "  2. Click 'Create account' under the MCC — Google will prompt for 2FA.\n"
            "  3. Pass the 2FA challenge.\n"
            "  4. Re-copy the FULL Cookie header from any ads.google.com request and\n"
            "     overwrite cookie.txt — the new RAPT cookie carries the DRAPT token.\n"
            "  5. Re-run this script (CHALLENGE_ID is auto-extracted each run).\n"
            "\n"
            "Alternatively, set RAPT_DRAPT at the top of the script manually:\n"
            f"  CHALLENGE_ID = '{chid}'\n"
            "  RAPT_DRAPT   = '<token after DRAPT: in the RAPT cookie>'"
        )

    print(f"\n[3] POST PublishReauthMessageService/PublishReauthMessage ...")
    pub = publish_reauth(session, cookies, cfg, drapt, challenge)
    print(f"    http={pub['http']}")
    print(f"    raw : {pub['raw'][:300]}")
    if pub["data"] is None or pub["data"].get("1") != 2:
        sys.exit("PublishReauthMessage didn't return {1:2} — check raw response above.")

    # Step 4: also inject the RAPT cookie into our session so the retry carries
    # the proof. Cookie name 'RAPT', value mirrors the browser format.
    cookies["RAPT"] = f"AUTH:0+TYPE:6+DRAPT:{drapt}"

    # Step 5: retry the create.
    print("\n[4] Retrying ClientCustomerSignupService/Mutate ...")
    retry = mutate_create_child(session, cookies, cfg)
    print(f"    http={retry['http']}")
    print(json.dumps(retry["data"], indent=2, ensure_ascii=False)[:1200]
          if retry["data"] else retry["raw"][:800])


if __name__ == "__main__":
    main()
