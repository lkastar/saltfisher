/** The Chrome extension's logic, tested from the project's one test command.
 *
 *  The extension lives in `extension/` and loads unpacked with no build step,
 *  so its source is plain ES modules. It is exercised here rather than under a
 *  second toolchain: `npx vitest run` stays the only frontend test command.
 *
 *  `background.js` is loaded for real, with a stub `chrome` planted on
 *  `globalThis` first. Reimplementing its flow in the test would keep passing
 *  after the real one started posting somewhere else or handing the popup a
 *  cookie value.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import { describeReport, flattenCookies } from "../../../extension/lib/cookies.js";
import { addressSpace, normalisePanelOrigin } from "../../../extension/lib/panel.js";
import { readEnvSnapshot } from "../../../extension/lib/env.js";

/** Every file that ships in the unpacked extension, as text. */
const SOURCES = import.meta.glob("../../../extension/**/*.{js,json,html}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/** Path as it reads in the repo, e.g. `extension/background.js`. */
const shortPath = (key: string) => key.replace(/^.*\/extension\//, "extension/");

/** Source with its prose removed.
 *
 *  The destination audit below is about what the code can reach, and a comment
 *  that spells out an example address is not a destination. Only whole-line
 *  and block comments go: a `//` inside a string literal is code, and eating
 *  it would hide the very thing being looked for.
 *
 *  JS only. A match pattern like `*://*.taobao.com/*` in the manifest opens
 *  what looks exactly like a block comment.
 */
const withoutComments = (key: string, source: string) =>
  key.endsWith(".js")
    ? source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "")
    : source;

// --------------------------------------------------------------------------- //
// Cookie flattening -- the one piece of real logic
// --------------------------------------------------------------------------- //

const cookie = (name: string, value: string, domain: string) => ({ name, value, domain });

describe("flattenCookies", () => {
  it("produces the header shape the panel already parses", () => {
    const flat = flattenCookies([
      cookie("unb", "2218219939144", ".goofish.com"),
      cookie("sgcookie", "E100abcdef==", ".goofish.com"),
    ]);
    // `=` padding inside a value must survive: the panel splits on the first
    // `=` only, so the header may carry more of them.
    expect(flat.header).toBe("unb=2218219939144; sgcookie=E100abcdef==");
    expect(flat.names).toEqual(["sgcookie", "unb"]);
    expect(flat.duplicates).toBe(0);
    expect(flat.conflicts).toBe(0);
  });

  it("keeps the goofish copy of every name that exists on both domains", () => {
    // The five measured in research/cookie-domains.md. Taobao listed FIRST so
    // a rule of "last one wins" or "first one wins" both fail here.
    const both = ["cookie2", "unb", "_tb_token_", "sgcookie", "x5sec"];
    const flat = flattenCookies([
      ...both.map((name) => cookie(name, `taobao-${name}`, ".taobao.com")),
      ...both.map((name) => cookie(name, `goofish-${name}`, ".goofish.com")),
    ]);

    expect(flat.names).toEqual([...both].sort());
    for (const name of both) {
      expect(flat.header).toContain(`${name}=goofish-${name}`);
      expect(flat.header).not.toContain(`taobao-${name}`);
    }
    expect(flat.duplicates).toBe(5);
    expect(flat.conflicts).toBe(5);
  });

  it("prefers goofish whichever order the two domains arrive in", () => {
    const goofishFirst = flattenCookies([
      cookie("cookie2", "keep", ".goofish.com"),
      cookie("cookie2", "drop", ".taobao.com"),
    ]);
    expect(goofishFirst.header).toBe("cookie2=keep");
    expect(goofishFirst.duplicates).toBe(1);
  });

  it("counts a same-name same-value pair as a duplicate but not a conflict", () => {
    // Flattening loses nothing here, and saying "1 conflict" would send the
    // user looking for damage that did not happen. Saying nothing at all would
    // make "the dedupe ran and found nothing" look like "the dedupe is gone".
    const flat = flattenCookies([
      cookie("unb", "same", ".goofish.com"),
      cookie("unb", "same", ".taobao.com"),
    ]);
    expect(flat.duplicates).toBe(1);
    expect(flat.conflicts).toBe(0);
    expect(flat.header).toBe("unb=same");
  });

  it("treats a goofish subdomain as goofish and keeps the first of two", () => {
    const flat = flattenCookies([
      cookie("t", "apex", ".goofish.com"),
      cookie("t", "passport", "passport.goofish.com"),
    ]);
    expect(flat.header).toBe("t=apex");
    expect(flat.duplicates).toBe(1);
  });

  it("carries the taobao-only signing token through untouched", () => {
    // The whole reason the extension exists. `_m_h5_tk` is never set on
    // goofish, so a preference rule that dropped non-goofish cookies outright
    // would silently defeat the feature.
    const flat = flattenCookies([
      cookie("unb", "1", ".goofish.com"),
      cookie("_m_h5_tk", "deadbeef_1757000000000", ".taobao.com"),
    ]);
    expect(flat.header).toContain("_m_h5_tk=deadbeef_1757000000000");
    expect(flat.names).toContain("_m_h5_tk");
    expect(flat.duplicates).toBe(0);
  });

  it("drops what is not a credential instead of sending an empty one", () => {
    const flat = flattenCookies([
      cookie("empty", "", ".goofish.com"),
      { name: "", value: "x", domain: ".goofish.com" },
      cookie("good", "1", ".goofish.com"),
    ]);
    expect(flat.header).toBe("good=1");
    expect(flat.names).toEqual(["good"]);
  });

  it("survives a cookie with no domain at all", () => {
    const flat = flattenCookies([
      { name: "a", value: "1" },
      cookie("a", "2", ".goofish.com"),
    ]);
    // Domainless loses to goofish; nothing throws on the missing field.
    expect(flat.header).toBe("a=2");
  });
});

// --------------------------------------------------------------------------- //
// Panel address
// --------------------------------------------------------------------------- //

describe("normalisePanelOrigin", () => {
  it("assumes plain http for an address typed without a scheme", () => {
    // `new URL("192.168.1.10:8000")` does NOT throw -- it reads
    // `192.168.1.10:` as the scheme -- so this cannot be left to URL alone.
    expect(normalisePanelOrigin("192.168.1.10:8000")).toBe("http://192.168.1.10:8000");
    expect(normalisePanelOrigin("localhost:8000")).toBe("http://localhost:8000");
  });

  it("strips whatever the user pasted past the origin", () => {
    expect(normalisePanelOrigin("http://localhost:8000/settings")).toBe("http://localhost:8000");
    expect(normalisePanelOrigin("  https://panel.example/  ")).toBe("https://panel.example");
  });

  it("returns empty for anything that is not an address", () => {
    expect(normalisePanelOrigin("")).toBe("");
    expect(normalisePanelOrigin(undefined)).toBe("");
    expect(normalisePanelOrigin("http://")).toBe("");
    expect(normalisePanelOrigin("::::")).toBe("");
  });
});

describe("addressSpace", () => {
  it("calls loopback loopback, not local", () => {
    // The bug this replaces, hit for real on 2026-09-08 against
    // http://127.0.0.1:8000 -- the address a self-hosted panel actually runs
    // on. `targetAddressSpace` is an assertion, and Chrome refuses a mismatch:
    //   Request had a target IP address space of `local` yet the resource is
    //   in address space `loopback`
    // A two-state model of a three-state API does not degrade, it blocks.
    for (const host of ["localhost", "panel.localhost", "127.0.0.1", "127.1.2.3", "[::1]"]) {
      expect(addressSpace(host), host).toBe("loopback");
    }
  });

  it("calls the private blocks local", () => {
    for (const host of ["10.0.0.5", "192.168.1.10", "172.16.0.1", "172.31.255.254", "[fd00::1]"]) {
      expect(addressSpace(host), host).toBe("local");
    }
  });

  it("asserts nothing about a public address", () => {
    // Claiming either value for a public panel would be false in the other
    // direction and would break a fetch that works today.
    for (const host of ["panel.example", "172.32.0.1", "11.0.0.1", "203.0.113.9", ""]) {
      expect(addressSpace(host), host).toBeNull();
    }
  });
});

// --------------------------------------------------------------------------- //
// The environment snapshot
// --------------------------------------------------------------------------- //

/** Evaluate `readEnvSnapshot` the way `chrome.scripting.executeScript` does:
 *  serialised and re-evaluated against the page's globals. A closure over
 *  anything outside the function body would be a ReferenceError there, and is
 *  one here too. */
function runCollector(nav: Record<string, unknown>): Record<string, unknown> {
  const screen = { width: 1920, height: 1080, colorDepth: 24 };
  const Intl = {
    DateTimeFormat: () => ({
      resolvedOptions: () => ({ timeZone: "Europe/Berlin", locale: "de-DE" }),
    }),
  };
  return new Function(
    "navigator",
    "screen",
    "window",
    "Intl",
    `return (${readEnvSnapshot.toString()})();`,
  )(nav, screen, { devicePixelRatio: 2 }, Intl) as Record<string, unknown>;
}

describe("readEnvSnapshot", () => {
  it("reads a Chromium environment into the shape the panel already accepts", () => {
    expect(
      runCollector({
        userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/141.0.0.0",
        platform: "Win32",
        language: "zh-CN",
        languages: ["zh-CN", "zh"],
        hardwareConcurrency: 24,
        deviceMemory: 8,
        maxTouchPoints: 0,
        userAgentData: {
          toJSON: () => ({
            brands: [{ brand: "Google Chrome", version: "141" }],
            mobile: false,
            platform: "Windows",
          }),
        },
      }),
    ).toEqual({
      user_agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/141.0.0.0",
      platform: "Win32",
      language: "zh-CN",
      languages: ["zh-CN", "zh"],
      hardware_concurrency: 24,
      device_memory: 8,
      max_touch_points: 0,
      ua_data: {
        brands: [{ brand: "Google Chrome", version: "141" }],
        mobile: false,
        platform: "Windows",
      },
      screen_width: 1920,
      screen_height: 1080,
      device_pixel_ratio: 2,
      color_depth: 24,
      time_zone: "Europe/Berlin",
      locale: "de-DE",
    });
  });

  it("omits what the browser does not offer instead of sending null", () => {
    const snapshot = runCollector({
      userAgent: "Mozilla/5.0 (X11; Linux x86_64; rv:129.0) Firefox/129.0",
      language: "en-US",
      maxTouchPoints: 0,
    });
    expect(snapshot).not.toHaveProperty("ua_data");
    expect(snapshot).not.toHaveProperty("device_memory");
    expect(Object.values(snapshot)).not.toContain(null);
    // ...and 0 is a value, not an absence.
    expect(snapshot.max_touch_points).toBe(0);
  });

  it("gives up the snapshot rather than the cookies", () => {
    expect(
      runCollector({
        get userAgent(): string {
          throw new TypeError("no");
        },
      }),
    ).toEqual({});
  });
});

// --------------------------------------------------------------------------- //
// The service worker, loaded for real against a stub `chrome`
// --------------------------------------------------------------------------- //

type ImportResult = {
  ok: boolean;
  error?: string;
  report?: {
    names: string[];
    duplicates: number;
    conflicts: number;
    envCaptured: boolean;
    envError?: string;
  };
};

type Harness = {
  send: (message: Record<string, unknown>) => Promise<ImportResult>;
  calls: { url: string; init: RequestInit & { targetAddressSpace?: string } }[];
};

const GOOFISH_COOKIES = [
  cookie("cookie2", "goofish-cookie2-value", ".goofish.com"),
  cookie("unb", "2218219939144", ".goofish.com"),
  cookie("_tb_token_", "e73b1f5a3e7d1", ".goofish.com"),
];
const TAOBAO_COOKIES = [
  cookie("cookie2", "taobao-cookie2-value", ".taobao.com"),
  cookie("_m_h5_tk", "deadbeefcafe_1757000000000", ".taobao.com"),
];
const EVERY_VALUE = [...GOOFISH_COOKIES, ...TAOBAO_COOKIES].map((c) => c.value);

const PANEL_REPLY = { cookie_names: ["_m_h5_tk", "_tb_token_", "cookie2", "unb"] };

async function loadBackground(
  options: {
    stored?: Record<string, unknown>;
    cookies?: Record<string, ReturnType<typeof cookie>[]>;
    env?: Record<string, unknown> | Error;
    granted?: boolean;
    respond?: () => Promise<Response> | Response;
    /** Answers the `/api/health` probe the fetch-failure path makes. */
    healthy?: boolean;
  } = {},
): Promise<Harness> {
  const listeners: ((m: unknown, s: unknown, r: (v: ImportResult) => void) => unknown)[] = [];
  const cookieJar = options.cookies ?? {
    "goofish.com": GOOFISH_COOKIES,
    "taobao.com": TAOBAO_COOKIES,
  };
  const calls: Harness["calls"] = [];

  const chrome = {
    runtime: { onMessage: { addListener: (fn: (typeof listeners)[number]) => listeners.push(fn) } },
    storage: {
      local: {
        get: async () => options.stored ?? { panelOrigin: "http://192.168.1.10:8000", apiToken: "tok" },
      },
    },
    permissions: {
      // Granted unless a test says otherwise. The real one can go missing
      // between two clicks, which is the whole reason background.js checks it.
      contains: async () => options.granted ?? true,
    },
    cookies: { getAll: async ({ domain }: { domain: string }) => cookieJar[domain] ?? [] },
    scripting: {
      executeScript: async () => {
        if (options.env instanceof Error) throw options.env;
        return [{ result: options.env ?? { user_agent: "UA", time_zone: "Asia/Shanghai" } }];
      },
    },
  };

  const globals = globalThis as unknown as Record<string, unknown>;
  globals.chrome = chrome;
  globals.fetch = vi.fn(async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    // The health probe is a separate request with its own answer: the whole
    // point of it is to behave differently from the import.
    if (url.endsWith("/api/health")) {
      if (options.healthy) return new Response("{}", { status: 200 });
      throw new TypeError("Failed to fetch");
    }
    return options.respond
      ? await options.respond()
      : new Response(JSON.stringify(PANEL_REPLY), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
  });

  vi.resetModules();
  await import("../../../extension/background.js");
  expect(listeners).toHaveLength(1);

  return {
    calls,
    send: (message) =>
      new Promise<ImportResult>((resolve) => {
        const kept = listeners[0]!(message, null, resolve);
        // Chrome only waits for an async reply when the listener says so.
        expect(kept).toBe(true);
      }),
  };
}

describe("background service worker", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("says so when the panel address was never set", async () => {
    const { send, calls } = await loadBackground({ stored: {} });
    const result = await send({ type: "import", tabId: 1 });
    expect(result.ok).toBe(false);
    expect(result.error).toContain("面板地址");
    // Not silent, and nothing was sent anywhere.
    expect(calls).toHaveLength(0);
  });

  it("says so when the API token was never set", async () => {
    const { send, calls } = await loadBackground({
      stored: { panelOrigin: "http://localhost:8000" },
    });
    const result = await send({ type: "import", tabId: 1 });
    expect(result.ok).toBe(false);
    expect(result.error).toContain("API token");
    expect(calls).toHaveLength(0);
  });

  it("says so when the browser is not logged in", async () => {
    const { send, calls } = await loadBackground({ cookies: {} });
    const result = await send({ type: "import", tabId: 1 });
    expect(result.ok).toBe(false);
    expect(result.error).toContain("登录闲鱼");
    expect(calls).toHaveLength(0);
  });

  it("posts the flattened header, the token and the snapshot to the stored panel", async () => {
    const { send, calls } = await loadBackground();
    const result = await send({
      type: "import",
      tabId: 7,
      pageOrigin: "https://www.goofish.com",
    });

    expect(result.ok).toBe(true);
    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toBe("http://192.168.1.10:8000/api/session/cookies");
    expect(calls[0]!.init.method).toBe("POST");
    const headers = calls[0]!.init.headers as Record<string, string>;
    // The service worker is in no page goofish controls, so it may carry the
    // panel's real token -- unlike the bookmarklet, which gets a ticket.
    expect(headers.authorization).toBe("Bearer tok");

    const body = JSON.parse(String(calls[0]!.init.body)) as Record<string, unknown>;
    expect(body.origin).toBe("https://www.goofish.com");
    expect(body.cookie_header).toContain("_m_h5_tk=deadbeefcafe_1757000000000");
    expect(body.cookie_header).toContain("cookie2=goofish-cookie2-value");
    expect(body.cookie_header).not.toContain("taobao-cookie2-value");
    expect(body.env).toEqual({ user_agent: "UA", time_zone: "Asia/Shanghai" });
  });

  it("hands the popup names and counts and not one cookie value", async () => {
    const { send } = await loadBackground();
    const result = await send({ type: "import", tabId: 7 });

    const serialised = JSON.stringify(result);
    // Every value that was collected, not a chosen one -- including the
    // taobao copy that lost the conflict and the token that is the point.
    for (const value of EVERY_VALUE) {
      expect(serialised, value).not.toContain(value);
    }
    expect(result.report).toEqual({
      names: PANEL_REPLY.cookie_names,
      duplicates: 1,
      conflicts: 1,
      envCaptured: true,
    });
  });

  it("states the address space the panel is actually in", async () => {
    // 127.0.0.1 is the address a self-hosted panel runs on, and it is
    // `loopback`, not `local`. Declaring `local` here is what made every
    // import after the first one fail with a bare "Failed to fetch".
    const loopback = await loadBackground({
      stored: { panelOrigin: "http://127.0.0.1:8000", apiToken: "tok" },
    });
    await loopback.send({ type: "import", tabId: 1 });
    expect(loopback.calls[0]!.init.targetAddressSpace).toBe("loopback");

    const lan = await loadBackground();
    await lan.send({ type: "import", tabId: 1 });
    expect(lan.calls[0]!.init.targetAddressSpace).toBe("local");

    const public_ = await loadBackground({
      stored: { panelOrigin: "https://panel.example", apiToken: "tok" },
    });
    await public_.send({ type: "import", tabId: 1 });
    expect(public_.calls[0]!.init).not.toHaveProperty("targetAddressSpace");
  });

  it("points a blocked or unreachable panel back at the devtools flow", async () => {
    const { send } = await loadBackground({
      respond: () => {
        throw new TypeError("Failed to fetch");
      },
    });
    const result = await send({ type: "import", tabId: 1 });
    expect(result.ok).toBe(false);
    expect(result.error).toContain("Failed to fetch");
    expect(result.error).toContain("开发者工具");
  });

  it("says the host permission is missing instead of failing opaquely", async () => {
    // The failure this replaces: a runtime-granted optional permission can be
    // gone by the next click (reloading an unpacked extension drops it), and
    // without it Chrome sends an ordinary cross-origin request instead of a
    // privileged one. The panel then gets a preflight it answers 405 -- no
    // OPTIONS route, and the extension origin is not in
    // ALLOWED_IMPORT_ORIGINS -- so fetch reports a bare "Failed to fetch" and
    // the user goes looking at a panel that is running fine. Hit for real on
    // 2026-09-08: the first import worked, every one after it did not.
    const { send, calls } = await loadBackground({ granted: false });
    const result = await send({ type: "import", tabId: 1 });
    expect(result.ok).toBe(false);
    expect(result.error).toContain("权限");
    expect(result.error).toContain("允许");
    // And it costs no request at all: checked before the cookies are read.
    expect(calls).toHaveLength(0);
  });

  it("separates a dead panel from a blocked request", async () => {
    // /api/health needs no auth and no custom header, so it is CORS-simple:
    // if it answers while the import does not, the panel is up and this one
    // request is what got stopped. Saying "面板没在跑" there sends the user to
    // restart a server that is already running.
    const blocked = await loadBackground({
      healthy: true,
      respond: () => {
        throw new TypeError("Failed to fetch");
      },
    });
    const stopped = await blocked.send({ type: "import", tabId: 1 });
    expect(stopped.error).toContain("活着");
    expect(stopped.error).not.toContain("面板没在跑");

    const down = await loadBackground({
      healthy: false,
      respond: () => {
        throw new TypeError("Failed to fetch");
      },
    });
    const dead = await down.send({ type: "import", tabId: 1 });
    expect(dead.error).toContain("面板没在跑");
    expect(dead.error).not.toContain("活着");
  });

  it("says WHY the environment snapshot is missing, not just that it is", async () => {
    // Silent by nature: the cookies come from the cookie store whatever tab is
    // active, so the import reports success while the panel quietly keeps its
    // built-in identity. On the first real run the click happened on the
    // panel's own tab -- a different host from goofish, so injection had no
    // permission -- and nothing on screen said the snapshot had been dropped.
    const { send } = await loadBackground({ env: new Error("no access") });
    const result = await send({ type: "import", tabId: 1 });
    expect(result.ok).toBe(true);
    const report = result.report;
    if (!report) throw new Error("a successful import must carry a report");
    expect(report.envCaptured).toBe(false);
    expect(report.envError).toContain("闲鱼");
    expect(describeReport(report)).toContain("闲鱼标签页");
  });

  it("names the localhost/IPv6 trap instead of blaming the panel", async () => {
    // The cause that looks exactly like "the panel is down" while the panel is
    // up and answering curl: `localhost` resolves to ::1 first and uvicorn's
    // default bind is IPv4 only, so fetch hits a closed port. Hit for real on
    // 2026-09-08. A message that says "面板没在跑" here sends the user to
    // restart a server that is already running.
    const { send } = await loadBackground({
      stored: { panelOrigin: "http://localhost:8000", apiToken: "t" },
      respond: () => {
        throw new TypeError("Failed to fetch");
      },
    });
    const result = await send({ type: "import", tabId: 1 });
    expect(result.error).toContain("127.0.0.1");
    expect(result.error).not.toContain("面板没在跑");
  });

  it("still blames the usual suspects for a non-localhost panel", async () => {
    const { send } = await loadBackground({
      stored: { panelOrigin: "http://192.168.1.10:8000", apiToken: "t" },
      respond: () => {
        throw new TypeError("Failed to fetch");
      },
    });
    const result = await send({ type: "import", tabId: 1 });
    expect(result.error).toContain("面板没在跑");
    expect(result.error).not.toContain("127.0.0.1");
  });

  it("forwards the panel's own refusal instead of a bare status", async () => {
    const { send } = await loadBackground({
      respond: () =>
        new Response(JSON.stringify({ detail: "解析到 cookie 但没有登录态字段" }), { status: 400 }),
    });
    const result = await send({ type: "import", tabId: 1 });
    expect(result.ok).toBe(false);
    expect(result.error).toContain("400");
    expect(result.error).toContain("没有登录态字段");
  });

  it("gives up the snapshot rather than the import when the tab cannot be read", async () => {
    const { send, calls } = await loadBackground({ env: new Error("no host permission") });
    const result = await send({ type: "import", tabId: 1 });

    expect(result.ok).toBe(true);
    // Absent, never null: the panel reads an absent `env` as "keep defaults".
    expect(JSON.parse(String(calls[0]!.init.body))).not.toHaveProperty("env");
    expect(result.report?.envCaptured).toBe(false);
  });

  it("ignores messages that are not an import", async () => {
    const { calls } = await loadBackground();
    expect(calls).toHaveLength(0);
  });
});

