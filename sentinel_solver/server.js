/**
 * chatgpt2api Sentinel Solver
 *
 * Private HTTP service that solves ChatGPT Sentinel challenges inside a real
 * Chromium browser (Playwright). It deliberately accepts ONLY the challenge
 * parameters (seed/difficulty/dx/p) and never receives account tokens,
 * cookies, or user prompts. A shared-secret key is required on every call.
 *
 * Endpoints (all require `X-Solver-Key: <SOLVER_KEY>` unless SOLVER_KEY=""):
 *   GET  /health   -> { ok, browser }
 *   POST /solve    -> {
 *        body: {
 *          seed?: string, difficulty?: string,        // PoW (optional)
 *          dx?: string,                               // Turnstile dx (optional)
 *          p?: string,                                // requirements token for turnstile xor
 *          userAgent?: string,
 *          proxy?: string,                            // per-request proxy URL
 *          locale?: string, timezoneId?: string,      // browser fingerprint
 *          screen?: { width?: number, height?: number }
 *        }
 *        response: { ok: true, proofToken?: string, turnstileToken?: string, userAgent }
 *      }
 */
"use strict";

const http = require("http");
const { chromium } = require("playwright");

const HOST = process.env.SOLVER_HOST || "127.0.0.1";
const PORT = parseInt(process.env.SOLVER_PORT || "8787", 10);
const SOLVER_KEY = process.env.SOLVER_KEY || "";
const PAGE_TIMEOUT_MS = parseInt(process.env.SOLVER_TIMEOUT_MS || "30000", 10);
const POW_LIMIT = parseInt(process.env.SOLVER_POW_LIMIT || "5000000", 10);

const DEFAULT_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36";
let browserPromise = null;

function getBrowser() {
  if (!browserPromise) {
    browserPromise = chromium
      .launch({
        headless: true,
        args: [
          "--disable-blink-features=AutomationControlled",
          "--no-sandbox",
          "--disable-dev-shm-usage",
        ],
      })
      .catch((err) => {
        browserPromise = null;
        throw err;
      });
  }
  return browserPromise;
}

/** Build the current 25-field config in the page using the real navigator. */
function __buildConfigInPage(userAgent) {
  const perfNow = performance.now();
  const screenRes = `${window.screen.width}x${window.screen.height}`;
  const navEntries = Object.entries(Object.getOwnPropertyDescriptors(Navigator.prototype))
    .map(([key, descriptor]) => {
      try {
        return `${key}−${descriptor.get ? descriptor.get.call(navigator) : "undefined"}`;
      } catch (err) {
        return `${key}−undefined`;
      }
    });
  const navigatorKey =
    navEntries.find((e) => /^(vendorSub|plugins|mimeTypes|hardwareConcurrency)−/.test(e)) ||
    `${Object.keys(Object.getOwnPropertyDescriptors(Navigator.prototype))[0] || "vendorSub"}−undefined`;
  const documentKeys = Object.getOwnPropertyNames(document);
  const documentKey = documentKeys[Math.floor(Math.random() * documentKeys.length)] || "location";
  const winKeys = ["Object", "Function", "Array", "Number", "parseFloat", "undefined"];
  const windowKey = winKeys[Math.floor(Math.random() * winKeys.length)];
  const now = new Date();
  const tzName = now.toString().match(/\(([^)]+)\)/);
  const offsetMin = -now.getTimezoneOffset();
  const sign = offsetMin >= 0 ? "+" : "-";
  const abs = Math.abs(offsetMin);
  const hh = String(Math.floor(abs / 60)).padStart(2, "0");
  const mm = String(abs % 60).padStart(2, "0");
  const dateStr =
    now.toString().replace(/GMT([+-]\d{4}).*/, `GMT${sign}${hh}${mm}`) +
    ` (${tzName ? tzName[1] : "Coordinated Universal Time"})`;

  return [
    screenRes,
    dateStr,
    4294705152,
    Math.random(),
    userAgent,
    document.scripts && document.scripts.length
      ? document.scripts[document.scripts.length - 1].src
      : "https://chatgpt.com/backend-api/sentinel/sdk.js",
    null,
    null,
    navigator.language || "en-US",
    Math.random(),
    navigatorKey,
    documentKey,
    windowKey,
    perfNow,
    crypto.randomUUID(),
    "",
    navigator.hardwareConcurrency || 8,
    Date.now() - perfNow,
    0, 0, 0, 0, 0, 0,
    0,
  ];
}

