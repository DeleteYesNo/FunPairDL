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

// ── detectAxis: only known axes are axes; other dot-words belong to the name ──
check("axis: raw suffix is the main script", ctx.detectAxis("Title ver.!!.raw.funscript"), "main");
check("axis: raw + pitch → pitch", ctx.detectAxis("Title ver.!!.raw.pitch.funscript"), "pitch");
check("axis: raw + surge → surge", ctx.detectAxis("Title ver.!!.raw.surge.funscript"), "surge");
check("axis: plain name is main", ctx.detectAxis("Title.funscript"), "main");
check("axis: known axis, any case", ctx.detectAxis("Title.Roll.funscript"), "roll");
check("axis: erodeck code", ctx.detectAxis("Title.R2.funscript"), "r2");
check("axis: L0 is main", ctx.detectAxis("Title.L0.funscript"), "main");
check("axis: L0 with a trailing qualifier is main", ctx.detectAxis("Title.L0.max.funscript"), "main");
check("axis: stroke is main", ctx.detectAxis("Title.stroke.funscript"), "main");
check("axis: word axis with glued qualifier keeps its spelling", ctx.detectAxis("Title.suckManual.funscript"), "suckManual");
check("axis: word axis with separator qualifier", ctx.detectAxis("Title.twist_v2.funscript"), "twist_v2");
check("axis: stroke with qualifier is still main", ctx.detectAxis("Title.strokeSoft.funscript"), "main");
check("axis: a plain word that merely starts with an axis is not one", ctx.detectAxis("Title.rolling.funscript"), "main");
check("axis: unrelated word is main", ctx.detectAxis("Title.manual.funscript"), "main");
// ".v2" is the erodeck valve axis code, which the backend maps the same way.
check("axis: erodeck v2 code is the valve axis", ctx.detectAxis("Title.v2.funscript"), "v2");

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

// ── e621 post pages are known video hosts (label + priority) ──
check("e621 label", ctx.getVideoLabel("https://e621.net/posts/1234567?q=someartist"), "e621");
check("e926 label", ctx.getVideoLabel("https://e926.net/posts/1"), "e621");
check("e621 priority is a known-host tier", ctx.getVideoPriority("https://e621.net/posts/1234567", false), 7);
check("e621 comment priority", ctx.getVideoPriority("https://e621.net/posts/1234567", true), 7.5);
check("e621 post is not a non-video path", ctx.isNonVideoPath("https://e621.net/posts/1234567"), false);

// ── _distributeOrphanScripts: generic "Script" section feeds work sections ──
{
  const sec = (name, videos, scripts) => ({
    name,
    videos: videos.map((u) => ({ url: u, label: "e621", source: "OP", priority: 7 })),
    scripts: scripts.map((f) => ({ url: `https://forum.example/${f}`, filename: f, source: "OP", axis: "main" })),
  });
  const parsedSecs = ctx._distributeOrphanScripts([
    sec("Alpha’s training / Alpha’s chill training", ["https://e621.net/posts/1"], []),
    sec("Alpha invites you over / AlphaVR", ["https://e621.net/posts/2"], []),
    sec("Alpha’s training the trainer. / AlphaDoggy", ["https://e621.net/posts/3"], []),
    sec("Script", [], [
      "Alpha’s_Chill_TrainingVR(8K-H265@60fps).funscript",
      "AlphaVR(8K-HEVC@60fps).funscript",
      "AlphaDoggyVR(8K-H265@60fps).funscript",
      "Alpha_longer.funscript",
      "AlphaDoggy_longer.funscript",
    ]),
  ]);
  const names = (s) => s.scripts.map((x) => x.filename);
  check("orphan: section 1 gets its VR script", names(parsedSecs[0])[0], "Alpha’s_Chill_TrainingVR(8K-H265@60fps).funscript");
  check("orphan: section 2 gets the exact-name script", names(parsedSecs[1]).join("|"), "AlphaVR(8K-HEVC@60fps).funscript");
  check("orphan: section 3 gets both of its scripts", names(parsedSecs[2]).join("|"),
    "AlphaDoggyVR(8K-H265@60fps).funscript|AlphaDoggy_longer.funscript");
  check("orphan: series-name-only script stays behind", names(parsedSecs[3]).join("|"), "Alpha_longer.funscript");
  check("orphan: donor keeps its name", parsedSecs[3].name, "Script");

  // A donor that empties out disappears.
  const emptied = ctx._distributeOrphanScripts([
    sec("Delta [AuthX]", ["https://e621.net/posts/4"], []),
    sec("Gamma [AuthX]", ["https://e621.net/posts/5"], []),
    sec("Downloads", [], ["Delta_multi.funscript", "gamma.roll.funscript"]),
  ]);
  check("orphan: word match routes by unique heading word", emptied.length, 2);
  check("orphan: Delta script under Delta", names(emptied[0]).join("|"), "Delta_multi.funscript");
  check("orphan: axis-suffixed Gamma script under Gamma", names(emptied[1]).join("|"), "gamma.roll.funscript");

  // Never touch: one video section (single mode anyway), a named
  // script-only section (its own work), or an ambiguous script.
  const untouched = ctx._distributeOrphanScripts([
    sec("Alpha Part 1", ["https://e621.net/posts/6"], []),
    sec("Alpha Part 2", ["https://e621.net/posts/7"], []),
    sec("Bonus work", [], ["Alpha_Part_1.funscript"]),
    sec("Scripts", [], ["Alpha.funscript"]),
  ]);
  check("orphan: named script section is left alone", names(untouched[2]).join("|"), "Alpha_Part_1.funscript");
  check("orphan: ambiguous script (shared words only) stays", names(untouched[3]).join("|"), "Alpha.funscript");
  check("orphan: single video section → no-op",
    ctx._distributeOrphanScripts([sec("Only", ["https://e621.net/posts/8"], []), sec("Script", [], ["Only.funscript"])])[1].scripts.length, 1);
}

