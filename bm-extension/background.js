// BM Ads Account Setup — background service worker.
//
// The BM Next (NEAT) frontend doesn't expose WIZ globals, so reading
// f.sid + xsrf from the page fails. Instead we listen for the tab's own
// outgoing RPCs to /manager/_/rpc/*, pull the values off the wire, and
// stash them in chrome.storage. The popup reads from storage — as long
// as the user has interacted with the linkedapps tab at least once (or
// just landed on it, since the page fires GaiaAccountsService.Get on
// load), we always have fresh tokens.

const RPC_URL_PATTERN = "https://business.google.com/manager/_/rpc/*";

// Ignore read-only chatter — only mutations are useful for auto-learning
// the create endpoint. Anything with these tokens in the method name is
// almost certainly not the RPC we want to learn.
const READONLY_METHOD_RX = /^(Get|List|Fetch|Query|Check|Load|Search|Poll|Batch|GetAdsLocaleConstants)/i;

chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    try { captureFromRequest(details); }
    catch (e) { console.error("BM header capture failed:", e); }
  },
  {urls: [RPC_URL_PATTERN]},
  ["requestHeaders", "extraHeaders"]
);

// Second listener captures the request BODY of every RPC POST so we can
// replay the exact payload shape the browser's own UI uses when the user
// eventually completes a create-account manually. Once we've seen one
// successful mutation, the popup uses that endpoint + template for all
// subsequent creates without asking anyone to paste a cURL.
chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    try { captureRequestBody(details); }
    catch (e) { console.error("BM body capture failed:", e); }
  },
  {urls: [RPC_URL_PATTERN]},
  ["requestBody"]
);

function captureRequestBody(details) {
  if (details.method !== "POST") return;
  const url = new URL(details.url);
  const m = url.pathname.match(/\/manager\/_\/rpc\/([A-Za-z0-9_]+)\/([A-Za-z0-9_]+)/);
  if (!m) return;
  const service = m[1];
  const method  = m[2];
  if (READONLY_METHOD_RX.test(method)) return;

  // requestBody comes back either as decoded formData or raw bytes.
  let body = "";
  if (details.requestBody) {
    if (details.requestBody.formData) {
      const parts = [];
      for (const [k, arr] of Object.entries(details.requestBody.formData)) {
        for (const v of arr) parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(v)}`);
      }
      body = parts.join("&");
    } else if (details.requestBody.raw) {
      try {
        const dec = new TextDecoder("utf-8");
        body = details.requestBody.raw
          .map((r) => dec.decode(new Uint8Array(r.bytes)))
          .join("");
      } catch (e) { /* leave body empty */ }
    }
  }

  // Extract just the __ar payload so the popup can reuse the shape.
  let ar = null;
  const arMatch = body.match(/(?:^|&)__ar=([^&]*)/);
  if (arMatch) {
    try { ar = JSON.parse(decodeURIComponent(arMatch[1].replace(/\+/g, " "))); }
    catch (e) { /* ar stays null; popup falls back to a best-guess shape */ }
  }

  chrome.storage.local.get(["learnedRpcs"]).then(({learnedRpcs = []}) => {
    const rec = {
      service, method,
      ar,
      at: Date.now(),
    };
    // Keep newest per endpoint; cap total to 30.
    const filtered = learnedRpcs.filter(
      (r) => !(r.service === service && r.method === method)
    );
    filtered.unshift(rec);
    chrome.storage.local.set({learnedRpcs: filtered.slice(0, 30)});
  });
}

function captureFromRequest(details) {
  const url = new URL(details.url);

  // f.sid lives in the query string; it's a long numeric session id.
  const fsid = url.searchParams.get("f.sid") || "";
  const authuser = url.searchParams.get("authuser") || "0";

  // XSRF token is a per-session value in a request header.
  let xsrf = "";
  for (const h of details.requestHeaders || []) {
    if (h.name.toLowerCase() === "x-framework-xsrf-token") {
      xsrf = h.value || "";
      break;
    }
  }

  if (!fsid && !xsrf) return;   // nothing useful, skip

  // Business id (a=om-...) and Merchant Center id (mc=...) come from the
  // tab's URL, not the RPC itself. Look them up from the initiating tab.
  chrome.tabs.get(details.tabId).then((tab) => {
    let a = "", mc = "";
    try {
      const t = new URL(tab.url || "");
      a  = t.searchParams.get("a")  || "";
      mc = t.searchParams.get("mc") || "";
    } catch (e) { /* no-op */ }

    // Merge with whatever we already have so we don't overwrite good
    // values with empty ones from a partial request.
    chrome.storage.local.get(["session"]).then(({session = {}}) => {
      const next = {
        xsrf:     xsrf     || session.xsrf     || "",
        fsid:     fsid     || session.fsid     || "",
        a:        a        || session.a        || "",
        mc:       mc       || session.mc       || "",
        authuser: authuser || session.authuser || "0",
        capturedAt: Date.now(),
        source: "webRequest",
      };
      chrome.storage.local.set({session: next});
    });
  }).catch(() => {
    // Tab may have closed by the time we ran; just persist what we have.
    chrome.storage.local.set({
      session: {xsrf, fsid, authuser, capturedAt: Date.now(), source: "webRequest"},
    });
  });
}
