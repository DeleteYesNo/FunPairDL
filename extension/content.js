// FunPairDL Content Script for EroScripts (discuss.eroscripts.com)
// Parses posts to extract video + funscript links
// Supports: single-video posts AND multi-video collection posts

// ─── Host gate (audit [2]) ───
// content.js is injected profile-wide in the embedded browser, so it executes
// in every tab (pixeldrain, gofile, mega.nz, the hidden MEGA login page, ...).
// All active behaviour — observers, timers, the relogin poll — must only run
// on EroScripts itself. Pure helper functions stay defined unconditionally so
// tests (tests/content_js_test.mjs) can exercise them; the bootstrap at the
// bottom of the file checks this gate before registering anything. An empty /
// missing hostname (test-harness stubs) is allowed through.
function _funpairdlHostAllowed() {
  try {
    const host = (typeof location !== "undefined" && location.hostname)
      ? String(location.hostname).toLowerCase() : "";
    if (!host) return true;
    return host === "eroscripts.com" || host.endsWith(".eroscripts.com");
  } catch (e) { return true; }
}

// Escape a value for interpolation into an HTML attribute (double quotes
// included — a quote in a filename/URL must not truncate the attribute, or
// dataset reads would send wrong filename/URL mappings to the backend;
// audit [2e]). Safe for text content too.
function escapeAttr(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/"/g, "&quot;")
    .replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Video source priority (lower = higher priority)
const VIDEO_PRIORITY = {
  // File hosters
  "pixeldrain.com": 1,
  "mega.nz": 2,
  "mega.co.nz": 2,
  "gofile.io": 3,
  // Video sites (yt-dlp supported)
  "rule34video.com": 4,
  "rule34.xxx": 4,
  "iwara.tv": 5,
  "hanime1.me": 6,
  "bilibili.com": 7,
  "b23.tv": 7,
  // HMV-specific sites
  "hmvmania.com": 7,
  "socigames.com": 7,
  // Booru animation posts (webm + mp4 transcodes via JSON API)
  "e621.net": 7,
  "e926.net": 7,
  // Adult video sites (yt-dlp supported)
  "pornhub.com": 8,
  "xvideos.com": 8,
  "xnxx.com": 8,
  "xhamster.com": 8,
  "spankbang.com": 8,
  "eporner.com": 8,
  "redtube.com": 8,
  "youporn.com": 8,
  "tube8.com": 8,
  "tnaflix.com": 8,
  // General video sites
  "youtube.com": 9,
  "youtu.be": 9,
  "dailymotion.com": 9,
  "vimeo.com": 9,
  "streamable.com": 9,
  "twitter.com": 10,
  "x.com": 10,
};
const VIDEO_DOMAINS = Object.keys(VIDEO_PRIORITY);

// Known multi-axis suffixes (matches erodeck AXIS_SUFFIX_RE)
const AXIS_SUFFIXES = [
  "twist", "surge", "sway", "roll", "pitch", "vibe", "vibration", "vib",
  "pump", "stroke", "suck", "valve", "lube",
  "L0", "L1", "L2", "L3", "R0", "R1", "R2", "V0", "V1", "V2", "A0", "A1", "A2",
];
const AXIS_SUFFIX_SET = new Set(AXIS_SUFFIXES.map((s) => s.toLowerCase()));
// Spellings of the main (stroke) axis — shown as "main", never as an axis tag.
const AXIS_MAIN_ALIASES = new Set(["l0", "stroke"]);

// ─── Utility functions ───

function isNonVideoPath(url) {
  try {
    const u = new URL(url);
    const host = u.hostname.toLowerCase().replace("www.", "");
    const path = u.pathname.toLowerCase();
    // Twitter/X: only a /status/<id> tweet can embed a video. A bare profile
    // link (x.com/SomeArtist) is just an author credit — counting it as a
    // "video" makes its section look like a video section and wrongly trips
    // collection mode, splitting a single post into per-heading folders.
    if ((host === "x.com" || host === "twitter.com" ||
         host.endsWith(".x.com") || host.endsWith(".twitter.com")) &&
        !path.includes("/status/")) {
      return true;
    }
    return /^\/(members|users|channels?|model|pornstar|profile|account)\b/.test(path);
  } catch (e) { return false; }
}

function isBundleUrl(url) {
  try {
    const u = new URL(url);
    const host = u.hostname.toLowerCase();
    const path = u.pathname;
    if (host.includes("pixeldrain.com") && /^\/(l|d)\//.test(path)) return true;
    if ((host.includes("mega.nz") || host.includes("mega.co.nz")) && path.includes("/folder/")) return true;
    if (host.includes("gofile.io") && /^\/d\//.test(path)) return true;
  } catch (e) {}
  return false;
}

// Headings like "🎥 Video Link", "Video", "Video Download" mark the section
// that holds THE video — even when it lives on a host we don't recognize (an
// artist's own site like artist-example.com, a niche host). The emoji renders as an
// <img>, so only the text ("Video Link") survives in textContent.
const VIDEO_LINK_HEADING_RE = /\bvideo\b/i;

// Hosts that appear *near* a video link but are never the video itself: the
// forum's own infra, author-support, socials, and the ad/affiliate networks
// EroScripts injects. Used to filter unknown-host candidates so we don't offer
// a Patreon/Discord/ad link as a video.
const NON_VIDEO_HOSTS = [
  "eroscripts.com", "discourse.org",
  "patreon.com", "fantia.jp", "subscribestar", "ko-fi.com", "boosty.to",
  "discord.gg", "discord.com", "t.me", "telegram.",
  "linktr.ee",
  // ad / affiliate networks seen in EroScripts posts
  "feeliate.com", "experiencesexonline.com", "synsual.me",
  "ayvasoftware.io", "funosr.com", "funsr.com", "yourhobbiescustomized.com",
  "uptimerobot.com",
];

// A heading that is literally a video filename ("Work_longer.mp4") — how a
// file host's onebox card titles itself when a commenter posts a longer or
// upscaled cut. Its link is the video even though the host is unknown.
const VIDEO_FILE_HEADING_RE = /\.(mp4|mkv|webm|mov|avi|m4v|wmv)\s*$/i;

function _isVideoLinkHeadingText(text) {
  const t = (text || "").trim();
  return VIDEO_LINK_HEADING_RE.test(t) || VIDEO_FILE_HEADING_RE.test(t);
}

// A work name taken from a heading, or "" when the heading is a generic
// label ("Video link", "Downloads") rather than the work's name. A trailing
// video extension is dropped so "Work_longer.mp4" names a folder
// "Work_longer".
function _cleanWorkName(text) {
  const t = (text || "").trim().replace(VIDEO_FILE_HEADING_RE, "").trim();
  if (!t || _isGenericSectionName(t) || VIDEO_LINK_HEADING_RE.test(t)) return "";
  return t;
}

// Name of the work a video link belongs to: its owning heading — the last
// heading before the link, or the onebox <h3> that wraps the link itself.
function _workNameFromHeading(cookedEl, url) {
  let link = null;
  try {
    for (const a of cookedEl.querySelectorAll("a[href]")) {
      if (a.getAttribute("href") !== url) continue;
      // A onebox card links its URL twice: a small source link at the top
      // and the <h3> title. The title IS the work name — take it directly
      // rather than the heading that happens to precede the source link
      // (that is the previous work's).
      const inHeading = a.closest("h1,h2,h3,h4,h5,h6");
      if (inHeading) return _cleanWorkName(inHeading.textContent);
      if (!link) link = a;
    }
  } catch (e) { return ""; }
  if (!link) return "";
  const headings = cookedEl.querySelectorAll("h1,h2,h3,h4,h5,h6");
  for (let i = headings.length - 1; i >= 0; i--) {
    if (headings[i].compareDocumentPosition(link) & Node.DOCUMENT_POSITION_FOLLOWING) {
      return _cleanWorkName(headings[i].textContent);
    }
  }
  return "";
}

// Whether an UNKNOWN-host link may be offered as a video candidate: a real
// off-site page (http[s]), not forum/social/ad infra, not a profile/members
// path, and not itself a script or downloadable asset. yt-dlp's generic
// extractor resolves it from there; the user still confirms the pick in the
// panel, so a stray false positive is harmless.
function _isOfferableVideoHost(href) {
  let u;
  try { u = new URL(href); } catch (e) { return false; }
  if (u.protocol !== "http:" && u.protocol !== "https:") return false;
  const host = u.hostname.toLowerCase().replace("www.", "");
  if (VIDEO_DOMAINS.some((d) => host.includes(d))) return false;  // known host: handled elsewhere
  if (NON_VIDEO_HOSTS.some((d) => host.includes(d))) return false;
  if (isNonVideoPath(href)) return false;
  if (/\.(funscript|zip|rar|7z|png|jpe?g|gif|webp|svg|css|js|avif|mp3)$/i.test(u.pathname)) return false;
  return true;
}

// Collect links sitting under a "Video Link"-type heading that live on a host
// we don't recognize. They're almost always the real video — they're under an
// explicit video heading, not in the signature/ad area — so offer them as
// low-priority candidates (every known host outranks them).
function _extractHeadingScopedVideos(containerEl, isOP) {
  const out = [];
  let headings, links;
  try { headings = Array.from(containerEl.querySelectorAll("h1,h2,h3,h4,h5,h6")); }
  catch (e) { return out; }
  if (!headings.length || !headings.some((h) => _isVideoLinkHeadingText(h.textContent))) {
    return out;
  }
  try { links = Array.from(containerEl.querySelectorAll("a[href]")); }
  catch (e) { return out; }
  for (const a of links) {
    const href = a.getAttribute("href");
    if (!href || !_isOfferableVideoHost(href)) continue;
    // Owner heading = the last heading that precedes this link in the
    // document. Scan in reverse and stop at the first hit (audit [2a] —
    // the forward scan paid O(headings) compareDocumentPosition per link).
    let owner = null;
    for (let i = headings.length - 1; i >= 0; i--) {
      if (headings[i].compareDocumentPosition(a) & Node.DOCUMENT_POSITION_FOLLOWING) {
        owner = headings[i];
        break;
      }
    }
    if (!owner || !_isVideoLinkHeadingText(owner.textContent)) continue;
    let label = "Link";
    try { label = new URL(href).hostname.replace("www.", ""); } catch (e) {}
    out.push({
      url: href,
      priority: isOP ? 11 : 11.5,   // below every VIDEO_PRIORITY entry
      source: isOP ? "OP" : "comment",
      label,
      isBundle: isBundleUrl(href),
      unknownHost: true,
    });
  }
  return out;
}

function getVideoPriority(url, isFromComment) {
  try {
    const host = new URL(url).hostname.toLowerCase().replace("www.", "");
    for (const [domain, priority] of Object.entries(VIDEO_PRIORITY)) {
      if (host.includes(domain)) return isFromComment ? priority + 0.5 : priority;
    }
  } catch (e) {}
  return isFromComment ? 3.5 : 3;
}

function getVideoLabel(url) {
  try {
    const host = new URL(url).hostname.toLowerCase().replace("www.", "");
    if (host.includes("pixeldrain")) return "Pixeldrain";
    if (host.includes("mega")) return "MEGA";
    if (host.includes("gofile")) return "GoFile";
    if (host.includes("rule34video")) return "Rule34Video";
    if (host.includes("rule34")) return "Rule34";
    if (host.includes("iwara")) return "Iwara";
    if (host.includes("hanime")) return "Hanime1";
    if (host.includes("hmvmania")) return "HMV Mania";
    if (host.includes("socigames")) return "SociGames";
    if (host.includes("e621") || host.includes("e926")) return "e621";
    if (host.includes("pornhub")) return "PornHub";
    if (host.includes("xvideos")) return "XVideos";
    if (host.includes("xnxx")) return "XNXX";
    if (host.includes("xhamster")) return "xHamster";
    if (host.includes("spankbang")) return "SpankBang";
    if (host.includes("eporner")) return "ePorner";
    if (host.includes("redtube")) return "RedTube";
    if (host.includes("youporn")) return "YouPorn";
    if (host.includes("youtube") || host.includes("youtu.be")) return "YouTube";
    if (host.includes("dailymotion")) return "Dailymotion";
    if (host.includes("vimeo")) return "Vimeo";
    if (host.includes("twitter") || host.includes("x.com")) return "Twitter/X";
    // Fallback: extract domain name and capitalize
    const parts = host.split(".");
    return parts.length >= 2 ? parts[parts.length - 2].charAt(0).toUpperCase() + parts[parts.length - 2].slice(1) : host;
  } catch (e) { return "Direct"; }
}

// Mirrors the backend's _parse_axis: scan the dot-components before
// ".funscript" right→left for a known axis ("X.raw.pitch" → pitch,
// "X.L0.max" → main). Any other word (".raw", ".final", ".suckManual") is
// part of the name, not an axis — the backend files such a script as the
// main (L0) script, so the panel must say "main" too. The old "any
// .word.funscript is an axis" fallback labelled "Title.raw.funscript" as a
// "raw" axis and left the pair with no main script in the UI.
function detectAxis(filename) {
  const stem = (filename || "").trim().replace(/\.funscript$/i, "");
  const parts = stem.split(".");
  for (let i = parts.length - 1; i >= 1; i--) {
    const p = parts[i].trim().toLowerCase();
    if (!AXIS_SUFFIX_SET.has(p)) continue;
    return AXIS_MAIN_ALIASES.has(p) ? "main" : p;
  }
  // A word axis with a qualifier glued on (".suckManual", ".twist_v2") is
  // still that axis — the backend files it under the axis, keeping the
  // full component as the suffix. Shown with the scripter's own spelling.
  for (let i = parts.length - 1; i >= 1; i--) {
    const axis = _axisFromPrefixed(parts[i].trim());
    if (axis) return axis;
  }
  return "main";
}

// "suckManual" → "suckManual" (an axis label), "rolling" → "" (not one): a
// known word axis (3+ letters, not the L0/R1 codes) followed by a qualifier
// starting with an uppercase letter, digit or separator.
function _axisFromPrefixed(part) {
  const low = part.toLowerCase();
  for (const word of AXIS_SUFFIX_SET) {
    if (word.length < 3 || !/^[a-z]+$/.test(word)) continue;
    if (low.length > word.length && low.startsWith(word)) {
      const rest = part.slice(word.length);
      if (/^[A-Z0-9_\- ]/.test(rest)) return AXIS_MAIN_ALIASES.has(word) ? "main" : part;
    }
  }
  return "";
}

/**
 * Detect the script author from DOM context around a funscript link.
 * Walks backwards from the link through preceding siblings to find the
 * nearest @mention or "AuthorName:" text. Works even when multiple authors
 * share the same <p> element.
 */
function detectScriptAuthor(scriptLink) {
  // Navigate to container level (browsers may restructure nested <a> tags)
  let targetEl = scriptLink;
  const container = scriptLink.closest("a.funscript-link-container");
  if (container) targetEl = container;

  const p = targetEl.closest("p");
  if (!p) return null;

  // Walk backwards through preceding siblings within the <p>
  let node = targetEl.previousSibling;
  while (node) {
    if (node.nodeType === Node.ELEMENT_NODE) {
      if (node.classList?.contains("mention"))
        return node.textContent.trim().replace(/^@/, "");
    }
    if (node.nodeType === Node.TEXT_NODE) {
      const text = node.textContent.trim();
      if (text) {
        // Match "AuthorName:" — only alphanumeric+underscore+hyphen, no spaces
        const match = text.match(/^([A-Za-z0-9_\-]{2,30})\s*[:：]/);
        if (match) return match[1].trim();
      }
    }
    node = node.previousSibling;
  }

  return null;
}

function getTopicTitle() {
  const titleEl = document.querySelector("#topic-title .fancy-title");
  if (titleEl) return titleEl.textContent.trim();
  const h1 = document.querySelector("h1");
  if (h1) return h1.textContent.trim();
  return document.title.replace(" - Scripts / Free Scripts - EroScripts", "").trim();
}

// Seconds → "m:ss" / "h:mm:ss"; "" when unknown.
function formatDuration(sec) {
  const s = Math.round(Number(sec) || 0);
  if (s <= 0) return "";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`
    : `${m}:${String(r).padStart(2, "0")}`;
}

// Why a script sits with a video (see plan_bundle_split's basis): label +
// colour. Green = certain, blue = inferred, orange = a positional guess.
const _BASIS_LABEL = {
  plan: ["手動", "#2e9e6a"],
  name: ["名稱", "#2e9e6a"],
  link: ["腳本註記", "#2e9e6a"],
  tokens: ["關鍵字/tags", "#4a90d9"],
  duration: ["時長", "#4a90d9"],
  order: ["順序(猜測)", "#c2842a"],
  none: ["未配對", "#666"],
};
const _BASIS_RANK = { none: 0, order: 1, duration: 2, tokens: 3, link: 4, name: 5, plan: 6 };

function _basisTagHTML(basis) {
  const e = _BASIS_LABEL[basis];
  if (!e) return "";
  return `<span class="funpairdl-tag-basis" style="background:${e[1]}" title="這組配對的依據">${e[0]}</span>`;
}

// The weakest basis among a group's scripts is the group's.
function _weakestBasis(list) {
  let best = "";
  for (const b of list || []) {
    if (!b) continue;
    if (!best || (_BASIS_RANK[b] ?? 0) < (_BASIS_RANK[best] ?? 0)) best = b;
  }
  return best;
}

function formatSize(bytes) {
  if (!bytes || bytes <= 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

// A Discourse funscript-attachment anchor renders as
//   "<download-glyph>sample-rope-demo-work.funscript (27.2 KB)"
// so the raw textContent carries a leading icon char and a trailing
// human-readable size. Both must be stripped or they end up baked into
// the filename (and the work title), e.g. the junk folder
// "sample-rope-demo-work.funscript_ (27.2 KB)". Reduce to the real name.
function cleanScriptName(raw, fullUrl) {
  let t = (raw || "").trim();
  // Trailing size annotation: " (27.2 KB)", "(1.3 MB)", " ( 800 B )"
  t = t.replace(/\s*\(\s*\d+(?:\.\d+)?\s*[KMGT]?B\s*\)\s*$/i, "").trim();
  // Leading non-filename glyphs (download icons, arrows, stray "?")
  t = t.replace(/^[^\p{L}\p{N}\[\(（【]+/u, "");
  // Drop any trailing junk after the script extension
  const m = t.match(/\.(?:fun|sync)script/i);
  if (m) t = t.slice(0, m.index + m[0].length);
  t = t.trim();
  return t || (fullUrl.split("/").pop() || "");
}

// ─── Link extraction (works on any container element) ───

function extractLinksFromElement(containerEl, isOP) {
  const videos = [];
  const scripts = [];

  // Video: embedded <video> tags
  containerEl.querySelectorAll("video source[src]").forEach((source) => {
    const src = source.getAttribute("src");
    if (src && !src.startsWith("blob:")) {
      videos.push({
        url: src, priority: getVideoPriority(src, !isOP),
        source: isOP ? "OP" : "comment", label: getVideoLabel(src),
        isBundle: isBundleUrl(src),
      });
    }
  });

  // Domains to ignore (not video sources)
  const SKIP_DOMAINS = [
    "eroscripts.com", "discord.gg", "discord.com", "patreon.com",
    "ko-fi.com", "buymeacoffee.com", "paypal.com", "gumroad.com",
    "funscript.org", "github.com", "reddit.com", "wikipedia.org",
    "google.com", "facebook.com", "instagram.com", "amazon.com",
    "theverge.com", "clearview.ai", "proton.me", "shop.funosr.com",
    "yourhobbiescustomized.com",
  ];

  // Video: links to known hosts + unknown external sites (potential yt-dlp sources)
  containerEl.querySelectorAll("a[href]").forEach((link) => {
    const href = link.getAttribute("href");
    if (!href || href.startsWith("blob:") || href.startsWith("#")) return;
    if (href.includes("discuss.eroscripts.com") && !href.includes(".funscript")) return;
    if (href.endsWith(".funscript")) return;
    try {
      const u = new URL(href);
      const host = u.hostname.toLowerCase().replace("www.", "");
      const isKnown = VIDEO_DOMAINS.some((d) => host.includes(d));
      if (isKnown && !isNonVideoPath(href)) {
        const metaFn = _linkMetaFilename(href);
        if (_isScriptFilename(metaFn)) {
          // Funscript hosted on a file-locker (pixeldrain/mega/...) — classify
          // as a script with its real filename so it pairs with the video
          // instead of becoming a stray "video" in its own group.
          if (!scripts.some((s) => s.url === href)) {
            const axis = detectAxis(metaFn);
            scripts.push({
              url: href, source: isOP ? "OP" : "comment",
              filename: metaFn, axis, isMultiAxis: axis !== "main",
              author: detectScriptAuthor(link),
            });
          }
        } else if (!videos.some((v) => v.url === href)) {
          videos.push({
            url: href, priority: getVideoPriority(href, !isOP),
            source: isOP ? "OP" : "comment", label: getVideoLabel(href),
            isBundle: isBundleUrl(href),
          });
        }
      } else if (!SKIP_DOMAINS.some((d) => host.includes(d))) {
        // Unknown external link — detect if URL or link text suggests a video page
        const path = u.pathname.toLowerCase();
        const hasVideoPath = /\/(video|watch|view_video|embed|play|clip|videos)/.test(path);
        const linkText = (link.textContent || "").toLowerCase();
        const textHintsVideo = /video|watch|stream|movie|porn|hentai|anime/.test(linkText);
        if (hasVideoPath || textHintsVideo) {
          if (!videos.some((v) => v.url === href)) {
            videos.push({
              url: href, priority: isOP ? 15 : 20,
              source: isOP ? "OP" : "comment", label: getVideoLabel(href),
              isBundle: false,
            });
          }
        }
      }
    } catch (e) {}
  });

  // Video: URLs inside <code> tags (some posters wrap MEGA/GoFile links in code blocks)
  containerEl.querySelectorAll("code").forEach((codeEl) => {
    const text = codeEl.textContent.trim();
    if (!text.startsWith("http")) return;
    try {
      const host = new URL(text).hostname.toLowerCase().replace("www.", "");
      if (VIDEO_DOMAINS.some((d) => host.includes(d))) {
        if (!videos.some((v) => v.url === text)) {
          videos.push({
            url: text, priority: getVideoPriority(text, !isOP),
            source: isOP ? "OP" : "comment", label: getVideoLabel(text),
            isBundle: isBundleUrl(text),
          });
        }
      }
    } catch (e) {}
  });

  // Scripts: .funscript-link-container
  containerEl.querySelectorAll('a.funscript-link-container[href*=".funscript"]').forEach((link) => {
    const href = link.getAttribute("href");
    if (href && !href.startsWith("blob:") && href.includes(".funscript")) {
      const fullUrl = href.startsWith("http") ? href : `https://discuss.eroscripts.com${href}`;
      const nameEl = link.querySelector("a") || link;
      const fname = cleanScriptName(nameEl.textContent, fullUrl);
      const axis = detectAxis(fname);
      const author = detectScriptAuthor(link);
      scripts.push({
        url: fullUrl, source: isOP ? "OP" : "comment",
        filename: fname, axis, isMultiAxis: axis !== "main",
        author: author,
      });
    }
  });

  // Scripts: fallback direct .funscript links
  if (scripts.length === 0) {
    containerEl.querySelectorAll('a[href$=".funscript"]').forEach((link) => {
      const href = link.getAttribute("href");
      if (href && !href.startsWith("blob:")) {
        const fullUrl = href.startsWith("http") ? href : `https://discuss.eroscripts.com${href}`;
        if (!scripts.some((s) => s.url === fullUrl)) {
          const fname = cleanScriptName(link.textContent, fullUrl);
          const axis = detectAxis(fname);
          const author = detectScriptAuthor(link);
          scripts.push({
            url: fullUrl, source: isOP ? "OP" : "comment",
            filename: fname, axis, isMultiAxis: axis !== "main",
            author: author,
          });
        }
      }
    });
  }

  // Scripts: external hosting links mentioning "script"
  containerEl.querySelectorAll("a[href]").forEach((link) => {
    const href = link.getAttribute("href");
    if (!href) return;
    const text = (link.textContent || "").toLowerCase();
    if (
      (text.includes("multi-axis") || text.includes("multi axis") ||
       text.includes("funscript") || text.includes("script")) &&
      !href.includes(".funscript") && !href.startsWith("blob:") && !href.startsWith("#")
    ) {
      try {
        const host = new URL(href).hostname.toLowerCase();
        if (host.includes("mega.nz") || host.includes("pixeldrain.com") ||
            host.includes("gofile.io") || host.includes("drive.google.com")) {
          scripts.push({
            url: href, source: isOP ? "OP" : "comment",
            filename: `[External] ${link.textContent.trim()}`,
            isMultiAxis: true, isExternal: true,
          });
        }
      } catch (e) {}
    }
  });

  // Unknown-host videos under an explicit "Video Link" heading (artist sites
  // etc.). Added last + low priority so known hosts always win; deduped.
  for (const v of _extractHeadingScopedVideos(containerEl, isOP)) {
    if (!videos.some((x) => x.url === v.url)) videos.push(v);
  }

  return { videos, scripts };
}

// ─── Section-based OP parsing (for multi-video collection posts) ───

/**
 * Check if a heading is just a formatted video link (Discourse renders bare
 * MEGA/Pixeldrain/etc. links on their own line as H3 "onebox" elements).
 * These should NOT be treated as section headings.
 */
function isVideoLinkHeading(heading) {
  const links = heading.querySelectorAll("a[href]:not(.anchor)");
  for (const link of links) {
    try {
      const host = new URL(link.href).hostname.toLowerCase().replace("www.", "");
      if (VIDEO_DOMAINS.some((d) => host.includes(d))) return true;
    } catch (e) {}
  }
  return false;
}

