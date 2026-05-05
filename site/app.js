/**
 * A股消息雷达 - 前端逻辑
 * 支持内联数据（file://兼容）和 fetch 两种加载方式
 */
(function () {
  "use strict";

  const DATA_BASE = "data";
  const RECENT_HOURS = 6;

  // ── DOM ───────────────────────────────────────
  const recentNewsEl = document.getElementById("recentNews");
  const sentimentBarEl = document.getElementById("sentimentBar");
  const archiveTreeEl = document.getElementById("archiveTree");
  const themeToggle = document.getElementById("themeToggle");
  const modalOverlay = document.getElementById("modalOverlay");
  const modalContent = document.getElementById("modalContent");
  const modalClose = document.getElementById("modalClose");
  const tabs = document.querySelectorAll(".tab");
  const panels = document.querySelectorAll(".tab-panel");
  const searchInput = document.getElementById("searchInput");
  const searchResults = document.getElementById("searchResults");
  const searchMeta = document.getElementById("searchMeta");
  const toastEl = document.getElementById("toast");

  // ── 全量新闻缓存（供搜索用）────────────────
  let allNewsItems = [];

  // ── Tab 切换 ──────────────────────────────────
  const tabPanelMap = { recent: "panelRecent", archive: "panelArchive", search: "panelSearch" };
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((t) => t.classList.remove("active"));
      panels.forEach((p) => p.classList.remove("active"));
      tab.classList.add("active");
      document.getElementById(tabPanelMap[tab.dataset.tab]).classList.add("active");
      if (tab.dataset.tab === "search") searchInput.focus();
    });
  });

  // ── 主题 ──────────────────────────────────────
  function initTheme() {
    const saved = localStorage.getItem("stock-radar-theme") || "dark";
    document.documentElement.setAttribute("data-theme", saved);
  }

  themeToggle.addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme");
    const next = cur === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("stock-radar-theme", next);
  });

  // ── 去重工具 ─────────────────────────────────────
  function normalizeTitle(title) {
    return title.toLowerCase()
      .replace(/[\s，。、：；！？""''（）\[\]【】\-—·,.!?:;'"()\[\]{}]/g, '')
      .replace(/(\d),(\d)/g, '$1$2');
  }

  function titlesAreSimilar(a, b) {
    const na = normalizeTitle(a);
    const nb = normalizeTitle(b);
    if (na === nb) return true;
    if (na.length > 4 && nb.length > 4) {
      if (na.includes(nb) || nb.includes(na)) return true;
    }
    function extractKw(t) {
      const cn = t.match(/[\u4e00-\u9fff]{2,}/g) || [];
      const en = t.match(/[a-z]{2,}/g) || [];
      const nums = t.match(/\d+\.?\d*/g) || [];
      return new Set([...cn, ...en, ...nums]);
    }
    const kwA = extractKw(na);
    const kwB = extractKw(nb);
    if (kwA.size && kwB.size) {
      let overlap = 0;
      for (const k of kwA) { if (kwB.has(k)) overlap++; }
      const smaller = Math.min(kwA.size, kwB.size);
      if (smaller > 0 && overlap / smaller >= 0.7) return true;
    }
    function bigramSimilarity(s1, s2) {
      if (s1.length < 2 || s2.length < 2) return 0;
      const bg = (s) => { const r = new Set(); for (let i = 0; i < s.length - 1; i++) r.add(s.slice(i, i + 2)); return r; };
      const a = bg(s1), b = bg(s2);
      let ov = 0;
      for (const x of a) { if (b.has(x)) ov++; }
      return (2 * ov) / (a.size + b.size);
    }
    if (bigramSimilarity(na, nb) >= 0.6) return true;
    return false;
  }

  function deduplicateNews(items) {
    const seenTitles = [];
    const seenUrls = new Set();
    return items.filter((item) => {
      const news = item.news || item;
      const title = news.title || '';

      const urls = (news.sources || [])
        .filter((s) => typeof s === 'object' && s.url && s.url.startsWith('http'))
        .map((s) => s.url);
      for (const u of urls) {
        if (seenUrls.has(u)) return false;
      }

      for (const s of seenTitles) {
        if (titlesAreSimilar(title, s)) return false;
      }

      seenTitles.push(title);
      urls.forEach((u) => seenUrls.add(u));
      return true;
    });
  }

  // ── 工具 ──────────────────────────────────────
  function isWithinHours(dateStr, hourStr, hours) {
    const d = new Date(dateStr + "T" + hourStr + ":00:00+08:00");
    const diff = Date.now() - d.getTime();
    return diff >= 0 && diff <= hours * 3600000;
  }

  function getSignalEmoji(signal) {
    if (signal === "bullish") return "🟢";
    if (signal === "bearish") return "🔴";
    return "⚪";
  }

  function getSignalLabel(signal) {
    if (signal === "bullish") return "利多";
    if (signal === "bearish") return "利空";
    return "中性";
  }

  function getUrgencyLabel(urgency) {
    if (urgency === "immediate") return "⚡立即";
    if (urgency === "hours") return "⏰数小时";
    return "📅数天";
  }

  function getImpactLevel(news) {
    const val = news.impact_level;
    if (typeof val === "number") return val;
    if (val === "critical") return 5;
    if (val === "major") return 4;
    if (val === "moderate") return 3;
    const parsed = parseInt(val, 10);
    return isNaN(parsed) ? 3 : parsed;
  }

  function renderStars(level) {
    const n = Math.max(1, Math.min(5, level));
    return "★".repeat(n) + "☆".repeat(5 - n);
  }

  // ── 多空比例条 ────────────────────────────────
  function renderSentimentBar(newsItems) {
    if (!sentimentBarEl || !newsItems.length) {
      if (sentimentBarEl) sentimentBarEl.style.display = "none";
      return;
    }
    const bullish = newsItems.filter((n) => (n.news || n).signal === "bullish").length;
    const bearish = newsItems.filter((n) => (n.news || n).signal === "bearish").length;
    const neutral = newsItems.length - bullish - bearish;
    const total = newsItems.length;

    const bullPct = ((bullish / total) * 100).toFixed(0);
    const bearPct = ((bearish / total) * 100).toFixed(0);

    sentimentBarEl.style.display = "";
    sentimentBarEl.innerHTML = `
      <span class="sb-label">当期大盘</span>
      <span class="sb-bullish">🟢 ${bullish}</span>
      <span class="sb-neutral">⚪ ${neutral}</span>
      <span class="sb-bearish">🔴 ${bearish}</span>
      <div class="sb-gauge">
        <div class="sb-bull-part" style="width:${bullPct}%"></div>
        <div class="sb-bear-part" style="width:${bearPct}%"></div>
      </div>
    `;
  }

  // ── 渲染 ──────────────────────────────────────
  function renderSources(sources) {
    if (!sources || !sources.length) return "";
    return sources.map((s) => {
      if (typeof s === "object") {
        const name = s.name || s.url || "";
        const url = s.url || "";
        if (url) return `<a href="${url}" target="_blank" rel="noopener">${name}</a>`;
        if (name) return `<a href="https://www.google.com/search?q=${encodeURIComponent(name)}" target="_blank" rel="noopener">${name}</a>`;
        return "";
      }
      return typeof s === "string" ? s : "";
    }).join(", ");
  }

  function renderNewsCard(news, date, hour) {
    const card = document.createElement("div");
    card.className = "news-card";
    card.addEventListener("click", () => showModal(news, date, hour));

    const signal = news.signal || "neutral";
    const signalClass = `signal-${signal}`;
    const impactLevel = getImpactLevel(news);
    const stars = renderStars(impactLevel);
    const urgency = news.urgency || "days";
    const urgencyClass = `urgency-${urgency}`;
    const category = news.category || "";

    const coinTags = (news.impact_stocks || news.impact_coins || []).map((c) => `<span class="tag coin-tag">${c}</span>`).join("");

    card.innerHTML = `
      <div class="card-header">
        <div class="card-header-left">
          <span class="tag ${signalClass}">${getSignalEmoji(signal)} ${getSignalLabel(signal)}</span>
          <span class="card-title">${news.title}</span>
        </div>
        <span class="impact-stars">${stars}</span>
      </div>
      <p class="card-summary">${news.summary || ""}</p>
      ${news.reason ? `<p class="card-reason">🤖 ${news.reason}</p>` : ""}
      <div class="card-tags">
        ${category ? `<span class="tag category-tag">${category}</span>` : ""}
        <span class="tag ${urgencyClass}">${getUrgencyLabel(urgency)}</span>
        ${coinTags}
      </div>
      ${news.sources && news.sources.length ? `<p class="card-sources">来源: ${renderSources(news.sources)}</p>` : ""}
    `;
    return card;
  }

  function renderEmpty(msg) {
    return `<p class="empty-state">${msg}</p>`;
  }

  // ── 弹窗 ──────────────────────────────────────
  function showModal(news, date, hour) {
    const signal = news.signal || "neutral";
    const impactLevel = getImpactLevel(news);
    const stars = renderStars(impactLevel);
    const category = news.category || "";
    const stocks = (news.impact_stocks || news.impact_coins || []).map((c) => `<span class="tag coin-tag">${c}</span>`).join("");
    const sources = renderSources(news.sources);

    modalContent.innerHTML = `
      <h2>${news.title}</h2>
      <p class="meta">
        ${date} ${hour}:00 ·
        <span class="signal-label ${signal}">${getSignalEmoji(signal)} ${getSignalLabel(signal)}</span> ·
        ${stars}
        ${category ? ` · ${category}` : ""} ·
        ${getUrgencyLabel(news.urgency || "days")}
      </p>
      <div class="body">
        <p>${news.summary || "暂无详细内容"}</p>
        ${news.reason ? `<p class="modal-reason">🤖 AI筛选原因：${news.reason}</p>` : ""}
        ${stocks ? `<div class="coin-list">${stocks}</div>` : ""}
        ${sources ? `<p class="modal-sources">来源: ${sources}</p>` : ""}
      </div>
    `;
    modalOverlay.classList.add("active");
  }

  modalClose.addEventListener("click", () => modalOverlay.classList.remove("active"));
  modalOverlay.addEventListener("click", (e) => {
    if (e.target === modalOverlay) modalOverlay.classList.remove("active");
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") modalOverlay.classList.remove("active");
  });

  // ── 归档树 ────────────────────────────────────
  function buildTree(archiveData) {
    const tree = {};
    for (const { date, hour, news } of archiveData) {
      const [y, m, d] = date.split("-");
      if (!tree[y]) tree[y] = {};
      if (!tree[y][m]) tree[y][m] = {};
      if (!tree[y][m][d]) tree[y][m][d] = [];
      tree[y][m][d].push({ hour, news, date });
    }
    return tree;
  }

  function makeToggle(label, count) {
    const btn = document.createElement("button");
    btn.className = "tree-toggle";
    btn.innerHTML = `<span class="arrow">▶</span><span class="node-label">${label}</span><span class="node-count">${count}条</span>`;
    return btn;
  }

  function createBranchNode(label, count, children) {
    const node = document.createElement("div");
    node.className = "tree-node";
    const toggle = makeToggle(label, count);
    toggle.addEventListener("click", () => node.classList.toggle("open"));
    node.appendChild(toggle);
    const container = document.createElement("div");
    container.className = "tree-children";
    children.forEach((c) => container.appendChild(c));
    node.appendChild(container);
    return node;
  }

  function createHourNode(label, newsArr, date, hour) {
    const node = document.createElement("div");
    node.className = "tree-node";
    const toggle = makeToggle(label, newsArr.length);
    toggle.addEventListener("click", () => node.classList.toggle("open"));
    node.appendChild(toggle);
    const list = document.createElement("div");
    list.className = "tree-news-list";
    newsArr.forEach((n) => {
      const item = document.createElement("div");
      item.className = "tree-news-item";
      const signal = n.signal || "neutral";
      item.innerHTML = `<span class="item-title">${n.title}</span><span class="item-signal">${getSignalEmoji(signal)}</span>`;
      item.addEventListener("click", () => showModal(n, date, hour));
      list.appendChild(item);
    });
    node.appendChild(list);
    return node;
  }

  function renderArchiveTree(archiveData) {
    if (!archiveData.length) {
      archiveTreeEl.innerHTML = renderEmpty("暂无归档消息");
      return;
    }
    const tree = buildTree(archiveData);
    archiveTreeEl.innerHTML = "";

    Object.keys(tree).sort((a, b) => b - a).forEach((year) => {
      const months = tree[year];
      let yearCount = 0;
      const monthNodes = Object.keys(months).sort((a, b) => b - a).map((month) => {
        const days = months[month];
        let monthCount = 0;
        const dayNodes = Object.keys(days).sort((a, b) => b - a).map((day) => {
          const hourEntries = days[day];
          let dayCount = 0;
          const hourNodes = hourEntries.sort((a, b) => b.hour.localeCompare(a.hour)).map((entry) => {
            dayCount += entry.news.length;
            return createHourNode(`${entry.hour}:00`, entry.news, entry.date, entry.hour);
          });
          monthCount += dayCount;
          return createBranchNode(`${parseInt(day)}日`, dayCount, hourNodes);
        });
        yearCount += monthCount;
        return createBranchNode(`${parseInt(month)}月`, monthCount, dayNodes);
      });
      archiveTreeEl.appendChild(createBranchNode(`${year}年`, yearCount, monthNodes));
    });
  }

  // ── 数据加载 ──────────────────────────────────

  async function loadIndex(bustCache) {
    if (window.__NEWS_INDEX__) return window.__NEWS_INDEX__;
    const url = bustCache
      ? `${DATA_BASE}/reports-index.json?_t=${Date.now()}`
      : `${DATA_BASE}/reports-index.json`;
    const resp = await fetch(url);
    if (!resp.ok) return null;
    return resp.json();
  }

  async function loadDetail(key, bustCache) {
    if (window.__NEWS_DATA__ && window.__NEWS_DATA__[key])
      return window.__NEWS_DATA__[key];
    try {
      const url = bustCache
        ? `${DATA_BASE}/${key}.json?_t=${Date.now()}`
        : `${DATA_BASE}/${key}.json`;
      const resp = await fetch(url);
      return resp.json();
    } catch {
      return null;
    }
  }

  async function loadData(bustCache) {
    try {
      const index = await loadIndex(bustCache);
      if (!index) {
        recentNewsEl.innerHTML = renderEmpty("暂无消息数据，等待首次抓取...");
        return;
      }

      const recentItems = [];
      const archiveData = [];

      for (const report of index) {
        const key = `${report.date}-${report.hour}`;
        const detail = await loadDetail(key, bustCache);
        if (!detail) continue;
        const news = detail.news || [];
        if (!news.length) continue;

        const hour = report.hour || "00";

        news.forEach((n) => allNewsItems.push({ news: n, date: report.date, hour }));

        if (isWithinHours(report.date, hour, RECENT_HOURS)) {
          news.forEach((n) => recentItems.push({ news: n, date: report.date, hour }));
        } else {
          archiveData.push({ date: report.date, hour, news });
        }
      }

      recentItems.sort((a, b) => getImpactLevel(b.news) - getImpactLevel(a.news));

      const dedupedRecent = deduplicateNews(recentItems);

      renderSentimentBar(dedupedRecent);

      if (dedupedRecent.length) {
        recentNewsEl.innerHTML = "";
        dedupedRecent.forEach((item) =>
          recentNewsEl.appendChild(renderNewsCard(item.news, item.date, item.hour))
        );
      } else {
        recentNewsEl.innerHTML = renderEmpty("近 6 小时暂无新消息");
      }

      renderArchiveTree(archiveData);
    } catch (err) {
      console.error("加载数据失败:", err);
      recentNewsEl.innerHTML = renderEmpty("数据加载失败，请稍后刷新");
    }
  }

  // ── 搜索 ──────────────────────────────────────
  let searchTimer = null;
  searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(doSearch, 250);
  });

  function doSearch() {
    const q = searchInput.value.trim().toLowerCase();
    if (!q) {
      searchResults.innerHTML = renderEmpty("输入关键词搜索所有消息");
      searchMeta.textContent = "";
      return;
    }
    const keywords = q.split(/\s+/);
    const matched = allNewsItems.filter(({ news }) => {
      const haystack = [
        news.title,
        news.summary,
        news.reason,
        news.category || "",
        ...(news.impact_stocks || news.impact_coins || []),
        news.signal || "",
      ].join(" ").toLowerCase();
      return keywords.every((kw) => haystack.includes(kw));
    });
    const dedupedMatched = deduplicateNews(matched);
    searchMeta.textContent = `找到 ${dedupedMatched.length} 条结果`;
    if (!dedupedMatched.length) {
      searchResults.innerHTML = renderEmpty("没有找到相关消息");
      return;
    }
    searchResults.innerHTML = "";
    dedupedMatched.sort((a, b) => getImpactLevel(b.news) - getImpactLevel(a.news));
    dedupedMatched.forEach((item) =>
      searchResults.appendChild(renderNewsCard(item.news, item.date, item.hour))
    );
  }

  // ── Toast 提示 ─────────────────────────────────
  function showToast(msg) {
    if (!toastEl) return;
    toastEl.textContent = msg;
    toastEl.classList.remove("hidden");
    setTimeout(() => toastEl.classList.add("hidden"), 2000);
  }

  // ── 自动刷新（30 秒轮询检测更新）─────────────
  const POLL_INTERVAL = 30_000;
  let lastIndexHash = null;
  let autoRefreshTimer = null;

  function hashString(str) {
    let h = 0;
    for (let i = 0; i < str.length; i++) {
      h = ((h << 5) - h + str.charCodeAt(i)) | 0;
    }
    return h;
  }

  async function checkForUpdates() {
    try {
      const resp = await fetch(`${DATA_BASE}/reports-index.json?_t=${Date.now()}`);
      if (!resp.ok) return;
      const text = await resp.text();
      const hash = hashString(text);
      if (lastIndexHash !== null && hash !== lastIndexHash) {
        console.log("[auto-refresh] 检测到数据更新，刷新中...");
        allNewsItems = [];
        await loadData(true);
        showToast("📡 新消息已更新");
      }
      lastIndexHash = hash;
    } catch {
      // 静默忽略网络错误
    }
  }

  function startAutoRefresh() {
    if (location.protocol === "file:") return;
    if (autoRefreshTimer) return;
    checkForUpdates();
    autoRefreshTimer = setInterval(checkForUpdates, POLL_INTERVAL);
  }

  // ── 初始化 ────────────────────────────────────
  initTheme();
  loadData(true).then(startAutoRefresh);
})();
