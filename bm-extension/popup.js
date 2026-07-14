// BM Ads Account Setup — extension popup.
//
// Session tokens come from the background service worker, which snoops
// the tab's outgoing RPCs and stashes f.sid + xsrf in chrome.storage.
// The popup only reconstructs a=/mc= from the active tab URL, then calls:
//   1. GaiaAccountsService.Get           -> whose account is this?
//   2. AdsOnboardingService.GetAdsLocaleConstants -> country/currency/timezone
//   3. AdsOnboardingService.CreateAdsAccount     -> the actual mutation
//
// (3) uses a payload shape reverse-engineered from Google's typical proto
// layout for onboarding flows — if it ever rejects with a schema error,
// the response body is logged verbatim so we can adjust field ids.

const BM_HOST = "https://business.google.com";
const LINKED_APPS_PATH = "/manager/linkedapps";

// ---------- tiny helpers ----------

const $ = (id) => document.getElementById(id);
const log = (msg) => {
  const el = $("log");
  const ts = new Date().toLocaleTimeString();
  el.textContent += `[${ts}] ${msg}\n`;
  el.scrollTop = el.scrollHeight;
};
const setStatus = (text, cls = "") => {
  const el = $("status");
  el.textContent = text;
  el.className = "badge " + cls;
};

const stripXssi = (t) => t.replace(/^\s*\)\]\}'?\s*/, "");

// ---------- session bootstrap ----------

async function detectSessionFromTab() {
  const {session: cached} = await chrome.storage.local.get(["session"]);
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});

  let tabIds = {a: "", mc: ""};
  if (tab && tab.url) {
    try {
      const u = new URL(tab.url);
      tabIds.a  = u.searchParams.get("a")  || "";
      tabIds.mc = u.searchParams.get("mc") || "";
    } catch (e) { /* no-op */ }
  }

  const merged = {
    xsrf:     (cached && cached.xsrf)     || "",
    fsid:     (cached && cached.fsid)     || "",
    a:        tabIds.a  || (cached && cached.a)  || "",
    mc:       tabIds.mc || (cached && cached.mc) || "",
    authuser: (cached && cached.authuser) || "0",
  };

  if (merged.xsrf && merged.fsid && merged.a && merged.mc) return merged;

  if (!tab || !tab.url ||
      !/^https:\/\/business\.google\.com\/manager\//.test(tab.url)) {
    throw new Error(
      "Open a business.google.com/manager/... tab first, then click Detect."
    );
  }

  const [{result}] = await chrome.scripting.executeScript({
    target: {tabId: tab.id},
    world: "MAIN",
    func: () => {
      const wg = (typeof window !== "undefined" && window.WIZ_global_data) || {};
      const u = new URL(location.href);
      let xsrf = wg.SNlM0e || "";
      if (!xsrf) {
        const m = document.documentElement.innerHTML.match(
          /"(A[A-Za-z0-9_\-]{20,}:\d{13})"/
        );
        if (m) xsrf = m[1];
      }
      let fsid = wg.FdrFJe || wg["FdrFJe"] || "";
      if (!fsid) {
        const m = document.documentElement.innerHTML.match(
          /"f\.sid"\s*:\s*"?(-?\d{15,20})"?/
        );
        if (m) fsid = m[1];
      }
      return {
        xsrf, fsid,
        a: u.searchParams.get("a") || "",
        mc: u.searchParams.get("mc") || "",
        authuser: u.searchParams.get("authuser") || "0",
      };
    },
  });

  const final = {
    xsrf:     merged.xsrf     || result.xsrf,
    fsid:     merged.fsid     || result.fsid,
    a:        merged.a        || result.a,
    mc:       merged.mc       || result.mc,
    authuser: merged.authuser || result.authuser || "0",
  };

  const missing = [];
  if (!final.xsrf) missing.push("xsrf");
  if (!final.fsid) missing.push("f.sid");
  if (!final.a)    missing.push("a=");
  if (!final.mc)   missing.push("mc=");
  if (missing.length) {
    throw new Error(
      `Couldn't read: ${missing.join(", ")}. ` +
      `Reload the tab (Ctrl+R) then click Detect — the background hook ` +
      `captures tokens from the page's own RPC traffic.`
    );
  }
  return final;
}