/**
 * Split the OP's .cooked element into heading-delimited sections.
 * Uses DOM position to associate links with their preceding heading,
 * regardless of nesting depth. Returns array of { name, videos[], scripts[] }.
 */
function parseOPSections(cookedEl) {
  // 1. Find all headings at any depth (skip those inside details/table/aside)
  const headings = [];
  const walker = document.createTreeWalker(cookedEl, NodeFilter.SHOW_ELEMENT);
  let node;
  while ((node = walker.nextNode())) {
    if (/^H[1-4]$/i.test(node.tagName)) {
      const nested = node.closest("details, table, aside, blockquote");
      if (nested && cookedEl.contains(nested) && nested !== cookedEl) continue;
      // Skip headings that are just formatted video links (e.g. MEGA links as H3)
      if (isVideoLinkHeading(node)) continue;
      headings.push(node);
    }
  }

  if (headings.length < 2) return [];

  // 2. Build section stubs
  const sectionMap = headings.map((h) => ({
    _heading: h,
    name: h.textContent.trim(),
    videos: [],
    scripts: [],
  }));

  // 3. Helper: find which section an element belongs to (last heading before it)
  function findSection(el) {
    for (let i = sectionMap.length - 1; i >= 0; i--) {
      const pos = sectionMap[i]._heading.compareDocumentPosition(el);
      if (pos & Node.DOCUMENT_POSITION_FOLLOWING) return sectionMap[i];
    }
    return null;
  }

  // 4. Assign video links to sections
  cookedEl.querySelectorAll("a[href]").forEach((link) => {
    const href = link.getAttribute("href");
    if (!href || href.startsWith("blob:") || href.startsWith("#")) return;
    if (href.includes("discuss.eroscripts.com") && !href.includes(".funscript")) return;
    try {
      const host = new URL(href).hostname.toLowerCase().replace("www.", "");
      if (VIDEO_DOMAINS.some((d) => host.includes(d)) && !isNonVideoPath(href)) {
        const sec = findSection(link);
        if (!sec) return;
        const metaFn = _linkMetaFilename(href);
        if (_isScriptFilename(metaFn)) {
          // A funscript hosted on a file-locker — count it as a script so it
          // doesn't make its section look like a "video section" and wrongly
          // trip collection mode.
          if (!sec.scripts.some((s) => s.url === href)) {
            const axis = detectAxis(metaFn);
            sec.scripts.push({
              url: href, source: "OP", filename: metaFn, axis,
              isMultiAxis: axis !== "main", author: detectScriptAuthor(link),
            });
          }
        } else if (!sec.videos.some((v) => v.url === href)) {
          sec.videos.push({
            url: href, priority: getVideoPriority(href, false),
            source: "OP", label: getVideoLabel(href), isBundle: isBundleUrl(href),
          });
        }
      }
    } catch (e) {}
  });

  // 4b. Assign video URLs inside <code> tags to sections
  cookedEl.querySelectorAll("code").forEach((codeEl) => {
    const text = codeEl.textContent.trim();
    if (!text.startsWith("http")) return;
    try {
      const host = new URL(text).hostname.toLowerCase().replace("www.", "");
      if (VIDEO_DOMAINS.some((d) => host.includes(d)) && !isNonVideoPath(text)) {
        const sec = findSection(codeEl);
        if (sec && !sec.videos.some((v) => v.url === text)) {
          sec.videos.push({
            url: text, priority: getVideoPriority(text, false),
            source: "OP", label: getVideoLabel(text), isBundle: isBundleUrl(text),
          });
        }
      }
    } catch (e) {}
  });

  // 5. Assign script links to sections
  cookedEl.querySelectorAll('a.funscript-link-container[href*=".funscript"], a[href$=".funscript"]').forEach((link) => {
    const href = link.getAttribute("href");
    if (!href || href.startsWith("blob:")) return;
    const fullUrl = href.startsWith("http") ? href : `https://discuss.eroscripts.com${href}`;
    const sec = findSection(link);
    if (sec && !sec.scripts.some((s) => s.url === fullUrl)) {
      const nameEl = link.querySelector("a") || link;
      const fname = cleanScriptName(nameEl.textContent, fullUrl);
      const axis = detectAxis(fname);
      const author = detectScriptAuthor(link);
      sec.scripts.push({
        url: fullUrl, source: "OP", filename: fname, axis, isMultiAxis: axis !== "main",
        author: author,
      });
    }
  });

  // 6. Clean up and return only sections with content
  return sectionMap
    .filter((s) => s.videos.length > 0 || s.scripts.length > 0)
    .map((s) => ({ name: s.name, videos: s.videos, scripts: s.scripts }));
}

// ─── Cloaked post recovery ───

function _getPreloadedPosts() {
  // Discourse stores all initial posts in a preloaded JSON blob.
  // Cloaked (lazy-loaded) posts aren't in the DOM but their content is here.
  try {
    const el = document.querySelector("#data-preloaded");
    if (!el || !el.dataset.preloaded) return [];
    const preloaded = JSON.parse(el.dataset.preloaded);
    for (const [key, value] of Object.entries(preloaded)) {
      if (!key.startsWith("topic_")) continue;
      const topicData = JSON.parse(value);
      return (topicData.post_stream?.posts || []).map((p) => ({
        postNumber: p.post_number,
        cooked: p.cooked || "",
        username: p.username || "",
      }));
    }
  } catch (e) {
    console.warn("FunPairDL: Failed to parse preloaded posts:", e);
  }
  return [];
}

// Discourse embeds per-post link metadata (`link_counts`) in the preloaded
// topic JSON, including each link's resolved title — e.g. a pixeldrain link's
// title is "Script Sub 64_2026.funscript ~ pixeldrain". This is the only
// reliable way to tell, at parse time (before any probe), that a funscript is
// hosted on a file-locker (pixeldrain/mega/gofile) rather than uploaded as a
// .funscript. Without it such a link looks identical to a video link and both
// gets miscounted as a "video" (breaking section/collection detection) and
// misclassified downstream.
let LINK_FILENAME_MAP = null;
let _METADATA_TOPIC_ID = null;  // topic id the current map was fetched for

function _mapFromPosts(posts, map) {
  for (const p of (posts || [])) {
    for (const lc of (p.link_counts || [])) {
      // title is "<filename> ~ <host>" for file hosts; strip the host part.
      if (lc && lc.url && lc.title) {
        const fn = lc.title.split(" ~ ")[0].trim();
        if (fn) map[lc.url] = fn;
      }
    }
  }
  return map;
}

// SSR fallback: the `#data-preloaded` blob holds the topic's posts ONLY on a
// direct full-page load. After SPA navigation it carries the previous page
// (e.g. the category listing) instead, so this often comes back empty — the
// authoritative source is ensureLinkMetadata()'s fetch.
function _buildLinkFilenameMap() {
  const map = {};
  try {
    const el = document.querySelector("#data-preloaded");
    if (el && el.dataset.preloaded) {
      const preloaded = JSON.parse(el.dataset.preloaded);
      for (const [key, value] of Object.entries(preloaded)) {
        if (!key.startsWith("topic_")) continue;
        _mapFromPosts(JSON.parse(value).post_stream?.posts, map);
        break;
      }
    }
  } catch (e) {
    console.warn("FunPairDL: link metadata parse failed:", e);
  }
  return map;
}

function _currentTopicId() {
  const m = location.pathname.match(/\/t\/[^/]+\/(\d+)/);
  return m ? m[1] : null;
}

// Fetch the topic JSON to get every link's resolved title (filename). Reliable
// regardless of how the topic was reached (full load vs SPA). Cached per topic.
async function ensureLinkMetadata() {
  const tid = _currentTopicId();
  if (!tid) return;
  if (_METADATA_TOPIC_ID === tid && LINK_FILENAME_MAP) return;
  try {
    const resp = await fetch(`/t/${tid}.json`, { credentials: "include" });
    if (!resp.ok) return;
    const data = await resp.json();
    LINK_FILENAME_MAP = _mapFromPosts(data.post_stream?.posts, {});
    _METADATA_TOPIC_ID = tid;
  } catch (e) {
    console.warn("FunPairDL: topic metadata fetch failed:", e);
  }
}

function _linkMetaFilename(url) {
  if (LINK_FILENAME_MAP === null) LINK_FILENAME_MAP = _buildLinkFilenameMap();
  return LINK_FILENAME_MAP[url] || "";
}

function _isScriptFilename(fn) {
  return /\.funscript$/i.test((fn || "").trim());
}

// True when a section heading is a generic container label ("Downloads",
// "Video link", "1080p", "Remake", …) rather than the work's real name —
// such sections should borrow the topic title instead of naming a folder
// after the heading. Leading/trailing decorative characters that posters add
// (༺ ༻ ─ ✧ emoji …) are stripped first so "༺Downloads" still reads generic.
function _isGenericSectionName(name) {
  const sname = (name || "").trim()
    .replace(/^[^\p{L}\p{N}]+/u, "").replace(/[^\p{L}\p{N}]+$/u, "")
    .replace(/:+$/, "").replace(/\s*\(.*\)\s*$/, "").trim();
  return (
    /^(videos?|funscripts?|scripts?|downloads?|direct\s*downloads?|links?|files?|media|mega|gofile|pixeldrain|dropbox|google\s*drive|onedrive|mirror|aio|drives?|sources?|embeds?|embedded|streams?|streaming|host(?:ing|ed)?|cloud|storage|bundles?|packs?|collections?|all[\s-]*in[\s-]*one|free|paid|premium|previews?)\s*\d*\b/i.test(sname) ||
    /^\d{3,4}p?$/i.test(sname) || /^[248]k$/i.test(sname) ||
    /^(remake|remade|original|remaster(?:ed)?|updated?|re-?script(?:ed)?|v\d+|version\s*\d*|alt(?:ernate|ernative)?)\b/i.test(sname)
  );
}

// ─── Collection mode: hand orphan scripts to the video section they name ───
//
// A common OP layout is one heading per work (each with its video link) and
// then ONE generic "Script"/"Downloads" heading holding every funscript.
// Section parsing faithfully yields N video-only sections plus a script-only
// section, which would send N videos without scripts and a pile of scripts
// without a video. Scripts are named after the work, so match each one to
// the section whose heading it contains and move it there.

// Encoding noise that appears in filenames but never identifies a work.
const _NAME_NOISE_RE =
  /(?<![a-z0-9])(?:\d{3,4}p|[248]k|\d{1,3}\s?fps|h\.?26[45]|x26[45]|hevc|av1|no[-_ ]?wm|wm)(?![a-z0-9])/g;

function _squashName(s) {
  return (s || "").toLowerCase().replace(_NAME_NOISE_RE, " ").replace(/[^a-z0-9]+/g, "");
}

function _nameTokens(s) {
  return new Set(
    (s || "").toLowerCase().replace(_NAME_NOISE_RE, " ")
      .split(/[^a-z0-9]+/).filter((t) => t.length >= 3));
}

// Squashed forms of a heading and of each "A / B", "A | B", "A - B",
// "A [B]" part of it. A script whose squashed stem contains one of these
// belongs to that heading.
function _sectionNameKeys(name) {
  const keys = new Set();
  const parts = (name || "").split(/\s*(?:\/|\||—|–|:|[\[\]()])\s*|\s+-\s+/);
  for (const p of parts) {
    const sq = _squashName(p);
    if (sq.length >= 4) keys.add(sq);
  }
  const whole = _squashName(name);
  if (whole.length >= 4) keys.add(whole);
  return keys;
}

// Pure: returns the sections with every script from a generic-named,
// video-less section moved into the video section it names. Scripts that
// name no section (or name several equally) stay put; an emptied donor
// section is dropped. Scoring: a heading key found inside the script name
// wins by key length; else a distinctive (unique-to-one-section, 4+ char)
// heading word equal to a script word. Keys/words shared by 2+ video
// sections (series name, author) never decide.
function _distributeOrphanScripts(sections) {
  const videoSecs = sections.filter((s) => s.videos.length > 0);
  if (videoSecs.length < 2) return sections;
  const donors = sections.filter(
    (s) => s.videos.length === 0 && s.scripts.length > 0 && _isGenericSectionName(s.name));
  if (donors.length === 0) return sections;

  const keyDf = {};
  const tokDf = {};
  const info = videoSecs.map((s) => {
    const keys = _sectionNameKeys(s.name);
    const toks = _nameTokens(s.name);
    for (const k of keys) keyDf[k] = (keyDf[k] || 0) + 1;
    for (const t of toks) tokDf[t] = (tokDf[t] || 0) + 1;
    return { s, keys, toks };
  });

  function _match(filename) {
    const stem = (filename || "").replace(/\.funscript$/i, "");
    const sq = _squashName(stem);
    const toks = _nameTokens(stem);
    let best = null, bestScore = 0, tie = false;
    for (const { s, keys, toks: stoks } of info) {
      let score = 0;
      for (const k of keys) {
        if (keyDf[k] === 1 && sq.includes(k)) score = Math.max(score, k.length);
      }
      if (!score) {
        for (const t of stoks) {
          if (t.length >= 4 && tokDf[t] === 1 && toks.has(t)) score = Math.max(score, t.length);
        }
      }
      if (!score) continue;
      if (score > bestScore) { best = s; bestScore = score; tie = false; }
      else if (score === bestScore) tie = true;
    }
    return tie ? null : best;
  }

  for (const d of donors) {
    const keep = [];
    for (const sc of d.scripts) {
      const target = _match(sc.filename);
      if (!target) { keep.push(sc); continue; }
      if (!target.scripts.some((x) => x.url === sc.url)) target.scripts.push(sc);
    }
    d.scripts = keep;
  }
  return sections.filter((s) => s.videos.length > 0 || s.scripts.length > 0);
}

// ─── Per-post in-DOM pairing helpers (for auto-grouping) ───

/**
 * Index every element under `root` in document order so we can compute
 * the "distance" between any two links by ordinal — used to match each
 * script to its nearest video within a comment that contains multiple
 * videos.
 */
function _buildElementIndex(root) {
  const index = new Map();
  let i = 0;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
  let node = walker.currentNode;
  while ((node = walker.nextNode())) {
    index.set(node, i++);
  }
  return index;
}

/**
 * Build a lookup of URL → document-order ordinal for every `<a href>` and
 * raw-URL `<code>` under `cookedEl`. Built ONCE per post and passed to
 * `_urlOrdinal` — the old per-URL querySelectorAll pass was O(items × links)
 * (audit [2a]). Links are indexed before codes and first occurrence wins,
 * matching the old lookup precedence.
 */
function _buildUrlOrdinalMap(cookedEl, elIndex) {
  const map = new Map();
  cookedEl.querySelectorAll("a[href]").forEach((link) => {
    const href = link.getAttribute("href");
    if (!href || map.has(href)) return;
    const ord = elIndex.get(link);
    if (ord === undefined) return;
    map.set(href, ord);
    // Uploaded scripts are extracted with an absolute URL while the DOM
    // href is site-relative ("/uploads/short-url/…"); without this alias
    // every script looked unplaceable and all fell to the first video.
    if (href.startsWith("/")) {
      const abs = `https://discuss.eroscripts.com${href}`;
      if (!map.has(abs)) map.set(abs, ord);
    }
  });
  cookedEl.querySelectorAll("code").forEach((code) => {
    const text = code.textContent.trim();
    if (!text || map.has(text)) return;
    const ord = elIndex.get(code);
    if (ord !== undefined) map.set(text, ord);
  });
  return map;
}

/**
 * Ordinal position of the element representing `url`, from the prebuilt
 * per-post map (see _buildUrlOrdinalMap). Returns Infinity if not found.
 */
function _urlOrdinal(ordMap, url) {
  const ord = ordMap.get(url);
  return ord === undefined ? Infinity : ord;
}

/**
 * Within one post, decide how videos and scripts pair up:
 *   - No videos:     one sub-group with all scripts (orphan-script comment).
 *   - One video:     one sub-group with everything (the common case).
 *   - Many videos:   one sub-group per video, scripts attached to whichever
 *                    video they sit closest to in the DOM. Orphans go to
 *                    the first sub-group.
 */
function _pairWithinPost(cookedEl, videos, scripts) {
  // Each sub-group carries the work name its video's heading gives it ("" when
  // there is none) — collection mode shows comment sub-groups as sections.
  if (videos.length === 0) {
    return scripts.length > 0 ? [{ name: "", videos: [], scripts }] : [];
  }
  if (videos.length === 1) {
    return [{ name: _workNameFromHeading(cookedEl, videos[0].url), videos, scripts }];
  }

  const elIndex = _buildElementIndex(cookedEl);
  const ordMap = _buildUrlOrdinalMap(cookedEl, elIndex);
  const videoOrds = videos.map((v) => _urlOrdinal(ordMap, v.url));
  const scriptOrds = scripts.map((s) => _urlOrdinal(ordMap, s.url));
  const buckets = videos.map(() => []);

  // Posts lay works out consistently: "video, its script, next video, its
  // script…" or the reverse. A video's ordinal is its first link — for a
  // onebox card that is the card's top — so the script after a card sits
  // nearer the NEXT card's top than its own card's; plain nearest-ordinal
  // would pair every script with the following video. Decide the layout
  // from whichever comes first, then walk in that direction.
  const firstV = Math.min(...videoOrds.filter(Number.isFinite), Infinity);
  const firstS = Math.min(...scriptOrds.filter(Number.isFinite), Infinity);
  const videoFirst = firstV <= firstS;

  function _nearest(sOrd) {
    let best = 0;
    let bestDist = Math.abs(sOrd - videoOrds[0]);
    for (let i = 1; i < videoOrds.length; i++) {
      const d = Math.abs(sOrd - videoOrds[i]);
      if (d < bestDist) { bestDist = d; best = i; }
    }
    return best;
  }

  scripts.forEach((s, si) => {
    const sOrd = scriptOrds[si];
    let pick = -1;
    if (Number.isFinite(sOrd)) {
      if (videoFirst) {
        // Last video above the script.
        for (let i = 0; i < videoOrds.length; i++) {
          if (Number.isFinite(videoOrds[i]) && videoOrds[i] <= sOrd) pick = i;
        }
      } else {
        // First video below the script.
        for (let i = 0; i < videoOrds.length; i++) {
          if (Number.isFinite(videoOrds[i]) && videoOrds[i] >= sOrd) { pick = i; break; }
        }
      }
    }
    if (pick < 0) pick = _nearest(sOrd);
    buckets[pick].push(s);
  });
  return videos.map((v, i) => ({
    name: _workNameFromHeading(cookedEl, v.url), videos: [v], scripts: buckets[i],
  }));
}

// Collection mode: each comment post's in-post pairing becomes its own
// section ("#7 @user"), so a commenter's alternate cuts are offered as
// separate works instead of one flat Comments pile. Entries are indices into
// the deduped comment arrays — the row keys "cv-N"/"cs-N" are those indices
// — and a URL already claimed by an earlier group is not listed twice.
function _buildCommentGroups(perPost, commentVideos, commentScripts) {
  const vIdx = new Map(commentVideos.map((v, i) => [v.url, i]));
  const sIdx = new Map(commentScripts.map((s, i) => [s.url, i]));
  const usedV = new Set();
  const usedS = new Set();
  const groups = [];
  for (const p of perPost) {
    if (p.isOP) continue;
    for (const sg of p.subGroups) {
      const videos = [];
      const scripts = [];
      for (const v of sg.videos) {
        const i = vIdx.get(v.url);
        if (i !== undefined && !usedV.has(i)) { usedV.add(i); videos.push(i); }
      }
      for (const s of sg.scripts) {
        const i = sIdx.get(s.url);
        if (i !== undefined && !usedS.has(i)) { usedS.add(i); scripts.push(i); }
      }
      if (videos.length === 0 && scripts.length === 0) continue;
      groups.push({
        id: `c${groups.length}`,
        name: sg.name || "",
        label: `#${p.postNumber}${p.username ? ` @${p.username}` : ""}`,
        videos, scripts,
      });
    }
  }
  // Defensive: anything no post claimed still gets a row.
  const restV = commentVideos.map((_, i) => i).filter((i) => !usedV.has(i));
  const restS = commentScripts.map((_, i) => i).filter((i) => !usedS.has(i));
  if (restV.length || restS.length) {
    groups.push({ id: `c${groups.length}`, name: "", label: "Comments", videos: restV, scripts: restS });
  }
  return groups;
}

// ─── Main parser ───

// Cloaked (lazy-loaded) posts aren't in the DOM; recovering one costs a full
// innerHTML parse + link extraction. The content is static for a given topic,
// so cache the parse per post id — parseAllPosts runs on page load AND again
// on every panel click (audit [2a]). The cache is invalidated when the topic
// changes or when the link-filename map upgrades from the SSR fallback to the
// fetched topic JSON (the map changes script-vs-video classification).
let _cloakedCacheTopicId = null;
let _cloakedCacheMapSource = null; // "fetched" | "ssr"
const _cloakedParseCache = new Map(); // postNumber → { videos, scripts, subGroups }

function _parseCloakedPost(pp) {
  const tid = _currentTopicId();
  const mapSource = (_METADATA_TOPIC_ID === tid && LINK_FILENAME_MAP) ? "fetched" : "ssr";
  if (tid !== _cloakedCacheTopicId || mapSource !== _cloakedCacheMapSource) {
    _cloakedParseCache.clear();
    _cloakedCacheTopicId = tid;
    _cloakedCacheMapSource = mapSource;
  }
  const hit = _cloakedParseCache.get(pp.postNumber);
  if (hit) return hit;
  const tempEl = document.createElement("div");
  tempEl.innerHTML = pp.cooked;
  const { videos, scripts } = extractLinksFromElement(tempEl, false);
  const entry = {
    videos, scripts,
    subGroups: (videos.length > 0 || scripts.length > 0)
      ? _pairWithinPost(tempEl, videos, scripts) : [],
  };
  _cloakedParseCache.set(pp.postNumber, entry);
  return entry;
}

