/**
 * 邮件看板 - AstrBot 插件 Pages 前端
 * 通过 window.AstrBotPluginPage bridge 与插件后端 Web API 通信。
 *
 * 进度机制：触发扫描/重新总结/生成汇总后，轮询 /status 接口，
 * 展示进度条、当前/总数、阶段、ETA 与耗时；任务完成时自动刷新列表
 * 并展示生成的报告。
 */
const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

const PRIO_ICON = { HIGH: "🔴", MEDIUM: "🟡", LOW: "🟢" };
const PRIO_LABEL = { HIGH: "高", MEDIUM: "中", LOW: "低" };
const OP_LABEL = { scan: "扫描", resummarize: "重新总结", report: "生成汇总" };
const TAG_OPTIONS = ["已完成", "代办", "重审", "优先处理"];

let currentUid = null;
let pollTimer = null;
let lastOp = null; // 最近一次触发、正在等待完成的任务："scan" | "resummarize" | "report"
let allTags = {};  // uid → { user_tags: [...], system_tags: [...] }

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

function fmtDuration(sec) {
  sec = Math.max(0, Math.round(Number(sec) || 0));
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return s ? `${m}分${s}s` : `${m}分`;
}

function setBusy(b) {
  ["btn-scan", "btn-report", "btn-resummarize"].forEach((id) => {
    $(id).disabled = b;
  });
}

function renderStatusBar(st) {
  const dark = bridge.getContext()?.isDark;
  const theme = dark ? "dark" : "light";
  const chips = [
    `<span class="chip">主题: ${theme}</span>`,
    `<span class="chip">插件: ${esc(bridge.getContext()?.displayName || "")}</span>`,
  ];
  if (st?.running) {
    chips.push(
      `<span class="chip warn">⏳ ${esc(OP_LABEL[st.operation] || st.operation || "任务")}进行中</span>`,
    );
  } else if (st?.completed) {
    chips.push(`<span class="chip ok">✅ 上次任务已完成</span>`);
  } else {
    chips.push(`<span class="chip">空闲</span>`);
  }
  $("status-bar").innerHTML = chips.join("");
}

function renderProgress(st) {
  if (st.running) {
    show("progress-view");
    const pct = Math.max(0, Math.min(100, Number(st.percent) || 0));
    $("progress-bar").style.width = `${pct}%`;
    $("progress-label").textContent = `${esc(OP_LABEL[st.operation] || st.operation || "任务")}: ${esc(st.label || "处理中")}`;
    $("progress-count").textContent = `${Number(st.current) || 0} / ${Number(st.total) || 0} (${pct}%)`;
    $("progress-phase").textContent = `阶段: ${esc(st.phase || "--")}`;
    $("progress-eta").textContent = st.eta_seconds ? `ETA: ${fmtDuration(st.eta_seconds)}` : "ETA: --";
    $("progress-elapsed").textContent = `耗时: ${fmtDuration(st.elapsed_seconds || 0)}`;
    $("progress-message").textContent = st.message || "";
    $("progress-spinner").classList.remove("hidden");
  } else {
    $("progress-spinner").classList.add("hidden");
  }
  renderStatusBar(st);
}

async function pollStatus() {
  let st = null;
  try {
    st = await bridge.apiGet("status");
  } catch (e) {
    // 接口暂不可用：只要还在等任务就继续轮询
    if (lastOp) {
      pollTimer = setTimeout(pollStatus, 1500);
    } else {
      setBusy(false);
    }
    return;
  }

  renderProgress(st);

  if (st.running) {
    pollTimer = setTimeout(pollStatus, 1200);
    return;
  }

  // 任务已结束
  setBusy(false);
  if (lastOp) {
    const op = lastOp;
    lastOp = null;
    handleDone(st, op);
  } else {
    pollTimer = null;
  }
}

function handleDone(st, op) {
  pollTimer = null;
  // 展示任务生成的报告（如有）
  if (st.report) {
    renderReport(st.report);
    hide("list-view");
    hide("detail-view");
    show("report-view");
    $("report-view").scrollIntoView({ behavior: "smooth" });
  }
  // 刷新列表（扫描/重新总结会新增或更新分析记录）
  loadList();
  loadTags();
  if (op === "report" && !st.report) {
    alert("汇总报告生成失败：暂无邮件分析记录，或 LLM 配置未就绪。");
  }
}

async function loadTags() {
  try {
    const data = await bridge.apiGet("tags");
    allTags = data.tags || {};
  } catch (e) {
    allTags = {};
  }
}