/** Current PoW: FNV-1a 32-bit + avalanche, hex compared as string; token gets ~S suffix. */
function __b64Json(value) {
  const json = JSON.stringify(value);
  const bytes = new TextEncoder().encode(json);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}

function __solvePowInPage(seed, difficulty, userAgent, limit) {
  const config = __buildConfigInPage(userAgent);
  function fnv1a(str) {
    let hash = 2166136261;
    for (let i = 0; i < str.length; i++) {
      hash ^= str.charCodeAt(i);
      hash = Math.imul(hash, 16777619) >>> 0;
    }
    hash ^= hash >>> 16;
    hash = Math.imul(hash, 2246822507) >>> 0;
    hash ^= hash >>> 13;
    hash = Math.imul(hash, 3266489909) >>> 0;
    hash ^= hash >>> 16;
    return (hash >>> 0).toString(16).padStart(8, "0");
  }
  const start = performance.now();
  const target = String(difficulty || "0");
  for (let i = 0; i < limit; i++) {
    const data = config.slice();
    data[3] = i;
    data[9] = Math.round(performance.now() - start);
    const payload = __b64Json(data);
    if (fnv1a(seed + payload).substring(0, target.length) <= target) {
      return "gAAAAAB" + payload + "~S";
    }
  }
  return null;
}

/** Solve the current requirements token: 25-field config, d[3]=1, d[9]=random, "C" prefix. */
function __buildRequirementsTokenInPage(userAgent) {
  const config = __buildConfigInPage(userAgent);
  const data = config.slice();
  data[3] = 1;
  data[9] = Math.round(5 + Math.random() * 45);
  return "gAAAAAC" + __b64Json(data);
}

