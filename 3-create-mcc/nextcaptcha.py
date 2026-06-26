"""
NextCaptcha API wrapper (https://nextcaptcha.com/vi/apidocs).

Supports the proxyless RecaptchaV3 task type used by Google Ads internal forms.
For Enterprise variants pass enterprise=True (NextCaptcha uses the same
task type name with isEnterprise:true under the hood).
"""

import time
import requests

BASE = "https://api.nextcaptcha.com"
CREATE_TASK   = f"{BASE}/createTask"
GET_RESULT    = f"{BASE}/getTaskResult"
GET_BALANCE   = f"{BASE}/getBalance"


class NextCaptchaError(Exception):
    pass


def get_balance(api_key: str, timeout: int = 15) -> float:
    """Return USD balance of the account. Raises NextCaptchaError on failure."""
    r = requests.post(GET_BALANCE, json={"clientKey": api_key}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if data.get("errorId"):
        raise NextCaptchaError(f"{data.get('errorCode')}: {data.get('errorDescription')}")
    return float(data.get("balance", 0))


def create_task(api_key: str, website_url: str, website_key: str,
                page_action: str = "", enterprise: bool = True,
                api_domain: str = "google.com",
                timeout: int = 15) -> int:
    task = {
        "type": "RecaptchaV3TaskProxyless",
        "websiteURL": website_url,
        "websiteKey": website_key,
        "pageAction": page_action,
        "apiDomain": api_domain,
        "isEnterprise": bool(enterprise),
    }
    r = requests.post(CREATE_TASK,
                      json={"clientKey": api_key, "task": task},
                      timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if data.get("errorId"):
        raise NextCaptchaError(f"{data.get('errorCode')}: {data.get('errorDescription')}")
    return data["taskId"]


def get_task_result(api_key: str, task_id: int, timeout: int = 15) -> dict:
    r = requests.post(GET_RESULT,
                      json={"clientKey": api_key, "taskId": task_id},
                      timeout=timeout)
    r.raise_for_status()
    return r.json()


def create_task_v2(api_key: str, website_url: str, website_key: str,
                   is_invisible: bool = False, timeout: int = 15) -> int:
    """Create a RecaptchaV2TaskProxyless task (checkbox 'I'm not a robot')."""
    task = {
        "type": "RecaptchaV2TaskProxyless",
        "websiteURL": website_url,
        "websiteKey": website_key,
        "isInvisible": bool(is_invisible),
    }
    r = requests.post(CREATE_TASK,
                      json={"clientKey": api_key, "task": task},
                      timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if data.get("errorId"):
        raise NextCaptchaError(f"{data.get('errorCode')}: {data.get('errorDescription')}")
    return data["taskId"]


def _poll_until_ready(api_key, task_id, poll_interval, max_wait, on_progress):
    started = time.time()
    while True:
        if time.time() - started > max_wait:
            raise NextCaptchaError(f"Timed out after {max_wait}s waiting for taskId={task_id}")
        time.sleep(poll_interval)
        res = get_task_result(api_key, task_id)
        status = res.get("status")
        if status == "processing":
            if on_progress:
                on_progress("processing...")
            continue
        if status == "ready":
            sol = res.get("solution", {}) or {}
            token = sol.get("gRecaptchaResponse") or sol.get("token") or ""
            if not token:
                raise NextCaptchaError(f"ready but no token in solution: {sol}")
            if on_progress:
                on_progress(f"ready ({len(token)} chars)")
            return token
        if res.get("errorId"):
            raise NextCaptchaError(f"{res.get('errorCode')}: {res.get('errorDescription')}")
        if on_progress:
            on_progress(f"status={status}")


def solve_recaptcha_v3(api_key: str, website_url: str, website_key: str,
                       page_action: str = "", enterprise: bool = True,
                       poll_interval: float = 3.0,
                       max_wait: float = 120.0,
                       on_progress=None) -> str:
    """V3 / V3-Enterprise solver."""
    task_id = create_task(api_key, website_url, website_key,
                          page_action=page_action, enterprise=enterprise)
    if on_progress:
        on_progress(f"task created (id={task_id}, V3)")
    return _poll_until_ready(api_key, task_id, poll_interval, max_wait, on_progress)


def solve_recaptcha_v2(api_key: str, website_url: str, website_key: str,
                       is_invisible: bool = False,
                       poll_interval: float = 3.0,
                       max_wait: float = 180.0,
                       on_progress=None) -> str:
    """V2 checkbox / invisible solver. V2 jobs usually take 20-60s."""
    task_id = create_task_v2(api_key, website_url, website_key,
                             is_invisible=is_invisible)
    if on_progress:
        on_progress(f"task created (id={task_id}, V2 checkbox)")
    return _poll_until_ready(api_key, task_id, poll_interval, max_wait, on_progress)
