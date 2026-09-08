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
import { normalisePanelOrigin } from "./lib/panel.js";

const panelInput = document.getElementById("panel-origin");
const tokenInput = document.getElementById("api-token");
const status = document.getElementById("status");

const say = (text) => {
  status.textContent = text;
};

const stored = await chrome.storage.local.get(["panelOrigin", "apiToken"]);
panelInput.value = stored.panelOrigin ?? "";
tokenInput.value = stored.apiToken ?? "";
if (!stored.panelOrigin) say("先填面板地址和 API token，点「保存」。");

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
  const granted = await chrome.permissions.request({ origins: [`${origin}/*`] });
  if (!granted) {
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