function parseAllPosts(rootOverride, titleOverride, metaMapOverride) {
  // rootOverride/titleOverride/metaMapOverride: remote parsing — a detached
  // DOM rebuilt from another topic's JSON (see _parseTopicRemote). All
  // existing callers pass nothing and parse the live page as before.
  const root = rootOverride || document;
  const posts = root.querySelectorAll(".topic-post");
  if (posts.length === 0) return null;

  if (metaMapOverride) {
    // Foreign topic: use the map built from ITS json, and drop the cached
    // topic id so the live page's next parse rebuilds its own map.
    LINK_FILENAME_MAP = metaMapOverride;
    _METADATA_TOPIC_ID = null;
  } else if (_METADATA_TOPIC_ID !== _currentTopicId()) {
    // Prefer the metadata ensureLinkMetadata() fetched for THIS topic. Only
    // fall back to the SSR blob when we don't have a fetched map for the
    // current topic (e.g. parsed before the fetch resolved).
    LINK_FILENAME_MAP = _buildLinkFilenameMap();
  }

  const title = titleOverride || getTopicTitle();
  const opCooked = posts[0]?.querySelector(".cooked");

  // Try section-based parsing on OP. Scripts parked under a generic
  // "Script" heading are handed to the work sections they name first.
  const sections = opCooked ? _distributeOrphanScripts(parseOPSections(opCooked)) : [];

  // Walk every post (including OP) building both:
  //   - flat comment* arrays for the existing collection mode UI
  //   - per-post sub-groups for the new single-mode auto-grouping
  const commentVideos = [];
  const commentScripts = [];
  const scannedPostNumbers = new Set();
  // perPost: [{ postNumber, isOP, username, cookedEl, subGroups: [...] }]
  const perPost = [];

  function _username(postEl) {
    const a = postEl?.querySelector(".names .username a, .first.username a");
    return (a?.textContent || "").trim();
  }

  for (let i = 0; i < posts.length; i++) {
    const pn = posts[i].dataset?.postNumber || posts[i].querySelector("[data-post-number]")?.dataset?.postNumber;
    const postNumber = pn ? parseInt(pn) : (i + 1);
    if (pn) scannedPostNumbers.add(postNumber);

    const el = posts[i].querySelector(".cooked");
    if (!el) continue;
    const isOP = i === 0;
    const { videos, scripts } = extractLinksFromElement(el, isOP);

    if (!isOP) {
      commentVideos.push(...videos);
      commentScripts.push(...scripts);
    }

    if (videos.length === 0 && scripts.length === 0) continue;
    perPost.push({
      postNumber,
      isOP,
      username: _username(posts[i]),
      subGroups: _pairWithinPost(el, videos, scripts),
    });
  }

  // Scan cloaked (lazy-loaded) comments that Discourse hasn't rendered yet.
  // Their content is available in the preloaded JSON data embedded in the page.
  const cloakedPosts = root.querySelectorAll(".post-stream--cloaked");
  if (cloakedPosts.length > 0) {
    const preloadedPosts = _getPreloadedPosts();
    for (const pp of preloadedPosts) {
      if (pp.postNumber <= 1) continue; // Skip OP
      if (scannedPostNumbers.has(pp.postNumber)) continue;
      const { videos, scripts, subGroups } = _parseCloakedPost(pp);
      commentVideos.push(...videos);
      commentScripts.push(...scripts);
      if (videos.length === 0 && scripts.length === 0) continue;
      perPost.push({
        postNumber: pp.postNumber,
        isOP: false,
        username: pp.username || "",
        subGroups,
      });
    }
  }

  // Dedup helpers
  function dedupArr(arr, key = "url") {
    const seen = new Set();
    return arr.filter(item => { if (seen.has(item[key])) return false; seen.add(item[key]); return true; });
  }

  // Collection mode: only when 2+ sections each have their own video(s).
  // Multiple video URLs within the SAME section are mirrors (same video, different hosts),
  // not separate content. Posts with 1 video section + multiple script sections should
  // stay in single mode so everything becomes ONE pair.
  if (sections.length >= 2) {
    const sectionsWithVideos = sections.filter(s => s.videos.length > 0);
    if (sectionsWithVideos.length >= 2) {
      const cVideos = dedupArr(commentVideos);
      const cScripts = dedupArr(commentScripts);
      return {
        mode: "collection",
        title,
        sections,
        commentVideos: cVideos,
        commentScripts: cScripts,
        commentGroups: _buildCommentGroups(perPost, cVideos, cScripts),
      };
    }
    // Single video (or no video) across sections → flatten to single mode
  }

  // Single mode: build flat arrays for probing + auto-group assignments.
  // Each item is tagged with `autoGroup` ("Main" / "Alt 1" / ...) derived
  // from per-post sub-groups. The Main group always exists even when the
  // OP post has no parseable content (rare).
  const autoGroups = [{ name: "Main", sourceLabel: "OP" }];
  let altCounter = 0;
  const allVideos = [];
  const allScripts = [];

  function _label(p) {
    return p.isOP ? "OP" : `#${p.postNumber}${p.username ? ` @${p.username}` : ""}`;
  }

  for (const p of perPost) {
    for (let i = 0; i < p.subGroups.length; i++) {
      const sg = p.subGroups[i];
      let groupName;
      // OP content all lands in Main — multiple videos in the OP are
      // distinct works, not Alt variants, so don't scatter them into
      // Alt 1/2/3. The user groups manually if needed, and the backend
      // auto-splits genuinely distinct works into separate pairs on send.
      // Only other posts (comments) become Alt groups — an alternate
      // script posted by someone else is a real "alt".
      if (p.isOP) {
        groupName = "Main";
      } else {
        altCounter += 1;
        groupName = `Alt ${altCounter}`;
        autoGroups.push({ name: groupName, sourceLabel: _label(p) });
      }
      for (const v of sg.videos) allVideos.push({ ...v, autoGroup: groupName });
      for (const s of sg.scripts) allScripts.push({ ...s, autoGroup: groupName });
    }
  }

  // Fall back: nothing parsed from OP cooked but sections existed (rare)
  if (allVideos.length === 0 && allScripts.length === 0) {
    if (sections.length >= 1) {
      for (const s of sections) {
        for (const v of s.videos) allVideos.push({ ...v, autoGroup: "Main" });
        for (const x of s.scripts) allScripts.push({ ...x, autoGroup: "Main" });
      }
    } else if (opCooked) {
      const { videos, scripts } = extractLinksFromElement(opCooked, true);
      for (const v of videos) allVideos.push({ ...v, autoGroup: "Main" });
      for (const s of scripts) allScripts.push({ ...s, autoGroup: "Main" });
    }
  }

  const dedupedVideos = dedupArr(allVideos);
  const dedupedScripts = dedupArr(allScripts);
  dedupedVideos.sort((a, b) => a.priority - b.priority);
  dedupedScripts.sort((a, b) => {
    if (a.source === "OP" && b.source !== "OP") return -1;
    if (a.source !== "OP" && b.source === "OP") return 1;
    return 0;
  });

  // Prune Alt entries that ended up with no items after dedup
  const usedGroups = new Set([
    ...dedupedVideos.map((v) => v.autoGroup),
    ...dedupedScripts.map((s) => s.autoGroup),
  ]);
  usedGroups.add("Main"); // Main always present
  const liveAutoGroups = autoGroups.filter((g) => usedGroups.has(g.name));

  return {
    mode: "single",
    title,
    videos: dedupedVideos,
    scripts: dedupedScripts,
    autoGroups: liveAutoGroups,
  };
}

// ─── Messaging helpers (works in both Chrome extension and QWebEngine) ───

// Wait for QWebChannel bridge if we're in embedded mode (qt.webChannelTransport exists)
function _waitForBridge() {
  if (window.funpairdlBridge) return Promise.resolve();
  if (typeof qt === "undefined") return Promise.resolve(); // Chrome extension, no bridge needed
  // QWebEngine: bridge script runs at DocumentCreation but QWebChannel init is async
  return new Promise((resolve) => {
    window.addEventListener("funpairdl-bridge-ready", resolve, { once: true });
    // Safety timeout
    setTimeout(resolve, 3000);
  });
}

function _sendMsg(type, data) {
  if (window.funpairdlBridge) {
    return window.funpairdlBridge.sendMessage(type, data);
  }
  // In QWebEngine but bridge not ready yet — wait for it
  if (typeof qt !== "undefined") {
    return _waitForBridge().then(() => {
      if (window.funpairdlBridge) return window.funpairdlBridge.sendMessage(type, data);
      return { _error: "Bridge not available" };
    });
  }
  // Chrome extension fallback
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage({ type, ...data }, (response) => {
        if (chrome.runtime.lastError) resolve({ _error: chrome.runtime.lastError.message });
        else resolve(response || {});
      });
    } catch (e) { resolve({ _error: e.message }); }
  });
}

async function resolveShortUrl(url) {
  if (!url.includes("discuss.eroscripts.com/uploads/short-url/")) return url;

  // Resolve in-browser via fetch() to avoid Discourse auth-token rotation.
  // When aiohttp sends the _t cookie, Discourse rotates the token server-side
  // and returns the new one in Set-Cookie — but aiohttp discards it, leaving
  // the browser with a stale token that eventually gets invalidated (logout).
  // Using the browser's fetch() keeps cookie rotation in sync.
  try {
    const resp = await fetch(url, {
      method: "HEAD",
      credentials: "same-origin",
      redirect: "follow",
    });
    if (resp.ok && resp.url !== url) {
      console.log("FunPairDL: Resolved (in-browser)", url, "->", resp.url);
      return resp.url;
    }
  } catch (e) {
    console.debug("FunPairDL: In-browser resolve failed, trying backend:", e);
  }

  // Fallback: backend resolve (extension mode or fetch() blocked)
  const response = await _sendMsg("resolve-url", { url });
  if (response && response.success) {
    console.log("FunPairDL: Resolved (backend)", url, "->", response.finalUrl);
    return response.finalUrl;
  }
  console.warn("FunPairDL: Failed to resolve", url, response);
  return url;
}

async function resolveAllUrls(urls) {
  return Promise.all(urls.map(resolveShortUrl));
}

// ─── Probe throttle + cache (audit [7], client side) ───
// setupProbing used to fire one probe per link all at once (30+ concurrent
// bridge→backend round trips per panel open) and nothing was reused across
// panel opens. Cap concurrency at 4 and cache successful results module-wide
// keyed by URL, so re-opening the panel (or the same link in another section)
// reuses the earlier answer instead of re-probing.
const _PROBE_MAX_CONCURRENT = 4;
const _PROBE_CACHE_MAX = 500;
// Tabs live for days, so a lifetime cache would serve stale metadata (a file
// re-uploaded to a new size/name). Entries older than this count as misses; the
// backend's own 600 s cache absorbs the re-probe cost.
const _PROBE_CACHE_TTL_MS = 30 * 60 * 1000;
// One automatic re-probe after a failure (see setupProbing._retryLater).
const PROBE_RETRY_DELAY_MS = 6000;
const _probeCache = new Map();     // url → { ts, value } successful probe response
const _probeInflight = new Map();  // url → pending Promise (dedup concurrent)
const _probeSizeByUrl = new Map(); // url → { ts, value } probed byte size (send-pair "sizes")
let _probeActive = 0;
const _probeWaiters = [];

function _probeAcquire() {
  if (_probeActive < _PROBE_MAX_CONCURRENT) {
    _probeActive += 1;
    return Promise.resolve();
  }
  return new Promise((resolve) => _probeWaiters.push(resolve));
}

function _probeRelease() {
  const next = _probeWaiters.shift();
  if (next) next(); // hand the slot straight to the next waiter
  else _probeActive -= 1;
}

// Remember every byte size a probe reveals (the link itself and any bundle
// member files) so send-time can pass them to the backend as `sizes`.
function _recordProbeSizes(url, info) {
  if (!info) return;
  const now = Date.now();
  if (typeof info.size === "number" && info.size > 0) {
    _probeSizeByUrl.set(url, { ts: now, value: Math.floor(info.size) });
  }
  for (const f of (info.files || [])) {
    if (f && f.url && typeof f.size === "number" && f.size > 0) {
      _probeSizeByUrl.set(f.url, { ts: now, value: Math.floor(f.size) });
    }
  }
  while (_probeSizeByUrl.size > 2000) {
    _probeSizeByUrl.delete(_probeSizeByUrl.keys().next().value);
  }
}

// Probed byte size for a single URL, honouring the TTL (stale → drop + 0).
function _probeSizeEntry(url) {
  const e = _probeSizeByUrl.get(url);
  if (!e) return 0;
  if (Date.now() - e.ts >= _PROBE_CACHE_TTL_MS) { _probeSizeByUrl.delete(url); return 0; }
  return e.value > 0 ? e.value : 0;
}

// Probed byte size known for a link, keyed by either the original or the
// resolved URL. Returns 0 when unknown.
function _probedSizeFor(originalUrl, resolvedUrl) {
  return _probeSizeEntry(originalUrl) || _probeSizeEntry(resolvedUrl) || 0;
}

async function _probeOnce(url) {
  await _probeAcquire();
  try {
    const response = await _sendMsg("probe-url", { url });
    if (!response || !response.success) return null;
    _recordProbeSizes(url, response);
    // Only cache results that carried something useful — failures may be
    // transient and should be retried on the next panel open.
    if (response.size || response.filename ||
        (response.files && response.files.length) ||
        (response.formats && response.formats.length)) {
      _probeCache.set(url, { ts: Date.now(), value: response });
      while (_probeCache.size > _PROBE_CACHE_MAX) {
        _probeCache.delete(_probeCache.keys().next().value);
      }
    }
    return response;
  } catch (e) {
    return null;
  } finally {
    _probeRelease();
  }
}

async function probeUrl(url) {
  // All probing goes through the backend /probe endpoint,
  // which dynamically handles GoFile wt tokens, MEGA crypto, etc.
  const cached = _probeCache.get(url);
  if (cached) {
    if (Date.now() - cached.ts < _PROBE_CACHE_TTL_MS) return cached.value;
    _probeCache.delete(url); // stale → re-probe
  }
  const inflight = _probeInflight.get(url);
  if (inflight) return inflight;
  const p = _probeOnce(url);
  _probeInflight.set(url, p);
  try {
    return await p;
  } finally {
    _probeInflight.delete(url);
  }
}

async function sendPairToServer(data) {
  const payload = {
    name: data.title,
    preferred_resolution: data.preferredResolution || "best",
    auto_rename: data.autoRename !== false,
  };
  if (data.groups && data.groups.length > 0) {
    // New grouped payload — backend uses this to lay out Main/Alt folders
    payload.groups = data.groups.map((g) => ({
      name: g.name,
      video_urls: g.videoUrls || [],
      script_urls: g.scriptUrls || [],
      script_authors: g.scriptAuthors || {},
      filenames: g.filenames || {},
      sizes: g.sizes || {},   // probed byte sizes {url: bytes}, >0 only
      bundle_plan: g.bundlePlan || {},  // bundle file url → sub-group label
      inherit_multi_axis: g.inheritMultiAxis !== false,
      display_name: (g.displayName || "").trim(),
    }));
  } else {
    payload.video_urls = data.videoUrls || [];
    payload.script_urls = data.scriptUrls || [];
    if (data.bundlePlan && Object.keys(data.bundlePlan).length > 0) payload.bundle_plan = data.bundlePlan;
    if (data.scriptAuthors && Object.keys(data.scriptAuthors).length > 0) {
      payload.script_authors = data.scriptAuthors;
    }
    if (data.filenames && Object.keys(data.filenames).length > 0) {
      payload.filenames = data.filenames;
    }
    if (data.sizes && Object.keys(data.sizes).length > 0) {
      payload.sizes = data.sizes;
    }
  }
  // In embedded mode, send data directly; in extension, wrap in "data" field
  if (window.funpairdlBridge) {
    return await _sendMsg("send-pair", payload);
  }
  return await _sendMsg("send-pair", { data: payload });
}

async function checkServer() {
  try {
    const response = await _sendMsg("check-status", {});
    return !!(response && response.online);
  } catch (e) { return false; }
}

// ─── Panel UI: shared rendering helpers ───

// A drag handle is only rendered in single mode (where groups exist to drop
// into). Collection-mode sections have no group bodies, so dragging is moot.
function _dragHandleHTML(withHandle) {
  return withHandle
    ? `<span class="funpairdl-drag-handle" draggable="true" title="拖曳到其他群組(按住 Ctrl 或 Shift 拖曳可把勾選的列一起帶走)">⠿</span>`
    : "";
}

// Host tag pinned to the right of a row. The row's main text starts as the
// host label but showProbeExtras() swaps it for the probed filename, so
// without this tag the source vanishes once a probe lands.
function _sourceTagHTML(url, label) {
  let text = (label || "").trim();
  if (!text) {
    try { text = new URL(url).hostname.replace("www.", ""); } catch (e) {}
  }
  return text ? `<span class="funpairdl-tag-source">${escapeAttr(text)}</span>` : "";
}

function renderVideoItem(v, idx, namePrefix, checked, withHandle = false) {
  const badge = v.source === "OP" ? "OP" : "Comment";
  const badgeClass = v.source === "OP" ? "funpairdl-badge-op" : "funpairdl-badge-comment";
  const bundleTag = v.isBundle ? '<span class="funpairdl-tag-bundle">Bundle</span>' : "";
  return `
    <label class="funpairdl-item" title="${escapeAttr(v.url)}" data-key="${namePrefix}-${idx}" data-kind="video" data-index="${idx}">
      ${_dragHandleHTML(withHandle)}
      <input type="checkbox" name="${namePrefix}" value="${idx}" ${checked ? "checked" : ""}>
      <span class="funpairdl-badge ${badgeClass}">${badge}</span>
      <span class="funpairdl-label">${escapeAttr(v.label)}</span>
      ${bundleTag}
      <span class="funpairdl-size" data-probe="${namePrefix}-${idx}"></span>
      ${_sourceTagHTML(v.url, v.label)}
      <span class="funpairdl-priority">P${Math.floor(v.priority)}</span>
    </label>`;
}

function renderScriptItem(s, idx, namePrefix, checked, withHandle = false) {
  const badge = s.source === "OP" ? "OP" : "Comment";
  const badgeClass = s.source === "OP" ? "funpairdl-badge-op" : "funpairdl-badge-comment";
  let axisTag = "";
  if (s.axis && s.axis !== "main") axisTag = `<span class="funpairdl-tag-axis">${s.axis}</span>`;
  else if (s.axis === "main") axisTag = `<span class="funpairdl-tag-main">main</span>`;
  const externalTag = s.isExternal ? '<span class="funpairdl-tag-external">External</span>' : "";
  const safe = escapeAttr(s.filename);
  return `
    <label class="funpairdl-item" title="${safe}" data-key="${namePrefix}-${idx}" data-kind="script" data-index="${idx}">
      ${_dragHandleHTML(withHandle)}
      <input type="checkbox" name="${namePrefix}" value="${idx}" ${checked ? "checked" : ""}>
      <span class="funpairdl-badge ${badgeClass}">${badge}</span>
      <span class="funpairdl-label">${safe}</span>
      ${axisTag}${externalTag}
      <span class="funpairdl-size" data-probe="${namePrefix}-${idx}"></span>
      ${s.isExternal ? _sourceTagHTML(s.url, getVideoLabel(s.url)) : ""}
    </label>`;
}

// ─── Group state & manipulation (single mode only) ───

/**
 * Map a group's zero-based index to the on-disk subfolder suffix that
 * matches the backend convention (`Main` → root, first Alt → `.alt/`,
 * subsequent → `.alt1/`, `.alt2/`, ...). Used purely for the UI preview
 * shown in the group header; the backend computes its own suffix from
 * the Pair's group ordering at organize time.
 */
function _altFolderLabel(groupIdx) {
  if (groupIdx === 0) return "(root)";
  return groupIdx === 1 ? ".alt/" : `.alt${groupIdx - 1}/`;
}

/**
 * Pull a meaningful default stem out of a funscript filename. Strips the
 * common EroScripts "Iwara - " / "Source video - " prefixes and the
 * trailing `[hash] [Source]` brackets that aren't part of the actual
 * scene name. Returns "" if nothing usable is left.
 */
function _cleanScriptStem(filename) {
  if (!filename) return "";
  let s = filename.replace(/\.funscript$/i, "");
  // Strip known axis suffix
  s = s.replace(/\.([a-zA-Z][a-zA-Z0-9]{1,30})$/, (match, axis) => {
    return AXIS_SUFFIXES.includes(axis) || AXIS_SUFFIXES.includes(axis.toLowerCase())
      ? "" : match;
  });
  // Drop common provider prefix like "Iwara - " / "Source - "
  s = s.replace(/^\s*(?:iwara|source(?:\s*video)?)\s*[-—–]\s*/i, "");
  // Drop trailing bracketed iwara IDs and labels like " [ChgJmVOBSBkwR0] [Source]"
  s = s.replace(/\s*[\[(][^\])]{4,40}[\])]\s*/g, " ");
  return s.trim();
}

/** Derive an initial display name for an Alt group from its items. */
function _deriveAltDisplayName(parsed, groupName) {
  for (let i = 0; i < parsed.scripts.length; i++) {
    if ((parsed.scripts[i].autoGroup || "Main") !== groupName) continue;
    const stem = _cleanScriptStem(parsed.scripts[i].filename);
    if (stem) return stem;
  }
  return "";
}

/** Build initial group state from auto-detected groups in `parsed`. */
function _initGroupState(parsed) {
  if (parsed.groupState) return;
  const groups = (parsed.autoGroups || [{ name: "Main", sourceLabel: "OP" }])
    .map((g) => g.name);
  if (!groups.includes("Main")) groups.unshift("Main");
  const inheritAxes = {};
  const altNames = {};
  for (const g of groups) {
    if (g === "Main") continue;
    inheritAxes[g] = true;
    altNames[g] = _deriveAltDisplayName(parsed, g);
  }
  const sourceLabels = {};
  for (const g of (parsed.autoGroups || [])) sourceLabels[g.name] = g.sourceLabel;
  const itemGroup = {};
  parsed.videos.forEach((v, i) => { itemGroup[`video-${i}`] = v.autoGroup || "Main"; });
  parsed.scripts.forEach((s, i) => { itemGroup[`script-${i}`] = s.autoGroup || "Main"; });
  parsed.groupState = { groups, inheritAxes, sourceLabels, itemGroup, altNames };
}

/** All current group names (used to populate per-item dropdowns). */
function _groupOptionsHTML(currentGroup, allGroups) {
  let html = "";
  for (const g of allGroups) {
    html += `<option value="${g}" ${g === currentGroup ? "selected" : ""}>${g}</option>`;
  }
  html += `<option value="__new__">+ 新 Alt</option>`;
  return html;
}

/** Update the inheritance preview line under each Alt group. */
function _updateInheritancePreviews(panel, parsed) {
  // Determine Main's axes (non-main canonical) from current group state.
  const mainAxes = [];
  parsed.scripts.forEach((s, i) => {
    if (parsed.groupState.itemGroup[`script-${i}`] !== "Main") return;
    if (!s.axis || s.axis === "main") return;
    if (!mainAxes.includes(s.axis)) mainAxes.push(s.axis);
  });

  for (const g of parsed.groupState.groups) {
    if (g === "Main") continue;
    const previewEl = panel.querySelector(`.funpairdl-inherit-preview[data-group="${g}"]`);
    if (!previewEl) continue;
    const inherit = parsed.groupState.inheritAxes[g] !== false;
    if (!inherit || mainAxes.length === 0) {
      previewEl.textContent = "";
      continue;
    }
    // Which axes are already in this Alt group?
    const altAxes = new Set();
    parsed.scripts.forEach((s, i) => {
      if (parsed.groupState.itemGroup[`script-${i}`] !== g) return;
      if (s.axis && s.axis !== "main") altAxes.add(s.axis);
    });
    const willInherit = mainAxes.filter((a) => !altAxes.has(a));
    if (willInherit.length === 0) {
      previewEl.textContent = "";
      continue;
    }
    previewEl.textContent = `─ hardlink 自 Main: ${willInherit.join(", ")}`;
  }
}

/**
 * If a bundle dropdown is sitting next to `item`, return it so callers
 * can move the pair together (otherwise it would orphan when the item
 * is moved to a different group body).
 */
function _itemBundleDropdown(item) {
  const sib = item.nextElementSibling;
  return (sib && sib.classList.contains("funpairdl-bundle-files")) ? sib : null;
}

function _moveItemToGroup(panel, parsed, item, targetGroup) {
  parsed.groupState.itemGroup[item.dataset.key] = targetGroup;
  const body = panel.querySelector(`.funpairdl-group-body[data-group="${targetGroup}"]`);
  if (!body) return;
  const dropdown = _itemBundleDropdown(item);
  body.appendChild(item);
  if (dropdown) body.appendChild(dropdown);
  // Keep the per-item dropdown in sync so a drag-move (or any other caller)
  // leaves the row's selector showing the group it now lives in.
  const sel = item.querySelector(".funpairdl-item-group-select");
  if (sel && sel.value !== targetGroup) sel.value = targetGroup;
  // Main's membership changed → the pairing preview must follow.
  _scheduleWorkPlan(panel, parsed);
}

/** Re-render all group blocks (called on add/remove group). */
function _rerenderGroupBlocks(panel, parsed) {
  // Preserve item DOM nodes (with their probe results + bundle dropdowns)
  // by detaching them as (item, dropdown?) pairs and re-attaching after
  // the skeleton is rebuilt.
  const pairs = [];
  panel.querySelectorAll(".funpairdl-item[data-key]").forEach((it) => {
    pairs.push([it, _itemBundleDropdown(it)]);
  });
  for (const [it, dd] of pairs) { it.remove(); if (dd) dd.remove(); }

  const wrap = panel.querySelector(".funpairdl-groups-root");
  wrap.outerHTML = _buildGroupsRootHTML(parsed);

  const root = panel.querySelector(".funpairdl-groups-root");
  for (const [it, dd] of pairs) {
    const key = it.dataset.key;
    const target = parsed.groupState.itemGroup[key] || "Main";
    const body = root.querySelector(`.funpairdl-group-body[data-group="${target}"]`);
    if (!body) continue;
    // Refresh the per-item group dropdown options so it lists
    // newly-added groups too.
    const sel = it.querySelector(".funpairdl-item-group-select");
    if (sel) sel.innerHTML = _groupOptionsHTML(target, parsed.groupState.groups);
    body.appendChild(it);
    if (dd) body.appendChild(dd);
  }

  _attachGroupBlockEvents(panel, parsed);
  _updateInheritancePreviews(panel, parsed);
  // The pairing preview lived inside Main's block, which was just rebuilt.
  _scheduleWorkPlan(panel, parsed);
}

// Kept as an alias for the shared attribute escaper defined at the top.
function _escAttr(s) {
  return escapeAttr(s);
}

/**
 * Front-end preview of the on-disk subfolder for a given Alt — kept in
 * sync with the backend rule (display_name + ".alt", or topic + slot
 * suffix when blank). Used purely for showing the user what the folder
 * will end up being called.
 */
function _altFolderPreview(parsed, slotIdx, groupName) {
  if (groupName === "Main") return "(根目錄)";
  const name = (parsed.groupState.altNames[groupName] || "").trim();
  const base = (parsed.title || "Untitled").trim();
  if (name) return `${name}.alt/`;
  return slotIdx === 1 ? `${base}.alt/` : `${base}.alt${slotIdx - 1}/`;
}

function _buildGroupsRootHTML(parsed) {
  const { groups, inheritAxes, sourceLabels, altNames } = parsed.groupState;
  let html = `<div class="funpairdl-groups-root">`;
  for (let gi = 0; gi < groups.length; gi++) {
    const g = groups[gi];
    const isMain = g === "Main";
    const folder = _altFolderPreview(parsed, gi, g);
    const src = sourceLabels[g] ? `<span class="funpairdl-group-source">${_escAttr(sourceLabels[g])}</span>` : "";
    const nameInput = isMain ? "" : `
      <input type="text" class="funpairdl-alt-name-input" data-group="${g}"
             placeholder="Alt 名稱" value="${_escAttr(altNames[g] || "")}">`;
    const inheritToggle = isMain ? "" : `
      <label class="funpairdl-inherit-toggle">
        <input type="checkbox" class="funpairdl-inherit-cb" data-group="${g}" ${inheritAxes[g] !== false ? "checked" : ""}>
        繼承 Main 多軸
      </label>`;
    const removeBtn = isMain ? "" : `<button class="funpairdl-group-remove" data-group="${g}" title="解散此群組,項目併回 Main" type="button">✕</button>`;
    html += `<div class="funpairdl-group-block" data-group="${g}">
      <div class="funpairdl-group-header">
        <span class="funpairdl-group-name">▾ ${g}</span>
        ${nameInput}
        <span class="funpairdl-alt-folder-preview" data-group="${g}">${_escAttr(folder)}</span>
        ${src}
        ${inheritToggle}
        ${removeBtn}
      </div>
      <div class="funpairdl-group-body" data-group="${g}"></div>
      <div class="funpairdl-inherit-preview" data-group="${g}"></div>
    </div>`;
  }
  html += `<div class="funpairdl-group-controls">
    <button id="funpairdl-add-alt" class="funpairdl-add-alt-btn" type="button">+ 新增 Alt 群組</button>
  </div></div>`;
  return html;
}

