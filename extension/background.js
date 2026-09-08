/** The service worker: reads the cookie store, posts to the panel.
 *
 *  The network call lives HERE and not in a content script for one reason: a
 *  content script executes in the page's origin and stays bound by that page's
 *  CORS, whatever the manifest says. A service worker with host permissions is
 *  not (research/mv3-capabilities.md §2), which is why the panel needs no CORS
 *  change for this path -- `ALLOWED_IMPORT_ORIGINS` in `app/api/session.py`
 *  remains the bookmarklet's alone.
 *
 *  It runs in no page, so nothing goofish serves can read what it sends. That
 *  is why this path carries the panel's own bearer token while the bookmarklet
 *  has to carry a one-time ticket instead.
 */

import { flattenCookies } from "./lib/cookies.js";
import { readEnvSnapshot } from "./lib/env.js";
import { IMPORT_PATH, isLocalAddress, normalisePanelOrigin } from "./lib/panel.js";

// The two domains in `host_permissions`, and the whole reason there are two:
// `_m_h5_tk` is only ever set on taobao. `chrome.cookies.getAll` matches
// subdomains, so these two filters cover `.goofish.com`,
// `passport.goofish.com` and `.taobao.com` alike.
const COOKIE_DOMAINS = ["goofish.com", "taobao.com"];

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== "import") return false;
  runImport(message).then(sendResponse, (error) =>
    sendResponse({ ok: false, error: String(error?.message || error) }),
  );
  // Synchronous `true` is what keeps the reply channel open for the await
  // above; without it the popup gets an immediate undefined.
  return true;
});

/**
 * @param {{tabId?: number, pageOrigin?: string}} message
 * @returns {Promise<{ok: true, report: object} | {ok: false, error: string}>}
 */
async function runImport(message) {
  const stored = await chrome.storage.local.get(["panelOrigin", "apiToken"]);
  const origin = normalisePanelOrigin(stored.panelOrigin);
  // Not silent: an unconfigured extension that just does nothing on click is
  // indistinguishable from a broken one.
  if (!origin) {
    return { ok: false, error: "还没设置面板地址。在上面填好地址和 API token，点「保存」。" };
  }
  if (!stored.apiToken) {
    return { ok: false, error: "还没设置 API token（面板的 SFD_API_TOKEN），填好后点「保存」。" };
  }

  const collected = [];
  for (const domain of COOKIE_DOMAINS) {
    collected.push(...(await chrome.cookies.getAll({ domain })));
  }
  const flat = flattenCookies(collected);
  if (flat.names.length === 0) {
    return {
      ok: false,
      error: "一个 cookie 都没取到。请先在这个浏览器里登录闲鱼（过完滑块），再点导入。",
    };
  }

  const env = await readTabEnv(message.tabId);
  const body = JSON.stringify({
    cookie_header: flat.header,
    // Omitted rather than defaulted when the active tab has none to give:
    // `CookieImport.origin` already defaults to the goofish site, and a second
    // copy of that literal here is one more thing to keep in step -- and one
    // more absolute URL in an extension that is meant to have none.
    origin: message.pageOrigin || undefined,
    // Absent rather than null when the snapshot could not be read: the panel
    // treats an absent `env` as "keep the built-in defaults".
    env,
  });

  const url = `${origin}${IMPORT_PATH}`;
  /** @type {RequestInit & {targetAddressSpace?: string}} */
  const init = {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${stored.apiToken}`,
    },
    body,
  };
  // Chrome is rolling out Local Network Access and the docs do not say whether
  // extensions are subject to it. Stating the expectation costs nothing --
  // builds that do not know the option ignore it -- and it is only true for a
  // private address, so it is set only for one.
  if (isLocalAddress(new URL(url).hostname)) init.targetAddressSpace = "local";

  let response;
  try {
    response = await fetch(url, init);
  } catch (error) {
    return {
      ok: false,
      error:
        `连不上 ${origin}（${error}）。面板没在跑、地址写错、或者 Chrome 的本地网络访问` +
        `限制拦下了这次请求，都会这样。改用面板设置页的开发者工具流程——那条路不受这些限制。`,
    };
  }

  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    return {
      ok: false,
      error: `面板拒绝了这次导入（HTTP ${response.status}）：${payload?.detail ?? "无详情"}`,
    };
  }

  // Built field by field, values never included. The popup cannot leak a
  // cookie value it was never handed.
  return {
    ok: true,
    report: {
      names: Array.isArray(payload?.cookie_names) ? payload.cookie_names : flat.names,
      duplicates: flat.duplicates,
      conflicts: flat.conflicts,
      envCaptured: env !== undefined,
    },
  };
}

/**
 * Read the page's environment, or nothing.
 *
 * Injected into the tab rather than read off the popup's own window because
 * the point of the snapshot is to describe the browser the cookies came from
 * as the page itself reports it. A tab we have no host permission for throws,
 * and that costs the snapshot only.
 *
 * @param {number | undefined} tabId
 */
async function readTabEnv(tabId) {
  if (typeof tabId !== "number") return undefined;
  try {
    const [injected] = await chrome.scripting.executeScript({
      target: { tabId },
      func: readEnvSnapshot,
    });
    const env = injected?.result;
    return env && Object.keys(env).length > 0 ? env : undefined;
  } catch {
    return undefined;
  }
}
