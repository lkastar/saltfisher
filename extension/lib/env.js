/** The environment snapshot, read off the page the cookies belong to.
 *
 *  Same field set the bookmarklet sends and the same rule behind it: only
 *  read-only properties already sitting on `navigator`, `screen` and `Intl`.
 *  No probing, no canvas, no extra request. `sec-ch-ua` is NOT rebuilt here --
 *  the panel derives it from `ua_data.brands` in `app/collector/fingerprint.py`
 *  and a second derivation is a second thing that can drift.
 */

/**
 * Injected verbatim by `chrome.scripting.executeScript`, which serialises this
 * function and evaluates it in the tab. It therefore may not reference
 * anything outside its own body -- no imports, no module-level constants.
 *
 * @returns {Record<string, unknown>} omitting whatever the browser does not
 *   offer, never sending null: absent has to keep meaning "the browser did not
 *   say" rather than "the browser said it has none".
 */
export function readEnvSnapshot() {
  /** @type {Record<string, unknown>} */
  const snapshot = {};
  const put = (key, value) => {
    if (value !== undefined && value !== null) snapshot[key] = value;
  };
  try {
    const nav = navigator;
    const resolved = Intl.DateTimeFormat().resolvedOptions();
    put("user_agent", nav.userAgent);
    put("platform", nav.platform);
    put("language", nav.language);
    put("languages", nav.languages && Array.prototype.slice.call(nav.languages));
    put("hardware_concurrency", nav.hardwareConcurrency);
    put("device_memory", nav.deviceMemory);
    put("max_touch_points", nav.maxTouchPoints);
    put("ua_data", nav.userAgentData && nav.userAgentData.toJSON());
    put("screen_width", screen.width);
    put("screen_height", screen.height);
    put("device_pixel_ratio", window.devicePixelRatio);
    put("color_depth", screen.colorDepth);
    put("time_zone", resolved.timeZone);
    put("locale", resolved.locale);
  } catch {
    // Losing the snapshot must never cost the cookies -- the credentials are
    // why the user clicked. Same trade the bookmarklet makes.
    return {};
  }
  return snapshot;
}