function _refreshAltFolderPreviews(panel, parsed) {
  parsed.groupState.groups.forEach((g, gi) => {
    const el = panel.querySelector(`.funpairdl-alt-folder-preview[data-group="${g}"]`);
    if (el) el.textContent = _altFolderPreview(parsed, gi, g);
  });
}

function _nextAltName(parsed) {
  // Find the smallest positive N not yet in use as "Alt N".
  const used = new Set();
  for (const g of parsed.groupState.groups) {
    const m = g.match(/^Alt\s+(\d+)$/i);
    if (m) used.add(parseInt(m[1]));
  }
  let n = 1;
  while (used.has(n)) n += 1;
  return `Alt ${n}`;
}

function _attachGroupBlockEvents(panel, parsed) {
  // Per-item group dropdown
  panel.querySelectorAll(".funpairdl-item-group-select").forEach((sel) => {
    sel.addEventListener("change", (e) => {
      e.stopPropagation();
      const item = sel.closest(".funpairdl-item");
      if (!item) return;
      let target = sel.value;
      if (target === "__new__") {
        target = _nextAltName(parsed);
        parsed.groupState.groups.push(target);
        parsed.groupState.inheritAxes[target] = true;
        parsed.groupState.sourceLabels[target] = "manual";
        parsed.groupState.altNames[target] = "";
        parsed.groupState.itemGroup[item.dataset.key] = target;
        _rerenderGroupBlocks(panel, parsed);
        return;
      }
      _moveItemToGroup(panel, parsed, item, target);
      _updateInheritancePreviews(panel, parsed);
    });
    // Prevent label click on the select from toggling the checkbox
    sel.addEventListener("click", (e) => e.stopPropagation());
    sel.addEventListener("mousedown", (e) => e.stopPropagation());
  });

  // Inherit toggle
  panel.querySelectorAll(".funpairdl-inherit-cb").forEach((cb) => {
    cb.addEventListener("change", () => {
      parsed.groupState.inheritAxes[cb.dataset.group] = cb.checked;
      _updateInheritancePreviews(panel, parsed);
    });
  });

  // Alt name input → live update folder preview
  panel.querySelectorAll(".funpairdl-alt-name-input").forEach((inp) => {
    inp.addEventListener("input", () => {
      parsed.groupState.altNames[inp.dataset.group] = inp.value;
      _refreshAltFolderPreviews(panel, parsed);
    });
    // Don't let label clicks toggle nearby checkboxes
    inp.addEventListener("click", (e) => e.stopPropagation());
  });

  // Remove group → fold items back into Main
  panel.querySelectorAll(".funpairdl-group-remove").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const target = btn.dataset.group;
      const idx = parsed.groupState.groups.indexOf(target);
      if (idx <= 0) return; // Main can't be removed
      parsed.groupState.groups.splice(idx, 1);
      delete parsed.groupState.inheritAxes[target];
      delete parsed.groupState.sourceLabels[target];
      if (parsed.groupState.altNames) delete parsed.groupState.altNames[target];
      for (const k of Object.keys(parsed.groupState.itemGroup)) {
        if (parsed.groupState.itemGroup[k] === target) {
          parsed.groupState.itemGroup[k] = "Main";
        }
      }
      _rerenderGroupBlocks(panel, parsed);
    });
  });

  // Add new Alt group
  const addBtn = panel.querySelector("#funpairdl-add-alt");
  if (addBtn) {
    addBtn.addEventListener("click", () => {
      const name = _nextAltName(parsed);
      parsed.groupState.groups.push(name);
      parsed.groupState.inheritAxes[name] = true;
      parsed.groupState.sourceLabels[name] = "manual";
      parsed.groupState.altNames[name] = "";
      _rerenderGroupBlocks(panel, parsed);
    });
  }
}

// ─── Panel UI: Single mode ───

function buildSinglePanelHTML(parsed) {
  // Initialise group state from the parser's auto-detected layout.
  // The actual <label> items are injected later (see populateSingleItems)
  // so each one is created exactly once — that keeps probe results and
  // checkbox state intact when the user adds/removes Alt groups.
  _initGroupState(parsed);

  if (parsed.videos.length === 0 && parsed.scripts.length === 0) {
    return `<div class="funpairdl-empty">No video or script links found</div>`;
  }

  const toolbar = `<div class="funpairdl-collection-toolbar">
    <label class="funpairdl-item funpairdl-select-all">
      <input type="checkbox" id="funpairdl-select-all" checked>
      <span class="funpairdl-label" style="font-weight:700">全選 / 全不選</span>
    </label>
  </div>`;
  return toolbar + _buildGroupsRootHTML(parsed);
}

/**
 * Inject all <label> items into their assigned group bodies, attaching
 * a per-item group dropdown. Called once after the panel HTML is in the
 * DOM; subsequent group changes move existing nodes around rather than
 * recreating them.
 */
function populateSingleItems(panel, parsed) {
  const root = panel.querySelector(".funpairdl-groups-root");
  if (!root) return;

  const allGroups = parsed.groupState.groups;

  function _injectItem(html, key) {
    const tmp = document.createElement("div");
    tmp.innerHTML = html.trim();
    const node = tmp.firstElementChild;
    // Append the group dropdown inside the label. <select> inside <label>
    // does not forward clicks to the checkbox, so this is safe.
    const select = document.createElement("select");
    select.className = "funpairdl-item-group-select";
    select.innerHTML = _groupOptionsHTML(parsed.groupState.itemGroup[key] || "Main", allGroups);
    node.appendChild(select);
    return node;
  }

  parsed.videos.forEach((v, i) => {
    const key = `video-${i}`;
    const node = _injectItem(renderVideoItem(v, i, "video", true, true), key);
    const target = parsed.groupState.itemGroup[key] || "Main";
    const body = root.querySelector(`.funpairdl-group-body[data-group="${target}"]`);
    if (body) body.appendChild(node);
  });

  parsed.scripts.forEach((s, i) => {
    const key = `script-${i}`;
    const node = _injectItem(renderScriptItem(s, i, "script", true, true), key);
    const target = parsed.groupState.itemGroup[key] || "Main";
    const body = root.querySelector(`.funpairdl-group-body[data-group="${target}"]`);
    if (body) body.appendChild(node);
  });

  _attachGroupBlockEvents(panel, parsed);
  _updateInheritancePreviews(panel, parsed);
}

// ─── Panel UI: Collection mode ───

function buildCollectionPanelHTML(parsed) {
  let html = "";

  // Select All / None
  html += `<div class="funpairdl-collection-toolbar">
    <label class="funpairdl-item funpairdl-select-all">
      <input type="checkbox" id="funpairdl-select-all" checked>
      <span class="funpairdl-label" style="font-weight:700">Select All (${parsed.sections.length} sections)</span>
    </label>
    <button id="funpairdl-add-section" class="funpairdl-add-alt-btn funpairdl-add-section-btn" type="button"
            title="建立一個空群組;把列拖進去,送出時就是獨立的一組">+ 新增群組</button>
    <button id="funpairdl-reset-layout" class="funpairdl-add-alt-btn funpairdl-add-section-btn" type="button"
            title="把所有列送回解析出的原段落,並移除自建群組">還原編排</button>
  </div>`;

  parsed.sections.forEach((section, si) => {
    const vCount = section.videos.length;
    const sCount = section.scripts.length;
    const summary = [vCount ? `${vCount}V` : "", sCount ? `${sCount}S` : ""].filter(Boolean).join(" + ");

    html += `<div class="funpairdl-section-group" data-section="${si}">
      <div class="funpairdl-section-header">
        <input type="checkbox" class="funpairdl-section-cb" data-section="${si}" checked>
        <span class="funpairdl-section-toggle" data-section="${si}">▸</span>
        <span class="funpairdl-section-name">${escapeAttr(section.name)}</span>
        <span class="funpairdl-section-count">${summary}</span>
      </div>
      <div class="funpairdl-section-body" style="display:none">`;

    // Rows carry a grip in collection mode too: sections are only as good
    // as the OP's headings, so the user can drag a script (or video) into
    // the section it really belongs to before sending.
    if (vCount > 0) {
      html += `<div class="funpairdl-subsection-title">Videos</div>`;
      section.videos.forEach((v, vi) => {
        html += renderVideoItem(v, vi, `sv-${si}`, vi === 0, true);
      });
    }
    if (sCount > 0) {
      // Group scripts by author
      const authorGroups = new Map();
      section.scripts.forEach((s, idx) => {
        const key = s.author || "";
        if (!authorGroups.has(key)) authorGroups.set(key, []);
        authorGroups.get(key).push({ s, idx });
      });
      const hasMultipleAuthors = authorGroups.size > 1 ||
        (authorGroups.size === 1 && !authorGroups.has(""));

      if (hasMultipleAuthors) {
        html += `<div class="funpairdl-subsection-title">Scripts</div>`;
        let isFirstAuthor = true;
        for (const [author, items] of authorGroups) {
          const authorDisplay = author || "Unknown";
          const escapedAuthor = escapeAttr(authorDisplay);
          html += `<div class="funpairdl-author-group">
            <div class="funpairdl-author-header">
              <span class="funpairdl-author-name">${escapedAuthor}</span>
              <span class="funpairdl-author-count">${items.length} scripts</span>
            </div>`;
          items.forEach(({ s, idx }) => {
            html += renderScriptItem(s, idx, `ss-${si}`, isFirstAuthor, true);
          });
          html += `</div>`;
          isFirstAuthor = false;
        }
      } else {
        html += `<div class="funpairdl-subsection-title">Scripts</div>`;
        section.scripts.forEach((s, si2) => {
          html += renderScriptItem(s, si2, `ss-${si}`, true, true);
        });
      }
    }

    html += `</div></div>`;
  });

  // Comment posts: one section per in-post pairing, unchecked by default.
  // Row keys stay "cv-N"/"cs-N" (indices into the flat comment arrays).
  for (const g of (parsed.commentGroups || [])) {
    let body = "";
    if (g.videos.length > 0) {
      body += `<div class="funpairdl-subsection-title">Videos</div>`;
      for (const i of g.videos) body += renderVideoItem(parsed.commentVideos[i], i, "cv", false, true);
    }
    if (g.scripts.length > 0) {
      body += `<div class="funpairdl-subsection-title">Scripts</div>`;
      for (const i of g.scripts) body += renderScriptItem(parsed.commentScripts[i], i, "cs", false, true);
    }
    html += _collectionGroupHTML(
      g.id, escapeAttr(g.name || g.label), g.name ? escapeAttr(g.label) : "",
      `${g.videos.length}V + ${g.scripts.length}S`, body, false, {});
  }

  return html;
}

// One collapsible section block. `opts.editable` renders the name as a text
// input (user-created groups), `opts.removable` adds a ✕, `opts.open`
// starts it expanded.
function _collectionGroupHTML(id, titleHtml, subLabelHtml, countText, bodyHtml, checked, opts) {
  const o = opts || {};
  const nameCell = o.editable
    ? `<input type="text" class="funpairdl-alt-name-input funpairdl-section-name-input" data-section="${id}"
              placeholder="群組名稱(留空則用帖子標題)" value="${titleHtml}">`
    : `<span class="funpairdl-section-name">${titleHtml}</span>`;
  const sub = subLabelHtml ? `<span class="funpairdl-section-sub">${subLabelHtml}</span>` : "";
  const remove = o.removable
    ? `<button class="funpairdl-group-remove funpairdl-section-remove" data-section="${id}"
               title="移除此群組,裡面的列送回原段落" type="button">✕</button>`
    : "";
  return `<div class="funpairdl-section-group" data-section="${id}">
      <div class="funpairdl-section-header">
        <input type="checkbox" class="funpairdl-section-cb" data-section="${id}" ${checked ? "checked" : ""}>
        <span class="funpairdl-section-toggle" data-section="${id}">${o.open ? "▾" : "▸"}</span>
        ${nameCell}${sub}
        <span class="funpairdl-section-count">${countText}</span>${remove}
      </div>
      <div class="funpairdl-section-body" style="display:${o.open ? "block" : "none"}">${bodyHtml}</div>
    </div>`;
}

// Folder/pair name for a section id: OP sections use their heading, comment
// groups the work name their heading gave them, user groups what was typed.
// Empty or generic ("Video link", "Downloads") → the topic title.
function _collectionPairName(parsed, id) {
  let name = "";
  if (/^\d+$/.test(id)) name = (parsed.sections[parseInt(id)] || {}).name || "";
  else if (id.startsWith("c")) name = ((parsed.commentGroups || []).find((g) => g.id === id) || {}).name || "";
  else if (id.startsWith("x")) name = ((parsed.extraSections || []).find((g) => g.id === id) || {}).name || "";
  name = name.trim();
  return (!name || _isGenericSectionName(name)) ? parsed.title : name;
}

// Add an empty, user-named section (id "x1", "x2", …). Idempotent per id so
// the batch card can replay saved groups.
function _addCollectionSection(panel, parsed, name, id, focus) {
  if (!parsed.extraSections) parsed.extraSections = [];
  if (id) {
    const existing = panel.querySelector(`.funpairdl-section-group[data-section="${id}"]`);
    if (existing) return existing;
  } else {
    let n = 1;
    while (parsed.extraSections.some((x) => x.id === `x${n}`) ||
           panel.querySelector(`.funpairdl-section-group[data-section="x${n}"]`)) n++;
    id = `x${n}`;
  }
  parsed.extraSections.push({ id, name: name || "" });
  const tmp = document.createElement("div");
  tmp.innerHTML = _collectionGroupHTML(id, escapeAttr(name || ""), "", "empty", "", true,
    { editable: true, removable: true, open: true });
  const group = tmp.firstElementChild;
  const groups = panel.querySelectorAll(".funpairdl-section-group");
  const last = groups[groups.length - 1];
  const parent = last ? last.parentNode : panel.querySelector(".funpairdl-panel-body");
  // Sit after the OP sections / earlier user groups, before comment groups.
  const anchor = parent.querySelector('.funpairdl-section-group[data-section^="c"]');
  if (anchor) parent.insertBefore(group, anchor); else parent.appendChild(group);
  _wireCollectionGroup(panel, parsed, group);
  updateSendButton(panel, parsed);
  if (focus) {
    const inp = group.querySelector(".funpairdl-section-name-input");
    if (inp) inp.focus();
  }
  return group;
}

// Undo every drag move and drop every user group — the panel returns to
// what parsing produced. Also the escape hatch for a saved arrangement the
// batch card replays.
function _resetCollectionLayout(panel, parsed) {
  panel.querySelectorAll(".funpairdl-item[data-key]").forEach((row) => {
    if (row.dataset.home) _moveItemToSection(panel, parsed, row, row.dataset.home);
  });
  for (const x of [...(parsed.extraSections || [])]) {
    const group = panel.querySelector(`.funpairdl-section-group[data-section="${x.id}"]`);
    if (group) group.remove();
  }
  parsed.extraSections = [];
  parsed.sectionOverride = {};
  _refreshSectionCounts(panel);
  updateSendButton(panel, parsed);
  panel.dispatchEvent(new Event("change", { bubbles: true }));
}

// Remove a user section; its rows go back to the section they were parsed in.
function _removeCollectionSection(panel, parsed, id) {
  const group = panel.querySelector(`.funpairdl-section-group[data-section="${id}"]`);
  if (!group) return;
  group.querySelectorAll(".funpairdl-item[data-key]").forEach((row) => {
    _moveItemToSection(panel, parsed, row, row.dataset.home || "0");
  });
  group.remove();
  parsed.extraSections = (parsed.extraSections || []).filter((x) => x.id !== id);
  _refreshSectionCounts(panel);
  updateSendButton(panel, parsed);
  panel.dispatchEvent(new Event("change", { bubbles: true }));
}

// ─── Create panel element ───

function createPanel(parsed) {
  const panel = document.createElement("div");
  panel.id = "funpairdl-panel";
  panel.dataset.mode = parsed.mode;

  let html = `
    <div class="funpairdl-panel-header">
      <span class="funpairdl-icon">⬇</span>
      <span>FunPairDL</span>
      ${parsed.mode === "collection" ? '<span class="funpairdl-tag-bundle" style="margin-left:6px">Collection</span>' : ""}
      <button class="funpairdl-panel-close" id="funpairdl-close">✕</button>
    </div>
    <div class="funpairdl-panel-body">`;

  if (parsed.mode === "collection") {
    html += buildCollectionPanelHTML(parsed);
  } else {
    html += buildSinglePanelHTML(parsed);
  }

  html += `</div>
    <div class="funpairdl-panel-footer">
      <div class="funpairdl-resolution-row">
        <label class="funpairdl-resolution-label">Resolution</label>
        <select id="funpairdl-resolution" class="funpairdl-resolution-select">
          <option value="best">Best</option>
          <option value="2160">2160p (4K)</option>
          <option value="1080">1080p</option>
          <option value="720">720p</option>
          <option value="480">480p</option>
          <option value="360">360p</option>
        </select>
      </div>
      <div class="funpairdl-resolution-row">
        <label class="funpairdl-item" style="margin:0;padding:2px 0">
          <input type="checkbox" id="funpairdl-auto-rename" checked>
          <span class="funpairdl-label">Auto Rename</span>
        </label>
      </div>
      <button id="funpairdl-send" class="funpairdl-send-btn">Send to FunPairDL</button>
    </div>`;

  panel.innerHTML = html;
  return panel;
}

// ─── Probing logic ───

function setupProbing(panel, parsed) {
  const probeResults = {};

  function updateVideoSize(probeKey, info) {
    const sizeEl = panel.querySelector(`[data-probe="${probeKey}"]`);
    if (!sizeEl || !info) return;

    if (info.formats && info.formats.length > 0) {
      const withHeight = info.formats.filter(f => f.height > 0);
      const resSelect = document.getElementById("funpairdl-resolution");
      const pref = resSelect ? resSelect.value : "best";
      let targetFmt = null;
      if (pref !== "best" && withHeight.length > 0) {
        const target = parseInt(pref);
        targetFmt = withHeight.find(f => f.height === target);
        if (!targetFmt) targetFmt = info.formats[info.formats.length - 1];
      } else {
        targetFmt = info.formats[info.formats.length - 1];
      }
      const fmtSize = targetFmt && targetFmt.size ? formatSize(targetFmt.size) : "";
      if (withHeight.length > 0) {
        const lo = withHeight[0].height;
        const hi = withHeight[withHeight.length - 1].height;
        const resRange = lo === hi ? `${hi}p` : `${lo}p~${hi}p`;
        sizeEl.textContent = [resRange, fmtSize].filter(Boolean).join(" ");
      } else {
        sizeEl.textContent = fmtSize;
      }
    } else if (info.size) {
      sizeEl.textContent = formatSize(info.size);
    } else {
      sizeEl.textContent = "";
    }
    // Duration next to the size — the number a user can check a script
    // against by eye.
    const dur = formatDuration(info.duration);
    if (dur) sizeEl.textContent = [sizeEl.textContent, dur].filter(Boolean).join(" · ");
    // Filename + bundle are handled by showProbeExtras()
  }

  // Shared: create bundle dropdown + update filename label
  function showProbeExtras(sizeEl, probeKey, info) {
    // Filename display
    if (info.filename) {
      const item = sizeEl.closest(".funpairdl-item");
      if (item) {
        const labelEl = item.querySelector(".funpairdl-label");
        if (labelEl) {
          const current = labelEl.textContent.trim();
          if (!current.includes(".") || current.length < 6 || current.startsWith("[External]")) {
            labelEl.textContent = info.filename;
          }
          item.title = info.filename;
        }
      }
    }

    // Bundle dropdown (Pixeldrain lists, MEGA folders, GoFile folders)
    if (info.files && info.files.length > 0) {
      const item = sizeEl.closest(".funpairdl-item");
      if (item && !item.nextElementSibling?.classList?.contains("funpairdl-bundle-files")) {
        const dropdown = document.createElement("div");
        dropdown.className = "funpairdl-bundle-files";
        dropdown.style.display = "none";
        dropdown.innerHTML = info.files.map((f) => _bundleFileRowHTML(f, probeKey)).join("");
        item.after(dropdown);
        // Several works in one bundle: show how they will be split into
        // pairs, as sub-groups the user can rearrange before sending.
        _planBundleLayout(panel, parsed, dropdown, info.files, probeKey);

        let bundleTag = item.querySelector(".funpairdl-tag-bundle");
        if (!bundleTag) {
          bundleTag = document.createElement("span");
          bundleTag.className = "funpairdl-tag-bundle";
          const sizeSpan = item.querySelector(".funpairdl-size");
          if (sizeSpan) sizeSpan.before(bundleTag);
        }
        bundleTag.textContent = `${info.files.length} files ▾`;
        bundleTag.style.cursor = "pointer";
        bundleTag.addEventListener("click", (e) => {
          e.preventDefault(); e.stopPropagation();
          const visible = dropdown.style.display !== "none";
          dropdown.style.display = visible ? "none" : "block";
          bundleTag.textContent = visible ? `${info.files.length} files ▾` : `${info.files.length} files ▴`;
        });
      }
    }
  }

  // A failed probe is often transient (a Cloudflare challenge, a dropped
  // connection, a host that answers on the second try). Failures are never
  // cached, so one delayed re-probe recovers those instead of leaving "?"
  // until the panel is reopened.
  const _retried = new Set();
  function _retryLater(probeKey, fn) {
    if (_retried.has(probeKey)) return false;
    _retried.add(probeKey);
    setTimeout(fn, PROBE_RETRY_DELAY_MS);
    return true;
  }

  function probeVideo(v, probeKey) {
    const sizeEl = panel.querySelector(`[data-probe="${probeKey}"]`);
    if (!sizeEl) return;
    sizeEl.textContent = "...";
    probeUrl(v.url).then((info) => {
      if (!info) {
        sizeEl.textContent = "?";
        _retryLater(probeKey, () => probeVideo(v, probeKey));
        return;
      }
      probeResults[probeKey] = info;
      // A file-locker URL (pixeldrain /u/, mega /file/, ...) carries no type
      // hint, so a funscript hosted there is initially treated as a "video".
      // Remember the probed filename so send-time can re-route it to scripts.
      if (info.filename) v.probedFilename = info.filename;
      if (Array.isArray(info.tags) && info.tags.length) v.probedTags = info.tags;
      if (info.thumbnail) v.probedThumb = info.thumbnail;
      if (info.duration) v.probedDuration = Number(info.duration) || 0;
      updateVideoSize(probeKey, info);
      showProbeExtras(sizeEl, probeKey, info);
      _attachMediaHints(sizeEl.closest(".funpairdl-item"), info);
      _scheduleWorkPlan(panel, parsed);
    });
  }

  function probeScript(s, probeKey) {
    const sizeEl = panel.querySelector(`[data-probe="${probeKey}"]`);
    if (!sizeEl) return;
    sizeEl.textContent = "...";
    const handleResult = (info) => {
      if (!info) {
        sizeEl.textContent = "?";
        _retryLater(probeKey, () => probeScript(s, probeKey));
        return;
      }
      probeResults[probeKey] = info;
      sizeEl.textContent = [info.size ? formatSize(info.size) : "", formatDuration(info.duration)]
        .filter(Boolean).join(" · ");
      if (info.duration) s.probedDuration = Number(info.duration) || 0;
      if (info.video_url) s.probedLink = info.video_url;
      showProbeExtras(sizeEl, probeKey, info);
      _scheduleWorkPlan(panel, parsed);
    };
    if (s.url.includes("discuss.eroscripts.com/uploads/short-url/")) {
      resolveShortUrl(s.url).then((resolved) => {
        if (resolved === s.url) { sizeEl.textContent = "?"; return; }
        probeUrl(resolved).then(handleResult);
      });
    } else {
      probeUrl(s.url).then(handleResult);
    }
  }

  // Probe based on mode
  if (parsed.mode === "collection") {
    parsed.sections.forEach((section, si) => {
      section.videos.forEach((v, vi) => probeVideo(v, `sv-${si}-${vi}`));
      section.scripts.forEach((s, si2) => probeScript(s, `ss-${si}-${si2}`));
    });
    if (parsed.commentVideos) parsed.commentVideos.forEach((v, i) => probeVideo(v, `cv-${i}`));
    if (parsed.commentScripts) parsed.commentScripts.forEach((s, i) => probeScript(s, `cs-${i}`));
  } else {
    parsed.videos.forEach((v, i) => probeVideo(v, `video-${i}`));
    parsed.scripts.forEach((s, i) => probeScript(s, `script-${i}`));
  }

  // Resolution change → update all video sizes
  const resSelect = document.getElementById("funpairdl-resolution");

  // Load saved resolution preference, falling back to server default_resolution
  function _applyResolution(val) {
    if (val && resSelect) {
      resSelect.value = val;
      for (const [key, info] of Object.entries(probeResults)) updateVideoSize(key, info);
    }
  }

  // Fetch server config for default_resolution (used when no browser preference)
  const configPromise = _sendMsg("get-config", {}).catch(() => ({}));

  if (window.funpairdlBridge) {
    window.funpairdlBridge.storage.get("preferredResolution").then((val) => {
      if (val) { _applyResolution(val); }
      else { configPromise.then((cfg) => _applyResolution(cfg.default_resolution)); }
    });
  } else if (typeof chrome !== "undefined" && chrome.storage) {
    chrome.storage.local.get("preferredResolution", (result) => {
      if (result.preferredResolution) { _applyResolution(result.preferredResolution); }
      else { configPromise.then((cfg) => _applyResolution(cfg.default_resolution)); }
    });
  } else {
    // No storage available — use server config
    configPromise.then((cfg) => _applyResolution(cfg.default_resolution));
  }

  resSelect.addEventListener("change", () => {
    if (window.funpairdlBridge) {
      window.funpairdlBridge.storage.set({ preferredResolution: resSelect.value });
    } else if (typeof chrome !== "undefined" && chrome.storage) {
      chrome.storage.local.set({ preferredResolution: resSelect.value });
    }
    for (const [key, info] of Object.entries(probeResults)) updateVideoSize(key, info);
  });
}

