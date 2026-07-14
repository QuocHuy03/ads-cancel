"""
Country + currency dropdown data for the MCC create UI, plus a thin wrapper
around Google Ads TimeZoneConstantService/List that returns the timezones
allowed for a given country.

Keeping this in its own file so create_mcc_ui.py stays focused on UX.
"""

import json
import re
from collections import Counter
from urllib.parse import urlencode

import requests

import auto


# -- countries: ISO 3166-1 alpha-2 codes Google Ads supports for sub-accounts.
# Sorted alphabetically by display name.
COUNTRIES: list[tuple[str, str]] = [
    ("AF", "Afghanistan"),    ("AX", "Åland Islands"),    ("AL", "Albania"),
    ("DZ", "Algeria"),        ("AS", "American Samoa"),   ("AD", "Andorra"),
    ("AO", "Angola"),         ("AI", "Anguilla"),         ("AQ", "Antarctica"),
    ("AG", "Antigua and Barbuda"), ("AR", "Argentina"),    ("AM", "Armenia"),
    ("AW", "Aruba"),          ("AU", "Australia"),        ("AT", "Austria"),
    ("AZ", "Azerbaijan"),     ("BS", "Bahamas"),          ("BH", "Bahrain"),
    ("BD", "Bangladesh"),     ("BB", "Barbados"),         ("BY", "Belarus"),
    ("BE", "Belgium"),        ("BZ", "Belize"),           ("BJ", "Benin"),
    ("BM", "Bermuda"),        ("BT", "Bhutan"),           ("BO", "Bolivia"),
    ("BA", "Bosnia and Herzegovina"), ("BW", "Botswana"),  ("BR", "Brazil"),
    ("IO", "British Indian Ocean Territory"), ("VG", "British Virgin Islands"),
    ("BN", "Brunei"),         ("BG", "Bulgaria"),         ("BF", "Burkina Faso"),
    ("BI", "Burundi"),        ("KH", "Cambodia"),         ("CM", "Cameroon"),
    ("CA", "Canada"),         ("CV", "Cape Verde"),       ("BQ", "Caribbean Netherlands"),
    ("KY", "Cayman Islands"), ("CF", "Central African Republic"),
    ("TD", "Chad"),           ("CL", "Chile"),            ("CN", "China"),
    ("CX", "Christmas Island"), ("CC", "Cocos (Keeling) Islands"),
    ("CO", "Colombia"),       ("KM", "Comoros"),          ("CG", "Congo - Brazzaville"),
    ("CD", "Congo - Kinshasa"), ("CK", "Cook Islands"),    ("CR", "Costa Rica"),
    ("CI", "Côte d'Ivoire"),  ("HR", "Croatia"),          ("CW", "Curaçao"),
    ("CY", "Cyprus"),         ("CZ", "Czechia"),          ("DK", "Denmark"),
    ("DJ", "Djibouti"),       ("DM", "Dominica"),         ("DO", "Dominican Republic"),
    ("EC", "Ecuador"),        ("EG", "Egypt"),            ("SV", "El Salvador"),
    ("GQ", "Equatorial Guinea"), ("ER", "Eritrea"),       ("EE", "Estonia"),
    ("SZ", "Eswatini"),       ("ET", "Ethiopia"),         ("FK", "Falkland Islands"),
    ("FO", "Faroe Islands"),  ("FJ", "Fiji"),             ("FI", "Finland"),
    ("FR", "France"),         ("GF", "French Guiana"),    ("PF", "French Polynesia"),
    ("GA", "Gabon"),          ("GM", "Gambia"),           ("GE", "Georgia"),
    ("DE", "Germany"),        ("GH", "Ghana"),            ("GI", "Gibraltar"),
    ("GR", "Greece"),         ("GL", "Greenland"),        ("GD", "Grenada"),
    ("GP", "Guadeloupe"),     ("GU", "Guam"),             ("GT", "Guatemala"),
    ("GG", "Guernsey"),       ("GN", "Guinea"),           ("GW", "Guinea-Bissau"),
    ("GY", "Guyana"),         ("HT", "Haiti"),            ("HN", "Honduras"),
    ("HK", "Hong Kong"),      ("HU", "Hungary"),          ("IS", "Iceland"),
    ("IN", "India"),          ("ID", "Indonesia"),        ("IQ", "Iraq"),
    ("IE", "Ireland"),        ("IM", "Isle of Man"),      ("IL", "Israel"),
    ("IT", "Italy"),          ("JM", "Jamaica"),          ("JP", "Japan"),
    ("JE", "Jersey"),         ("JO", "Jordan"),           ("KZ", "Kazakhstan"),
    ("KE", "Kenya"),          ("KI", "Kiribati"),         ("XK", "Kosovo"),
    ("KW", "Kuwait"),         ("KG", "Kyrgyzstan"),       ("LA", "Laos"),
    ("LV", "Latvia"),         ("LB", "Lebanon"),          ("LS", "Lesotho"),
    ("LR", "Liberia"),        ("LY", "Libya"),            ("LI", "Liechtenstein"),
    ("LT", "Lithuania"),      ("LU", "Luxembourg"),       ("MO", "Macao"),
    ("MG", "Madagascar"),     ("MW", "Malawi"),           ("MY", "Malaysia"),
    ("MV", "Maldives"),       ("ML", "Mali"),             ("MT", "Malta"),
    ("MH", "Marshall Islands"),("MQ", "Martinique"),       ("MR", "Mauritania"),
    ("MU", "Mauritius"),      ("YT", "Mayotte"),          ("MX", "Mexico"),
    ("FM", "Micronesia"),     ("MD", "Moldova"),          ("MC", "Monaco"),
    ("MN", "Mongolia"),       ("ME", "Montenegro"),       ("MS", "Montserrat"),
    ("MA", "Morocco"),        ("MZ", "Mozambique"),       ("MM", "Myanmar"),
    ("NA", "Namibia"),        ("NR", "Nauru"),            ("NP", "Nepal"),
    ("NL", "Netherlands"),    ("NC", "New Caledonia"),    ("NZ", "New Zealand"),
    ("NI", "Nicaragua"),      ("NE", "Niger"),            ("NG", "Nigeria"),
    ("NU", "Niue"),           ("NF", "Norfolk Island"),   ("MK", "North Macedonia"),
    ("MP", "Northern Mariana Islands"), ("NO", "Norway"),  ("OM", "Oman"),
    ("PK", "Pakistan"),       ("PW", "Palau"),            ("PS", "Palestine"),
    ("PA", "Panama"),         ("PG", "Papua New Guinea"), ("PY", "Paraguay"),
    ("PE", "Peru"),           ("PH", "Philippines"),      ("PN", "Pitcairn Islands"),
    ("PL", "Poland"),         ("PT", "Portugal"),         ("PR", "Puerto Rico"),
    ("QA", "Qatar"),          ("RE", "Réunion"),          ("RO", "Romania"),
    ("RU", "Russia"),         ("RW", "Rwanda"),           ("BL", "Saint Barthélemy"),
    ("SH", "Saint Helena"),   ("KN", "Saint Kitts and Nevis"),
    ("LC", "Saint Lucia"),    ("MF", "Saint Martin"),     ("PM", "Saint Pierre and Miquelon"),
    ("VC", "Saint Vincent and the Grenadines"), ("WS", "Samoa"),
    ("SM", "San Marino"),     ("ST", "São Tomé and Príncipe"),
    ("SA", "Saudi Arabia"),   ("SN", "Senegal"),          ("RS", "Serbia"),
    ("SC", "Seychelles"),     ("SL", "Sierra Leone"),     ("SG", "Singapore"),
    ("SX", "Sint Maarten"),   ("SK", "Slovakia"),         ("SI", "Slovenia"),
    ("SB", "Solomon Islands"),("SO", "Somalia"),          ("ZA", "South Africa"),
    ("GS", "South Georgia and the South Sandwich Islands"),
    ("KR", "South Korea"),    ("SS", "South Sudan"),      ("ES", "Spain"),
    ("LK", "Sri Lanka"),      ("SR", "Suriname"),         ("SJ", "Svalbard and Jan Mayen"),
    ("SE", "Sweden"),         ("CH", "Switzerland"),      ("TW", "Taiwan"),
    ("TJ", "Tajikistan"),     ("TZ", "Tanzania"),         ("TH", "Thailand"),
    ("TL", "Timor-Leste"),    ("TG", "Togo"),             ("TK", "Tokelau"),
    ("TO", "Tonga"),          ("TT", "Trinidad and Tobago"), ("TN", "Tunisia"),
    ("TR", "Türkiye"),        ("TM", "Turkmenistan"),     ("TC", "Turks and Caicos Islands"),
    ("TV", "Tuvalu"),         ("UG", "Uganda"),           ("UA", "Ukraine"),
    ("AE", "United Arab Emirates"), ("GB", "United Kingdom"),
    ("US", "United States"),  ("UY", "Uruguay"),          ("UZ", "Uzbekistan"),
    ("VU", "Vanuatu"),        ("VA", "Vatican City"),     ("VE", "Venezuela"),
    ("VN", "Vietnam"),        ("WF", "Wallis and Futuna"),("EH", "Western Sahara"),
    ("YE", "Yemen"),          ("ZM", "Zambia"),           ("ZW", "Zimbabwe"),
]