// ---------- RPC layer ----------

function rpcHeaders(session) {
  return {
    "accept": "*/*",
    "content-type": "application/x-www-form-urlencoded",
    "x-framework-xsrf-token": session.xsrf,
    "x-same-domain": "1",
  };
}

async function rpcCall(session, service, method, ar = {}, trackingIdSuffix = "%3A1") {
  const url = `${BM_HOST}/manager/_/rpc/${service}/${method}`
    + `?authuser=${session.authuser}`
    + `&rpcTrackingId=${service}.${method}${trackingIdSuffix}`
    + `&f.sid=${session.fsid}`;
  const params = new URLSearchParams();
  params.set("a", session.a);
  params.set("f.sid", session.fsid);
  params.set("__ar", JSON.stringify(ar));
  const res = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: rpcHeaders(session),
    body: params.toString(),
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`${service}.${method} HTTP ${res.status}: ${text.slice(0, 400)}`);
  try {
    return JSON.parse(stripXssi(text));
  } catch (e) {
    throw new Error(`${service}.${method} parse: ${e.message}. Body: ${text.slice(0, 400)}`);
  }
}

async function gaiaGet(session) {
  return rpcCall(session, "GaiaAccountsService", "Get", {2: true}, "%3A2");
}

async function getLocaleConstants(session) {
  return rpcCall(session, "AdsOnboardingService", "GetAdsLocaleConstants", {});
}

async function listLinkedApps(session) {
  return rpcCall(session, "LinkedFirstPartyService", "List", {}, "%3A2");
}

async function getMerchantInfo(session) {
  return rpcCall(session, "OneMerchantService", "Get", {}, "%3A2");
}

// ---------- Locale reshape ----------

function parseLocaleResponse(raw) {
  // Top-level "2" is the timezone catalog. Each entry has "1" = Google's
  // internal timezone ID (what we POST back on create) and "2" = display name.
  const timezones = (raw["2"] || []).map((tz) => ({
    id: Number(tz["1"]), name: tz["2"] || "",
  }));

  const countries = (raw["1"] || []).map((entry) => {
    const c   = entry["1"] || {};
    const cur = entry["3"] || {};
    // Country's "2" is a list of INDICES into the timezone catalog above,
    // NOT a list of ids matching timezone["1"]. Google's UI resolves by
    // position; if we look up by id we get wildly wrong names (e.g. VN
    // pointing to Honolulu because tz.id 364 happens to be Honolulu but
    // position 364 is Asia/Ho_Chi_Minh).
    const indices = (entry["2"] || []).map(Number);
    const tzs = [];
    const seen = new Set();
    for (const idx of indices) {
      const t = timezones[idx];
      if (!t || seen.has(t.id)) continue;   // drop dupes (some countries repeat)
      seen.add(t.id);
      tzs.push(t);
    }
    return {
      code: c["1"] || "",
      name: c["2"] || "",
      currency: {code: cur["1"] || "", name: cur["2"] || ""},
      timezones: tzs,       // resolved {id, name} pairs, ready for the dropdown
    };
  });
  return {countries, timezones};
}

// ---------- state ----------

let SESSION = null;
let LOCALES = null;

// ---------- rendering ----------

function renderWho(gaia) {
  const who = gaia["1"] || {};
  const email = who["1"] || "?";
  const name  = who["2"] || "?";
  $("who").textContent = `${email}  (${name})`;
  $("biz-id").textContent = SESSION?.a || "—";
  $("mc-id").textContent = SESSION?.mc || "—";
}

// YouTube-linked flows require the underlying Google account to be a
// personal @gmail.com — Workspace / custom-domain accounts don't have
// the YouTube tie. Anything else is treated as a hard error so the user
// can switch account before wasting a create.
function isGmailAccount(email) {
  return typeof email === "string" && /@gmail\.com$/i.test(email.trim());
}

// Pick a reasonable default country from the browser's locale — falls back
// to Vietnam because that's what the tab's mcn-client-locale is set to.
function defaultCountryCode() {
  try {
    // navigator.language: "vi-VN" | "en-US" | "en" | ...
    const parts = (navigator.language || "").split("-");
    if (parts.length > 1) return parts[1].toUpperCase();   // "en-US" -> "US"
    // Bare language -> map a few common ones.
    const map = {vi: "VN", en: "US", ja: "JP", ko: "KR", zh: "CN"};
    return map[parts[0].toLowerCase()] || "VN";
  } catch (e) { return "VN"; }
}