// ─── Collection mode: section toggle/select logic ───

// Wire one section block: expand/collapse, section checkbox ⇄ its rows,
// name editing and removal for user groups. Called for every block at panel
// setup and again for each group added later.
function _wireCollectionGroup(panel, parsed, group) {
  const id = group.dataset.section;
  const toggle = group.querySelector(".funpairdl-section-toggle");
  const body = group.querySelector(".funpairdl-section-body");
  const header = group.querySelector(".funpairdl-section-header");
  const cb = group.querySelector(".funpairdl-section-cb");

  if (toggle && body) {
    toggle.addEventListener("click", () => {
      const visible = body.style.display !== "none";
      body.style.display = visible ? "none" : "block";
      toggle.textContent = visible ? "▸" : "▾";
    });
  }
  if (header) {
    header.style.cursor = "pointer";
    header.addEventListener("click", (e) => {
      if (e.target.tagName === "INPUT" || e.target.tagName === "BUTTON") return;
      if (toggle) toggle.click();
    });
  }
  if (cb && body) {
    // Section checkbox → check/uncheck every row in it
    cb.addEventListener("change", () => {
      body.querySelectorAll('input[type="checkbox"]').forEach((inner) => {
        inner.checked = cb.checked;
      });
      updateSendButton(panel, parsed);
    });
    // Row checkbox → bubble up to the section checkbox
    body.addEventListener("change", (e) => {
      if (e.target.type !== "checkbox") return;
      cb.checked = body.querySelector('input[type="checkbox"]:checked') !== null;
      updateSendButton(panel, parsed);
    });
  }
  const nameInp = group.querySelector(".funpairdl-section-name-input");
  if (nameInp) {
    nameInp.addEventListener("input", () => {
      const ex = (parsed.extraSections || []).find((x) => x.id === id);
      if (ex) ex.name = nameInp.value;
    });
  }
  const rm = group.querySelector(".funpairdl-section-remove");
  if (rm) {
    rm.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      _removeCollectionSection(panel, parsed, id);
    });
  }
}

function setupCollectionEvents(panel, parsed) {
  panel.querySelectorAll(".funpairdl-section-group")
    .forEach((group) => _wireCollectionGroup(panel, parsed, group));

  const addBtn = panel.querySelector("#funpairdl-add-section");
  if (addBtn) {
    addBtn.addEventListener("click", () => {
      _addCollectionSection(panel, parsed, "", null, true);
      panel.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }
  const resetBtn = panel.querySelector("#funpairdl-reset-layout");
  if (resetBtn) resetBtn.addEventListener("click", () => _resetCollectionLayout(panel, parsed));

  // Select All checkbox
  const selectAll = panel.querySelector("#funpairdl-select-all");
  if (selectAll) {
    selectAll.addEventListener("change", () => {
      panel.querySelectorAll(".funpairdl-section-cb").forEach((cb) => {
        cb.checked = selectAll.checked;
        cb.dispatchEvent(new Event("change"));
      });
    });
  }

  updateSendButton(panel, parsed);
}

function updateSendButton(panel, parsed) {
  const sendBtn = panel.querySelector("#funpairdl-send");
  if (!sendBtn || parsed.mode !== "collection") return;
  const checkedCount = panel.querySelectorAll(".funpairdl-section-cb:checked").length;
  sendBtn.textContent = `Send ${checkedCount} pair${checkedCount !== 1 ? "s" : ""} to FunPairDL`;
}

// ─── Send logic ───

// Returns {sent, failed, error?} — pairs enqueued / pairs that errored — so
// the headless auto-send path can report an outcome. The interactive click
// handler ignores the return value; all UI feedback still happens on sendBtn.
async function handleSend(panel, parsed) {
  const sendBtn = panel.querySelector("#funpairdl-send");
  sendBtn.disabled = true;
  sendBtn.textContent = "Checking server...";

  const serverOk = await checkServer();
  if (!serverOk) {
    sendBtn.textContent = "Server offline!";
    sendBtn.classList.add("funpairdl-error");
    setTimeout(() => {
      sendBtn.textContent = parsed.mode === "collection" ? "Send to FunPairDL" : "Send to FunPairDL";
      sendBtn.classList.remove("funpairdl-error");
      sendBtn.disabled = false;
      if (parsed.mode === "collection") updateSendButton(panel, parsed);
    }, 3000);
    return { sent: 0, failed: 0, error: "server_offline" };
  }

  const resSelect = document.getElementById("funpairdl-resolution");
  const preferredResolution = resSelect ? resSelect.value : "best";
  const autoRenameCb = document.getElementById("funpairdl-auto-rename");
  const autoRename = autoRenameCb ? autoRenameCb.checked : true;

  if (parsed.mode === "collection") {
    return await handleCollectionSend(panel, parsed, sendBtn, preferredResolution, autoRename);
  }
  return await handleSingleSend(panel, parsed, sendBtn, preferredResolution, autoRename);
}

async function handleSingleSend(panel, parsed, sendBtn, preferredResolution, autoRename) {
  sendBtn.textContent = "Resolving URLs...";

  // Bucket selected URLs by group, then resolve per-bucket. Within each
  // bucket we still resolve everything in parallel — the only reason we
  // bucket first is to keep the group association after resolution.
  const buckets = {}; // groupName → { videoUrls, scriptUrls, scriptAuthorMap }
  function _bucket(g) {
    if (!buckets[g]) buckets[g] = { videoUrls: [], scriptUrls: [], scriptAuthorMap: {}, filenames: {}, bundlePlan: {} };
    return buckets[g];
  }
  const bundlePlan = panel._bundlePlan || {};

  panel.querySelectorAll('input[name="video"]:checked').forEach((cb) => {
    const idx = parseInt(cb.value);
    const video = parsed.videos[idx];
    const key = `video-${idx}`;
    const gname = (parsed.groupState && parsed.groupState.itemGroup[key]) || "Main";
    const b = _bucket(gname);
    // Work group the row was placed in by the pairing preview (plain rows).
    if (bundlePlan[video.url]) b.bundlePlan[video.url] = bundlePlan[video.url];
    const bundleCbs = panel.querySelectorAll(`.funpairdl-bundle-cb[data-probe-key="${key}"]`);
    if (bundleCbs.length > 0) {
      bundleCbs.forEach((bcb) => {
        if (bcb.checked) {
          const realName = bcb.dataset.fileName || "";
          const fn = realName.toLowerCase();
          // Carry the real filename — bundle file URLs are random ids, so
          // without this the backend can't name pairs or match scripts.
          if (realName) b.filenames[bcb.dataset.fileUrl] = realName;
          if (fn.endsWith(".funscript")) b.scriptUrls.push(bcb.dataset.fileUrl);
          else b.videoUrls.push(bcb.dataset.fileUrl);
          // The sub-group this file was placed in (see _renderBundleGroups).
          if (bundlePlan[bcb.dataset.fileUrl]) b.bundlePlan[bcb.dataset.fileUrl] = bundlePlan[bcb.dataset.fileUrl];
        }
      });
    } else {
      // Probe may have revealed the real filename for a file-locker link
      // (pixeldrain /u/, mega /file/, gofile) whose URL is a random id. Carry
      // it in the filenames map keyed by the SAME URL we push so the backend
      // names the item — and so its off-slot prober (which only fires on items
      // with total_bytes==0) still runs even when we seed a probed size.
      if (video.probedFilename) b.filenames[video.url] = video.probedFilename;
      if ((video.probedFilename || "").toLowerCase().endsWith(".funscript")) {
        // Probe revealed this "video" link is actually a funscript (common when
        // the script is hosted on pixeldrain/mega rather than as a .funscript
        // upload). Route it to scripts so it pairs with the real video instead
        // of landing in a separate group as an orphaned "video".
        b.scriptUrls.push(video.url);
      } else {
        b.videoUrls.push(video.url);
      }
    }
  });

  panel.querySelectorAll('input[name="script"]:checked').forEach((cb) => {
    const idx = parseInt(cb.value);
    const script = parsed.scripts[idx];
    const key = `script-${idx}`;
    const gname = (parsed.groupState && parsed.groupState.itemGroup[key]) || "Main";
    const b = _bucket(gname);
    b.scriptUrls.push(script.url);
    if (script.author) b.scriptAuthorMap[script.url] = script.author;
    if (bundlePlan[script.url]) b.bundlePlan[script.url] = bundlePlan[script.url];
  });

  // Build groups list in the user-visible order; drop empties.
  const groupOrder = (parsed.groupState && parsed.groupState.groups) || ["Main"];
  const groups = [];
  for (const gname of groupOrder) {
    const b = buckets[gname];
    if (!b || (b.videoUrls.length === 0 && b.scriptUrls.length === 0)) continue;
    const [resolvedV, resolvedS] = await Promise.all([
      resolveAllUrls(b.videoUrls), resolveAllUrls(b.scriptUrls),
    ]);
    const resolvedAuthors = {};
    for (let i = 0; i < b.scriptUrls.length; i++) {
      const author = b.scriptAuthorMap[b.scriptUrls[i]];
      if (author) resolvedAuthors[resolvedS[i]] = author;
    }
    // Re-key the real filenames onto the resolved URLs (the backend stores
    // items by their resolved URL).
    const resolvedFilenames = {};
    resolvedV.forEach((u, i) => { if (b.filenames[b.videoUrls[i]]) resolvedFilenames[u] = b.filenames[b.videoUrls[i]]; });
    resolvedS.forEach((u, i) => { if (b.filenames[b.scriptUrls[i]]) resolvedFilenames[u] = b.filenames[b.scriptUrls[i]]; });
    // Probed byte sizes keyed by resolved URL — the backend seeds each
    // item's total_bytes from these so Size shows immediately in the queue.
    const resolvedSizes = {};
    resolvedV.forEach((u, i) => { const sz = _probedSizeFor(b.videoUrls[i], u); if (sz > 0) resolvedSizes[u] = sz; });
    resolvedS.forEach((u, i) => { const sz = _probedSizeFor(b.scriptUrls[i], u); if (sz > 0) resolvedSizes[u] = sz; });
    // Work-group labels keyed by the resolved URL (a forum short-url
    // resolves to the CDN URL the backend stores).
    const resolvedPlan = {};
    resolvedV.forEach((u, i) => { const lb = b.bundlePlan[b.videoUrls[i]]; if (lb) resolvedPlan[u] = lb; });
    resolvedS.forEach((u, i) => { const lb = b.bundlePlan[b.scriptUrls[i]]; if (lb) resolvedPlan[u] = lb; });
    groups.push({
      name: gname,
      videoUrls: resolvedV,
      scriptUrls: resolvedS,
      scriptAuthors: resolvedAuthors,
      filenames: resolvedFilenames,
      sizes: resolvedSizes,
      bundlePlan: resolvedPlan,
      inheritMultiAxis: (parsed.groupState && parsed.groupState.inheritAxes[gname] !== false),
      displayName: (parsed.groupState && parsed.groupState.altNames && parsed.groupState.altNames[gname]) || "",
    });
  }

  if (groups.length === 0) {
    sendBtn.textContent = "Nothing selected!";
    setTimeout(() => { sendBtn.disabled = false; sendBtn.textContent = "Send to FunPairDL"; }, 2000);
    return { sent: 0, failed: 0, error: "nothing_selected" };
  }

  sendBtn.textContent = "Sending...";
  const result = await sendPairToServer({
    title: parsed.title, groups,
    preferredResolution, autoRename,
  });

  if (result.success) {
    sendBtn.textContent = "Sent!";
    sendBtn.classList.add("funpairdl-success");
    setTimeout(() => {
      panel.classList.remove("funpairdl-panel-open");
      setTimeout(() => { if (panel.parentNode) panel.remove(); }, 300);
    }, 1500);
    return { sent: 1, failed: 0, pairIds: result.pair_id ? [result.pair_id] : [] };
  }
  sendBtn.textContent = `Error: ${result.error}`;
  sendBtn.classList.add("funpairdl-error");
  setTimeout(() => {
    sendBtn.textContent = "Send to FunPairDL";
    sendBtn.classList.remove("funpairdl-error");
    sendBtn.disabled = false;
  }, 3000);
  return { sent: 0, failed: 1, error: result.error || "send_failed" };
}

// Where a collection row was parsed from, by its checkbox name:
// "sv-3"/"ss-3" → section 3, "cv"/"cs" → the Comments block.
function _collectionItemOrigin(name) {
  const m = /^s([vs])-(\d+)$/.exec(name || "");
  if (m) return { kind: m[1] === "v" ? "video" : "script", section: m[2] };
  if (name === "cv") return { kind: "video", section: "comments" };
  if (name === "cs") return { kind: "script", section: "comments" };
  return null;
}

// Pure: group checked collection rows by the section they should be sent
// with — their parsed section unless a drag moved them (`overrides` maps
// row key → target section id). Returns { sectionId: { videos, scripts } }
// with each entry as { name, value, key }.
function _bucketCollectionInputs(entries, overrides) {
  const buckets = {};
  for (const { name, value, section } of entries) {
    const origin = _collectionItemOrigin(name);
    if (!origin) continue;
    const key = `${name}-${value}`;
    const moved = overrides && overrides[key];
    // Precedence: an explicit drag move, then the section the row's DOM sits
    // in (comment rows render inside per-post groups), then the parsed origin.
    let target = origin.section;
    if (moved !== undefined && moved !== null) target = moved;
    else if (section !== undefined && section !== null) target = section;
    target = String(target);
    if (!buckets[target]) buckets[target] = { videos: [], scripts: [] };
    buckets[target][origin.kind === "video" ? "videos" : "scripts"]
      .push({ name, value: parseInt(value), key });
  }
  return buckets;
}

function _collectionRowObject(parsed, name, value) {
  const origin = _collectionItemOrigin(name);
  if (!origin) return null;
  if (origin.section === "comments") {
    return origin.kind === "video" ? parsed.commentVideos[value] : parsed.commentScripts[value];
  }
  const section = parsed.sections[parseInt(origin.section)];
  if (!section) return null;
  return origin.kind === "video" ? section.videos[value] : section.scripts[value];
}

async function handleCollectionSend(panel, parsed, sendBtn, preferredResolution, autoRename) {
  const pairs = [];

  // Checked rows, bucketed by the section they sit in now (drag moves
  // included). Every row keeps its original checkbox name/value, so probe
  // results and bundle dropdowns stay attached wherever it was dropped.
  const checked = [...panel.querySelectorAll('.funpairdl-item input[type="checkbox"][name]:checked')]
    .map((cb) => {
      const group = cb.closest(".funpairdl-section-group");
      return { name: cb.name, value: cb.value, section: group ? group.dataset.section : undefined };
    });
  const buckets = _bucketCollectionInputs(checked, parsed.sectionOverride || {});
  // Sections in on-screen order: OP sections, user groups, comment groups.
  const targets = [...panel.querySelectorAll(".funpairdl-section-group")].map((g) => g.dataset.section);
  const bundlePlanAll = panel._bundlePlan || {};

  for (const target of targets) {
    const bucket = buckets[target];
    if (!bucket) continue;
    const sectionCb = panel.querySelector(`.funpairdl-section-cb[data-section="${target}"]`);
    if (!sectionCb || !sectionCb.checked) continue;

    const videoUrls = [];
    const scriptUrls = [];
    const scriptAuthorMap = {};
    const filenameMap = {};
    const bundlePlan = {};

    for (const row of bucket.videos) {
      const v = _collectionRowObject(parsed, row.name, row.value);
      if (!v) continue;
      const bundleCbs = panel.querySelectorAll(`.funpairdl-bundle-cb[data-probe-key="${row.key}"]`);
      if (bundleCbs.length > 0) {
        bundleCbs.forEach((bcb) => {
          if (bcb.checked) {
            const realName = bcb.dataset.fileName || "";
            const fn = realName.toLowerCase();
            if (realName) filenameMap[bcb.dataset.fileUrl] = realName;
            if (fn.endsWith(".funscript")) scriptUrls.push(bcb.dataset.fileUrl);
            else videoUrls.push(bcb.dataset.fileUrl);
            if (bundlePlanAll[bcb.dataset.fileUrl]) bundlePlan[bcb.dataset.fileUrl] = bundlePlanAll[bcb.dataset.fileUrl];
          }
        });
      } else {
        // Probe may have revealed the real filename for a file-locker link
        // (pixeldrain /u/, mega /file/, gofile) whose URL is a random id —
        // record it keyed by the SAME URL we push so the backend names the
        // item and its off-slot prober still fires despite the seeded size.
        if (v.probedFilename) filenameMap[v.url] = v.probedFilename;
        if ((v.probedFilename || "").toLowerCase().endsWith(".funscript")) {
          // Probe revealed this "video" link is actually a funscript — route it
          // to scripts so it pairs instead of becoming an orphaned video.
          scriptUrls.push(v.url);
        } else {
          videoUrls.push(v.url);
        }
      }
    }

    for (const row of bucket.scripts) {
      const script = _collectionRowObject(parsed, row.name, row.value);
      if (!script) continue;
      scriptUrls.push(script.url);
      if (script.author) scriptAuthorMap[script.url] = script.author;
    }

    if (videoUrls.length === 0 && scriptUrls.length === 0) continue;

    const pairName = _collectionPairName(parsed, target);

    // Merge into existing pair with same name
    const existing = pairs.find((p) => p.name === pairName);
    if (existing) {
      existing.videoUrls.push(...videoUrls);
      existing.scriptUrls.push(...scriptUrls);
      Object.assign(existing.scriptAuthorMap, scriptAuthorMap);
      Object.assign(existing.filenameMap, filenameMap);
      Object.assign(existing.bundlePlan, bundlePlan);
    } else {
      pairs.push({ name: pairName, videoUrls, scriptUrls, scriptAuthorMap, filenameMap, bundlePlan });
    }
  }

  if (pairs.length === 0) {
    sendBtn.textContent = "Nothing selected!";
    setTimeout(() => { sendBtn.disabled = false; updateSendButton(panel, parsed); }, 2000);
    return { sent: 0, failed: 0, error: "nothing_selected" };
  }

  sendBtn.textContent = `Resolving URLs (0/${pairs.length})...`;

  let sentCount = 0;
  let failCount = 0;
  const sentPairIds = [];

  for (let i = 0; i < pairs.length; i++) {
    const p = pairs[i];
    sendBtn.textContent = `Resolving (${i + 1}/${pairs.length})...`;
    const [resolvedV, resolvedS] = await Promise.all([
      resolveAllUrls(p.videoUrls), resolveAllUrls(p.scriptUrls),
    ]);

    // Map resolved URLs to authors
    const resolvedAuthors = {};
    if (p.scriptAuthorMap) {
      for (let j = 0; j < p.scriptUrls.length; j++) {
        const author = p.scriptAuthorMap[p.scriptUrls[j]];
        if (author) resolvedAuthors[resolvedS[j]] = author;
      }
    }

    // Re-key real filenames onto resolved URLs (backend stores by resolved URL)
    const resolvedFilenames = {};
    if (p.filenameMap) {
      resolvedV.forEach((u, j) => { if (p.filenameMap[p.videoUrls[j]]) resolvedFilenames[u] = p.filenameMap[p.videoUrls[j]]; });
      resolvedS.forEach((u, j) => { if (p.filenameMap[p.scriptUrls[j]]) resolvedFilenames[u] = p.filenameMap[p.scriptUrls[j]]; });
    }

    // Probed byte sizes keyed by resolved URL (backend seeds total_bytes)
    const resolvedSizes = {};
    resolvedV.forEach((u, j) => { const sz = _probedSizeFor(p.videoUrls[j], u); if (sz > 0) resolvedSizes[u] = sz; });
    resolvedS.forEach((u, j) => { const sz = _probedSizeFor(p.scriptUrls[j], u); if (sz > 0) resolvedSizes[u] = sz; });

    sendBtn.textContent = `Sending (${i + 1}/${pairs.length})...`;
    const result = await sendPairToServer({
      title: p.name, videoUrls: resolvedV, scriptUrls: resolvedS,
      preferredResolution, scriptAuthors: resolvedAuthors, autoRename,
      filenames: resolvedFilenames,
      sizes: resolvedSizes,
      bundlePlan: p.bundlePlan || {},
    });

    if (result.success) {
      sentCount++;
      if (result.pair_id) sentPairIds.push(result.pair_id);
    } else {
      failCount++;
    }
  }

  if (failCount === 0) {
    sendBtn.textContent = `Sent ${sentCount} pairs!`;
    sendBtn.classList.add("funpairdl-success");
    setTimeout(() => {
      panel.classList.remove("funpairdl-panel-open");
      setTimeout(() => { if (panel.parentNode) panel.remove(); }, 300);
    }, 2000);
  } else {
    sendBtn.textContent = `${sentCount} sent, ${failCount} failed`;
    sendBtn.classList.add("funpairdl-error");
    setTimeout(() => {
      sendBtn.classList.remove("funpairdl-error");
      sendBtn.disabled = false;
      updateSendButton(panel, parsed);
    }, 3000);
  }
  return { sent: sentCount, failed: failCount, pairIds: sentPairIds };
}

// ─── Batch overlay (Qt "⬇All") ───
// Full-page overlay rendered in the CURRENT EroScripts tab, one card per
// topic; each card embeds the REAL panel body — probing with file sizes,
// bundle expansion, group dropdowns, drag-to-group, select-all — built from
// the topic's JSON via _parseTopicRemote, so feature parity with the sidebar
// panel without loading any tab. Background tabs render with a 0×0 viewport
// (Discourse virtualizes the post stream to zero posts) and mass tab loads
// trip the forum's 429 rate limit — that's why nothing here ever drives the
// target tabs themselves.

function _topicIdFromUrl(url) {
  const m = String(url || "").match(/\/t\/[^/]+\/(\d+)/);
  return m ? m[1] : null;
}

// Rebuild the minimal post-stream structure the parser reads (.topic-post /
// .cooked / .names .username a / data-post-number) from a topic's JSON.
// Detached — never appended to the live page.
function _buildTopicRootFromJson(data) {
  const root = document.createElement("div");
  for (const p of ((data.post_stream && data.post_stream.posts) || [])) {
    const post = document.createElement("article");
    post.className = "topic-post";
    post.dataset.postNumber = String(p.post_number || "");
    const names = document.createElement("div");
    names.className = "names";
    const uname = document.createElement("span");
    uname.className = "username";
    const a = document.createElement("a");
    a.textContent = p.username || "";
    uname.appendChild(a);
    names.appendChild(uname);
    post.appendChild(names);
    const cooked = document.createElement("div");
    cooked.className = "cooked";
    cooked.innerHTML = p.cooked || "";
    post.appendChild(cooked);
    root.appendChild(post);
  }
  return root;
}

// Fetch a topic's JSON and run the standard parser over a detached rebuild
// of its posts. Returns { parsed } or { error }.
async function _parseTopicRemote(url) {
  if (!_funpairdlHostAllowed()) return { error: "not_eroscripts" };
  const tid = _topicIdFromUrl(url);
  if (!tid) return { error: "not_topic" };
  let data = null;
  try {
    const resp = await fetch(`/t/${tid}.json`, { credentials: "include" });
    if (resp.ok) data = await resp.json();
    else if (resp.status === 403 || resp.status === 404) return { error: "no_access" };
  } catch (e) { data = null; }
  if (!data || !data.post_stream) return { error: "fetch_failed" };
  const root = _buildTopicRootFromJson(data);
  const map = _mapFromPosts(data.post_stream.posts, {});
  const parsed = parseAllPosts(root, (data.title || "").trim(), map);
  if (!parsed) return { error: "no_links" };
  const { totalV, totalS } = _parsedTotals(parsed);
  if (totalV === 0 && totalS === 0) return { error: "no_links" };
  return { parsed };
}

const _BATCH_OVERLAY_CSS = `
#funpairdl-batch-overlay {
  position: fixed; inset: 0; z-index: 999999;
  background: rgba(0, 0, 0, 0.55);
  overflow-y: auto; padding: 24px 0;
}
.funpairdl-batch-box {
  max-width: 880px; margin: 0 auto; background: #1a1a2e; color: #e0e0e0;
  border-radius: 10px; border: 1px solid #333;
  box-shadow: 0 8px 40px rgba(0, 0, 0, 0.6);
  font-family: -apple-system, "Segoe UI", sans-serif;
}
.funpairdl-batch-header {
  position: sticky; top: 0; z-index: 5;
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  padding: 12px 16px; background: #16213e; border-bottom: 1px solid #333;
  border-radius: 10px 10px 0 0;
}
.funpairdl-batch-title { font-weight: 700; font-size: 15px; margin-right: auto; }
.funpairdl-batch-send-all { width: auto !important; padding: 8px 14px !important; }
.funpairdl-batch-cards { padding: 10px 14px 18px; }
.funpairdl-batch-card {
  border: 1px solid #333; border-radius: 8px; margin-top: 10px; background: #101728;
}
.funpairdl-batch-card-header {
  display: flex; align-items: center; gap: 10px; padding: 10px 12px;
  font-weight: 600; font-size: 14px;
}
.funpairdl-batch-card-cb { width: 16px; height: 16px; accent-color: #4a90d9; flex-shrink: 0; }
.funpairdl-batch-card-title { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.funpairdl-batch-card-status { color: #9aa5b1; font-size: 12px; font-weight: 400; flex-shrink: 0; }
.funpairdl-batch-card-sent { opacity: 0.65; }
.funpairdl-batch-card-dead { opacity: 0.5; }
.funpairdl-batch-card-body { padding: 0 10px 10px; }
.funpairdl-batch-panel .funpairdl-panel-body {
  max-height: none; overflow: visible; padding: 0;
}
.funpairdl-batch-card-actions { padding: 8px 2px 2px; }
.funpairdl-batch-card-actions .funpairdl-send-btn {
  width: auto; padding: 7px 12px; font-size: 13px;
}
.funpairdl-batch-close-after {
  display: flex; align-items: center; gap: 4px; flex-shrink: 0;
  font-size: 12px; font-weight: 400; color: #9aa5b1; cursor: pointer;
}
.funpairdl-batch-close-after input { accent-color: #4a90d9; width: 14px; height: 14px; }
`;

// ─── Per-topic selection persistence (localStorage, survives restarts) ───
// Saves each card's checkbox states (items, bundle files, sections), the
// include toggle, and the close-after-download toggle, keyed by topic id.
// Restored when the overlay is reopened so half-finished curation isn't lost.

const _BATCH_SEL_PREFIX = "fpdl_batch_sel_";
const _BATCH_SEL_TTL_MS = 30 * 24 * 3600 * 1000;

function _batchSelKey(url) {
  const tid = _topicIdFromUrl(url);
  return tid ? _BATCH_SEL_PREFIX + tid : null;
}

function _batchSaveCardState(card) {
  const key = _batchSelKey(card._url);
  if (!key) return;
  try {
    const includeCb = card.querySelector(".funpairdl-batch-card-cb");
    const closeCb = card.querySelector(".funpairdl-batch-close-cb");
    const state = {
      ts: Date.now(),
      include: includeCb ? !!includeCb.checked : true,
      closeAfter: closeCb ? !!closeCb.checked : true,
      items: {},
      bundles: {},
      // Collection rows dragged into another section (row key → section id)
      // and the user-created groups they may have been dragged into.
      moves: { ...((card._parsed && card._parsed.sectionOverride) || {}) },
      extraSections: ((card._parsed && card._parsed.extraSections) || [])
        .map((x) => ({ id: x.id, name: x.name || "" })),
      // Bundle sub-group / work-group arrangement (url → label) and any
      // empty work groups the user created.
      bundlePlan: { ...((card._panel && card._panel._bundlePlan) || {}) },
      workGroupsExtra: [...((card._panel && card._panel._workGroupsExtra) || [])],
    };
    if (card._panel) {
      card._panel.querySelectorAll('.funpairdl-item input[type="checkbox"][name]').forEach((cb) => {
        if (/^(video|script|sv-\d+|ss-\d+|cv|cs)$/.test(cb.name)) {
          state.items[`${cb.name}-${cb.value}`] = cb.checked;
        }
      });
      card._panel.querySelectorAll(".funpairdl-section-cb").forEach((cb) => {
        state.items[`sec-${cb.dataset.section}`] = cb.checked;
      });
      card._panel.querySelectorAll(".funpairdl-bundle-cb").forEach((cb) => {
        if (cb.dataset.fileUrl) state.bundles[cb.dataset.fileUrl] = cb.checked;
      });
    }
    localStorage.setItem(key, JSON.stringify(state));
  } catch (e) {}
}

function _batchLoadCardState(url) {
  const key = _batchSelKey(url);
  if (!key) return null;
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const st = JSON.parse(raw);
    if (!st || Date.now() - (st.ts || 0) > _BATCH_SEL_TTL_MS) {
      localStorage.removeItem(key);
      return null;
    }
    return st;
  } catch (e) { return null; }
}

// Idempotent: applies whatever checkboxes exist right now. Called again on a
// short interval because bundle dropdowns appear asynchronously after probes
// — stops as soon as the user touches the card (card._dirty).
function _batchApplyCardState(card, st) {
  if (!card._panel || !st) return;
  const items = st.items || {};
  const bundles = st.bundles || {};
  // Replay drag moves first (no-op for rows already in place) so the
  // checkbox states below land on rows in their final sections.
  if (card._parsed && card._parsed.mode === "collection") {
    for (const x of (st.extraSections || [])) {
      if (x && x.id) _addCollectionSection(card._panel, card._parsed, x.name || "", x.id, false);
    }
    for (const [key, target] of Object.entries(st.moves || {})) {
      const row = card._panel.querySelector(`.funpairdl-item[data-key="${key}"]`);
      if (row) _moveItemToSection(card._panel, card._parsed, row, target);
    }
  }
  card._panel.querySelectorAll('.funpairdl-item input[type="checkbox"][name]').forEach((cb) => {
    const k = `${cb.name}-${cb.value}`;
    if (k in items) cb.checked = items[k];
  });
  card._panel.querySelectorAll(".funpairdl-section-cb").forEach((cb) => {
    const k = `sec-${cb.dataset.section}`;
    if (k in items) cb.checked = items[k];
  });
  card._panel.querySelectorAll(".funpairdl-bundle-cb").forEach((cb) => {
    const u = cb.dataset.fileUrl;
    if (u && u in bundles) cb.checked = bundles[u];
  });
  // Bundle sub-groups: seed the plan so a dropdown rendered later honours
  // it, and re-lay-out dropdowns that already exist.
  if (st.bundlePlan && Object.keys(st.bundlePlan).length > 0) {
    card._panel._bundlePlan = Object.assign(card._panel._bundlePlan || {}, st.bundlePlan);
    card._panel.querySelectorAll(".funpairdl-bundle-files").forEach((dd) => {
      _applyBundlePlanToDom(dd, card._panel._bundlePlan);
    });
  }
  if (Array.isArray(st.workGroupsExtra) && st.workGroupsExtra.length > 0) {
    card._panel._workGroupsExtra = [...st.workGroupsExtra];
  }
  if (card._parsed && card._parsed.mode === "single" &&
      ((st.bundlePlan && Object.keys(st.bundlePlan).length > 0) || (st.workGroupsExtra || []).length > 0)) {
    _scheduleWorkPlan(card._panel, card._parsed);
  }
}

function _batchPruneSavedStates() {
  try {
    const dead = [];
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (!k || !k.startsWith(_BATCH_SEL_PREFIX)) continue;
      try {
        const st = JSON.parse(localStorage.getItem(k) || "null");
        if (!st || Date.now() - (st.ts || 0) > _BATCH_SEL_TTL_MS) dead.push(k);
      } catch (e) { dead.push(k); }
    }
    dead.forEach((k) => localStorage.removeItem(k));
  } catch (e) {}
}

