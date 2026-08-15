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
const AXIS_REGEX_KNOWN = new RegExp(`\\.(${AXIS_SUFFIXES.join("|")})\\.funscript$`, "i");
// Broader pattern: any .word.funscript where word is alphanumeric (catches custom axes like .suckManual)
const AXIS_REGEX_ANY = /\.([a-zA-Z][a-zA-Z0-9]{1,30})\.funscript$/;

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

function _isVideoLinkHeadingText(text) {
  return VIDEO_LINK_HEADING_RE.test((text || "").trim());
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

function detectAxis(filename) {
  // Try known axes first (exact match)
  const known = filename.match(AXIS_REGEX_KNOWN);
  if (known) return known[1].toLowerCase();
  // Fall back to any .word.funscript pattern (custom axes like .suckManual)
  const any = filename.match(AXIS_REGEX_ANY);
  if (any) return any[1];
  return "main";
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
    if (ord !== undefined) map.set(href, ord);
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
  if (videos.length === 0) {
    return scripts.length > 0 ? [{ videos: [], scripts }] : [];
  }
  if (videos.length === 1) {
    return [{ videos, scripts }];
  }

  const elIndex = _buildElementIndex(cookedEl);
  const ordMap = _buildUrlOrdinalMap(cookedEl, elIndex);
  const videoOrds = videos.map((v) => _urlOrdinal(ordMap, v.url));
  const buckets = videos.map(() => []);

  for (const s of scripts) {
    const sOrd = _urlOrdinal(ordMap, s.url);
    let best = 0;
    let bestDist = Math.abs(sOrd - videoOrds[0]);
    for (let i = 1; i < videoOrds.length; i++) {
      const d = Math.abs(sOrd - videoOrds[i]);
      if (d < bestDist) { bestDist = d; best = i; }
    }
    buckets[best].push(s);
  }
  return videos.map((v, i) => ({ videos: [v], scripts: buckets[i] }));
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

  // Try section-based parsing on OP
  const sections = opCooked ? parseOPSections(opCooked) : [];

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
      return {
        mode: "collection",
        title,
        sections,
        commentVideos: dedupArr(commentVideos),
        commentScripts: dedupArr(commentScripts),
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
      inherit_multi_axis: g.inheritMultiAxis !== false,
      display_name: (g.displayName || "").trim(),
    }));
  } else {
    payload.video_urls = data.videoUrls || [];
    payload.script_urls = data.scriptUrls || [];
    if (data.scriptAuthors && Object.keys(data.scriptAuthors).length > 0) {
      payload.script_authors = data.scriptAuthors;
    }
    if (data.filenames && Object.keys(data.filenames).length > 0) {
      payload.filenames = data.filenames;
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
    ? `<span class="funpairdl-drag-handle" draggable="true" title="拖曳到群組(可先勾選多個一起拖)">⠿</span>`
    : "";
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

    if (vCount > 0) {
      html += `<div class="funpairdl-subsection-title">Videos</div>`;
      section.videos.forEach((v, vi) => {
        html += renderVideoItem(v, vi, `sv-${si}`, vi === 0);
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
            html += renderScriptItem(s, idx, `ss-${si}`, isFirstAuthor);
          });
          html += `</div>`;
          isFirstAuthor = false;
        }
      } else {
        html += `<div class="funpairdl-subsection-title">Scripts</div>`;
        section.scripts.forEach((s, si2) => {
          html += renderScriptItem(s, si2, `ss-${si}`, true);
        });
      }
    }

    html += `</div></div>`;
  });

  // Comment items (if any)
  if (parsed.commentVideos.length > 0 || parsed.commentScripts.length > 0) {
    html += `<div class="funpairdl-section-group" data-section="comments">
      <div class="funpairdl-section-header">
        <input type="checkbox" class="funpairdl-section-cb" data-section="comments">
        <span class="funpairdl-section-toggle" data-section="comments">▸</span>
        <span class="funpairdl-section-name">Comments</span>
        <span class="funpairdl-section-count">${parsed.commentVideos.length}V + ${parsed.commentScripts.length}S</span>
      </div>
      <div class="funpairdl-section-body" style="display:none">`;
    parsed.commentVideos.forEach((v, i) => {
      html += renderVideoItem(v, i, "cv", false);
    });
    parsed.commentScripts.forEach((s, i) => {
      html += renderScriptItem(s, i, "cs", false);
    });
    html += `</div></div>`;
  }

  return html;
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
        let listHtml = "";
        for (const f of info.files) {
          const fname = escapeAttr(f.name);
          const fsize = f.size ? formatSize(f.size) : "";
          const furl = escapeAttr(f.url || "");
          listHtml += `<label class="funpairdl-bundle-file funpairdl-bundle-selectable" title="${fname}">
            <input type="checkbox" class="funpairdl-bundle-cb"
                   data-probe-key="${probeKey}"
                   data-file-url="${furl}"
                   data-file-name="${fname}" checked>
            <span class="funpairdl-bundle-fname">${fname}</span>
            <span class="funpairdl-bundle-fsize">${fsize}</span>
          </label>`;
        }
        dropdown.innerHTML = listHtml;
        item.after(dropdown);

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

  function probeVideo(v, probeKey) {
    const sizeEl = panel.querySelector(`[data-probe="${probeKey}"]`);
    if (!sizeEl) return;
    sizeEl.textContent = "...";
    probeUrl(v.url).then((info) => {
      if (!info) { sizeEl.textContent = "?"; return; }
      probeResults[probeKey] = info;
      // A file-locker URL (pixeldrain /u/, mega /file/, ...) carries no type
      // hint, so a funscript hosted there is initially treated as a "video".
      // Remember the probed filename so send-time can re-route it to scripts.
      if (info.filename) v.probedFilename = info.filename;
      updateVideoSize(probeKey, info);
      showProbeExtras(sizeEl, probeKey, info);
    });
  }

  function probeScript(s, probeKey) {
    const sizeEl = panel.querySelector(`[data-probe="${probeKey}"]`);
    if (!sizeEl) return;
    sizeEl.textContent = "...";
    const handleResult = (info) => {
      if (!info) { sizeEl.textContent = "?"; return; }
      probeResults[probeKey] = info;
      sizeEl.textContent = info.size ? formatSize(info.size) : "";
      showProbeExtras(sizeEl, probeKey, info);
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

function setupCollectionEvents(panel, parsed) {
  // Section toggle (expand/collapse)
  panel.querySelectorAll(".funpairdl-section-toggle").forEach((toggle) => {
    toggle.addEventListener("click", () => {
      const si = toggle.dataset.section;
      const body = panel.querySelector(`.funpairdl-section-group[data-section="${si}"] .funpairdl-section-body`);
      if (!body) return;
      const visible = body.style.display !== "none";
      body.style.display = visible ? "none" : "block";
      toggle.textContent = visible ? "▸" : "▾";
    });
  });

  // Section header click (expand/collapse, excluding checkbox)
  panel.querySelectorAll(".funpairdl-section-header").forEach((header) => {
    header.style.cursor = "pointer";
    header.addEventListener("click", (e) => {
      if (e.target.tagName === "INPUT") return;
      const toggle = header.querySelector(".funpairdl-section-toggle");
      if (toggle) toggle.click();
    });
  });

  // Section checkbox → check/uncheck all items in section
  panel.querySelectorAll(".funpairdl-section-cb").forEach((cb) => {
    cb.addEventListener("change", () => {
      const si = cb.dataset.section;
      const body = panel.querySelector(`.funpairdl-section-group[data-section="${si}"] .funpairdl-section-body`);
      if (!body) return;
      body.querySelectorAll('input[type="checkbox"]').forEach((inner) => {
        inner.checked = cb.checked;
      });
      updateSendButton(panel, parsed);
    });
  });

  // Item checkbox → bubble up to section checkbox
  panel.querySelectorAll(".funpairdl-section-body").forEach((body) => {
    body.addEventListener("change", (e) => {
      if (e.target.type !== "checkbox") return;
      const group = body.closest(".funpairdl-section-group");
      if (!group) return;
      const sectionCb = group.querySelector(".funpairdl-section-cb");
      if (!sectionCb) return;
      const anyChecked = body.querySelector('input[type="checkbox"]:checked') !== null;
      sectionCb.checked = anyChecked;
      updateSendButton(panel, parsed);
    });
  });

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
    return;
  }

  const resSelect = document.getElementById("funpairdl-resolution");
  const preferredResolution = resSelect ? resSelect.value : "best";
  const autoRenameCb = document.getElementById("funpairdl-auto-rename");
  const autoRename = autoRenameCb ? autoRenameCb.checked : true;

  if (parsed.mode === "collection") {
    await handleCollectionSend(panel, parsed, sendBtn, preferredResolution, autoRename);
  } else {
    await handleSingleSend(panel, parsed, sendBtn, preferredResolution, autoRename);
  }
}

async function handleSingleSend(panel, parsed, sendBtn, preferredResolution, autoRename) {
  sendBtn.textContent = "Resolving URLs...";

  // Bucket selected URLs by group, then resolve per-bucket. Within each
  // bucket we still resolve everything in parallel — the only reason we
  // bucket first is to keep the group association after resolution.
  const buckets = {}; // groupName → { videoUrls, scriptUrls, scriptAuthorMap }
  function _bucket(g) {
    if (!buckets[g]) buckets[g] = { videoUrls: [], scriptUrls: [], scriptAuthorMap: {}, filenames: {} };
    return buckets[g];
  }

  panel.querySelectorAll('input[name="video"]:checked').forEach((cb) => {
    const idx = parseInt(cb.value);
    const video = parsed.videos[idx];
    const key = `video-${idx}`;
    const gname = (parsed.groupState && parsed.groupState.itemGroup[key]) || "Main";
    const b = _bucket(gname);
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
        }
      });
    } else if ((video.probedFilename || "").toLowerCase().endsWith(".funscript")) {
      // Probe revealed this "video" link is actually a funscript (common when
      // the script is hosted on pixeldrain/mega rather than as a .funscript
      // upload). Route it to scripts so it pairs with the real video instead
      // of landing in a separate group as an orphaned "video".
      b.scriptUrls.push(video.url);
    } else {
      b.videoUrls.push(video.url);
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
    groups.push({
      name: gname,
      videoUrls: resolvedV,
      scriptUrls: resolvedS,
      scriptAuthors: resolvedAuthors,
      filenames: resolvedFilenames,
      inheritMultiAxis: (parsed.groupState && parsed.groupState.inheritAxes[gname] !== false),
      displayName: (parsed.groupState && parsed.groupState.altNames && parsed.groupState.altNames[gname]) || "",
    });
  }

  if (groups.length === 0) {
    sendBtn.textContent = "Nothing selected!";
    setTimeout(() => { sendBtn.disabled = false; sendBtn.textContent = "Send to FunPairDL"; }, 2000);
    return;
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
  } else {
    sendBtn.textContent = `Error: ${result.error}`;
    sendBtn.classList.add("funpairdl-error");
    setTimeout(() => {
      sendBtn.textContent = "Send to FunPairDL";
      sendBtn.classList.remove("funpairdl-error");
      sendBtn.disabled = false;
    }, 3000);
  }
}