async function __solveTurnstileInPage(dx, p, vcxsrvDebug) {
  return await (async () => {
    function xorString(text, key) {
      if (!key) return text;
      let out = "";
      for (let i = 0; i < text.length; i++) {
        out += String.fromCharCode(text.charCodeAt(i) ^ key.charCodeAt(i % key.length));
      }
      return out;
    }
    function toStr(value) {
      if (value === null || value === undefined) return "undefined";
      if (typeof value === "number") return String(value);
      if (typeof value === "string") {
        const special = {
          "window.Math": "[object Math]",
          "window.Reflect": "[object Reflect]",
          "window.performance": "[object Performance]",
          "window.localStorage": "[object Storage]",
          "window.Object": "function Object() { [native code] }",
          "window.Reflect.set": "function set() { [native code] }",
          "window.performance.now": "function () { [native code] }",
          "window.Object.create": "function create() { [native code] }",
          "window.Object.keys": "function keys() { [native code] }",
          "window.Math.random": "function random() { [native code] }",
        };
        return special[value] || value;
      }
      if (Array.isArray(value) && value.every((v) => typeof v === "string")) return value.join(",");
      return String(value);
    }

    const decoded = atob(dx);
    const tokenList = JSON.parse(xorString(decoded, p || ""));
    const processMap = new Map();
    const startNs = performance.now() * 1e6;
    let result = "";

    class OrderedMap {
      constructor() {
        this.keys = [];
        this.values = {};
      }
      add(key, value) {
        if (!(key in this.values)) this.keys.push(key);
        this.values[key] = value;
      }
    }

    const funcs = {
      1: (e, t) => processMap.set(e, xorString(toStr(processMap.get(e)), toStr(processMap.get(t)))),
      2: (e, t) => processMap.set(e, t),
      3: (e) => {
        const bytes = new TextEncoder().encode(String(e));
        let bin = "";
        for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
        result = btoa(bin);
      },
      5: (e, t) => {
        const cur = processMap.get(e);
        const inc = processMap.get(t);
        if (Array.isArray(cur)) return processMap.set(e, cur.concat([inc]));
        if (typeof cur === "string" || typeof cur === "number" || typeof inc === "string" || typeof inc === "number") {
          return processMap.set(e, toStr(cur) + toStr(inc));
        }
        processMap.set(e, "NaN");
      },
      6: (e, t, n) => {
        const tv = processMap.get(t);
        const nv = processMap.get(n);
        if (typeof tv === "string" && typeof nv === "string") {
          const value = `${tv}.${nv}`;
          processMap.set(e, value === "window.document.location" ? "https://chatgpt.com/" : value);
        }
      },
      7: (e, ...args) => {
        const target = processMap.get(e);
        const values = args.map((a) => processMap.get(a));
        if (target === "window.Reflect.set") {
          const [obj, keyName, val] = values;
          if (obj && typeof obj.add === "function") obj.add(String(keyName), val);
        } else if (typeof target === "function") {
          target(...values);
        }
      },
      8: (e, t) => processMap.set(e, processMap.get(t)),
      14: (e, t) => processMap.set(e, JSON.parse(processMap.get(t))),
      15: (e, t) => processMap.set(e, JSON.stringify(processMap.get(t))),
      17: (e, t, ...args) => {
        const target = processMap.get(t);
        const callArgs = args.map((a) => processMap.get(a));
        if (target === "window.performance.now") {
          processMap.set(e, (performance.now() * 1e6 - startNs + Math.random()) / 1e6);
        } else if (target === "window.Object.create") {
          processMap.set(e, new OrderedMap());
        } else if (target === "window.Object.keys") {
          if (callArgs[0] === "window.localStorage") {
            processMap.set(e, [
              "STATSIG_LOCAL_STORAGE_INTERNAL_STORE_V4",
              "STATSIG_LOCAL_STORAGE_STABLE_ID",
              "client-correlated-secret",
              "oai/apps/capExpiresAt",
              "oai-did",
              "STATSIG_LOCAL_STORAGE_LOGGING_REQUEST",
              "UiState.isNavigationCollapsed.1",
            ]);
          } else {
            processMap.set(e, Object.keys(window.localStorage || {}));
          }
        } else if (target === "window.Math.random") {
          processMap.set(e, Math.random());
        } else if (typeof target === "function") {
          processMap.set(e, target(...callArgs));
        }
      },
      18: (e) => processMap.set(e, atob(toStr(processMap.get(e)))),
      19: (e) => {
        const bytes = new TextEncoder().encode(toStr(processMap.get(e)));
        let bin = "";
        for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
        processMap.set(e, btoa(bin));
      },
      20: (e, t, n, ...args) => {
        if (processMap.get(e) === processMap.get(t)) {
          const target = processMap.get(n);
          if (typeof target === "function") target(...args.map((a) => processMap.get(a)));
        }
      },
      21: () => {},
      23: (e, t, ...args) => {
        const target = processMap.get(t);
        if (processMap.get(e) !== null && typeof target === "function") target(...args);
      },
      24: (e, t, n) => {
        const tv = processMap.get(t);
        const nv = processMap.get(n);
        if (typeof tv === "string" && typeof nv === "string") processMap.set(e, `${tv}.${nv}`);
      },
    };

    for (const [key, fn] of Object.entries(funcs)) processMap.set(Number(key), fn);
    processMap.set(9, tokenList);
    processMap.set(10, "window");
    processMap.set(16, p || "");

    if (vcxsrvDebug) return { dbg: tokenList.slice(0, 5), result: "" };
    for (const token of tokenList) {
      try {
        const fn = processMap.get(token[0]);
        if (typeof fn === "function") fn(...token.slice(1));
      } catch (err) {
        continue;
      }
    }
    return result || null;
  })();
}