function _batchInjectStyles() {
  if (document.getElementById("funpairdl-batch-style")) return;
  const style = document.createElement("style");
  style.id = "funpairdl-batch-style";
  style.textContent = _BATCH_OVERLAY_CSS;
  document.head.appendChild(style);
}

// Card panel: the sidebar panel's BODY (same builders, same classes → same
// content.css styling) without the fixed-position #funpairdl-panel shell.
// The per-card #funpairdl-send button lets handleSend() run unchanged and
// doubles as the card's live progress display.
function _batchBuildCardPanel(parsed) {
  const panel = document.createElement("div");
  panel.className = "funpairdl-batch-panel";
  panel.dataset.mode = parsed.mode;
  const bodyHtml = parsed.mode === "collection"
    ? buildCollectionPanelHTML(parsed)
    : buildSinglePanelHTML(parsed);
  panel.innerHTML = `
    <div class="funpairdl-panel-body">${bodyHtml}</div>
    <div class="funpairdl-batch-card-actions">
      <button id="funpairdl-send" class="funpairdl-send-btn" type="button">送出這一帖</button>
    </div>`;
  return panel;
}

const _BATCH_ERROR_TEXT = {
  no_links: "無可下載項目",
  fetch_failed: "讀取失敗",
  no_access: "無權限或已刪除",
  not_topic: "非帖子",
  not_eroscripts: "非 EroScripts",
};

async function _batchBuildCard(card, url) {
  const statusEl = card.querySelector(".funpairdl-batch-card-status");
  const bodyWrap = card.querySelector(".funpairdl-batch-card-body");
  const ready = await _parseTopicRemote(url);
  if (ready.error) {
    statusEl.textContent = _BATCH_ERROR_TEXT[ready.error] || ready.error;
    card.classList.add("funpairdl-batch-card-dead");
    const cb = card.querySelector(".funpairdl-batch-card-cb");
    if (cb) { cb.checked = false; cb.disabled = true; }
    bodyWrap.textContent = "";
    return;
  }
  const parsed = ready.parsed;
  card._parsed = parsed;
  if (parsed.title) {
    card.querySelector(".funpairdl-batch-card-title").textContent = parsed.title;
  }
  const panel = _batchBuildCardPanel(parsed);
  card._panel = panel;
  bodyWrap.textContent = "";
  bodyWrap.appendChild(panel);
  statusEl.textContent = "";
  if (parsed.mode === "single") {
    populateSingleItems(panel, parsed);
    _setupSingleSelectAll(panel);
    _enableDragToGroup(panel, parsed);
    _scheduleWorkPlan(panel, parsed);
  } else {
    setupCollectionEvents(panel, parsed);
    _enableDragToSection(panel, parsed);
  }
  _enableDragSelect(panel);
  setupProbing(panel, parsed);
  panel.querySelector("#funpairdl-send")
    .addEventListener("click", () => _batchSendCard(card));

  // Restore the last saved selection for this topic, then keep re-applying
  // briefly (bundle checkboxes appear asynchronously after probes) until the
  // user touches the card. Any change after that autosaves, debounced.
  const saved = _batchLoadCardState(url);
  if (saved) {
    const includeCb = card.querySelector(".funpairdl-batch-card-cb");
    const closeCb = card.querySelector(".funpairdl-batch-close-cb");
    if (includeCb && typeof saved.include === "boolean") includeCb.checked = saved.include;
    if (closeCb && typeof saved.closeAfter === "boolean") closeCb.checked = saved.closeAfter;
    _batchApplyCardState(card, saved);
    const restoreTimer = setInterval(() => {
      if (card._dirty || !card._panel || !card._panel.parentNode) {
        clearInterval(restoreTimer);
        return;
      }
      _batchApplyCardState(card, saved);
    }, 1500);
    setTimeout(() => clearInterval(restoreTimer), 20_000);
  }
  card.addEventListener("change", () => {
    card._dirty = true;
    if (card._saveTimer) clearTimeout(card._saveTimer);
    card._saveTimer = setTimeout(() => _batchSaveCardState(card), 400);
  });
}

async function _batchSendCard(card) {
  // handleSend removes the panel ~1.5 s after a full success (the card body
  // collapses, marking it done) — the header keeps the final status.
  if (card._sending) return { sent: 0, failed: 0 };
  if (!card._panel || !card._panel.parentNode) {
    return card._lastResult || { sent: 0, failed: 0 };
  }
  card._sending = true;
  const statusEl = card.querySelector(".funpairdl-batch-card-status");
  try {
    _batchSaveCardState(card);  // snapshot the final selection
    const res = (await handleSend(card._panel, card._parsed)) || {};
    card._lastResult = res;
    if ((res.sent || 0) > 0 && !(res.failed || 0)) {
      statusEl.textContent = `✓ 已送出 ${res.sent} 組`;
      card.classList.add("funpairdl-batch-card-sent");
      const cb = card.querySelector(".funpairdl-batch-card-cb");
      if (cb) cb.checked = false;
      // Auto-close: hand the created pair ids to the app; it closes this
      // topic's tab(s) once every pair finishes downloading.
      const closeCb = card.querySelector(".funpairdl-batch-close-cb");
      if (closeCb && closeCb.checked && (res.pairIds || []).length > 0) {
        _sendMsg("register-autoclose", {
          url: card._url, pair_ids: res.pairIds,
        });
        statusEl.textContent += "(下載完自動關分頁)";
      }
    } else if (res.error === "nothing_selected") {
      statusEl.textContent = "未勾選任何項目";
    } else if (res.error === "server_offline") {
      statusEl.textContent = "✗ 後端離線";
    } else if (res.failed) {
      statusEl.textContent = `${res.sent || 0} 組送出,${res.failed} 組失敗`;
    } else if (res.error) {
      statusEl.textContent = `✗ ${res.error}`;
    }
    return res;
  } finally {
    card._sending = false;
  }
}

window.funpairdlBatchOpen = function (urls) {
  if (!_funpairdlHostAllowed()) return "not_eroscripts";
  _batchInjectStyles();
  _batchPruneSavedStates();
  const old = document.getElementById("funpairdl-batch-overlay");
  if (old) old.remove();
  // The sidebar panel shares control ids (#funpairdl-resolution,
  // #funpairdl-auto-rename) — close it so the overlay's are the only ones.
  const sidebar = document.getElementById("funpairdl-panel");
  if (sidebar) sidebar.remove();

  const overlay = document.createElement("div");
  overlay.id = "funpairdl-batch-overlay";
  overlay.innerHTML = `
    <div class="funpairdl-batch-box">
      <div class="funpairdl-batch-header">
        <span class="funpairdl-batch-title">⬇ 批量下載(${(urls || []).length} 個帖子)</span>
        <div class="funpairdl-resolution-row" style="margin:0">
          <label class="funpairdl-resolution-label">Resolution</label>
          <select id="funpairdl-resolution" class="funpairdl-resolution-select">
            <option value="best">Best</option>
            <option value="2160">2160p (4K)</option>
            <option value="1080">1080p</option>
            <option value="720">720p</option>
            <option value="480">480p</option>
            <option value="360">360p</option>
          </select>
          <label class="funpairdl-item" style="margin:0;padding:2px 6px">
            <input type="checkbox" id="funpairdl-auto-rename" checked>
            <span class="funpairdl-label">Auto Rename</span>
          </label>
        </div>
        <button class="funpairdl-batch-send-all funpairdl-send-btn" type="button">送出勾選的帖子</button>
        <button class="funpairdl-panel-close funpairdl-batch-close" type="button">✕</button>
      </div>
      <div class="funpairdl-batch-cards"></div>
    </div>`;
  document.body.appendChild(overlay);
  overlay.querySelector(".funpairdl-batch-close")
    .addEventListener("click", () => overlay.remove());

  const cardsEl = overlay.querySelector(".funpairdl-batch-cards");
  const cards = [];
  for (const url of (urls || [])) {
    const card = document.createElement("div");
    card.className = "funpairdl-batch-card";
    card.innerHTML = `
      <div class="funpairdl-batch-card-header">
        <input type="checkbox" class="funpairdl-batch-card-cb" checked>
        <span class="funpairdl-batch-card-title">${escapeAttr(url)}</span>
        <label class="funpairdl-batch-close-after"
               title="這一帖送出後,等它的下載全部完成,自動關閉對應的瀏覽器分頁">
          <input type="checkbox" class="funpairdl-batch-close-cb" checked>完成後關分頁
        </label>
        <span class="funpairdl-batch-card-status">解析中…</span>
      </div>
      <div class="funpairdl-batch-card-body"></div>`;
    card._url = url;
    cardsEl.appendChild(card);
    cards.push(card);
  }

  // Build cards sequentially — one JSON fetch each, gentle on the forum.
  (async () => {
    for (const card of cards) {
      if (!overlay.parentNode) return; // overlay closed — stop fetching
      try {
        await _batchBuildCard(card, card._url);
      } catch (e) {
        card.querySelector(".funpairdl-batch-card-status").textContent =
          "解析失敗: " + ((e && e.message) || e);
      }
    }
  })();

  const sendAllBtn = overlay.querySelector(".funpairdl-batch-send-all");
  sendAllBtn.addEventListener("click", async () => {
    sendAllBtn.disabled = true;
    let sent = 0;
    let failed = 0;
    let done = 0;
    try {
      for (const card of cards) {
        if (!overlay.parentNode) return;
        const cb = card.querySelector(".funpairdl-batch-card-cb");
        if (!cb || !cb.checked || !card._panel || !card._panel.parentNode) continue;
        done += 1;
        sendAllBtn.textContent = `送出中… (${done})`;
        const res = await _batchSendCard(card);
        sent += res.sent || 0;
        failed += res.failed || 0;
      }
      sendAllBtn.textContent =
        `完成:${sent} 組已進佇列` + (failed ? `,${failed} 組失敗` : "");
    } finally {
      setTimeout(() => {
        sendAllBtn.disabled = false;
        sendAllBtn.textContent = "送出勾選的帖子";
      }, 6000);
    }
  });

  return "opened";
};

// ─── Selection helpers (drag-paint + select all/none) ───

// Selectable item checkboxes (excludes auto-rename, Alt-inherit toggles, the
// master select-all itself, and section collapse arrows).
const _SELECTABLE_CB = 'input[name="video"], input[name="script"], ' +
  '.funpairdl-bundle-cb, .funpairdl-section-cb';

function _dragTargetCheckbox(el) {
  if (el && el.tagName === "INPUT" && el.type === "checkbox") {
    return el.matches(_SELECTABLE_CB) ? el : null;
  }
  // Clicking anywhere on an item / bundle-file row toggles its checkbox.
  const label = el && el.closest && el.closest("label.funpairdl-item, label.funpairdl-bundle-file");
  if (label && !label.classList.contains("funpairdl-select-all")) {
    const cb = label.querySelector('input[type="checkbox"]');
    return cb && cb.matches(_SELECTABLE_CB) ? cb : null;
  }
  return null;
}

// Press on a checkbox/row and drag across others to set them all to the same
// state (the value the first one flips to) — fast way to (un)check a range.
function _enableDragSelect(panel) {
  let dragging = false;
  let paintValue = false;
  let suppressClick = false;

  function paint(cb) {
    if (cb && cb.checked !== paintValue) {
      cb.checked = paintValue;
      cb.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }

  panel.addEventListener("mousedown", (e) => {
    suppressClick = false;
    if (e.button !== 0) return;
    if (e.target.closest && e.target.closest(
        "select, button, .funpairdl-section-toggle, .funpairdl-tag-bundle, " +
        ".funpairdl-item-group-select, .funpairdl-drag-handle")) return;
    const cb = _dragTargetCheckbox(e.target);
    if (!cb) return;
    dragging = true;
    paintValue = !cb.checked;
    paint(cb);
    suppressClick = true;   // we toggled manually; cancel the native click
    e.preventDefault();     // also stops text selection while dragging
  });

  panel.addEventListener("mouseover", (e) => {
    if (!dragging) return;
    paint(_dragTargetCheckbox(e.target));
  });

  // Cancel the native toggle that would otherwise undo our manual mousedown
  // toggle (mouseup fires before click, so we clear the flag here, not on up).
  panel.addEventListener("click", (e) => {
    if (suppressClick && _dragTargetCheckbox(e.target)) {
      e.preventDefault();
      e.stopImmediatePropagation();
      suppressClick = false;
    }
  }, true);

  document.addEventListener("mouseup", () => { dragging = false; });
}

// Drag an item by its grip handle and drop it onto any group block to move it
// there. If the grabbed item is checked and other items are too, the whole
// checked selection moves together — so the fast workflow is: drag-select a
// range of checkboxes, then drag one handle to relocate them all at once.
// Uses event delegation on the panel, so it survives group re-renders (which
// detach/re-attach item nodes) and only needs wiring once. Single mode only.
function _enableDragToGroup(panel, parsed) {
  let draggedKeys = [];

  // Plain drag moves the one row under the grip; Ctrl/Shift held at drag
  // start brings every checked row along (same rule as collection mode).
  function _itemsToMove(item, multi) {
    if (!multi) return [item];
    const checked = [...panel.querySelectorAll(".funpairdl-item[data-key]")].filter((it) => {
      const c = it.querySelector('input[type="checkbox"]');
      return c && c.checked;
    });
    return checked.length > 1 ? checked : [item];
  }

  function _clearHighlights() {
    panel.querySelectorAll(".funpairdl-group-block.funpairdl-drag-over")
      .forEach((b) => b.classList.remove("funpairdl-drag-over"));
  }

  panel.addEventListener("dragstart", (e) => {
    const handle = e.target.closest && e.target.closest(".funpairdl-drag-handle");
    if (!handle) return;
    const item = handle.closest(".funpairdl-item[data-key]");
    if (!item) return;
    const moving = _itemsToMove(item, e.ctrlKey || e.shiftKey || e.metaKey);
    draggedKeys = moving.map((it) => it.dataset.key);
    moving.forEach((it) => it.classList.add("funpairdl-dragging"));
    e.dataTransfer.effectAllowed = "move";
    // Setting data is required for the drop event to fire in some engines.
    try { e.dataTransfer.setData("text/plain", draggedKeys.join(",")); } catch (_) {}
  });

  function _clearWorkHighlights() {
    panel.querySelectorAll(".funpairdl-work-group.funpairdl-drag-over")
      .forEach((g) => g.classList.remove("funpairdl-drag-over"));
  }

  panel.addEventListener("dragover", (e) => {
    if (draggedKeys.length === 0) return;
    // A work group (pairing preview inside Main) is a drop target of its own.
    const wg = e.target.closest && e.target.closest(".funpairdl-work-group");
    if (wg) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      _clearHighlights();
      if (!wg.classList.contains("funpairdl-drag-over")) {
        _clearWorkHighlights();
        wg.classList.add("funpairdl-drag-over");
      }
      return;
    }
    const block = e.target.closest && e.target.closest(".funpairdl-group-block");
    if (!block) return;
    e.preventDefault();            // mark this a valid drop target
    e.dataTransfer.dropEffect = "move";
    _clearWorkHighlights();
    if (!block.classList.contains("funpairdl-drag-over")) {
      _clearHighlights();
      block.classList.add("funpairdl-drag-over");
    }
  });

  panel.addEventListener("drop", (e) => {
    if (draggedKeys.length === 0) return;
    const wg = e.target.closest && e.target.closest(".funpairdl-work-group");
    if (wg) {
      e.preventDefault();
      _dropRowsOnWorkGroup(panel, parsed, draggedKeys, wg);
      _updateInheritancePreviews(panel, parsed);
      return;
    }
    const block = e.target.closest && e.target.closest(".funpairdl-group-block");
    if (!block) return;
    e.preventDefault();
    const target = block.dataset.group;
    for (const key of draggedKeys) {
      const item = panel.querySelector(`.funpairdl-item[data-key="${key}"]`);
      if (item) _moveItemToGroup(panel, parsed, item, target);
    }
    _updateInheritancePreviews(panel, parsed);
    _scheduleWorkPlan(panel, parsed);
  });

  panel.addEventListener("dragend", () => {
    panel.querySelectorAll(".funpairdl-dragging").forEach((it) => it.classList.remove("funpairdl-dragging"));
    _clearHighlights();
    _clearWorkHighlights();
    draggedKeys = [];
  });

  // A bare click on the grip would otherwise toggle the row's checkbox (it
  // lives inside the <label>). Swallow it in the capture phase so grabbing
  // the handle never flips the selection.
  panel.addEventListener("click", (e) => {
    if (e.target.closest && e.target.closest(".funpairdl-drag-handle")) {
      e.preventDefault();
      e.stopPropagation();
    }
  }, true);
}

// ─── Bundle sub-groups: the backend's planned split, draggable ───
//
// A pixeldrain list / MEGA or GoFile folder often holds several works. The
// backend splits such a bundle into one pair per work by name matching at
// download time — invisible to the user until folders appear, and wrong
// pairings could not be corrected. The bundle dropdown now asks the backend
// for that plan up front (/bundle/plan — the same code that will run) and
// shows it as sub-groups; files can be dragged between them, groups renamed
// or added, and the arrangement travels with the send as `bundle_plan`.

function _bundleFileRowHTML(f, probeKey) {
  const fname = escapeAttr(f.name);
  const fsize = f.size ? formatSize(f.size) : "";
  const furl = escapeAttr(f.url || "");
  return `<label class="funpairdl-bundle-file funpairdl-bundle-selectable" title="${fname}" data-file-url="${furl}">
    <span class="funpairdl-drag-handle funpairdl-bundle-grip" draggable="true" title="拖曳到其他子組" hidden>⠿</span>
    <input type="checkbox" class="funpairdl-bundle-cb"
           data-probe-key="${probeKey}"
           data-file-url="${furl}"
           data-file-name="${fname}" checked>
    <span class="funpairdl-bundle-fname">${fname}</span>
    <span class="funpairdl-bundle-fsize">${fsize}</span>
  </label>`;
}

// Split a bundle's file list the way send does: funscripts vs everything
// else (the backend types the rest by extension).
function _bundleVideosAndScripts(files) {
  const videos = [];
  const scripts = [];
  for (const f of files || []) {
    if (!f || !f.url) continue;
    const entry = { url: f.url, name: f.name || "" };
    if ((f.name || "").toLowerCase().endsWith(".funscript")) scripts.push(entry);
    else videos.push(entry);
  }
  return { videos, scripts };
}

async function _planBundleLayout(panel, parsed, dropdown, files, probeKey) {
  const { videos, scripts } = _bundleVideosAndScripts(files);
  if (videos.length < 2) return;
  let plan = null;
  try {
    plan = await _sendMsg("bundle-plan", { name: parsed.title || "", videos, scripts });
  } catch (e) { plan = null; }
  if (!plan || !plan.split || !Array.isArray(plan.groups) || plan.groups.length < 2) return;
  if (!dropdown.parentNode) return;
  _renderBundleGroups(panel, dropdown, plan.groups);
}

