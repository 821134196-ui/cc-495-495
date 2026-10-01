/* 实习三方协议管理 —— 前端 SPA（无依赖） */
"use strict";

const PARTY = { student: "学生", college: "学院", company: "单位" };
const APP_STATUS = { submitted: "待学院审核", college_approved: "待单位确认", signing: "签署中", effective: "已生效", ended: "已结束" };
const AG_STATUS = { signing: "签署中", effective: "已生效", superseded: "已被取代", terminated: "已终止" };
const CHANGE_TYPES = { withdraw: "单位撤岗", transfer: "学生转岗", early_end: "实习提前结束", contact_change: "单位联系人更换", attachment_fix: "协议附件不一致" };
const EVENT_KINDS = {
  application_submitted: "提交申请", college_approved: "学院审核通过", company_confirmed: "单位确认职责",
  version_created: "版本生成", signed: "完成签署", effective: "协议生效", superseded: "旧版失效",
  terminated: "协议终止", position_withdrawn: "单位撤岗", change_proposed: "发起变更",
  change_confirmed: "确认变更", change_applied: "变更生效", order_error: "签署顺序错误", unauthorized: "越权操作",
};
const KEY_EVENTS = new Set(["effective", "version_created", "change_applied"]);
const BAD_EVENTS = new Set(["order_error", "unauthorized", "terminated", "position_withdrawn"]);

let S = null;                 // /api/state 快照
const ui = { tab: "apply", selectedChange: null, chains: {} };

/* ---------------- 基础工具 ---------------- */
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, method = "GET", body = null) {
  const opt = { method, headers: {} };
  if (body) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.error) || `请求失败（${r.status}）`);
  return data;
}

let toastTimer = null;
function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " error" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 3200);
}

function actor() {
  const [role, id] = $("#actorSelect").value.split(":");
  return { role, id: Number(id) };
}
function actorRow() {
  const a = actor();
  const table = { student: S.students, teacher: S.teachers, contact: S.contacts }[a.role];
  return table.find((r) => r.id === a.id);
}
function authorizedFor(party, app) {
  const a = actor(), me = actorRow();
  if (!me) return false;
  if (party === "student") return a.role === "student" && a.id === app.student_id;
  if (party === "college") return a.role === "teacher" && me.college_id === app.student_college_id;
  if (party === "company") return a.role === "contact" && me.active === 1 && me.company_id === app.company_id;
  return false;
}

/* ---------------- 渲染入口 ---------------- */
async function refresh() {
  S = await api("/api/state");
  renderTodoBadge();
  ({ apply: renderApply, sign: renderSign, change: renderChange, todo: renderTodo, archive: renderArchive })[ui.tab]();
}

function renderTodoBadge() {
  const a = actor(), me = actorRow();
  const pill = $("#todoCount");
  if (a.role !== "teacher" || !me) { pill.hidden = true; return; }
  const cid = me.college_id;
  const n =
    S.applications.filter((x) => x.status === "submitted" && x.student_college_id === cid).length +
    S.applications.filter((x) => x.next_party === "college" && x.student_college_id === cid).length +
    S.applications.filter((x) => x.pending_change && x.student_college_id === cid &&
      x.pending_change.affected.includes("college") &&
      !x.pending_change.confirmations.some((c) => c.party === "college")).length;
  pill.hidden = n === 0;
  pill.textContent = n;
}