function renderCountries() {
  const combo = $("country");
  combo.innerHTML = "";
  const sorted = [...LOCALES.countries].sort((a, b) => a.name.localeCompare(b.name));
  const defaultCode = defaultCountryCode();
  let defaultIndex = 0;
  sorted.forEach((c, i) => {
    const opt = document.createElement("option");
    opt.value = c.code;
    opt.textContent = `${c.name} (${c.code})`;
    combo.appendChild(opt);
    if (c.code === defaultCode) defaultIndex = i;
  });
  combo.selectedIndex = defaultIndex;
  onCountryChanged();
  $("create-section").classList.remove("hidden");
}

function onCountryChanged() {
  const code = $("country").value;
  const c = LOCALES.countries.find((x) => x.code === code);
  const tzCombo = $("timezone");
  tzCombo.innerHTML = "";
  if (!c) { $("currency").value = ""; return; }
  $("currency").value = `${c.currency.code} — ${c.currency.name}`;
  // c.timezones is already resolved {id, name}[] from parseLocaleResponse.
  // Fall back to the full catalog when a country's list is empty.
  const tzs = c.timezones.length ? c.timezones : LOCALES.timezones;
  for (const tz of tzs) {
    const opt = document.createElement("option");
    opt.value = String(tz.id);        // Google's timezone ID — what create RPC expects
    opt.textContent = tz.name || `Timezone id=${tz.id}`;
    tzCombo.appendChild(opt);
  }
  log(`Country ${c.code} → ${c.currency.code}, ${tzs.length} timezone(s).`);
}

// ---------- main flow ----------

async function detectAndFetch() {
  setStatus("detecting…");
  $("btn-reload").classList.add("spinning");
  try {
    SESSION = await detectSessionFromTab();
    log(`Session OK — a=${SESSION.a}  mc=${SESSION.mc}  ` +
        `f.sid=${SESSION.fsid.slice(0, 8)}…  xsrf=${SESSION.xsrf.slice(0, 12)}…`);
    setStatus("fetching…");
    const [gaia, rawLocales] = await Promise.all([
      gaiaGet(SESSION),
      getLocaleConstants(SESSION),
    ]);
    LOCALES = parseLocaleResponse(rawLocales);
    log(`Got ${LOCALES.countries.length} countries, ${LOCALES.timezones.length} timezones.`);
    renderWho(gaia);

    // Gate: YouTube-tied Ads accounts need a real @gmail.com. Workspace
    // / custom-domain accounts don't have the YT link and the create RPC
    // silently succeeds but the account can't be attached to YT later.
    const email = gaia?.["1"]?.["1"] || "";
    if (!isGmailAccount(email)) {
      log(`❌ ERROR: ${email || "(unknown)"} không phải @gmail.com. ` +
          `Tài khoản YouTube yêu cầu Gmail — hãy switch sang account Gmail ` +
          `rồi reload lại tab BM.`);
      setStatus("no gmail", "err");
      $("btn-create").disabled = true;
      $("create-section").classList.add("hidden");
      // Still refresh the linked list so user sees existing accounts.
      renderCountries();
      $("create-section").classList.add("hidden");
      await refreshLinkedAccounts();
      return;
    }

    renderCountries();
    await refreshLinkedAccounts();
    setStatus("ready", "ok");
  } catch (e) {
    log(`ERROR: ${e.message}`);
    setStatus("error", "err");
  } finally {
    $("btn-reload").classList.remove("spinning");
  }
}

// ---------- Auto-create ---------------------------------------------------

// Fire the RPC from the popup context. host_permissions on
// business.google.com + credentials:'include' means Chrome attaches
// the tab's session cookies + x-client-data automatically — same
// mechanism rpcCall already uses for GaiaGet / GetLocaleConstants /
// LinkedFirstPartyService.List, all of which succeed. Going through
// chrome.scripting.executeScript {world:"MAIN", async func} was
// silently returning null from popup context on some Chrome builds,
// which broke the whole create loop.
async function pageFetch(url, body, xsrf) {
  const res = await fetch(url, {
    method: "POST",
    credentials: "include",
    headers: {
      "accept": "*/*",
      "content-type": "application/x-www-form-urlencoded",
      "x-framework-xsrf-token": xsrf,
      "x-same-domain": "1",
    },
    body,
  });
  return {status: res.status, text: await res.text()};
}