// --------------------------------------------------------------------------- //
// What the popup says
// --------------------------------------------------------------------------- //

describe("describeReport", () => {
  const base = { names: ["_m_h5_tk", "cookie2", "unb"], duplicates: 0, conflicts: 0, envCaptured: true };

  it("answers the one question the extension exists for", () => {
    expect(describeReport(base)).toContain("包含 _m_h5_tk");
    expect(describeReport({ ...base, names: ["cookie2", "unb"] })).toContain("没有取到 _m_h5_tk");
  });

  it("never claims recovery", () => {
    // Same rule as the bookmarklet and docs/operations.md: the import proves
    // nothing, only a real collection does.
    const text = describeReport(base);
    expect(text).not.toContain("已恢复");
    expect(text).toContain("立即运行");
  });

  it("reports the conflicts rather than swallowing them", () => {
    const text = describeReport({ ...base, duplicates: 5, conflicts: 2 });
    expect(text).toContain("5 个");
    expect(text).toContain("2 个");
    const quiet = describeReport({ ...base, duplicates: 5, conflicts: 0 });
    expect(quiet).toContain("5 个");
    expect(quiet).toContain("没有丢信息");
  });

  it("says nothing about deduping when nothing was deduped", () => {
    expect(describeReport(base)).not.toContain("去重");
  });

  it("shows no cookie value, because it is handed none", async () => {
    const { send } = await loadBackground();
    const result = await send({ type: "import", tabId: 7 });
    const text = describeReport(result.report!);
    for (const value of EVERY_VALUE) {
      expect(text, value).not.toContain(value);
    }
  });
});