# -- currencies: ISO 4217 codes Google Ads accepts at MCC sub-account creation.
# Display string mirrors what the Ads UI shows (name + code + symbol).
CURRENCIES: list[tuple[str, str]] = [
    ("AED", "United Arab Emirates Dirham (AED)"),
    ("ARS", "Argentine Peso (ARS)"),
    ("AUD", "Australian Dollar (AUD A$)"),
    ("BGN", "Bulgarian Lev (BGN)"),
    ("BOB", "Bolivian Boliviano (BOB)"),
    ("BRL", "Brazilian Real (BRL R$)"),
    ("CAD", "Canadian Dollar (CAD CA$)"),
    ("CHF", "Swiss Franc (CHF)"),
    ("CLP", "Chilean Peso (CLP)"),
    ("CNY", "Chinese Yuan (CNY CN¥)"),
    ("COP", "Colombian Peso (COP)"),
    ("CZK", "Czech Koruna (CZK)"),
    ("DKK", "Danish Krone (DKK)"),
    ("EGP", "Egyptian Pound (EGP)"),
    ("EUR", "Euro (EUR €)"),
    ("GBP", "British Pound (GBP £)"),
    ("HKD", "Hong Kong Dollar (HKD HK$)"),
    ("HUF", "Hungarian Forint (HUF)"),
    ("IDR", "Indonesian Rupiah (IDR)"),
    ("ILS", "Israeli New Shekel (ILS ₪)"),
    ("INR", "Indian Rupee (INR ₹)"),
    ("JPY", "Japanese Yen (JPY ¥)"),
    ("KES", "Kenyan Shilling (KES)"),
    ("KRW", "South Korean Won (KRW ₩)"),
    ("MXN", "Mexican Peso (MXN)"),
    ("MYR", "Malaysian Ringgit (MYR)"),
    ("NGN", "Nigerian Naira (NGN ₦)"),
    ("NOK", "Norwegian Krone (NOK)"),
    ("NZD", "New Zealand Dollar (NZD NZ$)"),
    ("PEN", "Peruvian Sol (PEN)"),
    ("PHP", "Philippine Peso (PHP ₱)"),
    ("PKR", "Pakistani Rupee (PKR)"),
    ("PLN", "Polish Złoty (PLN zł)"),
    ("RON", "Romanian Leu (RON)"),
    ("RUB", "Russian Ruble (RUB ₽)"),
    ("SAR", "Saudi Riyal (SAR)"),
    ("SEK", "Swedish Krona (SEK)"),
    ("SGD", "Singapore Dollar (SGD S$)"),
    ("THB", "Thai Baht (THB ฿)"),
    ("TRY", "Turkish Lira (TRY ₺)"),
    ("TWD", "New Taiwan Dollar (TWD NT$)"),
    ("UAH", "Ukrainian Hryvnia (UAH ₴)"),
    ("USD", "US Dollar (USD $)"),
    ("VND", "Vietnamese Dong (VND ₫)"),
    ("ZAR", "South African Rand (ZAR R)"),
]


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")


