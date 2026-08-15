// Regression tests for the pure helper functions in extension/content.js.
//
// content.js is injected as a plain script (not a module), so we can't import
// it. Instead we load its source into a vm context with stubbed browser
// globals — enough for the top-level bootstrap (observers, timers) to run
// without throwing — then exercise the pure functions it defines.
//
// Run directly (`node tests/content_js_test.mjs`) or via test_content_js.py.
import fs from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = fs.readFileSync(path.join(here, "..", "extension", "content.js"), "utf8");

// Minimal DOM/browser stubs so the bootstrap code at load time is a no-op.
const noop = () => {};
const fakeEl = {
  style: {}, classList: { add: noop, remove: noop, contains: () => false },
  addEventListener: noop, appendChild: noop, setAttribute: noop,
  querySelector: () => null, querySelectorAll: () => [],
  textContent: "", dataset: {},
};
const document = {
  querySelector: () => null, querySelectorAll: () => [], getElementById: () => null,
  createElement: () => ({ ...fakeEl }), body: { ...fakeEl },
  addEventListener: noop, createTreeWalker: () => ({ nextNode: () => null, currentNode: null }),
  title: "", readyState: "complete",
};
const ctx = {
  document, window: { addEventListener: noop },
  location: { pathname: "/", href: "", hostname: "discuss.eroscripts.com" },
  MutationObserver: class { observe() {} disconnect() {} },
  setTimeout: () => 0, setInterval: () => 0, clearTimeout: noop, clearInterval: noop,
  fetch: () => Promise.resolve({ ok: false, json: () => Promise.resolve({}) }),
  console, URL, qt: undefined,
  Node: { DOCUMENT_POSITION_FOLLOWING: 4 }, NodeFilter: { SHOW_ELEMENT: 1 },
  HTMLElement: class {},
};
vm.createContext(ctx);
vm.runInContext(src, ctx, { filename: "content.js" });

