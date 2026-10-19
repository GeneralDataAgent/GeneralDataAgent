"use strict";
(() => {
  const el = id => document.getElementById(id);
  let token = "", rows = [], cursor = null, selected = null, busy = false, generation = 0;
  const text = (tag, value, cls) => {
    const node = document.createElement(tag);
    if (value !== undefined) node.textContent = value;
    if (cls) node.className = cls;
    return node;
  };
  const notice = value => { el("notice").textContent = value; };
  const status = r => r.consumed ? "已使用" : r.disposition ? (r.disposition.disposition === "cancelled" ? "已取消" : "已拒绝") : r.grant ? "已批准" : "待审核";
  async function api(path, body) {
    const current = generation;
    const response = await fetch(`/v1/${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers: {authorization: `Bearer ${token}`, "content-type": "application/json"},
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store", credentials: "omit", redirect: "error"
    });
    if (generation !== current) throw new Error("连接已切换，请重新加载。");
    if (!response.ok) throw new Error(response.status === 403 ? "凭证、审核资格或工作范围不允许此操作。" : response.status === 409 ? "申请或审核状态已变化，请刷新并重新核对。" : "审核服务暂时不可用，请核对后重试。");
    return response.json();
  }
  function disconnect() {
    generation++; token = ""; rows = []; selected = null; cursor = null;
    el("credential").value = ""; el("connection").hidden = false;
    el("workspace").hidden = true; el("logout").hidden = true;
    el("drafts").replaceChildren(); el("detail").replaceChildren(); notice("");
  }
  function list() {
    el("drafts").replaceChildren(); el("more").hidden = cursor === null;
    if (!rows.length) el("drafts").append(text("p", "当前没有已授权给你的审核请求。", "hint"));
    for (const record of rows) {
      const r = record.request;
      const button = text("button", undefined, "draft");
      button.setAttribute("aria-pressed", String(selected === r.approval_id));
      button.append(text("span", r.resource, "draft-title"), text("span", `${status(record)} · ${r.user_id}`, "draft-meta"));
      button.addEventListener("click", () => { selected = r.approval_id; list(); detail(record); });
      el("drafts").append(button);
    }
  }
  function detail(record) {
    const r = record.request, pane = el("detail");
    pane.replaceChildren(text("span", status(record), "status"), text("h2", r.resource, "detail-title"));
    const dl = text("dl");
    for (const [key, value] of [["申请人", r.user_id], ["工作范围", `${r.tenant_id} / ${r.project_id} / ${r.data_domains.join("、")}`], ["有效期", new Date(r.expires_at).toLocaleString("zh-CN")], ["动作", r.action]]) {
      dl.append(text("dt", key), text("dd", value));
    }
    pane.append(dl);
    for (const [title, value] of [["影响范围", r.impact_scope], ["预计成本", r.cost_estimate], ["补偿方案", r.rollback_plan], ["完整批准内容", JSON.stringify(r.parameters, null, 2)]]) {
      const section = text("section", undefined, "section");
      section.append(text("h3", title), text("pre", value, "exact-content")); pane.append(section);
    }
    if (record.grant || record.disposition || record.consumed || new Date(r.expires_at) <= new Date()) return;
    const form = text("form", undefined, "section"), check = text("input"), label = text("label"), reason = text("input");
    check.type = "checkbox"; check.required = true; check.id = "confirm-review";
    label.append(check, document.createTextNode(" 我已核对以上完整内容及影响范围"));
    reason.type = "text"; reason.placeholder = "审核说明（拒绝时必填）"; reason.maxLength = 2000;
    reason.setAttribute("aria-label", "审核说明");
    const actions = text("div", undefined, "actions"), approve = text("button", "批准这份请求", "primary"), reject = text("button", "拒绝请求");
    approve.type = "submit"; reject.type = "button"; approve.disabled = busy; reject.disabled = busy;
    actions.append(approve, reject); form.append(label, text("p"), reason, actions); pane.append(form);
    async function decide(decision) {
      if (busy) return;
      if (!check.checked) { notice("请先核对并确认完整内容。"); return; }
      if (decision === "reject" && !reason.value.trim()) { notice("请填写拒绝原因。"); return; }
      busy = true; approve.disabled = true; reject.disabled = true; notice("正在记录审核决定…");
      try {
        await api(`approval-reviews/${r.approval_id}/${decision}`, {request_fingerprint: r.request_fingerprint, confirmed: true, reason: reason.value.trim() || "Reviewed exact request"});
        await load(); notice(decision === "approve" ? "已批准这份请求；尚未执行业务动作。" : "已拒绝请求。");
      } catch (error) { notice(error.message); }
      finally { busy = false; const current = rows.find(v => v.request.approval_id === selected); if (current && token) detail(current); }
    }
    form.addEventListener("submit", e => { e.preventDefault(); decide("approve"); });
    reject.addEventListener("click", () => decide("reject"));
  }
  async function load(more = false) {
    const page = await api(`approval-requests?limit=25${more && cursor ? `&before=${cursor}` : ""}`);
    rows = more ? [...rows, ...page.items] : page.items; cursor = page.next_cursor;
    list(); const current = rows.find(v => v.request.approval_id === selected); if (current) detail(current);
  }
  el("connect-form").addEventListener("submit", async e => {
    e.preventDefault(); if (busy) return; busy = true; generation++; token = el("credential").value;
    try { await load(); el("credential").value = ""; el("connection").hidden = true; el("workspace").hidden = false; el("logout").hidden = false; notice(""); }
    catch (error) { disconnect(); notice(error.message); } finally { busy = false; }
  });
  el("logout").addEventListener("click", disconnect);
  el("refresh").addEventListener("click", () => load().catch(e => notice(e.message)));
  el("more").addEventListener("click", () => load(true).catch(e => notice(e.message)));
})();
