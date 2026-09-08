/** Cookie flattening and the one sentence the popup is allowed to say.
 *
 *  Two host permissions mean the same cookie NAME arrives twice: `cookie2`,
 *  `unb`, `_tb_token_`, `sgcookie` and `x5sec` all exist on `.goofish.com` and
 *  on `.taobao.com` (research/cookie-domains.md). The panel's import endpoint
 *  takes a flat `Cookie:` header, which has no room for a domain, so one of
 *  the two has to be dropped -- and dropping one silently is how a session
 *  gets imported under the wrong account's token.
 *
 *  Nothing here is chrome-specific, which is the point: it is the only real
 *  logic in the extension and it runs under `npx vitest run`.
 */

/** The signing token. Only ever set on `.taobao.com`, which is the entire
 *  reason this extension exists rather than the bookmarklet. */
export const TOKEN_COOKIE = "_m_h5_tk";

const PREFERRED_DOMAIN = "goofish.com";

/** Whether a cookie came from the domain that wins a name collision.
 *
 *  Cookie domains arrive as `.goofish.com` (host-prefixed) or
 *  `passport.goofish.com` (host-only), so both shapes have to match.
 */
function preferred(cookie) {
  const domain = String(cookie.domain || "").replace(/^\./, "");
  return domain === PREFERRED_DOMAIN || domain.endsWith(`.${PREFERRED_DOMAIN}`);
}

/**
 * Flatten `chrome.cookies` records into the `cookie_header` the panel already
 * accepts, deduping by name.
 *
 * `duplicates` and `conflicts` are separate on purpose. A name present on both
 * domains with the SAME value loses nothing when flattened; a name present
 * with two DIFFERENT values means a real choice was made and the other value
 * is gone. The popup reports both, because "0 conflicts" and "the dedupe never
 * ran" look identical otherwise.
 *
 * @param {Array<{name?: string, value?: string, domain?: string}>} cookies
 * @returns {{header: string, names: string[], duplicates: number, conflicts: number}}
 */
export function flattenCookies(cookies) {
  /** @type {Map<string, {name: string, value: string, domain?: string}>} */
  const chosen = new Map();
  let duplicates = 0;
  let conflicts = 0;

  for (const cookie of cookies) {
    const name = cookie?.name;
    const value = cookie?.value;
    // An empty value is not a credential, and sending `name=` would overwrite
    // a good cookie already in the panel's jar with nothing.
    if (!name || !value) continue;

    const kept = chosen.get(name);
    if (kept === undefined) {
      chosen.set(name, { name, value, domain: cookie.domain });
      continue;
    }
    duplicates += 1;
    if (kept.value !== value) conflicts += 1;
    // First one wins unless the newcomer is the goofish copy. Two goofish
    // copies (`.goofish.com` and `passport.goofish.com`) therefore keep the
    // first, rather than letting enumeration order decide silently.
    if (!preferred(kept) && preferred(cookie)) {
      chosen.set(name, { name, value, domain: cookie.domain });
    }
  }

  return {
    header: [...chosen.values()].map((c) => `${c.name}=${c.value}`).join("; "),
    names: [...chosen.keys()].sort(),
    duplicates,
    conflicts,
  };
}

/**
 * The popup's whole vocabulary.
 *
 * Takes the report, which by construction carries names and counts and no
 * values -- `background.js` builds it field by field rather than by stripping
 * `header` off the flatten result, so there is no cookie value in this
 * function's reach to leak by accident.
 *
 * @param {{names: string[], duplicates: number, conflicts: number,
 *          envCaptured: boolean, envError?: string}} report
 */
export function describeReport(report) {
  const lines = [`面板收下了 ${report.names.length} 个 cookie。`];

  // The reason to install this at all. Whether it landed has to be readable
  // at a glance, not inferred from a name list.
  lines.push(
    report.names.includes(TOKEN_COOKIE)
      ? `包含 ${TOKEN_COOKIE}，mtop 直接可用，省掉书签脚本之后那个浏览器兜底周期。`
      : `没有取到 ${TOKEN_COOKIE}。它只存在于 taobao 域——确认你在这个浏览器里登录过闲鱼，` +
          `并且扩展的 *.taobao.com 权限还在。`,
  );

  if (report.duplicates > 0) {
    lines.push(
      `${report.duplicates} 个 cookie 同名地出现在两个域上，已按名字去重、保留 goofish 那份` +
        (report.conflicts > 0
          ? `；其中 ${report.conflicts} 个两边的值不一样，taobao 那份被丢掉了。`
          : `（两边的值都一样，没有丢信息）。`),
    );
  }

  if (!report.envCaptured) {
    // The reason, not just the fact. This one is silent by nature: the cookies
    // arrive from the cookie store whatever the active tab is, so the import
    // reports success while the panel keeps its built-in identity and nothing
    // on screen says the snapshot was dropped.
    lines.push(
      `没读到环境快照${report.envError ? `（${report.envError}）` : ""}，` +
        `面板继续用它内置的那份默认身份。要带上环境信息，请切到已登录的闲鱼标签页再点「导入」。`,
    );
  }

  // Same rule as the bookmarklet and docs/operations.md: the import is not the
  // result. 判定成功看行为，不看 cookie 名单.
  lines.push("这还不算恢复：回面板「监控任务」点一次「立即运行」，跑通了才算数。");
  return lines.join("\n");
}