let failures = 0;
function check(label, got, want) {
  const ok = got === want;
  if (!ok) failures++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}  (got ${JSON.stringify(got)}, want ${JSON.stringify(want)})`);
}

// ── isNonVideoPath: Twitter/X profile vs tweet, and host profile paths ──
check("x.com profile is non-video", ctx.isNonVideoPath("https://x.com/DiivesArt"), true);
check("twitter profile is non-video", ctx.isNonVideoPath("https://twitter.com/SomeArtist"), true);
check("x.com /status/ tweet is a video", ctx.isNonVideoPath("https://x.com/u/status/123"), false);
check("pixeldrain file is a video path", ctx.isNonVideoPath("https://pixeldrain.com/u/abc123"), false);
check("pornhub model page is non-video", ctx.isNonVideoPath("https://pornhub.com/model/foo"), true);

// ── _isScriptFilename ──
check("funscript ext", ctx._isScriptFilename("Script Sub 64_2026.funscript"), true);
check("funscript ext (mixed case)", ctx._isScriptFilename("X.FunScript"), true);
check("mp4 is not a script", ctx._isScriptFilename("Aisha Bunny - Wild.mp4"), false);
check("empty is not a script", ctx._isScriptFilename(""), false);

// ── _isGenericSectionName: decorated/plain generic vs real work names ──
check("decorated ༺Downloads is generic", ctx._isGenericSectionName("༺Downloads"), true);
check("Video link is generic", ctx._isGenericSectionName("Video link"), true);
check("Funscript file is generic", ctx._isGenericSectionName("Funscript file"), true);
check("resolution 1080p is generic", ctx._isGenericSectionName("1080p"), true);
check("Remake is generic", ctx._isGenericSectionName("Remake"), true);
check("real work name is NOT generic",
  ctx._isGenericSectionName("Aisha Bunny - Wild asian babe lets me cum"), false);
check("decorated real name is NOT generic", ctx._isGenericSectionName("༺༻ Gura Meal ༺༻"), false);

// ── cleanScriptName: strip Discourse size annotation + leading glyphs ──
const U = "https://eroscripts-discourse.eroscripts.com/original/4X/d/d/c/abcdef.funscript";
check("strips trailing size + leading glyph",
  ctx.cleanScriptName("sample-rope-demo-work.funscript? (27.2 KB)", U), "sample-rope-demo-work.funscript");
check("keeps axis suffix, drops size",
  ctx.cleanScriptName("name.twist.funscript (1.3 MB)", U), "name.twist.funscript");
check("keeps author brackets",
  ctx.cleanScriptName("[Author] Title.funscript (800 B)", U), "[Author] Title.funscript");
check("keeps CJK + fullwidth brackets",
  ctx.cleanScriptName("【multi】示例.funscript (12.0 KB)", U), "【multi】示例.funscript");
check("clean name passes through", ctx.cleanScriptName("clean-name.funscript", U), "clean-name.funscript");
check("empty falls back to URL basename", ctx.cleanScriptName("", U), "abcdef.funscript");

// ── _isVideoLinkHeadingText: which OP headings mark the video section ──
check("'Video Link' heading", ctx._isVideoLinkHeadingText("Video Link"), true);
check("'Video Link' with emoji-stripped text", ctx._isVideoLinkHeadingText(" Video Link "), true);
check("'Video' heading", ctx._isVideoLinkHeadingText("Video"), true);
check("'Video Download' heading", ctx._isVideoLinkHeadingText("Video Download"), true);
check("'Script' heading is not video", ctx._isVideoLinkHeadingText("Script"), false);
check("'Information' heading is not video", ctx._isVideoLinkHeadingText("Information"), false);

// ── _isOfferableVideoHost: unknown-host links that may be offered ──
check("artist site artist-example is offerable",
  ctx._isOfferableVideoHost("https://artist-example.com/samplework/"), true);
check("generic artist site is offerable",
  ctx._isOfferableVideoHost("https://someartistsite.net/work/123"), true);
check("patreon is not a video", ctx._isOfferableVideoHost("https://www.patreon.com/Kotarou3990"), false);
check("discord is not a video", ctx._isOfferableVideoHost("https://discord.gg/abc"), false);
check("ad/affiliate link is not a video",
  ctx._isOfferableVideoHost("https://offers.feeliate.com/?lp=22&offer=1"), false);
check("eroscripts internal is not offered",
  ctx._isOfferableVideoHost("https://discuss.eroscripts.com/t/foo/123"), false);
check("known host handled elsewhere, not re-offered",
  ctx._isOfferableVideoHost("https://www.iwara.tv/video/abc/slug"), false);
check("funscript asset is not a video",
  ctx._isOfferableVideoHost("https://cdn.example.com/x.funscript"), false);
check("x.com profile is not a video", ctx._isOfferableVideoHost("https://x.com/SomeArtist"), false);
check("non-http scheme is not a video", ctx._isOfferableVideoHost("ftp://foo/bar.mp4"), false);

// ── escapeAttr: full attribute escaping, double quotes included ──
check("escapeAttr escapes double quotes", ctx.escapeAttr('a"b'), "a&quot;b");
check("escapeAttr escapes amp/quote/lt/gt",
  ctx.escapeAttr('<a href="x">&'), "&lt;a href=&quot;x&quot;&gt;&amp;");
check("escapeAttr null-safe", ctx.escapeAttr(null), "");
check("escapeAttr undefined-safe", ctx.escapeAttr(undefined), "");
check("escapeAttr passes clean names through",
  ctx.escapeAttr("Aisha Bunny - Wild.mp4"), "Aisha Bunny - Wild.mp4");

// ── probe cache + throttle: module-level reuse, size recording ──
let probeCalls = 0;
ctx.window.funpairdlBridge = {
  sendMessage: (type, data) => {
    if (type === "probe-url") {
      probeCalls++;
      if (data.url.includes("fail")) return Promise.resolve({ success: false });
      return Promise.resolve({
        success: true, size: 4096, filename: "vid.mp4",
        files: [{ url: "https://host/bundle/f1", size: 111, name: "f1.mp4" }],
      });
    }
    return Promise.resolve({});
  },
};
const r1 = await ctx.probeUrl("https://example.com/video");
const r2 = await ctx.probeUrl("https://example.com/video");
check("probe returns backend result", r1 && r1.size, 4096);
check("second probe served from module cache", probeCalls, 1);
check("cached probe returns same result object", r2 === r1, true);
check("probe size recorded for send-pair sizes",
  ctx._probedSizeFor("https://example.com/video", "https://example.com/video"), 4096);
check("bundle member size recorded",
  ctx._probedSizeFor("https://host/bundle/f1", "https://host/bundle/f1"), 111);
check("unknown url size is 0", ctx._probedSizeFor("https://nope", "https://nope"), 0);
await ctx.probeUrl("https://example.com/fail");
await ctx.probeUrl("https://example.com/fail");
check("failed probes are not cached (retried)", probeCalls, 3);

// ── send-pair payload contract: per-group / top-level "sizes" ──
let sentPayload = null;
ctx.window.funpairdlBridge.sendMessage = (type, data) => {
  if (type === "send-pair") { sentPayload = data; return Promise.resolve({ success: true }); }
  return Promise.resolve({});
};
await ctx.sendPairToServer({
  title: "T", preferredResolution: "best",
  groups: [{ name: "Main", videoUrls: ["https://v"], scriptUrls: [], sizes: { "https://v": 42 } }],
});
check("grouped payload group carries sizes",
  JSON.stringify(sentPayload.groups[0].sizes), '{"https://v":42}');
check("grouped payload keeps video_urls",
  JSON.stringify(sentPayload.groups[0].video_urls), '["https://v"]');
await ctx.sendPairToServer({
  title: "T2", videoUrls: ["https://v2"], scriptUrls: [], sizes: { "https://v2": 7 },
});
check("flat payload carries top-level sizes",
  JSON.stringify(sentPayload.sizes), '{"https://v2":7}');

// ── host gate: foreign hosts register nothing, eroscripts bootstraps,
//    and pure helpers stay defined either way ──
function makeCtx(hostname) {
  let observeCalls = 0;
  const c = {
    document: { ...document },
    window: { addEventListener: noop },
    location: { pathname: "/", href: "", hostname },
    MutationObserver: class {
      observe() { observeCalls++; }
      disconnect() {}
    },
    setTimeout: () => 0, setInterval: () => 0, clearTimeout: noop, clearInterval: noop,
    fetch: () => Promise.resolve({ ok: false, json: () => Promise.resolve({}) }),
    console, URL, qt: undefined,
    Node: { DOCUMENT_POSITION_FOLLOWING: 4 }, NodeFilter: { SHOW_ELEMENT: 1 },
    HTMLElement: class {},
  };
  c._observeCalls = () => observeCalls;
  return c;
}

const gatedCtx = makeCtx("pixeldrain.com");
vm.createContext(gatedCtx);
vm.runInContext(src, gatedCtx, { filename: "content.js" });
check("host gate rejects foreign host", gatedCtx._funpairdlHostAllowed(), false);
check("no observers registered on foreign host", gatedCtx._observeCalls(), 0);
check("pure helpers still defined under gate", typeof gatedCtx.cleanScriptName, "function");
check("escapeAttr still defined under gate", typeof gatedCtx.escapeAttr, "function");

const eroCtx = makeCtx("discuss.eroscripts.com");
vm.createContext(eroCtx);
vm.runInContext(src, eroCtx, { filename: "content.js" });
check("host gate allows eroscripts subdomain", eroCtx._funpairdlHostAllowed(), true);
check("observers registered on eroscripts", eroCtx._observeCalls() > 0, true);
check("main stub context passes host gate", ctx._funpairdlHostAllowed(), true);

// ── batch overlay exports ──
check("batch open exported", typeof ctx.window.funpairdlBatchOpen, "function");
check("batch open exported on foreign host", typeof gatedCtx.window.funpairdlBatchOpen, "function");
check("batch open gated on foreign host",
  gatedCtx.window.funpairdlBatchOpen(["https://discuss.eroscripts.com/t/x/1"]),
  "not_eroscripts");

// ── _topicIdFromUrl ──
check("topic id from url", ctx._topicIdFromUrl("https://discuss.eroscripts.com/t/some-slug/12345"), "12345");
check("topic id from post-anchor url", ctx._topicIdFromUrl("https://discuss.eroscripts.com/t/some-slug/12345/3"), "12345");
check("topic id null on listing", ctx._topicIdFromUrl("https://discuss.eroscripts.com/latest"), null);
check("topic id null on blank", ctx._topicIdFromUrl("about:blank"), null);

// ── batch selection persistence key ──
check("sel key from topic url",
  ctx._batchSelKey("https://discuss.eroscripts.com/t/some-slug/777/2"), "fpdl_batch_sel_777");
check("sel key null for non-topic", ctx._batchSelKey("https://discuss.eroscripts.com/latest"), null);

// ── probe cache TTL: entries older than 30 min are re-probed ──
// Uses a fresh context with an injected, mutable-clock Date so we can jump
// past the TTL without touching wall-clock time.
let _clock = 1_000_000;
let ttlProbeCalls = 0;
const ttlCtx = {
  document: { ...document },
  window: {
    addEventListener: noop,
    funpairdlBridge: {
      sendMessage: (type) => {
        if (type === "probe-url") {
          ttlProbeCalls++;
          return Promise.resolve({ success: true, size: 2048, filename: "x.mp4" });
        }
        return Promise.resolve({});
      },
    },
  },
  location: { pathname: "/", href: "", hostname: "discuss.eroscripts.com" },
  MutationObserver: class { observe() {} disconnect() {} },
  setTimeout: () => 0, setInterval: () => 0, clearTimeout: noop, clearInterval: noop,
  fetch: () => Promise.resolve({ ok: false, json: () => Promise.resolve({}) }),
  console, URL, qt: undefined,
  Node: { DOCUMENT_POSITION_FOLLOWING: 4 }, NodeFilter: { SHOW_ELEMENT: 1 },
  HTMLElement: class {},
  Date: { now: () => _clock },
};
vm.createContext(ttlCtx);
vm.runInContext(src, ttlCtx, { filename: "content.js" });
await ttlCtx.probeUrl("https://example.com/ttl");
check("ttl: size cached while fresh",
  ttlCtx._probedSizeFor("https://example.com/ttl", "https://example.com/ttl"), 2048);
await ttlCtx.probeUrl("https://example.com/ttl");
check("ttl: fresh probe served from cache", ttlProbeCalls, 1);
_clock += 31 * 60 * 1000; // jump past the 30-minute TTL
check("ttl: stale size entry reads 0",
  ttlCtx._probedSizeFor("https://example.com/ttl", "https://example.com/ttl"), 0);
await ttlCtx.probeUrl("https://example.com/ttl");
check("ttl: stale cache triggers re-probe", ttlProbeCalls, 2);

if (failures) {
  console.error(`\n${failures} assertion(s) failed`);
  process.exit(1);
}
console.log("\nAll content.js helper assertions passed");
