/** The `javascript:` one-liner the user drags to their bookmarks bar.
 *
 *  It runs inside a page goofish serves, which is the whole reason it carries
 *  a one-time import ticket instead of `SFD_API_TOKEN`: anything on that page
 *  can read what this script sends.
 *
 *  Built here rather than inline in the page because it is a string that has
 *  to stay exactly right -- a stray quote makes a bookmark that does nothing
 *  when clicked, with no error anywhere -- and a `lib` function is the only
 *  thing in this project that can carry a test.
 */

/** Percent-encode the three characters a URL parser eats before the script
 *  ever runs.
 *
 *  A bookmark is parsed as a URL first and evaluated second, and the browser
 *  percent-decodes it in between -- which is why `%20` in a bookmarklet works.
 *  So `#` (opens the fragment) and `?` (opens the query) have to go in encoded
 *  or the source is silently truncated at the first one, and `%` has to go in
 *  encoded or it eats the two characters after it. Everything else survives
 *  verbatim, which keeps the copyable textarea readable.
 *
 *  `%` first: encoding it after the others would re-encode their escapes.
 */
function urlSafe(script: string): string {
  return script.replaceAll("%", "%25").replaceAll("#", "%23").replaceAll("?", "%3F");
}

/** Assemble the bookmarklet for one panel origin and one ticket.
 *
 *  `JSON.stringify` supplies both string literals, so nothing here depends on
 *  a ticket's alphabet staying URL-safe.
 *
 *  ponytail: hand-minified rather than run through a bundler. It is one
 *  function; a build step for it would be more machinery than script.
 */
export function buildBookmarklet(panelOrigin: string, ticket: string): string {
  const url = JSON.stringify(`${panelOrigin}/api/session/cookies`);
  const tok = JSON.stringify(ticket);
  const style =
    "position:fixed;left:16px;bottom:16px;z-index:2147483647;max-width:340px;" +
    "padding:10px 14px;border-radius:8px;font:13px/1.6 system-ui,sans-serif;" +
    "color:rgb(255,255,255);background:rgb(31,41,55);" +
    "box-shadow:0 4px 16px rgba(0,0,0,.45);white-space:pre-wrap";

  // No alert/confirm: both block the page, and a blocked goofish tab is how a
  // user ends up force-quitting the browser mid-import. The outcome is a DOM
  // node on the page they are already looking at.
  return "javascript:" + urlSafe(
    "(function(){" +
    `var U=${url},T=${tok};` +
    "var d=document.createElement('div');" +
    `d.setAttribute('style',${JSON.stringify(style)});` +
    "d.textContent='正在导入凭证…';" +
    "document.body.appendChild(d);" +
    // The environment snapshot. Read-only properties that are already on the
    // page -- no probe, no request, no canvas. `P` skips anything the browser
    // does not offer, so `userAgentData` (Chromium-only) and `deviceMemory`
    // arrive absent rather than as a null the backend would have to guess at.
    //
    // Wrapped in try/catch because losing the fingerprint must never cost the
    // cookie import: the credentials are the reason the user clicked.
    "var z={};try{" +
    "var n=navigator,s=screen,r=Intl.DateTimeFormat().resolvedOptions()," +
    "P=function(k,v){if(v!==undefined&&v!==null)z[k]=v};" +
    "P('user_agent',n.userAgent);P('platform',n.platform);P('language',n.language);" +
    "P('languages',n.languages&&[].slice.call(n.languages));" +
    "P('hardware_concurrency',n.hardwareConcurrency);P('device_memory',n.deviceMemory);" +
    "P('max_touch_points',n.maxTouchPoints);" +
    "P('ua_data',n.userAgentData&&n.userAgentData.toJSON());" +
    "P('screen_width',s.width);P('screen_height',s.height);" +
    "P('device_pixel_ratio',window.devicePixelRatio);P('color_depth',s.colorDepth);" +
    "P('time_zone',r.timeZone);P('locale',r.locale);" +
    "}catch(e){z={}}" +
    "var done=function(m){d.textContent=m;setTimeout(function(){d.remove()},20000)};" +
    "fetch(U,{method:'POST',headers:{'content-type':'application/json'," +
    "'x-sfd-import-ticket':T},body:JSON.stringify({cookie_header:document.cookie," +
    "origin:location.origin,env:z})})" +
    ".then(function(r){return r.json().then(function(b){return{ok:r.ok,s:r.status,b:b}})})" +
    // Deliberately not "已恢复". The import only means the cookies were
    // accepted; only a real collection proves anything, so the message the
    // user reads on the goofish page names the next step instead of claiming
    // a result (docs/operations.md: 判定成功看行为，不看 cookie 名单).
    ".then(function(x){done(x.ok?'已导入 '+x.b.cookie_names.length+" +
    "' 个 cookie。这还不算恢复：回面板「监控任务」点一次「立即运行」，成功了才算数。'" +
    ":'导入失败：'+(x.b.detail||x.s))})" +
    // A rejected fetch is most likely the page's own CSP refusing a
    // cross-origin request, which no retry fixes -- point at the flow that
    // always works instead of leaving a bare "TypeError" on screen.
    ".catch(function(e){done('导入失败：'+e+String.fromCharCode(10)+" +
    "'可能被闲鱼页面的 CSP 拦下了，改用设置页的开发者工具流程。')});" +
    "})()"
  );
}