/* ---------------- 岗位申请 ---------------- */
function renderApply() {
  const a = actor();
  const openPositions = S.positions.filter((p) => p.status === "open");
  const studentOptions = S.students.map((s) => `<option value="${s.id}" ${a.role === "student" && a.id === s.id ? "selected" : ""}>${esc(s.name)}（${esc(s.college_name)}）</option>`).join("");
  const posOptions = openPositions.map((p) => `<option value="${p.id}">${esc(p.title)} · ${esc(p.company_name)}</option>`).join("");

  $("#tab-apply").innerHTML = `
  <div class="grid cols-side">
    <div class="card">
      <h2>提交岗位申请</h2>
      <form class="stack" id="applyForm">
        <label>学生
          <select name="student_id" id="applyStudent">${studentOptions}</select>
        </label>
        <label>实习岗位（仅显示在招岗位）
          <select name="position_id">${posOptions || "<option value=''>暂无可申请岗位</option>"}</select>
        </label>
        <label>指导老师（本学院）
          <select name="teacher_id" id="applyTeacher"></select>
        </label>
        <label>开始日期 <input type="date" name="start_date" value="2026-10-12" required></label>
        <label>结束日期 <input type="date" name="end_date" value="2027-04-12" required></label>
        <button class="btn" type="submit">提交申请</button>
        <p class="muted">流程：学生提交 → 学院核对培养要求 → 单位确认职责 → 三方电子签署。</p>
      </form>
    </div>
    <div>
      <div class="card">
        <h2>岗位一览</h2>
        <table class="list">
          <thead><tr><th>岗位</th><th>单位</th><th>职责</th><th>状态</th></tr></thead>
          <tbody>${S.positions.map((p) => `
            <tr>
              <td>${esc(p.title)}</td><td>${esc(p.company_name)}</td>
              <td class="muted">${esc(p.duties)}</td>
              <td>${p.status === "open" ? '<span class="badge blue">在招</span>' : '<span class="badge warn">已撤岗</span>'}</td>
            </tr>`).join("")}
          </tbody>
        </table>
      </div>
      <div class="section-title"><h2>申请进度</h2><span class="muted">${S.applications.length} 条</span></div>
      <div id="applyList">${S.applications.map(renderApplyRow).join("") || '<div class="empty">暂无申请</div>'}</div>
    </div>
  </div>`;

  const fillTeachers = () => {
    const sid = Number($("#applyStudent").value);
    const stu = S.students.find((s) => s.id === sid);
    $("#applyTeacher").innerHTML = S.teachers
      .filter((t) => t.college_id === (stu && stu.college_id))
      .map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("");
  };
  $("#applyStudent").addEventListener("change", fillTeachers);
  fillTeachers();

  $("#applyForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    try {
      await api("/api/applications", "POST", {
        actor: actor(),
        student_id: Number(f.student_id.value),
        position_id: Number(f.position_id.value),
        teacher_id: Number(f.teacher_id.value),
        start_date: f.start_date.value,
        end_date: f.end_date.value,
      });
      toast("申请已提交，等待学院审核");
      await refresh();
    } catch (err) { toast(err.message, true); }
  });
}