// ── _bucketCollectionInputs: drag moves decide the send-time section ──
{
  const b = ctx._bucketCollectionInputs([
    { name: "sv-0", value: "0" }, { name: "ss-3", value: "1" }, { name: "ss-3", value: "2" },
    { name: "cs", value: "0" }, { name: "video", value: "0" },
  ], { "ss-3-1": "0", "cs-0": "2" });
  check("bucket: video stays in its own section", b["0"].videos.map((x) => x.key).join(), "sv-0-0");
  check("bucket: moved script lands in section 0", b["0"].scripts.map((x) => x.key).join(), "ss-3-1");
  check("bucket: unmoved script stays in section 3", b["3"].scripts.map((x) => x.key).join(), "ss-3-2");
  check("bucket: comment script moved into section 2", b["2"].scripts.map((x) => x.key).join(), "cs-0");
  check("bucket: single-mode names are ignored", Object.keys(b).sort().join(), "0,2,3");
  check("bucket: origin parse", JSON.stringify(ctx._collectionItemOrigin("cv")), '{"kind":"video","section":"comments"}');

  // Comment rows render inside per-post groups: the DOM section wins over
  // the parsed origin, and an explicit move wins over both.
  const c = ctx._bucketCollectionInputs([
    { name: "cv", value: "0", section: "c1" }, { name: "cs", value: "0", section: "c1" },
    { name: "cs", value: "1", section: "c1" }, { name: "ss-0", value: "0", section: "x1" },
  ], { "cs-1": "x1" });
  check("bucket: comment rows land in their post group", c["c1"].videos.length + c["c1"].scripts.length, 2);
  check("bucket: user group collects a dragged OP script and a moved comment script",
    c["x1"].scripts.map((x) => x.key).sort().join(), "cs-1,ss-0-0");
}

// ── comment groups & work names from headings ──
check("video-file heading counts as a video heading", ctx._isVideoLinkHeadingText("Alpha_longer.mp4"), true);
check("plain heading is not a video heading", ctx._isVideoLinkHeadingText("Notes"), false);
check("work name drops the video extension", ctx._cleanWorkName("Alpha_longer.mp4"), "Alpha_longer");
check("generic heading yields no work name", ctx._cleanWorkName("Video link"), "");
check("download heading yields no work name", ctx._cleanWorkName("Downloads"), "");
{
  const cv = [{ url: "https://host.example/f/1" }, { url: "https://host.example/f/2" }];
  const cs = [{ url: "https://forum.example/a.funscript" }, { url: "https://forum.example/b.funscript" },
              { url: "https://forum.example/c.funscript" }];
  const groups = ctx._buildCommentGroups([
    { isOP: true, postNumber: 1, subGroups: [{ name: "", videos: [], scripts: [] }] },
    { isOP: false, postNumber: 7, username: "poster", subGroups: [
      { name: "Alpha_longer", videos: [cv[0]], scripts: [cs[0]] },
      { name: "Beta_longer", videos: [cv[1]], scripts: [cs[1]] },
    ] },
    // Same script posted again in a later comment → not listed twice.
    { isOP: false, postNumber: 9, username: "", subGroups: [{ name: "", videos: [], scripts: [cs[1], cs[2]] }] },
  ], cv, cs);
  check("comment groups: one per in-post pairing plus the leftover", groups.length, 3);
  check("comment groups: ids", groups.map((g) => g.id).join(), "c0,c1,c2");
  check("comment groups: named after the work heading", groups[0].name, "Alpha_longer");
  check("comment groups: labelled by post", groups[0].label, "#7 @poster");
  check("comment groups: indices into flat arrays", `${groups[1].videos}|${groups[1].scripts}`, "1|1");
  check("comment groups: duplicate script not repeated", `${groups[2].scripts}`, "2");
  check("comment groups: unnamed post label", groups[2].label, "#9");

  const parsed = {
    title: "Topic Title", sections: [{ name: "Video link" }, { name: "Real Work" }],
    commentGroups: groups, extraSections: [{ id: "x1", name: " " }, { id: "x2", name: "Custom" }],
  };
  check("pair name: generic OP heading → topic title", ctx._collectionPairName(parsed, "0"), "Topic Title");
  check("pair name: real OP heading kept", ctx._collectionPairName(parsed, "1"), "Real Work");
  check("pair name: comment group uses its work name", ctx._collectionPairName(parsed, "c0"), "Alpha_longer");
  check("pair name: unnamed comment group → topic title", ctx._collectionPairName(parsed, "c2"), "Topic Title");
  check("pair name: blank user group → topic title", ctx._collectionPairName(parsed, "x1"), "Topic Title");
  check("pair name: named user group", ctx._collectionPairName(parsed, "x2"), "Custom");
}

