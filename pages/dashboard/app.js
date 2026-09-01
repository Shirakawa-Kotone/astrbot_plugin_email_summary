/**
 * 邮件看板 - AstrBot 插件 Pages 前端
 * 通过 window.AstrBotPluginPage bridge 与插件后端 Web API 通信。
 */
const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

const PRIO_ICON = { HIGH: "🔴", MEDIUM: "🟡", LOW: "🟢" };
const PRIO_LABEL = { HIGH: "高", MEDIUM: "中", LOW: "低" };

let currentUid = null;

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function prioIcon(p) {
  const key = String(p || "LOW").toUpperCase();
  return PRIO_ICON[key] || "⚪";
}

function show(id) {
  $(id).classList.remove("hidden");
}

function hide(id) {
  $(id).classList.add("hidden");
}

function renderStatusBar() {
  // 状态栏：展示插件上下文（亮暗主题等）提示，轻量实现
  const dark = bridge.getContext()?.isDark;
  const theme = dark ? "dark" : "light";
  const chips = [
    `<span class="chip">主题: ${theme}</span>`,
    `<span class="chip">插件: ${esc(bridge.getContext()?.displayName || "")}</span>`,
  ];
  $("status-bar").innerHTML = chips.join("");
}

function renderList(records, total) {
  $("list-count").textContent = `共 ${total} 封`;
  const body = $("list-body");
  if (!records || records.length === 0) {
    body.innerHTML =
      '<div class="empty">暂无邮件分析记录<br/>点击右上角「触发扫描」拉取并分析新邮件。</div>';
    return;
  }

  body.innerHTML = records
    .map((r) => {
      const err = r.analysis_error ? `<div class="card-error">⚠️ LLM 分析失败: ${esc(r.analysis_error)}</div>` : "";
      const summary = r.body_summary
        ? `<div class="card-summary">${esc(r.body_summary)}</div>`
        : "";
      const cat = r.category ? `<span>${esc(r.category)}</span>` : "";
      return `<div class="email-card" data-uid="${esc(r.uid)}">
        <div class="card-top">
          <span class="prio-icon">${prioIcon(r.priority)}</span>
          <span class="card-title">${esc(r.title || "(无标题)")}</span>
        </div>
        <div class="card-meta">
          <span>${esc(r.sender || "")}</span>
          <span>${esc(r.date || "")}</span>
          <span>${esc(PRIO_LABEL[String(r.priority || "low").toUpperCase()] || "低")}优先级</span>
          ${cat}
        </div>
        ${summary}
        ${err}
      </div>`;
    })
    .join("");

  body.querySelectorAll(".email-card").forEach((card) => {
    card.addEventListener("click", () => openDetail(card.dataset.uid));
  });
}

async function loadList() {
  try {
    const data = await bridge.apiGet("list", { limit: 50 });
    renderList(data.records || [], data.total || 0);
  } catch (e) {
    $("list-body").innerHTML = `<div class="error-box">加载列表失败: ${esc(e.message)}</div>`;
  }
}

function renderDetail(a) {
  const err = a.analysis_error
    ? `<div class="error-box">⚠️ LLM 分析失败: ${esc(a.analysis_error)}</div>`
    : "";
  const points = Array.isArray(a.key_points) && a.key_points.length
    ? `<div class="report-section"><h3>关键要点</h3>${a.key_points
        .map((p) => `<div class="report-item"><span class="d">• ${esc(p)}</span></div>`)
        .join("")}</div>`
    : "";
  const links = Array.isArray(a.links) && a.links.length
    ? `<div class="report-section"><h3>链接</h3>${a.links
        .map((l) => `<div class="report-item"><span class="d">🔗 ${esc(l)}</span></div>`)
        .join("")}</div>`
    : "";
  const amounts = Array.isArray(a.amounts) && a.amounts.length
    ? `<div class="report-section"><h3>涉及金额</h3>${a.amounts
        .map((m) => `<div class="report-item"><span class="d">💰 ${esc(m)}</span></div>`)
        .join("")}</div>`
    : "";
  const tags = Array.isArray(a.tags) && a.tags.length
    ? `<div class="report-section"><h3>标签</h3><div class="card-meta">${a.tags
        .map((t) => `<span class="chip">${esc(t)}</span>`)
        .join("")}</div></div>`
    : "";

  $("detail-body").innerHTML = `
    ${err}
    <div class="kv-grid">
      <div class="kv-item"><div class="k">标题</div><div class="v">${esc(a.title || "")}</div></div>
      <div class="kv-item"><div class="k">发件人</div><div class="v">${esc(a.sender || "")}</div></div>
      <div class="kv-item"><div class="k">日期</div><div class="v">${esc(a.date || "")}</div></div>
      <div class="kv-item"><div class="k">优先级</div><div class="v">${prioIcon(a.priority)} ${esc(PRIO_LABEL[String(a.priority || "low").toUpperCase()] || "低")}</div></div>
      <div class="kv-item"><div class="k">是否重要</div><div class="v">${a.is_important ? "✅ 是" : "❌ 否"}</div></div>
      <div class="kv-item"><div class="k">分类</div><div class="v">${esc(a.category || "其他")}</div></div>
      <div class="kv-item"><div class="k">子分类</div><div class="v">${esc(a.sub_category || "—")}</div></div>
      <div class="kv-item"><div class="k">情感</div><div class="v">${esc(a.sentiment || "—")}</div></div>
      <div class="kv-item"><div class="k">附件</div><div class="v">${a.has_attachment ? "有" : "无"}</div></div>
      <div class="kv-item"><div class="k">链接</div><div class="v">${a.has_links ? "有" : "无"}</div></div>
    </div>
    ${a.body_summary ? `<div class="report-section"><h3>摘要</h3><div class="card-summary">${esc(a.body_summary)}</div></div>` : ""}
    ${a.action_needed ? `<div class="report-section"><h3>需要行动</h3><div class="card-summary">⚡ ${esc(a.action_needed)}${a.action_deadline ? `（截止 ${esc(a.action_deadline)}）` : ""}</div></div>` : ""}
    ${points}
    ${amounts}
    ${tags}
    ${links}
  `;
}