async function handleSolve(body) {
  const {
    seed = "",
    difficulty = "",
    dx = "",
    p = "",
    userAgent = "",
    proxy = "",
    locale = "",
    timezoneId = "",
    screen = {},
  } = body || {};

  if (!seed && !dx) {
    return { status: 400, json: { ok: false, error: "seed or dx is required" } };
  }

  const ua = String(userAgent || DEFAULT_UA);
  const browser = await getBrowser();

  const context = await browser.newContext({
    userAgent: ua,
    locale: locale || "en-US",
    timezoneId: timezoneId || "America/Los_Angeles",
    viewport: {
      width: Math.max(320, parseInt(screen.width, 10) || 1920),
      height: Math.max(240, parseInt(screen.height, 10) || 1080),
    },
    proxy: proxy ? { server: proxy } : undefined,
    ignoreHTTPSErrors: true,
  });

  try {
    const page = await context.newPage();
    page.setDefaultTimeout(PAGE_TIMEOUT_MS);
    // Establish the same-origin browser environment the challenge runs in.
    await page.goto("https://chatgpt.com/", {
      waitUntil: "domcontentloaded",
      timeout: PAGE_TIMEOUT_MS,
    });
    const out = { ok: true, userAgent: ua };
    if (seed) {
      const fnSource = __b64Json.toString() + "\n" + __buildConfigInPage.toString() + "\n" + __solvePowInPage.toString();
      const proofToken = await page.evaluate(
        ({ src, seed: s, difficulty: d, ua, limit }) => {
          eval(src);
          return __solvePowInPage(s, d, ua, limit);
        },
        { src: fnSource, seed: String(seed), difficulty: String(difficulty || "0"), ua, limit: POW_LIMIT }
      );
      if (!proofToken) {
        return { status: 500, json: { ok: false, error: "pow_not_solved" } };
      }
      out.proofToken = proofToken;
    }
    if (dx) {
      const tsSource = __solveTurnstileInPage.toString();
      const turnstileToken = await page.evaluate(
        ({ src, dx: d, p: pp }) => {
          eval(src);
          return __solveTurnstileInPage(d, pp);
        },
        { src: tsSource, dx: String(dx), p: String(p || "") }
      );
      if (!turnstileToken) {
        return { status: 500, json: { ok: false, error: "turnstile_not_solved" } };
      }
      out.turnstileToken = turnstileToken;
    }
    return { status: 200, json: out };
  } finally {
    await context.close().catch(() => {});
  }
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (chunk) => {
      chunks.push(chunk);
      if (Buffer.concat(chunks).length > 1_000_000) {
        reject(new Error("request_too_large"));
        req.destroy();
      }
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

function send(res, status, data) {
  const payload = JSON.stringify(data);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(payload),
    "Cache-Control": "no-store",
  });
  res.end(payload);
}

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
    if (req.method === "GET" && url.pathname === "/health") {
      return send(res, 200, { ok: true, browser: Boolean(browserPromise) });
    }
    if (SOLVER_KEY && req.headers["x-solver-key"] !== SOLVER_KEY) {
      return send(res, 401, { ok: false, error: "unauthorized" });
    }
    if (req.method === "POST" && url.pathname === "/solve") {
      const raw = await readBody(req);
      let body;
      try {
        body = JSON.parse(raw || "{}");
      } catch (err) {
        return send(res, 400, { ok: false, error: "invalid_json" });
      }
      const result = await handleSolve(body);
      return send(res, result.status, result.json);
    }
    return send(res, 404, { ok: false, error: "not_found" });
  } catch (err) {
    return send(res, 500, { ok: false, error: String(err && err.message ? err.message : err) });
  }
});

server.listen(PORT, HOST, () => {
  console.log(`sentinel_solver listening on http://${HOST}:${PORT} (key ${SOLVER_KEY ? "required" : "disabled"})`);
});