function renderApplyRow(app) {
  const me = actorRow();
  const actions = [];
  if (app.status === "submitted" && actor().role === "teacher" && me && me.college_id === app.student_college_id)
    actions.push(`<button class="btn" onclick="A.review(${app.id})">学院审核通过</button>`);
  if (app.status === "college_approved" && actor().role === "contact" && me && me.active === 1 && me.company_id === app.company_id)
    actions.push(`<button class="btn" onclick="A.companyConfirm(${app.id})">确认岗位职责</button>`);
  if (app.status === "signing")
    actions.push(`<button class="btn ghost" onclick="A.goto('sign')">前往签署</button>`);
  if (app.status === "effective")
    actions.push(`<button class="btn ghost" onclick="A.goto('change')">发起/查看变更</button>`);
  const ag = app.agreement;
  return `<div class="card">
    <div class="card-head">
      <span class="title">#${app.id} ${esc(app.student_name)} · ${esc(app.position_title)} · ${esc(app.company_name)}</span>
      <span>
        <span class="badge ${app.status === "effective" ? "solid" : app.status === "ended" ? "dark" : "blue"}">${APP_STATUS[app.status]}</span>
        ${ag ? `<span class="badge">协议 v${ag.version} · 附件 A${ag.attachment_version}</span>` : ""}
      </span>
    </div>
    <div class="muted">周期 ${app.start_date} ~ ${app.end_date} · 指导老师 ${esc(app.teacher_name)} · 学院 ${esc(app.college_name)}</div>
    ${app.pending_change ? `<div class="muted" style="margin-top:6px">⏳ 变更单 #${app.pending_change.id}（${esc(app.pending_change.type_name)}）确认中</div>` : ""}
    ${actions.length ? `<div class="btn-row">${actions.join("")}</div>` : ""}
  </div>`;
}

/* ---------------- 三方签署（进度页：学生/学院/单位三方并列） ---------------- */
function renderSign() {
  const apps = S.applications.filter((x) => x.agreement);
  const main = apps.length ? apps.map(renderSignCard).join("") : '<div class="empty">暂无进入签署环节的申请</div>';
  $("#tab-sign").innerHTML = `
  <div class="grid cols-side">
    <div>${main}</div>
    <div class="card">
      <h2>通知（模拟适配器）</h2>
      <div class="notif-list">
        ${S.notifications.map((n) => `<div class="notif"><div><span class="to">${esc(n.recipient)}</span> <span class="muted">${n.created_at}</span></div><strong>${esc(n.subject)}</strong><div class="muted">${esc(n.body)}</div></div>`).join("") || '<div class="empty">暂无通知</div>'}
      </div>
    </div>
  </div>`;
}

function partyCell(app, party) {
  const ag = app.agreement;
  const sig = ag.signatures.find((s) => s.party === party);
  const isNext = app.next_party === party;
  const canSign = isNext && authorizedFor(party, app);
  return `<div class="party ${sig ? "signed" : ""}">
    <div class="who">${PARTY[party]}方 ${sig ? '<span class="badge solid">已签署</span>' : isNext ? '<span class="badge blue">待签署</span>' : '<span class="badge">未轮候</span>'}</div>
    <div class="detail">
      ${sig ? `${esc(sig.signer)}<br><span class="mono">${esc(sig.sig_hash)}</span><br>${sig.signed_at}` : isNext ? "等待该方签署" : "待前序方完成"}
    </div>
    ${canSign ? `<button class="btn" onclick="A.sign(${ag.id}, '${party}')">以${PARTY[party]}身份签署</button>` : ""}
  </div>`;
}

function renderSignCard(app) {
  const ag = app.agreement;
  return `<div class="card">
    <div class="card-head">
      <span class="title">#${app.id} ${esc(app.student_name)} · ${esc(ag.position_title)} · ${esc(ag.company_name)}</span>
      <span>
        <span class="badge ${ag.status === "effective" ? "solid" : ag.status === "signing" ? "blue" : "dark"}">v${ag.version} · ${AG_STATUS[ag.status]}</span>
        <span class="badge">附件 A${ag.attachment_version}</span>
      </span>
    </div>
    <div class="muted">实习周期 ${ag.start_date} ~ ${ag.end_date} · 单位联系人 ${esc(ag.contact_name)}</div>
    <div class="parties">
      ${partyCell(app, "student")}${partyCell(app, "college")}${partyCell(app, "company")}
    </div>
    <div class="step-hint">签署顺序固定为：学生 → 学院 → 单位；顺序错误将被拒绝并记录在版本链中。</div>
  </div>`;
}

/* ---------------- 变更对比 ---------------- */
function renderChange() {
  const effectiveApps = S.applications.filter((x) => x.status === "effective");
  $("#tab-change").innerHTML = `
  <div class="grid cols-side">
    <div>
      <div class="card">
        <h2>发起变更（仅已生效协议）</h2>
        ${effectiveApps.length ? `
        <form class="stack" id="changeForm">
          <label>申请
            <select name="app_id">${effectiveApps.map((x) => `<option value="${x.id}">#${x.id} ${esc(x.student_name)} · ${esc(x.position_title)}</option>`).join("")}</select>
          </label>
          <label>变更类型
            <select name="type" id="changeType">
              ${Object.entries(CHANGE_TYPES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}
            </select>
          </label>
          <div id="changeFields"></div>
          <button class="btn" type="submit">提交变更（进入受影响方确认）</button>
          <p class="muted">撤岗/联系人更换由单位联系人发起；转岗由学生本人发起；提前结束三方可发起；附件更正由本学院老师发起。</p>
        </form>` : '<div class="empty">当前没有已生效的协议</div>'}
      </div>
      <div class="card">
        <h2>变更单</h2>
        ${S.changes.map((c) => `
          <div class="change-item ${ui.selectedChange === c.id ? "selected" : ""}" onclick="A.selectChange(${c.id})">
            <div class="line1"><strong>#${c.id} ${esc(c.type_name)}</strong>
              <span class="badge ${c.status === "applied" ? "solid" : "blue"}">${c.status === "applied" ? "已生效" : "确认中"}</span></div>
            <div class="muted">${esc(c.student_name)} · ${esc(c.created_by)} · ${c.created_at}</div>
            <div class="muted">受影响方：${c.affected.map((p) => PARTY[p]).join("、")} · 已确认 ${c.confirmations.length}/${c.affected.length}</div>
          </div>`).join("") || '<div class="empty">暂无变更单</div>'}
      </div>
    </div>
    <div id="comparePanel"><div class="card"><div class="empty">选择左侧变更单查看前后对比</div></div></div>
  </div>`;

  const typeSel = $("#changeType");
  const renderFields = () => { $("#changeFields").innerHTML = changeFieldsHtml(typeSel.value); };
  if (typeSel) {
    typeSel.addEventListener("change", renderFields);
    renderFields();
    $("#changeForm").addEventListener("submit", submitChange);
  }
  if (ui.selectedChange) loadCompare(ui.selectedChange);
}

function changeFieldsHtml(type) {
  const openPositions = S.positions.filter((p) => p.status === "open");
  if (type === "withdraw")
    return `<label>撤岗生效日期 <input type="date" name="effective_date" value="2026-11-30" required></label>`;
  if (type === "transfer")
    return `<label>新岗位 <select name="new_position_id">${openPositions.map((p) => `<option value="${p.id}">${esc(p.title)} · ${esc(p.company_name)}</option>`).join("")}</select></label>
            <label>转岗生效日期 <input type="date" name="effective_date" value="2026-11-02" required></label>
            <label>新结束日期（留空则沿用原结束日期） <input type="date" name="new_end_date"></label>`;
  if (type === "early_end")
    return `<label>新结束日期 <input type="date" name="end_date" value="2026-11-30" required></label>`;
  if (type === "contact_change")
    return `<label>新联系人姓名 <input type="text" name="new_contact_name" placeholder="例如：孙强" required></label>`;
  if (type === "attachment_fix")
    return `<label>附件更正说明 <input type="text" name="note" placeholder="例如：补充保险条款附件"></label>`;
  return "";
}

async function submitChange(e) {
  e.preventDefault();
  const f = e.target;
  const type = f.type.value;
  const payload = {};
  for (const key of ["effective_date", "end_date", "new_end_date", "new_contact_name", "note"]) {
    if (f[key] && f[key].value) payload[key] = f[key].value;
  }
  if (f.new_position_id) payload.new_position_id = Number(f.new_position_id.value);
  try {
    const r = await api(`/api/applications/${f.app_id.value}/changes`, "POST", { actor: actor(), type, payload });
    toast(`变更单 #${r.id} 已提交，等待受影响方确认`);
    ui.selectedChange = r.id;
    await refresh();
  } catch (err) { toast(err.message, true); }
}

async function loadCompare(changeId) {
  ui.selectedChange = changeId;
  document.querySelectorAll(".change-item").forEach((el) => el.classList.remove("selected"));
  try {
    const cmp = await api(`/api/changes/${changeId}/compare`);
    const app = S.applications.find((x) => x.id === cmp.application_id);
    const [chain, days] = await Promise.all([
      api(`/api/applications/${cmp.application_id}/chain`).catch(() => null),
      app ? api(`/api/students/${app.student_id}/days`) : Promise.resolve(null),
    ]);
    renderCompare(cmp, days, chain);
  } catch (err) { toast(err.message, true); }
}

function renderCompare(cmp, days, chain) {
  const confirmed = new Set(cmp.confirmations.map((c) => c.party));
  const app = S.applications.find((x) => x.id === cmp.application_id);
  const confirmChips = cmp.affected.map((p) => {
    const done = confirmed.has(p);
    const who = cmp.confirmations.find((c) => c.party === p);
    const canConfirm = cmp.status === "pending" && !done && app && authorizedFor(p, app);
    return `<div class="confirm-chip ${done ? "done" : ""}">
      ${PARTY[p]}方：${done ? `已确认 · ${esc(who.confirmer)}` : "待确认"}
      ${canConfirm ? `<div class="btn-row"><button class="btn" onclick="A.confirmChange(${cmp.id}, '${p}')">确认变更</button></div>` : ""}
    </div>`;
  }).join("");

  const versionOf = {};
  if (chain) chain.versions.forEach((v) => (versionOf[v.id] = v.version));
  const daysHtml = days ? `
    <h3>实习天数（变更前后连续可查）</h3>
    <div class="days-strip">
      ${days.segments.map((s) => `<span class="days-seg">v${versionOf[s.agreement_id] || "?"} 段 <span class="mono">${s.start_date} ~ ${s.end_date}</span> · ${s.days} 天</span>`).join("→")}
      <span class="badge solid">合计 ${days.total_days} 天</span>
      ${days.continuous ? '<span class="badge blue">天数连续 ✓</span>' : '<span class="badge warn">存在间断！</span>'}
    </div>` : "";

  $("#comparePanel").innerHTML = `
  <div class="card">
    <div class="card-head">
      <span class="title">变更单 #${cmp.id} · ${esc(cmp.type_name)} · ${esc(cmp.student_name)}</span>
      <span class="badge ${cmp.status === "applied" ? "solid" : "blue"}">${cmp.status === "applied" ? "已生效" : "确认中"}</span>
    </div>
    <div class="muted">发起人：${esc(cmp.created_by)} · ${cmp.created_at} · 协议 v${cmp.base_version} → v${cmp.result_version ?? cmp.base_version + 1}</div>
    <table class="compare-table">
      <thead><tr><th>字段</th><th>变更前（v${cmp.base_version}）</th><th>变更后（v${cmp.result_version ?? cmp.base_version + 1}）</th></tr></thead>
      <tbody>
        ${cmp.rows.map((r) => `<tr class="${r.changed ? "changed" : ""}">
          <td>${esc(r.field)}</td>
          <td class="before">${esc(r.before)}</td>
          <td class="after">${esc(r.after)}${r.changed ? '<span class="diff-tag">已变更</span>' : ""}</td>
        </tr>`).join("")}
      </tbody>
    </table>
    <h3>受影响方确认</h3>
    <div class="confirm-strip">${confirmChips}</div>
    ${daysHtml}
  </div>`;
}

/* ---------------- 老师待办 ---------------- */
async function renderTodo() {
  const a = actor();
  if (a.role !== "teacher") {
    $("#tab-todo").innerHTML = '<div class="card"><div class="empty">请在右上角切换为「老师」身份查看待办。</div></div>';
    return;
  }
  try {
    const t = await api(`/api/teachers/${a.id}/todos`);
    $("#tab-todo").innerHTML = `
    <div class="card">
      <div class="card-head"><span class="title">${esc(t.teacher.name)} 老师 · ${esc(t.teacher.college_name)}</span>
      <span class="muted">仅显示本学院待办（跨学院申请不可见、不可审）</span></div>
      <h3>待审核申请（${t.reviews.length}）</h3>
      ${t.reviews.map((r) => `<div class="change-item"><div class="line1"><strong>#${r.id} ${esc(r.student_name)} · ${esc(r.position_title)}</strong>
        <button class="btn" onclick="A.review(${r.id})">审核通过</button></div>
        <div class="muted">周期 ${r.start_date} ~ ${r.end_date} · 提交于 ${r.created_at}</div></div>`).join("") || '<div class="empty">无待审核申请</div>'}
      <h3>待学院签署（${t.to_sign.length}）</h3>
      ${t.to_sign.map((r) => `<div class="change-item"><div class="line1"><strong>申请 #${r.application_id} ${esc(r.student_name)} · ${esc(r.position_title)}</strong>
        <button class="btn ghost" onclick="A.goto('sign')">前往签署</button></div>
        <div class="muted">协议 v${r.version} 等待学院方签署</div></div>`).join("") || '<div class="empty">无待签署协议</div>'}
      <h3>待确认变更（${t.changes.length}）</h3>
      ${t.changes.map((r) => `<div class="change-item"><div class="line1"><strong>变更单 #${r.id} ${esc(r.type_name)} · ${esc(r.student_name)}</strong>
        <button class="btn ghost" onclick="A.openChange(${r.id})">前往确认</button></div>
        <div class="muted">发起人：${esc(r.created_by)} · ${r.created_at}</div></div>`).join("") || '<div class="empty">无待确认变更</div>'}
    </div>`;
  } catch (err) { toast(err.message, true); }
}

/* ---------------- 协议归档 ---------------- */
function renderArchive() {
  const apps = S.applications.filter((x) => x.agreement);
  ui.openChains = ui.openChains || new Set();
  $("#tab-archive").innerHTML = apps.length ? apps.map((app) => {
    const chain = ui.openChains.has(app.id) ? ui.chains[app.id] : null;
    return `<div class="card">
      <div class="card-head">
        <span class="title">#${app.id} ${esc(app.student_name)} · ${esc(app.position_title)} · ${esc(app.company_name)}</span>
        <button class="btn subtle" onclick="A.toggleChain(${app.id})">${chain ? "收起版本链" : "展开版本链"}</button>
      </div>
      <div class="version-chips" id="versions-${app.id}"><span class="muted">加载中…</span></div>
      ${chain ? `<ul class="timeline">${chain.events.map((e) => `
        <li class="${KEY_EVENTS.has(e.kind) ? "key" : ""} ${BAD_EVENTS.has(e.kind) ? "bad" : ""}">
          <span class="t mono">${e.created_at}</span>
          <strong>${EVENT_KINDS[e.kind] || e.kind}</strong> ${esc(e.detail)}
        </li>`).join("")}</ul>` : ""}
    </div>`;
  }).join("") : '<div class="empty">暂无归档协议</div>';

  apps.forEach(async (app) => {
    try {
      const chain = ui.chains[app.id] || (await api(`/api/applications/${app.id}/chain`));
      ui.chains[app.id] = chain;
      const box = $(`#versions-${app.id}`);
      if (!box) return;
      box.innerHTML = chain.versions.map((v) => `
        <div class="version-chip">
          <span class="v">v${v.version} <span class="badge ${v.status === "effective" ? "solid" : v.status === "signing" ? "blue" : v.status === "terminated" ? "warn" : ""}">${AG_STATUS[v.status]}</span></span>
          <span class="muted">附件 A${v.attachment_version} · ${v.created_at}</span>
          <button class="btn ghost" onclick="A.viewFile(${v.id}, 'AG-${String(app.id).padStart(4, "0")} v${v.version}')">查看全文</button>
        </div>`).join("");
    } catch (err) { toast(err.message, true); }
  });
}

/* ---------------- 全局动作 ---------------- */
window.A = {
  goto(tab) { switchTab(tab); },
  async review(appId) {
    try { await api(`/api/applications/${appId}/college-review`, "POST", { actor: actor() }); toast("学院审核已通过"); await refresh(); }
    catch (err) { toast(err.message, true); }
  },
  async companyConfirm(appId) {
    try { await api(`/api/applications/${appId}/company-confirm`, "POST", { actor: actor() }); toast("单位已确认职责，协议 v1 已生成"); await refresh(); }
    catch (err) { toast(err.message, true); }
  },
  async sign(agreementId, party) {
    try {
      const r = await api(`/api/agreements/${agreementId}/sign`, "POST", { actor: actor(), party });
      toast(r.status === "effective" ? "三方签署完成，协议已生效" : `签署成功，下一方：${PARTY[r.next]}`);
      await refresh();
    } catch (err) { toast(err.message, true); }
  },
  async confirmChange(changeId, party) {
    try {
      const r = await api(`/api/changes/${changeId}/confirm`, "POST", { actor: actor(), party });
      toast(r.status === "applied" ? "全部方已确认，变更已生效" : "已确认，等待其余方");
      await refresh();
    } catch (err) { toast(err.message, true); }
  },
  selectChange(id) { ui.selectedChange = id; loadCompare(id); },
  openChange(id) { ui.selectedChange = id; switchTab("change"); },
  async toggleChain(appId) {
    ui.openChains = ui.openChains || new Set();
    if (ui.openChains.has(appId)) ui.openChains.delete(appId);
    else {
      ui.chains[appId] = await api(`/api/applications/${appId}/chain`);
      ui.openChains.add(appId);
    }
    renderArchive();
  },
  async viewFile(agreementId, label) {
    try {
      const text = await api(`/api/agreements-file?id=${agreementId}`);
      $("#modalTitle").textContent = `协议全文 · ${label}`;
      $("#modalBody").textContent = text;
      $("#modalMask").hidden = false;
    } catch (err) { toast(err.message, true); }
  },
};

/* ---------------- 启动 ---------------- */
function switchTab(tab) {
  ui.tab = tab;
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab}`));
  refresh().catch((e) => toast(e.message, true));
}

async function boot() {
  S = await api("/api/state");
  const sel = $("#actorSelect");
  const groups = [
    ["学生", S.students.map((s) => [`student:${s.id}`, `${s.name}（${s.college_name}）`])],
    ["老师", S.teachers.map((t) => [`teacher:${t.id}`, `${t.name}（${t.college_name}）`])],
    ["单位联系人", S.contacts.filter((c) => c.active === 1).map((c) => [`contact:${c.id}`, `${c.name}（${c.company_name}）`])],
  ];
  sel.innerHTML = groups.map(([label, opts]) =>
    `<optgroup label="${label}">${opts.map(([v, text]) => `<option value="${v}">${esc(text)}</option>`).join("")}</optgroup>`).join("");
  const saved = localStorage.getItem("actor");
  if (saved && [...sel.options].some((o) => o.value === saved)) sel.value = saved;
  sel.addEventListener("change", () => { localStorage.setItem("actor", sel.value); refresh().catch((e) => toast(e.message, true)); });

  $("#tabs").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-tab]");
    if (btn) switchTab(btn.dataset.tab);
  });
  $("#modalClose").addEventListener("click", () => ($("#modalMask").hidden = true));
  $("#modalMask").addEventListener("click", (e) => { if (e.target === e.currentTarget) e.currentTarget.hidden = true; });

  await refresh();
}

boot().catch((e) => toast(e.message, true));