async function openDetail(uid) {
  try {
    const a = await bridge.apiGet("detail", { uid });
    currentUid = uid;
    renderDetail(a);
    hide("list-view");
    hide("report-view");
    show("detail-view");
    $("detail-view").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    alert(`加载详情失败: ${e.message}`);
  }
}

function renderReport(r) {
  const important = Array.isArray(r.important_emails)
    ? r.important_emails
        .map(
          (it) => `<div class="report-item">
            <div class="t">${prioIcon(it.priority)} ${esc(it.title || "无标题")}</div>
            <div class="d">${esc(it.sender || "")} · ${esc(it.body_summary || "")}</div>
            ${it.action_needed ? `<div class="d">⚡ ${esc(it.action_needed)}</div>` : ""}
          </div>`,
        )
        .join("")
    : "";

  const actions = Array.isArray(r.action_items)
    ? r.action_items
        .map((a) => {
          if (typeof a === "string") return `<div class="action-item">⚡ ${esc(a)}</div>`;
          const task = a.task || a;
          const deadline = a.deadline ? `（截止 ${esc(a.deadline)}）` : "";
          return `<div class="action-item">⚡ ${esc(task)}${deadline}</div>`;
        })
        .join("")
    : "";

  const trends = Array.isArray(r.trends)
    ? `<div class="report-section"><h3>趋势</h3>${r.trends
        .map((t) => `<div class="report-item"><span class="d">${esc(t)}</span></div>`)
        .join("")}</div>`
    : "";

  $("report-body").innerHTML = `
    <div class="report-summary">${esc(r.summary || "")}</div>
    <div class="report-stats">
      <div class="stat-item"><b>${esc(r.total_count ?? 0)}</b>总数</div>
      <div class="stat-item"><b>${esc(r.important_count ?? 0)}</b>重要</div>
      <div class="stat-item"><b>${esc(r.action_required_count ?? 0)}</b>需行动</div>
    </div>
    ${important ? `<div class="report-section"><h3>🔴 重要邮件</h3>${important}</div>` : ""}
    ${actions ? `<div class="report-section"><h3>⚡ 待办事项</h3>${actions}</div>` : ""}
    ${trends}
  `;
}

async function generateReport() {
  $("btn-report").disabled = true;
  const orig = $("btn-report").textContent;
  $("btn-report").textContent = "生成中…";
  try {
    const r = await bridge.apiGet("report");
    renderReport(r);
    hide("list-view");
    hide("detail-view");
    show("report-view");
    $("report-view").scrollIntoView({ behavior: "smooth" });
  } catch (e) {
    alert(`生成汇总失败: ${e.message}`);
  } finally {
    $("btn-report").disabled = false;
    $("btn-report").textContent = orig;
  }
}

async function triggerScan() {
  $("btn-scan").disabled = true;
  const orig = $("btn-scan").textContent;
  $("btn-scan").textContent = "扫描中…";
  try {
    const r = await bridge.apiPost("scan");
    alert(r.message || "扫描已开始");
    // 稍后自动刷新一次列表
    setTimeout(loadList, 3000);
  } catch (e) {
    alert(`触发扫描失败: ${e.message}`);
  } finally {
    $("btn-scan").disabled = false;
    $("btn-scan").textContent = orig;
  }
}

document.querySelectorAll("[data-close]").forEach((btn) => {
  btn.addEventListener("click", () => {
    hide(btn.dataset.close);
    show("list-view");
  });
});

$("btn-refresh").addEventListener("click", loadList);
$("btn-report").addEventListener("click", generateReport);
$("btn-scan").addEventListener("click", triggerScan);

async function init() {
  await bridge.ready();
  renderStatusBar();
  await loadList();
}

init();