async function loadList() {
  try {
    const data = await bridge.apiGet("list", { limit: 50 });
    renderList(data.records || [], data.total || 0);
  } catch (e) {
    $("list-body").innerHTML = `<div class="error-box">加载列表失败: ${esc(e.message)}</div>`;
  }
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
      const attach = r.has_attachment ? " 📎" : "";

      // 标签特殊处理：代办强制中优先级，已完成标记
      const userTags = Array.isArray(r.user_tags) ? r.user_tags : [];
      let effectivePrio = r.priority;
      let tagBadges = "";
      if (userTags.includes("代办")) {
        effectivePrio = "medium";
      }
      if (userTags.includes("已完成")) {
        tagBadges = `<span class="chip done">✅ 已完成</span>`;
      } else if (userTags.length > 0) {
        tagBadges = userTags.map(t => `<span class="chip tag">${esc(t)}</span>`).join("");
      }

      // 获取系统标签（用于提示）
      const sysTags = allTags[String(r.uid)]?.system_tags || [];
      const hasDone = userTags.includes("已完成");
      const hasTodo = userTags.includes("代办");

      return `<div class="email-card" data-uid="${esc(String(r.uid))}">
        <div class="card-top">
          <span class="prio-icon">${prioIcon(effectivePrio)}</span>
          <span class="card-title">${esc(r.title || "(无标题)")}${attach}</span>
        </div>
        <div class="card-meta">
          <span>${esc(r.sender || "")}</span>
          <span>${esc(r.date || "")}</span>
          <span>${esc(PRIO_LABEL[String(effectivePrio || "low").toUpperCase()] || "低")}优先级</span>
          ${cat}
          ${tagBadges}
        </div>
        ${summary}
        ${err}
        ${tagBadges ? `<div class="card-tags" data-uid="${esc(String(r.uid))}">${userTags.length ? userTags.map(t => `<span class="chip tag">${esc(t)}</span>`).join("") : "<span class='text-muted'>点击查看详情添加标签</span>"}</div>` : ""}
      </div>`;
    })
    .join("");

  body.querySelectorAll(".email-card").forEach((card) => {
    card.addEventListener("click", () => openDetail(card.dataset.uid));
  });
}

