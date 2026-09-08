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

  // Checked HERE rather than trusted from the popup, because this is the
  // process that needs it. A runtime-granted optional permission can be gone
  // by the next click -- reloading an unpacked extension drops it -- and the
  // symptom is brutally misleading: without the permission Chrome stops
  // treating this as a privileged extension request and sends an ordinary
  // cross-origin one, so the panel gets a preflight it answers 405 (no
  // OPTIONS route, and the extension origin is not in ALLOWED_IMPORT_ORIGINS)
  // and fetch reports a bare "Failed to fetch". Measured 2026-09-08.
  if (!(await chrome.permissions.contains({ origins: [`${origin}/*`] }))) {
    return {
      ok: false,
      error:
        `没有访问 ${origin} 的权限。再点一次「导入」，在 Chrome 的弹框里选「允许」。` +
        `（重新加载扩展会把这个权限清掉，所以它可能昨天还是好的。）`,
    };
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

  const { env, envError } = await readTabEnv(message.tabId);
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
    // One extra request to tell two very different problems apart. `/api/health`
    // needs no auth and no custom header, so it is a CORS-simple request: if it
    // answers, the panel is reachable and the import request specifically is
    // what got stopped; if it does not, nothing is getting through.
    const reachable = await fetch(`${origin}/api/health`)
      .then((r) => r.ok)
      .catch(() => false);
    if (reachable) {
      return {
        ok: false,
        error:
          `面板在 ${origin} 上活着（/api/health 通了），但导入请求被拦下了（${error}）。` +
          `多半是扩展的 host 权限没生效——在 chrome://extensions 里重新加载本扩展，` +
          `再点一次「导入」并允许。还不行就走面板设置页的开发者工具流程。`,
      };
    }
    return {
      ok: false,
      error:
        `连不上 ${origin}（${error}）。` +
        // Named first because it is the one cause that looks like "the panel
        // is down" while the panel is up: `localhost` resolves to ::1 before
        // 127.0.0.1 on macOS, and uvicorn's default bind is IPv4 only, so the
        // fetch hits a closed IPv6 port. curl hides it by falling back; fetch
        // does not. Measured 2026-09-08.
        (/^https?:\/\/localhost(:|$)/.test(origin)
          ? "先把地址换成 127.0.0.1 再试一次——localhost 会先解析到 IPv6 的 ::1，" +
            "而面板默认只监听 IPv4，这种情况下面板明明在跑也连不上。"
          : "面板没在跑、地址写错、或者 Chrome 的本地网络访问限制拦下了这次请求，都会这样。") +
        `改用面板设置页的开发者工具流程——那条路不受这些限制。`,
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
      envError,
    },
  };
}

/**
 * Read the page's environment, or say why not.
 *
 * Injected into the tab rather than read off the popup's own window because
 * the point of the snapshot is to describe the browser the cookies came from
 * as the page itself reports it.
 *
 * Losing it must never cost the cookies -- those are why the user clicked --
 * but it must not be silent either. Injection needs host permission for THE
 * ACTIVE TAB, and clicking this from the panel's own tab is the easy way to
 * have none: the panel is a different host from goofish. The cookies still
 * arrive from the cookie store, so the import reports success while the
 * fingerprint quietly stays at its built-in defaults. That happened on the
 * first real run, 2026-09-08, and looked like nothing at all.
 *
 * @param {number | undefined} tabId
 * @returns {Promise<{env?: Record<string, unknown>, envError?: string}>}
 */
async function readTabEnv(tabId) {
  if (typeof tabId !== "number") {
    return { envError: "没有活动标签页可读" };
  }
  try {
    const [injected] = await chrome.scripting.executeScript({
      target: { tabId },
      func: readEnvSnapshot,
    });
    const env = injected?.result;
    if (env && Object.keys(env).length > 0) return { env };
    return { envError: "这个页面没给出任何环境信息" };
  } catch {
    return { envError: "读不了当前标签页——请在闲鱼页面上点「导入」" };
  }
}