// --------------------------------------------------------------------------- //
// Manifest and outbound-destination audit
// --------------------------------------------------------------------------- //

describe("manifest", () => {
  const manifest = JSON.parse(
    SOURCES[Object.keys(SOURCES).find((k) => k.endsWith("extension/manifest.json"))!]!,
  ) as Record<string, string[] | string | Record<string, string>>;

  it("asks for the four permissions it uses and no more", () => {
    // Asserted whole, not "does not contain webRequest": the next permission
    // added has to be argued for in a diff, not slipped in.
    expect(manifest.permissions).toEqual(["cookies", "storage", "scripting", "tabs"]);
  });

  it("does not ask to read the browsing history", () => {
    // `webRequest` puts "读取你的浏览记录" on the install prompt. The reference
    // implementation uses it to capture real `sec-ch-ua*` headers; the panel
    // rebuilds those from `ua_data.brands` instead
    // (app/collector/fingerprint.py, tests/test_fingerprint.py).
    for (const source of Object.values(SOURCES)) {
      expect(source).not.toContain("webRequest");
    }
  });

  it("declares taobao alongside goofish, which is the point", () => {
    expect(manifest.host_permissions).toEqual([
      "*://*.goofish.com/*",
      "*://*.taobao.com/*",
    ]);
    // Both schemes: a self-hosted panel is usually plain http, and an
    // https-only wildcard could not be granted for one.
    expect(manifest.optional_host_permissions).toEqual(["http://*/*", "https://*/*"]);
  });
});