def _tz_headers(cfg: dict, mcc_ocid: str, extras: dict = None) -> dict:
    """Same Chrome-mirroring headers we use for Mutate. Reuse the same
    referer (/aw/account/new) so Google sees a consistent dialog flow."""
    h = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://ads.google.com",
        "referer": (
            f"https://ads.google.com/aw/account/new?ocid={mcc_ocid}"
            f"&ascid={mcc_ocid}&euid={cfg['login_user_id']}"
            f"&__u={cfg['user_id']}&uscid={mcc_ocid}"
            f"&__c={cfg['customer_id']}&authuser={cfg['authuser']}"
        ),
        "user-agent": UA,
        "x-framework-xsrf-token": cfg["xsrf_token"],
        "x-same-domain": "1",
    }
    if extras:
        for k, v in extras.items():
            if v:
                h[k] = v
    return h


def list_timezones(session: requests.Session, cookies: dict, cfg: dict,
                   mcc_ocid: str, country_code: str,
                   extras: dict = None, drapt: str = "") -> list[tuple[str, str]]:
    """Return [(posix_name, display_name), ...] valid for the given country.

    Wraps /aw_mcc/_/rpc/TimeZoneConstantService/List, the same RPC the
    'Time zone' dropdown calls when the country changes in the Ads UI.
    `drapt` mirrors the body field the browser sends after 2FA — needed for
    sessions that have 2-Step Verification enforced."""
    url = (
        "https://ads.google.com/aw_mcc/_/rpc/TimeZoneConstantService/List"
        f"?authuser={cfg['authuser']}&xt=awn"
        "&rpcTrackingId=TimeZoneConstantService.List%3A3"
        f"&f.sid={cfg['f_sid']}"
    )
    ar = {
        "2": {
            "1": ["posix_name", "display_name", "offset"],
            "2": [
                {"1": "country_constant_code", "2": 1,
                 "4": [{"6": country_code.upper()}]},
            ],
        }
    }
    import time as _t
    aid = str(int(_t.time() * 1000))
    body = {
        "hl": "en_US",
        "__lu": cfg["login_user_id"],
        "__u":  cfg["user_id"],
        "__c":  cfg["customer_id"],
        "f.sid": cfg["f_sid"],
        "ps": "aw",
        "__ar": json.dumps(ar, separators=(",", ":")),
        "activityContext": "Anonymous",
        "requestPriority":  "HIGH_LATENCY_SENSITIVE",
        "activityType":     "ANONYMOUS",
        "activityId":       aid,
        "uniqueFingerprint": f"{cfg['f_sid']}_{aid}_1",
        "destinationPlace": "/aw/account/new",
    }
    if drapt:
        body["drapt"] = drapt
    r = session.post(url, headers=_tz_headers(cfg, mcc_ocid, extras),
                     cookies=cookies, data=urlencode(body), timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"TimeZoneConstantService HTTP {r.status_code}: {r.text[:200]}")
    data = json.loads(auto.strip_xssi(r.text))
    out = []
    for tz in data.get("1") or []:
        posix = tz.get("2") or ""
        display = tz.get("3") or posix
        if posix:
            out.append((posix, display))
    return out