async function openDetail(uid) {
  try {
    const a = await bridge.apiGet("detail", { uid });
    currentUid = uid;

    // 获取当前标签
    const tagInfo = allTags[String(uid)];
    const userTags = tagInfo?.user_tags || [];

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
    const attachments = Array.isArray(a.attachment_names) && a.attachment_names.length
      ? `<div class="report-section"><h3>📎 附件</h3>${a.attachment_names
          .map((n) => `<div class="report-item"><span class="d">📎 ${esc(n)}</span></div>`)
          .join("")}</div>`
      : "";

    // 系统标签展示（LLM 自动生成的）
    const sysTags = tagInfo?.system_tags || [];
    const sysTagsHtml = sysTags.length
      ? `<div class="report-section"><h3>系统标签</h3><div class="card-meta">${sysTags
          .map((t) => `<span class="chip">${esc(t)}</span>`)
          .join("")}</div></div>`
      : "";

    // 标签管理 UI
    const tagBadges = userTags.length
      ? userTags.map(t => `<span class="chip tag">${esc(t)}</span>`).join("")
      : "";

    const tagSelector = `<div class="tag-selector" data-uid="${esc(String(uid))}">
      <div class="current-tags">${tagBadges || "<span class='text-muted'>暂无标签</span>"}</div>
      <div class="tag-options">
        ${TAG_OPTIONS.map(opt => {
          const disabled = userTags.includes(opt) ? "disabled" : "";
          return `<button class="tag-add-btn" data-tag="${esc(opt)}" ${disabled}>+ ${esc(opt)}</button>`;
        }).join("")}
      </div>
    </div>`;

    $("detail-body").innerHTML = `
      <div class="detail-header">
        <h2>${esc(a.title || "(无标题)")}</h2>
        <div class="detail-badges">
          <span class="chip prio">${prioIcon(a.priority)} ${esc(PRIO_LABEL[String(a.priority || "low").toUpperCase()] || "低")}优先级</span>
          ${a.is_important ? '<span class="chip important">⭐ 重要</span>' : ''}
          ${a.category ? `<span class="chip">${esc(a.category)}</span>` : ''}
          ${a.has_attachment ? '<span class="chip">📎含附件</span>' : ''}
        </div>
      </div>
      <div class="kv-grid">
        <div class="kv-item"><div class="k">发件人</div><div class="v">${esc(a.sender || "")}</div></div>
        <div class="kv-item"><div class="k">日期</div><div class="v">${esc(a.date || "")}</div></div>
        <div class="kv-item"><div class="k">情感</div><div class="v">${esc(a.sentiment || "—")}</div></div>
        <div class="kv-item"><div class="k">链接</div><div class="v">${a.has_links ? "有" : "无"}</div></div>
      </div>
      ${a.body_summary ? `<div class="report-section"><h3>摘要</h3><div class="card-summary">${esc(a.body_summary)}</div></div>` : ""}
      ${a.action_needed ? `<div class="report-section"><h3>需要行动</h3><div class="card-summary">⚡ ${esc(a.action_needed)}${a.action_deadline ? `（截止 ${esc(a.action_deadline)}）` : ""}</div></div>` : ""}
      ${points}
      ${amounts}
      ${links}
      ${attachments}
      ${sysTagsHtml}
      <div class="report-section">
        <h3>🏷️ 用户标签</h3>
        ${tagSelector}
      </div>
      ${err}
    `;

    // 绑定标签按钮事件
    $("detail-body").querySelectorAll(".tag-add-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const uid = btn.closest(".tag-selector").dataset.uid;
        const tag = btn.dataset.tag;
        if (btn.disabled) return;
        try {
          await bridge.apiPost("tag", { uid, tag, action: "add" });
          loadTags();
          openDetail(uid); // 重新打开详情以刷新
          loadList(); // 刷新列表以更新标签显示
        } catch (e) {
          alert(`添加标签失败: ${e.message}`);
        }
      });
    });

    // 绑定移除标签按钮
    $("detail-body").querySelectorAll(".tag-remove-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const uid = btn.closest(".tag-selector").dataset.uid;
        const tag = btn.dataset.tag;
        try {
          await bridge.apiPost("tag", { uid, tag, action: "remove" });
          loadTags();
          openDetail(uid);
          loadList();
        } catch (e) {
          alert(`移除标签失败: ${e.message}`);
        }
      });
    });

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
  setBusy(true);
  lastOp = "report";
  $("progress-title").textContent = "生成汇总报告";
  show("progress-view");
  try {
    await bridge.apiGet("report");
    pollStatus();
  } catch (e) {
    setBusy(false);
    lastOp = null;
    alert(`生成汇总失败: ${e.message}`);
  }
}

async function triggerScan() {
  setBusy(true);
  lastOp = "scan";
  $("progress-title").textContent = "触发扫描";
  show("progress-view");
  try {
    await bridge.apiPost("scan");
    pollStatus();
  } catch (e) {
    setBusy(false);
    lastOp = null;
    alert(`触发扫描失败: ${e.message}`);
  }
}

async function triggerResummarize() {
  setBusy(true);
  lastOp = "resummarize";
  $("progress-title").textContent = "重新总结";
  show("progress-view");
  // 下拉选择：missing=仅未总结/上次失败的；all=全部邮件强制覆盖
  const force = $("resummarize-mode").value === "all";
  try {
    await bridge.apiPost("resummarize", { force });
    pollStatus();
  } catch (e) {
    setBusy(false);
    lastOp = null;
    alert(`触发重新总结失败: ${e.message}`);
  }
}

document.querySelectorAll("[data-close]").forEach((btn) => {
  btn.addEventListener("click", () => {
    hide(btn.dataset.close);
    show("list-view");
  });
});

$("btn-refresh").addEventListener("click", () => { loadList(); loadTags(); });
$("btn-report").addEventListener("click", generateReport);
$("btn-scan").addEventListener("click", triggerScan);
$("btn-resummarize").addEventListener("click", triggerResummarize);

async function init() {
  await bridge.ready();
  renderStatusBar(null);
  await Promise.all([loadList(), loadTags()]);
  // 若后台已有任务在运行（例如网页刷新前触发的扫描），恢复轮询
  try {
    const st = await bridge.apiGet("status");
    if (st.running) {
      lastOp = st.operation || "scan";
      setBusy(true);
      $("progress-title").textContent = OP_LABEL[st.operation] || "任务进度";
      pollStatus();
    } else {
      renderProgress(st);
    }
  } catch (e) {
    // 老版本 AstrBot 可能没有 /status 接口，忽略即可
  }
}

init();