function _bundleGroupEl(name, userMade, basis) {
  const el = document.createElement("div");
  el.className = "funpairdl-bundle-group";
  if (userMade) el.dataset.user = "1";
  el.innerHTML = `<div class="funpairdl-bundle-group-header">
      <input type="text" class="funpairdl-alt-name-input funpairdl-bundle-group-name"
             placeholder="子組名稱(資料夾名)" value="${escapeAttr(name)}">
      ${_basisTagHTML(basis || "")}<span class="funpairdl-bundle-group-count"></span>
      <button class="funpairdl-group-remove funpairdl-bundle-group-remove" type="button"
              title="解散此子組,檔案併回第一組">✕</button>
    </div>`;
  return el;
}

function _bundleGroupName(groupEl) {
  const inp = groupEl.querySelector(".funpairdl-bundle-group-name");
  return inp ? inp.value.trim() : "";
}

function _refreshBundleGroupCounts(dropdown) {
  dropdown.querySelectorAll(".funpairdl-bundle-group").forEach((g) => {
    let v = 0, s = 0;
    g.querySelectorAll(".funpairdl-bundle-file").forEach((row) => {
      const cb = row.querySelector(".funpairdl-bundle-cb");
      if ((cb && cb.dataset.fileName || "").toLowerCase().endsWith(".funscript")) s++; else v++;
    });
    const badge = g.querySelector(".funpairdl-bundle-group-count");
    if (badge) badge.textContent = [v ? `${v}V` : "", s ? `${s}S` : ""].filter(Boolean).join(" + ") || "empty";
  });
}

// Record the labels of every row so send-time has a full url → label map.
function _syncBundlePlanFromDom(panel, dropdown) {
  if (!panel._bundlePlan) panel._bundlePlan = {};
  dropdown.querySelectorAll(".funpairdl-bundle-group").forEach((g) => {
    const name = _bundleGroupName(g);
    g.querySelectorAll(".funpairdl-bundle-file[data-file-url]").forEach((row) => {
      if (name) panel._bundlePlan[row.dataset.fileUrl] = name;
      else delete panel._bundlePlan[row.dataset.fileUrl];
    });
  });
}

// Move rows into the groups their labels name, creating groups as needed.
// Idempotent — the batch card replays a saved plan through this repeatedly
// while the dropdowns are still appearing.
function _applyBundlePlanToDom(dropdown, plan) {
  if (!plan || !dropdown.querySelector(".funpairdl-bundle-group")) return false;
  let changed = false;
  dropdown.querySelectorAll(".funpairdl-bundle-file[data-file-url]").forEach((row) => {
    const label = plan[row.dataset.fileUrl];
    if (!label) return;
    const groups = [...dropdown.querySelectorAll(".funpairdl-bundle-group")];
    let target = groups.find((g) => _bundleGroupName(g) === label);
    if (!target) {
      target = _bundleGroupEl(label, true);
      const addBtn = dropdown.querySelector(".funpairdl-bundle-add-group");
      if (addBtn) addBtn.before(target); else dropdown.appendChild(target);
      changed = true;
    }
    if (row.parentNode !== target) { target.appendChild(row); changed = true; }
  });
  if (changed) {
    // Backend-made groups emptied by the plan disappear; user-made stay.
    dropdown.querySelectorAll(".funpairdl-bundle-group").forEach((g) => {
      if (!g.dataset.user && !g.querySelector(".funpairdl-bundle-file")) g.remove();
    });
    _refreshBundleGroupCounts(dropdown);
  }
  return changed;
}

function _renderBundleGroups(panel, dropdown, groups) {
  if (!panel._bundlePlan) panel._bundlePlan = {};
  const rows = new Map();
  dropdown.querySelectorAll(".funpairdl-bundle-file[data-file-url]").forEach((row) => {
    rows.set(row.dataset.fileUrl, row);
    const grip = row.querySelector(".funpairdl-bundle-grip");
    if (grip) grip.hidden = false;
  });

  const hint = document.createElement("div");
  hint.className = "funpairdl-bundle-plan-hint";
  hint.textContent = `這個 bundle 會拆成 ${groups.length} 組(每組一個資料夾);可拖曳檔案調整、改名或新增子組`;
  dropdown.appendChild(hint);

  const placed = new Set();
  groups.forEach((g, i) => {
    const el = _bundleGroupEl(g.name || `Group ${i + 1}`, false, g.basis || "");
    for (const url of [...(g.videos || []), ...(g.scripts || [])]) {
      const row = rows.get(url);
      if (row && !placed.has(url)) { el.appendChild(row); placed.add(url); }
    }
    dropdown.appendChild(el);
  });
  // Anything the plan did not mention rides with the first group.
  const first = dropdown.querySelector(".funpairdl-bundle-group");
  for (const [url, row] of rows) if (!placed.has(url) && first) first.appendChild(row);

  const addBtn = document.createElement("button");
  addBtn.type = "button";
  addBtn.className = "funpairdl-add-alt-btn funpairdl-bundle-add-group";
  addBtn.textContent = "+ 新增子組";
  dropdown.appendChild(addBtn);

  // A plan restored from the batch card (or set by an earlier render) wins
  // over the backend's suggestion for the files it names.
  _applyBundlePlanToDom(dropdown, panel._bundlePlan);
  _syncBundlePlanFromDom(panel, dropdown);
  _refreshBundleGroupCounts(dropdown);
  _wireBundleGroups(panel, dropdown);
}

function _wireBundleGroups(panel, dropdown) {
  const touched = () => {
    _syncBundlePlanFromDom(panel, dropdown);
    _refreshBundleGroupCounts(dropdown);
    panel.dispatchEvent(new Event("change", { bubbles: true }));
  };

  dropdown.addEventListener("input", (e) => {
    if (e.target.classList && e.target.classList.contains("funpairdl-bundle-group-name")) touched();
  });
  dropdown.addEventListener("click", (e) => {
    const rm = e.target.closest && e.target.closest(".funpairdl-bundle-group-remove");
    if (rm) {
      e.preventDefault(); e.stopPropagation();
      const g = rm.closest(".funpairdl-bundle-group");
      const groups = [...dropdown.querySelectorAll(".funpairdl-bundle-group")];
      if (!g || groups.length <= 1) return;
      const dest = groups.find((x) => x !== g);
      g.querySelectorAll(".funpairdl-bundle-file").forEach((row) => dest.appendChild(row));
      g.remove();
      touched();
      return;
    }
    const add = e.target.closest && e.target.closest(".funpairdl-bundle-add-group");
    if (add) {
      e.preventDefault(); e.stopPropagation();
      const n = dropdown.querySelectorAll(".funpairdl-bundle-group").length + 1;
      const g = _bundleGroupEl(`新子組 ${n}`, true);
      add.before(g);
      touched();
      const inp = g.querySelector(".funpairdl-bundle-group-name");
      if (inp) { inp.focus(); inp.select(); }
      return;
    }
    // Typing in the name field must not toggle checkboxes or fold sections.
    if (e.target.classList && e.target.classList.contains("funpairdl-bundle-group-name")) {
      e.stopPropagation();
    }
  });

  let dragged = null;
  dropdown.addEventListener("dragstart", (e) => {
    const grip = e.target.closest && e.target.closest(".funpairdl-bundle-grip");
    if (!grip) return;
    e.stopPropagation();  // not a section/group row — keep the panel handlers out
    dragged = grip.closest(".funpairdl-bundle-file");
    if (!dragged) return;
    dragged.classList.add("funpairdl-dragging");
    e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", dragged.dataset.fileUrl || ""); } catch (_) {}
  });
  dropdown.addEventListener("dragover", (e) => {
    if (!dragged) return;
    const g = e.target.closest && e.target.closest(".funpairdl-bundle-group");
    if (!g) return;
    e.preventDefault(); e.stopPropagation();
    e.dataTransfer.dropEffect = "move";
    dropdown.querySelectorAll(".funpairdl-bundle-group.funpairdl-drag-over")
      .forEach((x) => { if (x !== g) x.classList.remove("funpairdl-drag-over"); });
    g.classList.add("funpairdl-drag-over");
  });
  dropdown.addEventListener("drop", (e) => {
    if (!dragged) return;
    const g = e.target.closest && e.target.closest(".funpairdl-bundle-group");
    if (!g) return;
    e.preventDefault(); e.stopPropagation();
    if (dragged.parentNode !== g) { g.appendChild(dragged); touched(); }
  });
  dropdown.addEventListener("dragend", () => {
    if (dragged) dragged.classList.remove("funpairdl-dragging");
    dragged = null;
    dropdown.querySelectorAll(".funpairdl-drag-over").forEach((x) => x.classList.remove("funpairdl-drag-over"));
  });
}

// ─── Media hints: thumbnail + scene tags on a video row ───
//
// e621 posts in one topic often share a title, so the row text alone can't
// say which scene a video is. The probe returns the post's thumbnail and
// tags; the row gets a 🖼 that shows the thumbnail on hover and a short
// scene-tag line, so the user can tell the videos apart and check the
// pairing preview against them.

// Whole-word scene vocabulary (tags are matched with underscores as
// spaces, so "blowjob_face" hits "blowjob" but "bedroom_eyes" is not "bed").
const _SCENE_TAG_RE = /\b(position|sex|oral|fellatio|blowjob|shower|kneel\w*|stand\w*|sitting|lying|behind|front|cowgirl|missionary|doggy\w*|riding|handjob|footjob|titjob|anal|vaginal|masturbat\w*|69|facesit\w*|dominant|submissive|pov|outdoors?|indoors?|sofa|pool|bath\w*|loop|animated)\b/i;

function _sceneTags(tags, limit) {
  const uniq = [...new Set((tags || []).map((t) => String(t).trim().replace(/_/g, " ")).filter(Boolean))];
  const isScene = (t) => _SCENE_TAG_RE.test(t);
  const scene = uniq.filter(isScene);
  const rest = uniq.filter((t) => !isScene(t));
  return [...scene, ...rest].slice(0, limit);
}

let _thumbPop = null;
function _showThumbPop(src, x, y) {
  if (!_thumbPop) {
    _thumbPop = document.createElement("div");
    _thumbPop.className = "funpairdl-thumb-pop";
    _thumbPop.innerHTML = `<img alt="">`;
    document.body.appendChild(_thumbPop);
  }
  const img = _thumbPop.querySelector("img");
  if (img.getAttribute("src") !== src) img.setAttribute("src", src);
  _thumbPop.style.left = `${Math.max(8, x - 260)}px`;
  _thumbPop.style.top = `${Math.max(8, y - 20)}px`;
  _thumbPop.hidden = false;
}
function _hideThumbPop() { if (_thumbPop) _thumbPop.hidden = true; }

function _attachMediaHints(item, info) {
  if (!item || !info) return;
  const tags = Array.isArray(info.tags) ? info.tags : [];
  const thumb = info.thumbnail || "";
  if (!tags.length && !thumb) return;
  if (item.querySelector(".funpairdl-media-hints")) return;
  const wrap = document.createElement("span");
  wrap.className = "funpairdl-media-hints";
  if (thumb) {
    const icon = document.createElement("span");
    icon.className = "funpairdl-thumb-icon";
    icon.textContent = "🖼";
    icon.title = "懸停預覽縮圖";
    icon.addEventListener("mouseenter", (e) => _showThumbPop(thumb, e.clientX, e.clientY));
    icon.addEventListener("mousemove", (e) => _showThumbPop(thumb, e.clientX, e.clientY));
    icon.addEventListener("mouseleave", _hideThumbPop);
    icon.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); });
    wrap.appendChild(icon);
  }
  if (tags.length) {
    const line = document.createElement("span");
    line.className = "funpairdl-scene-tags";
    line.textContent = _sceneTags(tags, 5).join(" · ");
    line.title = _sceneTags(tags, 40).join(", ");
    wrap.appendChild(line);
  }
  const size = item.querySelector(".funpairdl-size");
  if (size) size.before(wrap); else item.appendChild(wrap);
}

// ─── Single mode: pairing preview for the Main group ───
//
// Main often holds several videos — mirrors of one work on different hosts,
// or genuinely different works — plus a pile of scripts, and nothing said
// which script belongs to which video, or whether the videos are the same
// work at all. The backend decides that at download time (one folder per
// work). Ask it up front, show the answer under Main as "work groups", tag
// each row with its group, and let the user drag rows onto a group (or a new
// one) to correct it. Corrections travel as bundle_plan (row url → label).

const WORK_PLAN_DEBOUNCE_MS = 700;
const _WORK_COLORS = ["#4a90d9", "#2e9e6a", "#c2842a", "#8b5cf6", "#d9534f", "#0ea5b7"];

function _scheduleWorkPlan(panel, parsed) {
  if (!parsed || parsed.mode !== "single") return;
  if (panel._workPlanTimer) clearTimeout(panel._workPlanTimer);
  panel._workPlanTimer = setTimeout(() => { _refreshWorkPlan(panel, parsed); }, WORK_PLAN_DEBOUNCE_MS);
}

// Plain (non-bundle) rows currently in Main, with the best name we have.
function _mainWorkRows(panel, parsed) {
  const body = panel.querySelector('.funpairdl-group-body[data-group="Main"]');
  const videos = [];
  const scripts = [];
  if (!body) return { videos, scripts };
  body.querySelectorAll(".funpairdl-item[data-key]").forEach((row) => {
    const idx = parseInt(row.dataset.index);
    if (row.dataset.kind === "video") {
      const v = parsed.videos[idx];
      if (!v) return;
      // A bundle row is planned inside its own dropdown.
      if (panel.querySelector(`.funpairdl-bundle-cb[data-probe-key="${row.dataset.key}"]`)) return;
      videos.push({
        url: v.url, name: v.probedFilename || "", row,
        hints: (v.probedTags || []).join(" "),
        duration: v.probedDuration || 0,
      });
    } else if (row.dataset.kind === "script") {
      const s = parsed.scripts[idx];
      if (s) scripts.push({
        url: s.url, name: s.filename || "", row,
        duration: s.probedDuration || 0, link: s.probedLink || "",
      });
    }
  });
  return { videos, scripts };
}

// Pure: display groups from the rows' labels — plan order first, then
// user-added empty groups, then an implicit "auto" bucket for unlabelled
// rows. Each group: { name, user, videos: [row entries], scripts: [...] }.
function _workPlanGroups(rows, plan, extraNames, order) {
  const groups = [];
  const byName = new Map();
  const get = (name, user) => {
    if (!byName.has(name)) {
      const g = { name, user: !!user, videos: [], scripts: [] };
      byName.set(name, g);
      groups.push(g);
    }
    return byName.get(name);
  };
  for (const n of order || []) get(n, false);
  for (const n of extraNames || []) get(n, true);
  const auto = { name: "", user: false, videos: [], scripts: [] };
  for (const r of rows.videos) (plan[r.url] ? get(plan[r.url], false) : auto).videos.push(r);
  for (const r of rows.scripts) (plan[r.url] ? get(plan[r.url], false) : auto).scripts.push(r);
  if (auto.videos.length || auto.scripts.length) groups.push(auto);
  return groups;
}

function _workPlanBlock(panel) {
  const main = panel.querySelector('.funpairdl-group-block[data-group="Main"]');
  if (!main) return null;
  let block = main.querySelector(".funpairdl-work-plan");
  if (!block) {
    block = document.createElement("div");
    block.className = "funpairdl-work-plan";
    const body = main.querySelector(".funpairdl-group-body");
    if (body) body.after(block); else main.appendChild(block);
  }
  return block;
}

function _setWorkBadge(row, idx, color) {
  let badge = row.querySelector(".funpairdl-tag-work");
  if (idx == null) { if (badge) badge.remove(); return; }
  if (!badge) {
    badge = document.createElement("span");
    badge.className = "funpairdl-tag-work";
    const size = row.querySelector(".funpairdl-size");
    if (size) size.before(badge); else row.appendChild(badge);
  }
  badge.textContent = `組${idx + 1}`;
  badge.style.background = color;
}

async function _refreshWorkPlan(panel, parsed) {
  if (!panel._bundlePlan) panel._bundlePlan = {};
  if (!panel._workGroupsExtra) panel._workGroupsExtra = [];
  const block = _workPlanBlock(panel);
  if (!block) return;
  const rows = _mainWorkRows(panel, parsed);
  const all = [...rows.videos, ...rows.scripts];
  if (rows.videos.length < 2 && panel._workGroupsExtra.length === 0) {
    block.hidden = true;
    for (const r of all) _setWorkBadge(r.row, null);
    return;
  }
  block.hidden = false;

  // Ask the backend once per distinct row set; drags and renames only
  // re-render.
  const key = JSON.stringify([
    rows.videos.map((r) => [r.url, r.name, r.hints || "", r.duration || 0]),
    rows.scripts.map((r) => [r.url, r.name, r.duration || 0, r.link || ""]),
  ]);
  if (panel._workPlanKey !== key) {
    const seq = (panel._workPlanSeq = (panel._workPlanSeq || 0) + 1);
    let plan = null;
    try {
      plan = await _sendMsg("bundle-plan", {
        name: parsed.title || "",
        videos: rows.videos.map((r) => ({
          url: r.url, name: r.name, hints: r.hints || "", duration: r.duration || null,
        })),
        scripts: rows.scripts.map((r) => ({
          url: r.url, name: r.name, duration: r.duration || null, link: r.link || "",
        })),
      });
    } catch (e) { plan = null; }
    if (seq !== panel._workPlanSeq) return;   // a newer request superseded this one
    panel._workPlanKey = key;
    panel._workPlanSplit = !!(plan && plan.split);
    panel._workPlanOrder = [];
    // Labels the backend seeded last time — a row still carrying its seeded
    // label was never touched by the user, so a fresh plan (names change as
    // probes reveal real filenames) may overwrite it. Labels that differ
    // from their seed are the user's and stay.
    const seeded = panel._workPlanSeeded || {};
    const nextSeeded = {};
    panel._workScriptBasis = {};
    if (plan && plan.split && Array.isArray(plan.groups)) {
      for (const g of plan.groups) {
        const name = g.name || "";
        if (!name) continue;
        panel._workPlanOrder.push(name);
        for (const u of [...(g.videos || []), ...(g.scripts || [])]) {
          const cur = panel._bundlePlan[u];
          if (!cur || cur === seeded[u]) panel._bundlePlan[u] = name;
          nextSeeded[u] = name;
        }
        for (const [u, b] of Object.entries(g.script_basis || {})) panel._workScriptBasis[u] = b;
      }
    } else {
      // Mirrors / one work: drop labels that were only ever seeded.
      for (const [u, name] of Object.entries(seeded)) {
        if (panel._bundlePlan[u] === name) delete panel._bundlePlan[u];
      }
    }
    panel._workPlanSeeded = nextSeeded;
  }
  _renderWorkPlan(panel, parsed);
}

function _renderWorkPlan(panel, parsed) {
  const block = _workPlanBlock(panel);
  if (!block) return;
  const rows = _mainWorkRows(panel, parsed);
  const groups = _workPlanGroups(rows, panel._bundlePlan || {}, panel._workGroupsExtra || [], panel._workPlanOrder || []);
  const labelled = groups.filter((g) => g.name);
  const nVideos = rows.videos.length;
  const nScripts = rows.scripts.length;

  let head;
  if (labelled.length === 0) {
    head = `✔ 送出後是同一部作品:${nVideos} 個影片互為鏡像(只會下載勾選的),${nScripts} 支腳本都屬於它。`;
  } else {
    head = `送出後會拆成 ${labelled.length} 個作品(每個一個資料夾);每組標籤是配對依據,橘色的「順序(猜測)」請自行核對;拖曳列到組上可調整,組名即資料夾名。`;
  }
  const seeded = panel._workPlanSeeded || {};
  const scriptBasis = panel._workScriptBasis || {};
  let html = `<div class="funpairdl-work-plan-head">${escapeAttr(head)}</div>`;
  groups.forEach((g, i) => {
    const color = _WORK_COLORS[i % _WORK_COLORS.length];
    const title = g.name
      ? `<input type="text" class="funpairdl-alt-name-input funpairdl-work-group-name" data-old="${escapeAttr(g.name)}" value="${escapeAttr(g.name)}" placeholder="作品名稱(資料夾名)">`
      : `<span class="funpairdl-work-group-auto">其餘(依名稱自動配對)</span>`;
    // A script the user dragged (label differs from the backend's seed) is
    // "plan"; otherwise what the backend reported for it.
    const basisOf = (r) => {
      const lb = (panel._bundlePlan || {})[r.url];
      if (lb && seeded[r.url] && lb !== seeded[r.url]) return "plan";
      if (lb && !seeded[r.url]) return "plan";
      return scriptBasis[r.url] || "";
    };
    const members = [
      ...g.videos.map((r) => {
        const label = (r.row.querySelector(".funpairdl-label") || {}).textContent || r.name || r.url;
        const scene = _sceneTags(r.hints ? r.hints.split(" ") : [], 4).join(", ");
        const dur = formatDuration(r.duration);
        return `<span class="funpairdl-work-member funpairdl-work-member-v">🎬 ${escapeAttr(label)}${dur ? ` <b>${dur}</b>` : ""}${scene ? ` <i>(${escapeAttr(scene)})</i>` : ""}</span>`;
      }),
      ...g.scripts.map((r) => {
        const dur = formatDuration(r.duration);
        return `<span class="funpairdl-work-member">📜 ${escapeAttr(r.name || r.url)}${dur ? ` <b>${dur}</b>` : ""}</span>`;
      }),
    ].join("");
    const gBasis = g.name ? _weakestBasis(g.scripts.map(basisOf)) : "";
    const remove = g.name && g.user && g.videos.length === 0 && g.scripts.length === 0
      ? `<button class="funpairdl-group-remove funpairdl-work-group-remove" type="button" data-name="${escapeAttr(g.name)}" title="移除空的作品組">✕</button>` : "";
    html += `<div class="funpairdl-work-group" data-name="${escapeAttr(g.name)}" style="border-left-color:${color}">
      <div class="funpairdl-work-group-header"><span class="funpairdl-tag-work" style="background:${color}">組${i + 1}</span>${title}
        ${_basisTagHTML(gBasis)}<span class="funpairdl-work-group-count">${g.videos.length}V + ${g.scripts.length}S</span>${remove}</div>
      <div class="funpairdl-work-members">${members || '<span class="funpairdl-work-empty">拖曳列到這裡</span>'}</div>
    </div>`;
    for (const r of [...g.videos, ...g.scripts]) _setWorkBadge(r.row, groups.length > 1 ? i : null, color);
  });
  html += `<button type="button" class="funpairdl-add-alt-btn funpairdl-work-add">+ 新增作品組</button>`;
  block.innerHTML = html;
  if (!block._wired) { _wireWorkPlan(panel, parsed, block); block._wired = true; }
}

function _wireWorkPlan(panel, parsed, block) {
  const touched = () => {
    _renderWorkPlan(panel, parsed);
    panel.dispatchEvent(new Event("change", { bubbles: true }));
  };
  block.addEventListener("click", (e) => {
    const add = e.target.closest && e.target.closest(".funpairdl-work-add");
    if (add) {
      e.preventDefault(); e.stopPropagation();
      let n = 1;
      const taken = new Set([...(panel._workPlanOrder || []), ...(panel._workGroupsExtra || []), ...Object.values(panel._bundlePlan || {})]);
      while (taken.has(`作品 ${n}`)) n++;
      panel._workGroupsExtra.push(`作品 ${n}`);
      touched();
      return;
    }
    const rm = e.target.closest && e.target.closest(".funpairdl-work-group-remove");
    if (rm) {
      e.preventDefault(); e.stopPropagation();
      panel._workGroupsExtra = (panel._workGroupsExtra || []).filter((n) => n !== rm.dataset.name);
      touched();
      return;
    }
    if (e.target.classList && e.target.classList.contains("funpairdl-work-group-name")) e.stopPropagation();
  });
  block.addEventListener("change", (e) => {
    const inp = e.target;
    if (!inp.classList || !inp.classList.contains("funpairdl-work-group-name")) return;
    e.stopPropagation();
    const oldName = inp.dataset.old;
    const newName = inp.value.trim();
    if (!newName || newName === oldName) { inp.value = oldName; return; }
    for (const [u, lb] of Object.entries(panel._bundlePlan || {})) if (lb === oldName) panel._bundlePlan[u] = newName;
    panel._workPlanOrder = (panel._workPlanOrder || []).map((n) => (n === oldName ? newName : n));
    panel._workGroupsExtra = (panel._workGroupsExtra || []).map((n) => (n === oldName ? newName : n));
    touched();
  });
}

// Drop handling for work groups (called from _enableDragToGroup).
function _dropRowsOnWorkGroup(panel, parsed, keys, groupEl) {
  const name = groupEl.dataset.name || "";
  if (!panel._bundlePlan) panel._bundlePlan = {};
  for (const key of keys) {
    const row = panel.querySelector(`.funpairdl-item[data-key="${key}"]`);
    if (!row) continue;
    const idx = parseInt(row.dataset.index);
    const obj = row.dataset.kind === "video" ? parsed.videos[idx] : parsed.scripts[idx];
    if (!obj) continue;
    if ((parsed.groupState.itemGroup[key] || "Main") !== "Main") _moveItemToGroup(panel, parsed, row, "Main");
    if (name) panel._bundlePlan[obj.url] = name; else delete panel._bundlePlan[obj.url];
  }
  _renderWorkPlan(panel, parsed);
  panel.dispatchEvent(new Event("change", { bubbles: true }));
}

// ─── Collection mode: drag rows between sections ───

function _syncSectionCheckbox(group) {
  if (!group) return;
  const cb = group.querySelector(".funpairdl-section-cb");
  const body = group.querySelector(".funpairdl-section-body");
  if (!cb || !body) return;
  cb.checked = body.querySelector('.funpairdl-item input[type="checkbox"]:checked') !== null;
}

