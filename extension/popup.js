/** The popup: two fields, two buttons, and the only place a permission is
 *  asked for.
 *
 *  Chrome grants an optional host permission only from inside a user gesture,
 *  and the panel's address is not known until the user has typed it -- so the
 *  request happens in the click handler below and nowhere else. Nothing is
 *  requested when the popup merely opens.
 *
 *  It never receives a cookie value: `background.js` builds its reply out of
 *  names and counts. There is nothing here to redact.
 */

import { describeReport } from "./lib/cookies.js";
import { DEFAULT_PANEL_ORIGIN, normalisePanelOrigin } from "./lib/panel.js";

const panelInput = document.getElementById("panel-origin");
const tokenInput = document.getElementById("api-token");
const status = document.getElementById("status");

const say = (text) => {
  status.textContent = text;
};

const stored = await chrome.storage.local.get(["panelOrigin", "apiToken", "lastResult"]);
// Prefilled, not just a placeholder: a placeholder still has to be typed, and
// this is where the panel is for both supported ways of running it. Anyone
// running it elsewhere overwrites one field.
panelInput.value = stored.panelOrigin ?? DEFAULT_PANEL_ORIGIN;
tokenInput.value = stored.apiToken ?? "";

/** Whether the panel host has been granted, for the origin currently typed. */
const granted = async (origin) =>
  Boolean(origin) && chrome.permissions.contains({ origins: [`${origin}/*`] });

/** Whatever happened last time, even if this window was not alive to hear it. */
function showLast() {
  const last = stored.lastResult;
  if (!last) return false;
  say(last.ok ? describeReport(last.report) : `上次导入失败：${last.error}`);
  return true;
}

// The address that is actually in the field, default included -- checking
// `stored.panelOrigin` here would nag for an address that is already filled in.
const current = normalisePanelOrigin(panelInput.value);

if (!stored.apiToken) {
  say("填上面板的 API token（SFD_API_TOKEN），点「保存」。地址已经按默认填好了。");
} else if (!(await granted(current))) {
  // Said up front rather than mid-click: by the time the dialog is up this
  // window may already be gone, so a warning printed just before the request
  // would never be read.
  say(
    "还需要一次授权：点「导入」后 Chrome 会问你是否允许访问这个面板地址。" +
      "允许之后这个小窗口很可能会被关掉——那不是出错。重新点一下扩展图标，再点一次「导入」即可。",
  );
} else {
  showLast();
}

async function save() {
  const origin = normalisePanelOrigin(panelInput.value);
  if (!origin) {
    say("面板地址看不懂。写成 192.168.1.10:8000 这样就行，不写协议按明文 http 处理。");
    return "";
  }
  await chrome.storage.local.set({ panelOrigin: origin, apiToken: tokenInput.value.trim() });
  panelInput.value = origin;
  return origin;
}

document.getElementById("save").addEventListener("click", async () => {
  if (await save()) say("已保存。");
});

document.getElementById("run").addEventListener("click", async () => {
  const origin = normalisePanelOrigin(panelInput.value);
  if (!origin) {
    say("先填面板地址，再点导入。");
    return;
  }
  // First await in this handler, deliberately: anything awaited before it
  // spends the user gesture and Chrome then refuses the request outright.
  // If the permission is already held this resolves without a dialog and the
  // window survives; if it is not, Chrome shows the dialog and very likely
  // closes this window, so everything below may simply never run. That is why
  // the outcome is persisted by the worker rather than only replied to.
  const allowed = await chrome.permissions.request({ origins: [`${origin}/*`] });
  if (!allowed) {
    say(`没拿到访问 ${origin} 的权限，没法导入。再点一次「导入」并在弹框里允许。`);
    return;
  }
  await save();

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  say("正在导入…");
  const result = await chrome.runtime.sendMessage({
    type: "import",
    tabId: tab?.id,
    // Forwarded as the import's `origin` field, exactly like the bookmarklet
    // sends `location.origin`. A tab on some other scheme has none to give.
    pageOrigin: tab?.url?.startsWith("http") ? new URL(tab.url).origin : undefined,
  });
  // Not "已恢复". The import only means the panel accepted the cookies;
  // describeReport() names the next step instead of claiming a result.
  say(result.ok ? describeReport(result.report) : `导入失败：${result.error}`);
});