describe("outbound destinations", () => {
  it("has only the network call sites enumerated here", () => {
    // By pattern, not by name: any way of reaching the network counts, so a
    // new one has to show up here even if it points somewhere innocent.
    // Enumerated rather than counted -- the count changed once already, when
    // the /api/health probe was added to tell "panel is down" apart from
    // "this request was blocked", and a count would have had to be bumped
    // without anyone re-reading where the call goes.
    const sinks = /\bfetch\s*\(|XMLHttpRequest|sendBeacon|WebSocket|EventSource|importScripts|\bimport\s*\(/g;
    const found = Object.entries(SOURCES).flatMap(([key, source]) =>
      [...source.matchAll(sinks)].map((m) => `${shortPath(key)}: ${m[0]}`),
    );
    expect(found).toEqual([
      "extension/background.js: fetch(", // the import POST
      "extension/background.js: fetch(", // the /api/health reachability probe
    ]);
  });

  it("builds every network destination out of the stored panel origin", () => {
    // The invariant the call-site count was only ever standing in for. Both
    // fetches must be template literals rooted at `origin`, which is whatever
    // `normalisePanelOrigin` made of what the user typed -- so neither one can
    // be pointed anywhere else without this failing.
    const [, source = ""] =
      Object.entries(SOURCES).find(([k]) => shortPath(k) === "extension/background.js") ?? [];
    const targets = [...source.matchAll(/fetch\(\s*([^,)]+)/g)].map((m) => m[1]?.trim());
    expect(targets).toEqual(["url", "`${origin}/api/health`"]);
    // ...and `url` itself is the same origin plus the shared path constant.
    expect(source).toContain("const url = `${origin}${IMPORT_PATH}`");
  });

  it("builds that one call's URL out of the stored panel origin and nothing else", () => {
    const background = SOURCES[
      Object.keys(SOURCES).find((k) => k.endsWith("extension/background.js"))!
    ]!;
    expect(background).toContain("const url = `${origin}${IMPORT_PATH}`");
    expect(background).toContain("await fetch(url, init)");
    // `origin` comes from storage by way of normalisePanelOrigin, and
    // IMPORT_PATH is a path with no host in it.
    expect(background).toContain("normalisePanelOrigin(stored.panelOrigin)");
    expect(SOURCES[Object.keys(SOURCES).find((k) => k.endsWith("extension/lib/panel.js"))!]).toContain(
      'export const IMPORT_PATH = "/api/session/cookies";',
    );
  });

  it("contains no absolute URL anywhere except the ones enumerated here", () => {
    // Every `scheme://` in the shipped files, whatever the file type. Asserted
    // as a whole list so a new one cannot pass by resembling an old one.
    const found = Object.entries(SOURCES).flatMap(([key, source]) =>
      [...withoutComments(key, source).matchAll(/[a-z*][a-z\d+.*-]*:\/\/[^\s"'`<>)]*/gi)].map(
        (m) => `${shortPath(key)}: ${m[0]}`,
      ),
    );
    expect(found.sort()).toEqual([
      // Permission patterns. They grant read access to a cookie store; they
      // are not destinations, and nothing in the code fetches them.
      "extension/manifest.json: *://*.goofish.com/*",
      "extension/manifest.json: *://*.taobao.com/*",
      "extension/manifest.json: http://*/*",
      "extension/manifest.json: https://*/*",
      // The scheme prepended to an address the user typed without one. The
      // host half is still entirely theirs.
      "extension/lib/panel.js: http://${raw}",
      // The `placeholder` attribute of the panel-address field. Markup, shown
      // to the user, never fetched.
      "extension/popup.html: http://192.168.1.10:8000",
      // Inside an error message, telling the user where to go reload the
      // extension when its host permission has gone missing. Prose in a
      // string literal -- and unfetchable by an extension anyway. Listed here
      // rather than exempted by pattern: the audit deliberately does not
      // strip string literals, because that is where a real destination would
      // hide.
      "extension/background.js: chrome://extensions",
    ].sort());
  });

  it("has no remote resource in the popup page", () => {
    const html = SOURCES[Object.keys(SOURCES).find((k) => k.endsWith("extension/popup.html"))!]!;
    // Local scripts and styles only; MV3's page CSP already forbids remote
    // script, but a remote font or image would still be a callout.
    expect([...html.matchAll(/(?:src|href)\s*=\s*"([^"]*)"/g)].map((m) => m[1])).toEqual([
      "popup.js",
    ]);
  });
});