# reCAPTCHA site keys always start with "6L" + 6 chars + "AAAAA" then 27 more.
# We grep the awn_mcc dart bundle for that exact shape.
_SITEKEY_RX = re.compile(r"6L[A-Za-z0-9_\-]{6}AAAAA[A-Za-z0-9_\-]{27}")
_BUNDLE_URL_RX = re.compile(
    r"https://www\.gstatic\.com/awn/mcc/[A-Za-z0-9_/\-\.]+?/main\.dart\.js"
)


def detect_recaptcha_sitekey(session: requests.Session, cookies: dict,
                              cfg: dict) -> str:
    """Auto-detect the reCAPTCHA v2 sitekey Google currently uses on the MCC
    create-account dialog.

    How it works:
      1. Load the MCC accounts page (HTML shell). Find the awn_mcc Dart
         bundle URL (https://www.gstatic.com/awn/mcc/...../main.dart.js).
      2. Download the bundle from gstatic (CDN, no auth needed).
      3. Grep for the reCAPTCHA sitekey constant — its shape is well known
         (starts '6L', 13th–17th chars are 'AAAAA').
      4. Return the most-frequent match (real sitekey gets referenced
         multiple times; base64 noise that happens to match is a one-off).
    """
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

    accounts_url = (
        f"https://ads.google.com/aw/accounts?ocid={cfg['manager_customer_id']}"
        f"&authuser={cfg['authuser']}"
    )
    r = session.get(accounts_url, cookies=cookies,
                    headers={"user-agent": ua, "accept": "text/html"},
                    allow_redirects=True, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"GET /aw/accounts -> HTTP {r.status_code}")

    m = _BUNDLE_URL_RX.search(r.text)
    if not m:
        raise RuntimeError(
            "Couldn't find awn_mcc dart bundle URL in /aw/accounts HTML. "
            "Try refreshing cookies."
        )
    bundle_url = m.group(0)

    rb = session.get(bundle_url, timeout=60)
    if rb.status_code != 200:
        raise RuntimeError(f"GET dart bundle -> HTTP {rb.status_code}")

    candidates = _SITEKEY_RX.findall(rb.text)
    if not candidates:
        raise RuntimeError("No reCAPTCHA sitekey constant found in dart bundle.")
    # Pick the most frequent match — real sitekey appears many times,
    # incidental base64 hits don't.
    sitekey, _count = Counter(candidates).most_common(1)[0]
    return sitekey