// ── formatDuration / basis helpers ──
check("duration m:ss", ctx.formatDuration(201.4), "3:21");
check("duration h:mm:ss", ctx.formatDuration(3725), "1:02:05");
check("duration unknown", ctx.formatDuration(0), "");
check("weakest basis is the guess", ctx._weakestBasis(["name", "order", "duration"]), "order");
check("weakest basis ignores blanks", ctx._weakestBasis(["", "tokens"]), "tokens");
check("basis tag renders the label", ctx._basisTagHTML("order").includes("順序(猜測)"), true);
check("unknown basis renders nothing", ctx._basisTagHTML(""), "");

// ── _workPlanGroups: rows grouped by their labels, plan order first ──
{
  const r = (url, name) => ({ url, name, row: null });
  const rows = {
    videos: [r("v1", "Alpha.mp4"), r("v2", "Alpha mirror.mp4"), r("v3", "Beta.mp4")],
    scripts: [r("s1", "Alpha.funscript"), r("s2", "Beta.funscript"), r("s3", "Loose.funscript")],
  };
  const g = ctx._workPlanGroups(rows, { v1: "Alpha", v2: "Alpha", s1: "Alpha", v3: "Beta", s2: "Beta" }, ["Custom"], ["Alpha", "Beta"]);
  check("work plan: order = plan groups, user group, auto bucket", g.map((x) => x.name).join("|"), "Alpha|Beta|Custom|");
  check("work plan: mirrors share a group", g[0].videos.map((x) => x.url).join(), "v1,v2");
  check("work plan: user group is flagged and empty", g[2].user && g[2].videos.length === 0, true);
  check("work plan: unlabelled script falls into the auto bucket", g[3].scripts.map((x) => x.url).join(), "s3");
  check("work plan: no labels → one auto bucket only", ctx._workPlanGroups(rows, {}, [], []).length, 1);
}

// ── source tag survives the probe swapping the row text for a filename ──
const vidRow = ctx.renderVideoItem(
  { url: "https://e621.net/posts/1234567", label: "e621", source: "OP", priority: 7 }, 0, "v", true);
check("video row carries a source tag", vidRow.includes('<span class="funpairdl-tag-source">e621</span>'), true);
check("source tag sits after the size slot",
  vidRow.indexOf("funpairdl-tag-source") > vidRow.indexOf("funpairdl-size"), true);
const unkRow = ctx.renderVideoItem(
  { url: "https://artist-example.com/work/1", label: "", source: "OP", priority: 11 }, 0, "v", true);
check("unknown host tag falls back to hostname",
  unkRow.includes('<span class="funpairdl-tag-source">artist-example.com</span>'), true);
const scrRow = ctx.renderScriptItem(
  { url: "https://discuss.eroscripts.com/uploads/short-url/x.funscript", filename: "x.funscript", source: "OP" }, 0, "s", true);
check("uploaded script row has no source tag", scrRow.includes("funpairdl-tag-source"), false);
const extRow = ctx.renderScriptItem(
  { url: "https://pixeldrain.com/u/abc", filename: "[External] scripts", source: "OP", isExternal: true }, 0, "s", true);
check("external script row shows its host", extRow.includes('<span class="funpairdl-tag-source">Pixeldrain</span>'), true);

if (failures) {
  console.error(`\n${failures} assertion(s) failed`);
  process.exit(1);
}
console.log("\nAll content.js helper assertions passed");