// UUID + short numeric suffix matches the shape Google's UI generates for
// the idempotency key on CreateAndLinkAdsCustomer. Same request twice
// with the same key returns the already-created account instead of a dupe.
function makeIdempotencyKey() {
  const uuid = (crypto.randomUUID ? crypto.randomUUID()
    : "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
        const r = Math.random() * 16 | 0;
        return (c === "x" ? r : (r & 0x3 | 0x8)).toString(16);
      }));
  return `${uuid}${String(Date.now()).slice(-9)}`;
}

// Payload shape captured from Google's own AdsLinkingDialog create request:
//   {"1": "<currency>", "2": "<country>", "4": "<name>",
//    "5": 1, "6": {"1": "<uuid+ts>"}, "8": {"2": {}}}
// Field 5 is the Ads product enum (=1 for Google Ads). Timezone is NOT sent
// — the backend derives it from the country. Field 8.2 is a set of flags
// (empty object means "defaults").
function buildCreatePayload(name, country, currency) {
  return {
    "1": currency,
    "2": country,
    "4": name,
    "5": 1,
    "6": {"1": makeIdempotencyKey()},
    "8": {"2": {}},
  };
}

// Interpolate the name pattern for one iteration. Supported tokens:
//   {i}    -> 1-based counter
//   {rand} -> 6-char base36 random
//   {ts}   -> unix seconds
function renderNameTemplate(pattern, i, total) {
  const hasToken = /\{(i|rand|ts)\}/i.test(pattern);
  // If user gave a plain name and there's more than one to create, auto
  // append the counter so accounts don't all collide on the same name.
  const p = (!hasToken && total > 1) ? `${pattern} #{i}` : pattern;
  const rand = () => Math.random().toString(36).slice(2, 8);
  const ts   = () => Math.floor(Date.now() / 1000);
  return p
    .replace(/\{i\}/gi, String(i))
    .replace(/\{rand\}/gi, rand())
    .replace(/\{ts\}/gi, String(ts()));
}

// One-shot create. Returns {ok, customer_id, name, error}.
async function createOne(name, country, currency) {
  const service = "AdsCustomerService";
  const method  = "CreateAndLinkAdsCustomer";
  const ar = buildCreatePayload(name, country, currency);
  const url = `${BM_HOST}/manager/_/rpc/${service}/${method}`
    + `?authuser=${SESSION.authuser}`
    + `&rpcTrackingId=${service}.${method}%3A1`
    + `&f.sid=${SESSION.fsid}`;
  const params = new URLSearchParams();
  params.set("a", SESSION.a);
  params.set("f.sid", SESSION.fsid);
  params.set("__ar", JSON.stringify(ar));

  try {
    const {status, text} = await pageFetch(url, params.toString(), SESSION.xsrf);
    if (status !== 200) return {ok: false, error: `HTTP ${status}: ${text.slice(0, 200)}`};
    const parsed = JSON.parse(stripXssi(text));
    const info = parsed?.["2"];
    if (info?.["1"]) return {ok: true, customer_id: info["1"], name: info["3"] || name};
    return {ok: false, error: `Unexpected response: ${JSON.stringify(parsed).slice(0, 200)}`};
  } catch (e) {
    return {ok: false, error: e.message};
  }
}

// Cooperatively-cancellable batch flag — Stop button flips this to true.
let STOP_REQUESTED = false;