async function handleCollectionSend(panel, parsed, sendBtn, preferredResolution, autoRename) {
  // Gather all checked sections
  const pairs = [];

  parsed.sections.forEach((section, si) => {
    const sectionCb = panel.querySelector(`.funpairdl-section-cb[data-section="${si}"]`);
    if (!sectionCb || !sectionCb.checked) return;

    const videoUrls = [];
    const scriptUrls = [];
    const scriptAuthorMap = {};
    const filenameMap = {};

    // Collect checked videos in this section
    panel.querySelectorAll(`input[name="sv-${si}"]:checked`).forEach((cb) => {
      const vi = parseInt(cb.value);
      const v = section.videos[vi];
      const bundleCbs = panel.querySelectorAll(`.funpairdl-bundle-cb[data-probe-key="sv-${si}-${vi}"]`);
      if (bundleCbs.length > 0) {
        bundleCbs.forEach((bcb) => {
          if (bcb.checked) {
            const realName = bcb.dataset.fileName || "";
            const fn = realName.toLowerCase();
            if (realName) filenameMap[bcb.dataset.fileUrl] = realName;
            if (fn.endsWith(".funscript")) scriptUrls.push(bcb.dataset.fileUrl);
            else videoUrls.push(bcb.dataset.fileUrl);
          }
        });
      } else if ((v.probedFilename || "").toLowerCase().endsWith(".funscript")) {
        // Probe revealed this "video" link is actually a funscript — route it
        // to scripts so it pairs instead of becoming an orphaned video.
        scriptUrls.push(v.url);
      } else {
        videoUrls.push(v.url);
      }
    });

    // Collect checked scripts in this section
    panel.querySelectorAll(`input[name="ss-${si}"]:checked`).forEach((cb) => {
      const script = section.scripts[parseInt(cb.value)];
      scriptUrls.push(script.url);
      if (script.author) scriptAuthorMap[script.url] = script.author;
    });

    if (videoUrls.length > 0 || scriptUrls.length > 0) {
      // Generic section heading ("Video link", "Downloads", "1080p", …) →
      // borrow the topic title; a real work name → keep it as the folder name.
      const pairName = _isGenericSectionName(section.name) ? parsed.title : section.name;

      // Merge into existing pair with same name
      const existing = pairs.find((p) => p.name === pairName);
      if (existing) {
        existing.videoUrls.push(...videoUrls);
        existing.scriptUrls.push(...scriptUrls);
        Object.assign(existing.scriptAuthorMap, scriptAuthorMap);
        Object.assign(existing.filenameMap, filenameMap);
      } else {
        pairs.push({ name: pairName, videoUrls, scriptUrls, scriptAuthorMap, filenameMap });
      }
    }
  });

  // Also check comment section
  const commentCb = panel.querySelector('.funpairdl-section-cb[data-section="comments"]');
  if (commentCb && commentCb.checked) {
    const cVideoUrls = [];
    const cScriptUrls = [];
    const cScriptAuthorMap = {};
    panel.querySelectorAll('input[name="cv"]:checked').forEach((cb) => {
      cVideoUrls.push(parsed.commentVideos[parseInt(cb.value)].url);
    });
    panel.querySelectorAll('input[name="cs"]:checked').forEach((cb) => {
      const script = parsed.commentScripts[parseInt(cb.value)];
      cScriptUrls.push(script.url);
      if (script.author) cScriptAuthorMap[script.url] = script.author;
    });
    if (cVideoUrls.length > 0 || cScriptUrls.length > 0) {
      // Merge comment URLs into existing pair if possible
      const existing = pairs.find((p) => p.name === parsed.title);
      if (existing) {
        existing.videoUrls.push(...cVideoUrls);
        existing.scriptUrls.push(...cScriptUrls);
        Object.assign(existing.scriptAuthorMap, cScriptAuthorMap);
      } else {
        pairs.push({ name: parsed.title, videoUrls: cVideoUrls, scriptUrls: cScriptUrls, scriptAuthorMap: cScriptAuthorMap, filenameMap: {} });
      }
    }
  }

  if (pairs.length === 0) {
    sendBtn.textContent = "Nothing selected!";
    setTimeout(() => { sendBtn.disabled = false; updateSendButton(panel, parsed); }, 2000);
    return;
  }

  sendBtn.textContent = `Resolving URLs (0/${pairs.length})...`;

  let sentCount = 0;
  let failCount = 0;

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

    sendBtn.textContent = `Sending (${i + 1}/${pairs.length})...`;
    const result = await sendPairToServer({
      title: p.name, videoUrls: resolvedV, scriptUrls: resolvedS,
      preferredResolution, scriptAuthors: resolvedAuthors, autoRename,
      filenames: resolvedFilenames,
    });

    if (result.success) sentCount++;
    else failCount++;
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
}

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

  function _itemsToMove(item) {
    const cb = item.querySelector('input[type="checkbox"]');
    if (cb && cb.checked) {
      const checked = [...panel.querySelectorAll(".funpairdl-item[data-key]")].filter((it) => {
        const c = it.querySelector('input[type="checkbox"]');
        return c && c.checked;
      });
      if (checked.length > 1) return checked;
    }
    return [item];
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
    const moving = _itemsToMove(item);
    draggedKeys = moving.map((it) => it.dataset.key);
    moving.forEach((it) => it.classList.add("funpairdl-dragging"));
    e.dataTransfer.effectAllowed = "move";
    // Setting data is required for the drop event to fire in some engines.
    try { e.dataTransfer.setData("text/plain", draggedKeys.join(",")); } catch (_) {}
  });

  panel.addEventListener("dragover", (e) => {
    if (draggedKeys.length === 0) return;
    const block = e.target.closest && e.target.closest(".funpairdl-group-block");
    if (!block) return;
    e.preventDefault();            // mark this a valid drop target
    e.dataTransfer.dropEffect = "move";
    if (!block.classList.contains("funpairdl-drag-over")) {
      _clearHighlights();
      block.classList.add("funpairdl-drag-over");
    }
  });

  panel.addEventListener("drop", (e) => {
    if (draggedKeys.length === 0) return;
    const block = e.target.closest && e.target.closest(".funpairdl-group-block");
    if (!block) return;
    e.preventDefault();
    const target = block.dataset.group;
    for (const key of draggedKeys) {
      const item = panel.querySelector(`.funpairdl-item[data-key="${key}"]`);
      if (item) _moveItemToGroup(panel, parsed, item, target);
    }
    _updateInheritancePreviews(panel, parsed);
  });

  panel.addEventListener("dragend", () => {
    panel.querySelectorAll(".funpairdl-dragging").forEach((it) => it.classList.remove("funpairdl-dragging"));
    _clearHighlights();
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

function injectButton() {
  if (document.getElementById("funpairdl-send-btn")) return;

  // Warm the link metadata cache so it's ready by the time the user clicks.
  ensureLinkMetadata();

  const parsed = parseAllPosts();
  if (!parsed) return;

  const totalV = parsed.mode === "collection"
    ? parsed.sections.reduce((n, s) => n + s.videos.length, 0) + (parsed.commentVideos?.length || 0)
    : parsed.videos.length;
  const totalS = parsed.mode === "collection"
    ? parsed.sections.reduce((n, s) => n + s.scripts.length, 0) + (parsed.commentScripts?.length || 0)
    : parsed.scripts.length;

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
    } else {
      _setupSingleSelectAll(panel);
      // Grip-handle drag to move items (and checked selections) between groups.
      _enableDragToGroup(panel, freshParsed);
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