// Recount the "2V + 1S" badges from what each section body holds now.
function _refreshSectionCounts(panel) {
  panel.querySelectorAll(".funpairdl-section-group").forEach((group) => {
    const badge = group.querySelector(".funpairdl-section-count");
    const body = group.querySelector(".funpairdl-section-body");
    if (!badge || !body) return;
    const v = body.querySelectorAll('.funpairdl-item[data-kind="video"]').length;
    const s = body.querySelectorAll('.funpairdl-item[data-kind="script"]').length;
    badge.textContent = [v ? `${v}V` : "", s ? `${s}S` : ""].filter(Boolean).join(" + ") || "empty";
  });
}

/**
 * Move a collection row (and its bundle dropdown) into another section.
 * The row keeps its checkbox name/value — probe results, bundle files and
 * the saved-selection keys all hang off those — and the move is recorded in
 * parsed.sectionOverride, which handleCollectionSend honours. A dropped row
 * is checked so it counts toward its new section right away.
 * Returns true when the row actually changed section.
 */
function _moveItemToSection(panel, parsed, item, target) {
  if (!item) return false;
  const group = panel.querySelector(`.funpairdl-section-group[data-section="${target}"]`);
  const body = group && group.querySelector(".funpairdl-section-body");
  if (!body) return false;
  const from = item.closest(".funpairdl-section-group");
  if (from === group) return false;

  // "home" = the section the row was rendered in; a move back there clears
  // the override instead of recording one.
  if (!item.dataset.home && from) item.dataset.home = from.dataset.section;
  const cb = item.querySelector('input[type="checkbox"][name]');
  if (!parsed.sectionOverride) parsed.sectionOverride = {};
  if (item.dataset.home === String(target)) delete parsed.sectionOverride[item.dataset.key];
  else parsed.sectionOverride[item.dataset.key] = String(target);

  const dropdown = _itemBundleDropdown(item);
  body.appendChild(item);
  if (dropdown) body.appendChild(dropdown);
  if (cb && !cb.checked) cb.checked = true;
  // Show the landing spot; the section starts collapsed.
  if (body.style.display === "none") {
    body.style.display = "block";
    const toggle = group.querySelector(".funpairdl-section-toggle");
    if (toggle) toggle.textContent = "▾";
  }
  _syncSectionCheckbox(from);
  _syncSectionCheckbox(group);
  _refreshSectionCounts(panel);
  return true;
}

function _enableDragToSection(panel, parsed) {
  let draggedKeys = [];

  // Remember where every row started so a move back home clears its
  // override and a removed user group can return its rows.
  panel.querySelectorAll(".funpairdl-item[data-key]").forEach((row) => {
    const g = row.closest(".funpairdl-section-group");
    if (g && !row.dataset.home) row.dataset.home = g.dataset.section;
  });

  // Plain drag moves the one row under the grip. Everything is checked by
  // default in collection mode, so "checked rows travel together" would drag
  // the whole panel — that needs Ctrl/Shift held when the drag starts.
  function _itemsToMove(item, multi) {
    if (!multi) return [item];
    const checked = [...panel.querySelectorAll(".funpairdl-item[data-key]")].filter((it) => {
      const c = it.querySelector('input[type="checkbox"]');
      return c && c.checked;
    });
    return checked.length > 1 ? checked : [item];
  }

  function _clearHighlights() {
    panel.querySelectorAll(".funpairdl-section-group.funpairdl-drag-over")
      .forEach((g) => g.classList.remove("funpairdl-drag-over"));
  }

  panel.addEventListener("dragstart", (e) => {
    const handle = e.target.closest && e.target.closest(".funpairdl-drag-handle");
    if (!handle) return;
    const item = handle.closest(".funpairdl-item[data-key]");
    if (!item) return;
    const moving = _itemsToMove(item, e.ctrlKey || e.shiftKey || e.metaKey);
    draggedKeys = moving.map((it) => it.dataset.key);
    moving.forEach((it) => it.classList.add("funpairdl-dragging"));
    e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", draggedKeys.join(",")); } catch (_) {}
  });

  panel.addEventListener("dragover", (e) => {
    if (draggedKeys.length === 0) return;
    const group = e.target.closest && e.target.closest(".funpairdl-section-group");
    if (!group) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    if (!group.classList.contains("funpairdl-drag-over")) {
      _clearHighlights();
      group.classList.add("funpairdl-drag-over");
    }
  });

  panel.addEventListener("drop", (e) => {
    if (draggedKeys.length === 0) return;
    const group = e.target.closest && e.target.closest(".funpairdl-section-group");
    if (!group) return;
    e.preventDefault();
    const target = group.dataset.section;
    let moved = false;
    for (const key of draggedKeys) {
      const item = panel.querySelector(`.funpairdl-item[data-key="${key}"]`);
      if (item && _moveItemToSection(panel, parsed, item, target)) moved = true;
    }
    if (moved) {
      updateSendButton(panel, parsed);
      // Let the batch card autosave pick the move up.
      panel.dispatchEvent(new Event("change", { bubbles: true }));
    }
  });

  panel.addEventListener("dragend", () => {
    panel.querySelectorAll(".funpairdl-dragging").forEach((it) => it.classList.remove("funpairdl-dragging"));
    _clearHighlights();
    draggedKeys = [];
  });

  // A click on the grip must not toggle the row's checkbox (see
  // _enableDragToGroup).
  panel.addEventListener("click", (e) => {
    if (e.target.closest && e.target.closest(".funpairdl-drag-handle")) {
      e.preventDefault();
      e.stopPropagation();
    }
  }, true);
}

// Single-mode master checkbox: toggles every item/bundle checkbox and reflects
// the aggregate state (checked / unchecked / indeterminate).
function _setupSingleSelectAll(panel) {
  const master = panel.querySelector("#funpairdl-select-all");
  if (!master) return;
  const items = () => panel.querySelectorAll(_SELECTABLE_CB);

  master.addEventListener("change", () => {
    items().forEach((cb) => {
      if (cb.checked !== master.checked) {
        cb.checked = master.checked;
        cb.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
  });

  function sync() {
    const all = [...items()];
    const checked = all.filter((c) => c.checked).length;
    master.checked = all.length > 0 && checked === all.length;
    master.indeterminate = checked > 0 && checked < all.length;
  }
  panel.addEventListener("change", (e) => {
    if (e.target !== master && e.target.matches && e.target.matches(_SELECTABLE_CB)) sync();
  });
  // Bundle dropdowns are added asynchronously after probing; re-sync once they
  // settle so the master reflects them too.
  setTimeout(sync, 1500);
}

// ─── Main injection ───

function _parsedTotals(parsed) {
  const totalV = parsed.mode === "collection"
    ? parsed.sections.reduce((n, s) => n + s.videos.length, 0) + (parsed.commentVideos?.length || 0)
    : parsed.videos.length;
  const totalS = parsed.mode === "collection"
    ? parsed.sections.reduce((n, s) => n + s.scripts.length, 0) + (parsed.commentScripts?.length || 0)
    : parsed.scripts.length;
  return { totalV, totalS };
}

function injectButton() {
  if (document.getElementById("funpairdl-send-btn")) return;

  // Warm the link metadata cache so it's ready by the time the user clicks.
  ensureLinkMetadata();

  const parsed = parseAllPosts();
  if (!parsed) return;

  const { totalV, totalS } = _parsedTotals(parsed);

  if (totalV === 0 && totalS === 0) return;

  const btn = document.createElement("button");
  btn.id = "funpairdl-send-btn";

  let countText = `${totalV}V + ${totalS}S`;
  if (parsed.mode === "collection") countText += ` (${parsed.sections.length} sections)`;

  btn.innerHTML = `
    <span class="funpairdl-icon">⬇</span>
    <span class="funpairdl-text">FunPairDL</span>
    <span class="funpairdl-count">${countText}</span>
  `;

  btn.addEventListener("click", async () => {
    let panel = document.getElementById("funpairdl-panel");
    if (panel) {
      // Slide out and remove
      panel.classList.remove("funpairdl-panel-open");
      setTimeout(() => { if (panel.parentNode) panel.remove(); }, 300);
      return;
    }

    // Make sure link metadata is loaded before we parse — it decides whether a
    // file-locker link is a video or a hosted funscript, which drives
    // collection-vs-single mode and folder naming.
    await ensureLinkMetadata();
    const freshParsed = parseAllPosts();
    panel = createPanel(freshParsed);
    document.body.appendChild(panel);

    // Single mode: items are injected after the skeleton lands in the DOM
    // so the auto-grouped layout is in place before probing kicks in.
    if (freshParsed.mode === "single") {
      populateSingleItems(panel, freshParsed);
    }

    // Trigger slide-in after DOM paint
    requestAnimationFrame(() => requestAnimationFrame(() => panel.classList.add("funpairdl-panel-open")));

    // Setup probing
    setupProbing(panel, freshParsed);

    // Collection mode events
    if (freshParsed.mode === "collection") {
      setupCollectionEvents(panel, freshParsed);
      // Grip-handle drag to move rows between sections.
      _enableDragToSection(panel, freshParsed);
    } else {
      _setupSingleSelectAll(panel);
      // Grip-handle drag to move items (and checked selections) between groups.
      _enableDragToGroup(panel, freshParsed);
      _scheduleWorkPlan(panel, freshParsed);
    }

    // Drag across checkboxes to (un)check a range at once (both modes).
    _enableDragSelect(panel);

    // Close button — slide out
    panel.querySelector("#funpairdl-close").addEventListener("click", () => {
      panel.classList.remove("funpairdl-panel-open");
      setTimeout(() => { if (panel.parentNode) panel.remove(); }, 300);
    });

    // Send button
    panel.querySelector("#funpairdl-send").addEventListener("click", () => handleSend(panel, freshParsed));
  });

  document.body.appendChild(btn);
}

// ─── Lifecycle ───

// waitForContent() is re-entered on every SPA navigation. Keep a single live
// observer (disconnect the previous one first) with a 30 s timeout so
// listing/search pages that never grow a ".topic-post .cooked" don't keep a
// whole-body subtree observer running forever (audit [2]). On TOPIC pages the
// timeout re-arms itself a bounded number of rounds instead of giving up:
// Chromium throttles a background tab's JS timers to ~1 wake/s, so with many
// tabs loading at once the Discourse boot routinely outlasts a single 30 s
// window — the old one-shot timeout was the "button missing until I refresh"
// bug.
let _contentObserver = null;
let _contentObserverTimeout = null;
let _contentObserverRounds = 0;
const _CONTENT_OBSERVER_MAX_ROUNDS = 6; // ≥3 min foreground, longer throttled

function _stopContentObserver() {
  if (_contentObserver) {
    try { _contentObserver.disconnect(); } catch (e) {}
    _contentObserver = null;
  }
  if (_contentObserverTimeout) {
    clearTimeout(_contentObserverTimeout);
    _contentObserverTimeout = null;
  }
}

// Bounded retry ladder for the actual injection: ".topic-post .cooked" can
// appear before the post's links hydrate (heavy multi-tab load bursts), and
// the old single 800 ms shot then parsed an empty post and missed forever.
function _scheduleInject(attempt = 0) {
  const delays = [800, 2000, 5000, 10_000];
  if (attempt >= delays.length) return;
  setTimeout(() => {
    injectButton();
    if (!document.getElementById("funpairdl-send-btn")) _scheduleInject(attempt + 1);
  }, delays[attempt]);
}

function waitForContent() {
  _contentObserverRounds = 0;
  _armContentObserver();
}

function _armContentObserver() {
  _stopContentObserver();
  // Always arm the observer, even when content is already present: an SPA
  // navigation can briefly show stale posts that get torn down and rebuilt,
  // so an early-return-without-observer would lose the button permanently
  // (audit [2]). injectButton is idempotent (it no-ops when the button already
  // exists), so the observer and the immediate schedule below can't double-add.
  _contentObserver = new MutationObserver(() => {
    if (document.querySelector(".topic-post .cooked")) {
      _stopContentObserver();
      _scheduleInject();
    }
  });
  _contentObserver.observe(document.body, { childList: true, subtree: true });
  _contentObserverTimeout = setTimeout(() => {
    _stopContentObserver();
    _contentObserverRounds += 1;
    // Topic URL, still no button → the page just hasn't finished booting
    // (throttled background tab / heavy load) — keep watching, bounded.
    // Listing/search pages (no topic id) stop for good, as before.
    if (_currentTopicId() &&
        !document.getElementById("funpairdl-send-btn") &&
        _contentObserverRounds < _CONTENT_OBSERVER_MAX_ROUNDS) {
      _armContentObserver();
    }
  }, 30_000);
  // Content already on the page → also schedule the immediate injection.
  if (document.querySelector(".topic-post .cooked")) {
    _scheduleInject();
  }
}

// Host gate (audit [2]): only bootstrap on EroScripts — everything below
// registers observers/timers that must not run on pixeldrain/gofile/mega/...
if (_funpairdlHostAllowed()) {
  waitForContent();
  // Backstop for tabs whose observer rounds ran out while Chromium throttled
  // their boot in the background: the moment the user actually looks at the
  // tab (or the app's load-boost marks it visible), re-arm injection if the
  // button still isn't there. waitForContent() is idempotent and cheap.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    if (document.getElementById("funpairdl-send-btn")) return;
    waitForContent();
  });
}

// ─── Scroll position restoration for Discourse SPA navigation ───

// Singleton URL watcher: registered exactly once at load (host-gated); SPA
// navigations re-enter waitForContent(), which manages its own observer.
function _setupSpaWatcher() {
  const _scrollPositions = {};

  let lastUrl = location.href;
  new MutationObserver(() => {
    if (location.href !== lastUrl) {
      // Save scroll position before navigating away
      _scrollPositions[lastUrl] = window.scrollY;

      const prevUrl = lastUrl;
      lastUrl = location.href;

      const oldBtn = document.getElementById("funpairdl-send-btn");
      if (oldBtn) oldBtn.remove();
      // Don't remove the sidebar panel on SPA navigation — it causes
      // the "sudden close" problem. The user can close it manually.
      waitForContent();

      // Restore scroll position if returning to a previously visited page
      if (_scrollPositions[lastUrl] !== undefined) {
        const savedY = _scrollPositions[lastUrl];
        // Discourse renders content async, wait for DOM to settle
        const tryRestore = (attempts) => {
          if (attempts <= 0) return;
          requestAnimationFrame(() => {
            if (document.body.scrollHeight > savedY) {
              window.scrollTo(0, savedY);
            } else {
              setTimeout(() => tryRestore(attempts - 1), 100);
            }
          });
        };
        setTimeout(() => tryRestore(15), 200);
      }
    }
  }).observe(document, { subtree: true, childList: true });
}

if (_funpairdlHostAllowed()) _setupSpaWatcher();

// ─── Auto re-login when Discourse detects session expiry ───
// Discourse shows a modal dialog when the server invalidates the session
// (via MessageBus). We detect this and automatically re-login using saved
// credentials, then reload the page — preserving the user's position.

(function setupAutoRelogin() {
  // Only run on EroScripts in the embedded browser
  if (!_funpairdlHostAllowed()) return;
  if (typeof location === "undefined" || !location.hostname ||
      !(location.hostname === "eroscripts.com" || location.hostname.endsWith(".eroscripts.com"))) return;
  if (!window.funpairdlBridge && typeof qt === "undefined") return;

  const SCROLL_KEY_PREFIX = "funpairdl_scroll_";
  const RESTORE_KEY = "funpairdl_scroll_restore";

  // ── Continuously save scroll position while browsing ──
  // This captures the REAL position before logout occurs.
  // The error page ("page doesn't exist") has scrollY ≈ 0, so we
  // must save while content is still visible.
  let _scrollSaveTimer = null;
  window.addEventListener("scroll", () => {
    if (_scrollSaveTimer) return;
    _scrollSaveTimer = setTimeout(() => {
      _scrollSaveTimer = null;
      // Only save when page has real content (not an error/login page)
      if (document.querySelector(".topic-post, .topic-body, .topic-list")) {
        sessionStorage.setItem(SCROLL_KEY_PREFIX + location.pathname, JSON.stringify({
          url: location.href,
          scrollY: window.scrollY,
          time: Date.now(),
        }));
      }
    }, 1500);
  }, { passive: true });

  // Stage the last-known-good scroll position into RESTORE_KEY so that whichever
  // path triggers the reload — the leader after logging in, OR a follower after
  // it sees the leader's done-marker — restores the user's place. Uses the
  // position saved while content was visible, not the current one (which may be
  // 0 on an error/login page).
  function _stageScrollRestore() {
    try {
      let scrollY = 0;
      const lastGood = sessionStorage.getItem(SCROLL_KEY_PREFIX + location.pathname);
      if (lastGood) {
        const parsed = JSON.parse(lastGood);
        scrollY = parsed.scrollY || 0;
      }
      sessionStorage.setItem(RESTORE_KEY, JSON.stringify({
        url: location.href,
        scrollY,
      }));
    } catch (_) {}
  }

  let _reloginInProgress = false;

  // ── Cross-tab relogin coordination (audit [2d]) ──
  // Every tab polls session state, so on expiry N tabs would each race their
  // own CSRF + POST /session (token-rotation lockout risk) and then reload
  // simultaneously — an N-tab full-Discourse-load storm. A localStorage
  // leader lock elects ONE tab to do the login; the others wait for the
  // done-marker and reload after a randomized 2–8 s stagger.
  const RELOGIN_LOCK_KEY = "fpdl_relogin_lock";
  const RELOGIN_DONE_KEY = "fpdl_relogin_done";
  const RELOGIN_LOCK_STALE_MS = 90_000; // lock older than this is up for grabs
  const RELOGIN_DONE_FRESH_MS = 30_000; // done-marker newer than this → just reload

  // True when another tab re-logged in within the last RELOGIN_DONE_FRESH_MS —
  // in that case this tab should reload, not POST /session again.
  function _reloginDoneFresh() {
    try {
      const doneTs = parseInt(localStorage.getItem(RELOGIN_DONE_KEY) || "0", 10) || 0;
      return !!doneTs && (Date.now() - doneTs) < RELOGIN_DONE_FRESH_MS;
    } catch (e) { return false; }
  }

  // localStorage.setItem propagates to other tabs asynchronously, so a bare
  // read-then-write lets two tabs racing inside that window both "acquire" and
  // each POST /session (token-rotation lockout). Instead write a UNIQUE token,
  // let the write settle, then read back: we are the leader only if the stored
  // value is still our token (a racing tab that wrote last would have replaced
  // it, and its own read-back would then see ours — exactly one wins).
  async function _tryAcquireReloginLock() {
    try {
      const now = Date.now();
      const ts = parseInt(localStorage.getItem(RELOGIN_LOCK_KEY) || "0", 10) || 0;
      if (ts && now - ts < RELOGIN_LOCK_STALE_MS) return false; // live leader exists
      const token = `${now}:${Math.random().toString(36).slice(2)}`;
      localStorage.setItem(RELOGIN_LOCK_KEY, token);
      await new Promise((r) => setTimeout(r, 250));
      let stored = null;
      try { stored = localStorage.getItem(RELOGIN_LOCK_KEY); } catch (e) { return true; }
      return stored === token;
    } catch (e) { return true; } // localStorage unavailable → act alone
  }

  function _releaseReloginLock() {
    try { localStorage.removeItem(RELOGIN_LOCK_KEY); } catch (e) {}
  }

  // Follower path: wait for the leader tab to write the done-marker, then
  // reload after a random 2–8 s delay. If the leader dies (lock goes stale
  // with no done-marker), re-arm so the periodic check can elect a new one.
  function _waitForLeaderRelogin() {
    const started = Date.now();
    let poll = null;
    let onStorage = null;
    function cleanup() {
      if (poll) clearInterval(poll);
      if (onStorage) window.removeEventListener("storage", onStorage);
      poll = null;
      onStorage = null;
    }
    function checkDone() {
      let doneTs = 0;
      try { doneTs = parseInt(localStorage.getItem(RELOGIN_DONE_KEY) || "0", 10) || 0; } catch (e) {}
      if (doneTs >= started - 5_000) {
        cleanup();
        // Preserve this tab's scroll position across its own reload, just like
        // the leader path does — otherwise follower tabs jump to the top.
        _stageScrollRestore();
        const delay = 2_000 + Math.random() * 6_000;
        console.log(`FunPairDL: Leader tab re-logged in — reloading in ${Math.round(delay / 1000)}s`);
        setTimeout(() => location.reload(), delay);
        return;
      }
      if (Date.now() - started > RELOGIN_LOCK_STALE_MS) {
        cleanup();
        _reloginInProgress = false; // leader died — allow a fresh attempt
      }
    }
    onStorage = (e) => { if (e.key === RELOGIN_DONE_KEY) checkDone(); };
    window.addEventListener("storage", onStorage);
    poll = setInterval(checkDone, 5_000);
    checkDone();
  }

  async function _attemptRelogin() {
    if (_reloginInProgress) return;
    _reloginInProgress = true;

    // Another tab already re-logged in moments ago — don't POST /session again
    // (a stale /session/current.json can still read 404 right after). Just
    // restore scroll and reload to pick up the freshly-restored session.
    if (_reloginDoneFresh()) {
      _stageScrollRestore();
      const delay = 2_000 + Math.random() * 6_000;
      setTimeout(() => location.reload(), delay);
      return;
    }

    // Leader election: only one tab app-wide performs the actual login.
    if (!(await _tryAcquireReloginLock())) {
      _waitForLeaderRelogin();
      return;
    }

    try {
      const creds = await _sendMsg("get-ero-credentials", {});
      if (!creds || !creds.username || !creds.password) {
        console.log("FunPairDL: No EroScripts credentials configured, skip auto-login");
        _releaseReloginLock();
        _reloginInProgress = false;
        return;
      }

      console.log("FunPairDL: Session expired — attempting auto re-login...");

      // Step 1: Get CSRF token
      const csrfResp = await fetch("/session/csrf", { credentials: "same-origin" });
      const csrfData = await csrfResp.json();
      const csrf = csrfData.csrf;
      if (!csrf) throw new Error("No CSRF token");

      // Step 2: Login via Discourse API
      const loginResp = await fetch("/session", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRF-Token": csrf,
        },
        body: `login=${encodeURIComponent(creds.username)}&password=${encodeURIComponent(creds.password)}`,
      });

      const loginData = await loginResp.json();
      if (loginData.error) {
        console.error("FunPairDL: Auto-login failed:", loginData.error);
        _releaseReloginLock();
        _reloginInProgress = false;
        return;
      }

      console.log("FunPairDL: Auto re-login successful, reloading...");

      // Tell the follower tabs the session is fixed (they reload staggered),
      // then give up leadership before our own reload.
      try { localStorage.setItem(RELOGIN_DONE_KEY, String(Date.now())); } catch (e) {}
      _releaseReloginLock();

      // Stage the last-known-good scroll position (shared with the follower
      // path) so the reload lands the user back where they were.
      _stageScrollRestore();
      location.reload();
    } catch (e) {
      console.error("FunPairDL: Auto re-login error:", e);
      _releaseReloginLock();
      _reloginInProgress = false;
    }
  }

  // Restore scroll position after auto-login reload
  try {
    const saved = sessionStorage.getItem(RESTORE_KEY);
    if (saved) {
      sessionStorage.removeItem(RESTORE_KEY);
      const { url, scrollY } = JSON.parse(saved);
      if (url === location.href && scrollY > 0) {
        const tryRestore = (attempts) => {
          if (attempts <= 0) return;
          requestAnimationFrame(() => {
            if (document.body.scrollHeight > scrollY) {
              window.scrollTo(0, scrollY);
            } else {
              setTimeout(() => tryRestore(attempts - 1), 150);
            }
          });
        };
        // Wait longer for content to load after login (Discourse needs time)
        setTimeout(() => tryRestore(30), 500);
      }
    }
  } catch (_) {}

  // ── Login status check (reusable) ──
  async function _checkAndRelogin() {
    if (_reloginInProgress) return;
    try {
      const resp = await fetch("/session/current.json", {
        credentials: "same-origin",
        headers: { "Accept": "application/json" },
      });
      if (resp.status === 404 || resp.status === 403) {
        console.log("FunPairDL: Not logged in — triggering auto re-login");
        _attemptRelogin();
      }
    } catch (_) {}
  }

  // Detection method 1: Immediate check on page load.
  // Catches: page reload after logout dismiss, direct navigation while logged out.
  setTimeout(_checkAndRelogin, 3000);

  // Detection method 2: Observe DOM for Discourse logout dialog.
  // Catches: mid-session logout via MessageBus (before user clicks dismiss).
  const _dialogObserver = new MutationObserver((mutations) => {
    for (const m of mutations) {
      for (const node of m.addedNodes) {
        if (!(node instanceof HTMLElement)) continue;
        // Cheap structural check FIRST — node.textContent serializes the
        // whole inserted subtree, and Discourse inserts entire posts on every
        // scroll cloak/uncloak burst (audit [2]).
        const isDialog =
          node.classList.contains("dialog-body") ||
          node.classList.contains("bootbox") ||
          node.classList.contains("modal-body") ||
          !!node.querySelector?.(".dialog-body, .bootbox-body, .modal-body");
        if (!isDialog) continue;
        const text = node.textContent || "";
        if (/logged?\s*out|log\s*in.*again|session.*expired/i.test(text)) {
          console.log("FunPairDL: Detected logout dialog");
          _attemptRelogin();
          return;
        }
      }
    }
  });
  _dialogObserver.observe(document.body, { childList: true, subtree: true });

  // Detection method 3: Periodic check (every 60 seconds).
  setInterval(_checkAndRelogin, 60_000);
})();