async function createAccount() {
  if (!SESSION) { log("No session — click Detect first."); return; }
  // Gate: don't let a Create fire against a non-Gmail account.
  const currentEmail = ($("who").textContent || "").split(" ")[0];
  if (currentEmail && !isGmailAccount(currentEmail)) {
    log(`❌ Blocked: ${currentEmail} không phải @gmail.com. ` +
        `YouTube-tied Ads accounts cần Gmail.`);
    setStatus("no gmail", "err");
    return;
  }
  const pattern = $("new-name").value.trim();
  if (!pattern) { log("Enter an account name pattern."); return; }
  const country = $("country").value;
  const c = LOCALES.countries.find((x) => x.code === country);
  const currency = c?.currency.code || "";
  if (!country || !currency) { log("Country / currency missing."); return; }

  const qty = Math.max(1, Math.min(100, Number($("qty").value) || 1));
  const delayMs = Math.max(0, Math.min(10000, Number($("delay-ms").value) || 0));

  const btn = $("btn-create");
  const stopBtn = $("btn-stop");
  btn.disabled = true;
  stopBtn.disabled = false;
  STOP_REQUESTED = false;
  setStatus(`creating 0/${qty}…`);

  log(`--- Batch create: ${qty} account(s), pattern="${pattern}", ` +
      `country=${country} ${currency}, delay=${delayMs}ms ---`);

  let ok = 0, fail = 0;
  for (let i = 1; i <= qty; i++) {
    if (STOP_REQUESTED) { log(`⏹ Stopped by user at ${i - 1}/${qty}.`); break; }
    const name = renderNameTemplate(pattern, i, qty);
    setStatus(`creating ${i}/${qty}…`);
    log(`[${i}/${qty}] → "${name}"`);
    const res = await createOne(name, country, currency);
    if (res.ok) {
      ok++;
      log(`[${i}/${qty}] ✅ ${res.name}  customer_id=${res.customer_id}`);
    } else {
      fail++;
      log(`[${i}/${qty}] ❌ ${res.error}`);
    }
    if (i < qty && delayMs > 0) {
      await new Promise((r) => setTimeout(r, delayMs));
    }
  }
  log(`--- Done: ${ok} ok, ${fail} fail ---`);
  setStatus(fail === 0 ? `${ok} created` : `${ok} ok / ${fail} fail`,
            fail === 0 ? "ok" : "err");
  btn.disabled = false;
  stopBtn.disabled = true;
  // One refresh at the end (avoid List spam during the loop).
  await refreshLinkedAccounts();
}

function stopBatch() {
  STOP_REQUESTED = true;
  log("Stop requested — will halt after current create.");
}

// ---------- Linked-accounts list ----------

// LinkedFirstPartyService.List returns entries of type 1 (Ads) and 2 (MC).
// For Ads: entry["2"] = {"1": customer_id, "5": role}, entry["3"] = name,
// entry["4"] = URL to the Ads UI. We filter to type 1 only.
function parseLinkedAccounts(raw) {
  const rows = (raw?.["1"] || [])
    .filter((e) => e?.["1"] === 1)                       // type 1 = Ads account
    .map((e) => ({
      customer_id: e?.["2"]?.["1"] || "",
      name: e?.["3"] || "(no name)",
      url: e?.["4"] || "",
    }));
  return rows;
}

function renderLinkedAccounts(rows) {
  const list = $("account-list");
  list.innerHTML = "";
  for (const r of rows) {
    const el = document.createElement("div");
    el.className = "item";
    const left = document.createElement("span");
    left.textContent = r.name;
    const right = document.createElement("span");
    right.className = "id";
    right.textContent = r.customer_id;
    el.appendChild(left);
    el.appendChild(right);
    if (r.url) {
      el.style.cursor = "pointer";
      el.addEventListener("click", () => chrome.tabs.create({url: r.url}));
    }
    list.appendChild(el);
  }
  $("acct-count").textContent = String(rows.length);
  $("account-list-section").classList.remove("hidden");
}

async function refreshLinkedAccounts() {
  if (!SESSION) return;
  try {
    const raw = await listLinkedApps(SESSION);
    const rows = parseLinkedAccounts(raw);
    renderLinkedAccounts(rows);
    log(`Linked Ads accounts: ${rows.length}`);
  } catch (e) {
    log(`List failed: ${e.message}`);
  }
}

// ---------- boot ----------

document.addEventListener("DOMContentLoaded", () => {
  $("btn-reload").addEventListener("click", detectAndFetch);
  $("btn-create").addEventListener("click", createAccount);
  $("btn-stop").addEventListener("click", stopBatch);
  $("btn-stop").disabled = true;   // enabled only while a batch is running
  $("country").addEventListener("change", onCountryChanged);
  detectAndFetch();
});
