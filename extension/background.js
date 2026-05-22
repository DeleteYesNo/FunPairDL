// FunPairDL Background Service Worker
const API_URL = "http://127.0.0.1:9172/api";

// Create context menu items
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "funpairdl-send-link",
    title: "Send link to FunPairDL",
    contexts: ["link"],
  });

  chrome.contextMenus.create({
    id: "funpairdl-send-video",
    title: "Send as video to FunPairDL",
    contexts: ["link"],
  });

  chrome.contextMenus.create({
    id: "funpairdl-send-script",
    title: "Send as script to FunPairDL",
    contexts: ["link"],
  });
});

// Handle context menu clicks
chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const url = info.linkUrl;
  if (!url) return;

  let fileType = "auto";
  if (info.menuItemId === "funpairdl-send-video") {
    fileType = "video";
  } else if (info.menuItemId === "funpairdl-send-script") {
    fileType = "funscript";
  }

  try {
    const response = await fetch(`${API_URL}/link`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: url,
        name: "",
        file_type: fileType,
      }),
    });

    if (response.ok) {
      // Show success badge
      chrome.action.setBadgeText({ text: "✓", tabId: tab.id });
      chrome.action.setBadgeBackgroundColor({ color: "#28a745", tabId: tab.id });
      setTimeout(() => {
        chrome.action.setBadgeText({ text: "", tabId: tab.id });
      }, 2000);
    } else {
      chrome.action.setBadgeText({ text: "!", tabId: tab.id });
      chrome.action.setBadgeBackgroundColor({ color: "#dc3545", tabId: tab.id });
      setTimeout(() => {
        chrome.action.setBadgeText({ text: "", tabId: tab.id });
      }, 3000);
    }
  } catch (error) {
    chrome.action.setBadgeText({ text: "✗", tabId: tab.id });
    chrome.action.setBadgeBackgroundColor({ color: "#dc3545", tabId: tab.id });
    setTimeout(() => {
      chrome.action.setBadgeText({ text: "", tabId: tab.id });
    }, 3000);
  }
});

// Cache backend config (gofile_token etc.) to avoid repeated fetches
let _cachedConfig = null;
function getConfig() {
  if (_cachedConfig) return Promise.resolve(_cachedConfig);
  return fetch(`${API_URL}/config`)
    .then((r) => r.json())
    .then((data) => { _cachedConfig = data; return data; })
    .catch(() => ({}));
}
// Pre-fetch config on startup
getConfig();

// Handle messages from content script
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === "check-status") {
    fetch(`${API_URL}/status`)
      .then((r) => r.json())
      .then((data) => sendResponse({ online: true, ...data }))
      .catch(() => sendResponse({ online: false }));
    return true; // async response
  }

  if (message.type === "send-pair") {
    // Include EroScripts cookies so the backend can download authenticated short-URLs
    chrome.cookies
      .getAll({ url: "https://discuss.eroscripts.com" })
      .then((cookies) => {
        const cookieStr = cookies.map((c) => `${c.name}=${c.value}`).join("; ");
        const payload = { ...message.data };
        if (cookieStr) payload.eroscripts_cookies = cookieStr;
        return fetch(`${API_URL}/pair`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
      })
      .then((r) => r.json())
      .then((data) => sendResponse({ success: true, ...data }))
      .catch((err) => sendResponse({ success: false, error: err.message }));
    return true;
  }

  if (message.type === "resolve-url") {
    // Resolve EroScripts short-url via Python backend (avoids CORS/opaque redirect).
    // Use chrome.cookies API to get httpOnly cookies the content script can't access.
    chrome.cookies
      .getAll({ url: "https://discuss.eroscripts.com" })
      .then((cookies) => {
        const cookieStr = cookies.map((c) => `${c.name}=${c.value}`).join("; ");
        return fetch(`${API_URL}/resolve`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: message.url, cookies: cookieStr }),
        });
      })
      .then((r) => r.json())
      .then((data) => {
        if (data.success) {
          sendResponse({ success: true, finalUrl: data.url });
        } else {
          sendResponse({ success: false, error: data.error || "Resolve failed" });
        }
      })
      .catch((err) => {
        sendResponse({ success: false, error: err.message });
      });
    return true;
  }

  if (message.type === "get-config") {
    getConfig().then((data) => sendResponse(data));
    return true;
  }

  if (message.type === "probe-url") {
    fetch(`${API_URL}/probe`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: message.url }),
    })
      .then((r) => r.json())
      .then((data) => sendResponse(data))
      .catch((err) => sendResponse({ success: false, error: err.message }));
    return true;
  }

  if (message.type === "probe-gofile") {
    // Probe GoFile directly from browser (bypasses backend network issues / uses browser VPN)
    const contentId = message.contentId;
    const token = message.token;
    fetch(`https://api.gofile.io/contents/${contentId}?wt=4fd6sg89d7s6`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((r) => r.json())
      .then((data) => {
        if (data.status !== "ok") {
          sendResponse({ success: false, error: data.status });
          return;
        }
        const children = data.data?.children || {};
        let totalSize = 0;
        const files = [];
        for (const child of Object.values(children)) {
          if (child.type === "file") {
            totalSize += child.size || 0;
            files.push({
              name: child.name || "",
              size: child.size || 0,
              url: `https://gofile.io/d/${child.code || child.id || contentId}`,
            });
          }
        }
        sendResponse({
          success: true,
          provider: "gofile",
          size: totalSize,
          filename:
            files.length === 1 ? files[0].name : `${files.length} files`,
          files: files.length > 1 ? files : undefined,
        });
      })
      .catch((err) => sendResponse({ success: false, error: err.message }));
    return true;
  }

  if (message.type === "send-link") {
    fetch(`${API_URL}/link`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(message.data),
    })
      .then((r) => r.json())
      .then((data) => sendResponse({ success: true, ...data }))
      .catch((err) => sendResponse({ success: false, error: err.message }));
    return true;
  }
});
