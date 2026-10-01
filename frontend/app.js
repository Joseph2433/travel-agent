/* ═══════════ 旅图 TravelAgent 前端逻辑 ═══════════ */
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

/* 后端地址：默认同源（本地开发/后端托管前端）。
   拆分部署时：config.js 里写 window.API_BASE，或带 ?api=https://xxx
   访问一次即写入 localStorage，之后免带参。 */
const API_BASE = (() => {
  const q = new URLSearchParams(location.search).get("api");
  if (q) localStorage.setItem("api_base", q.replace(/\/+$/, ""));
  return (localStorage.getItem("api_base") || window.API_BASE || "")
    .replace(/\/+$/, "");
})();

const state = { origin: null, dests: [], plans: [], intel: null, map: null,
                provinces: [], view: "home", planIdx: null, guide: null,
                lastPlanResp: null };

/* ── 页面视图路由：home(表单) / trace(思考) / dest(推荐) / plans(方案) / plan(详情)
   整页切换 + hash 支持浏览器前进后退 ── */
const VIEW_EL = { home: ".hero", trace: "#stageTrace", dest: "#stageDest",
                  plans: "#stagePlans", plan: "#stageDetail" };
const VIEW_HASH = { home: "", trace: "", dest: "#dest",
                    plans: "#plans", plan: "#plan" };

function showView(v, push = true){
  state.view = v;
  for (const [key, sel] of Object.entries(VIEW_EL))
    $(sel).classList.toggle("hidden", key !== v);
  window.scrollTo({top: 0});
  const h = VIEW_HASH[v];
  if (push && location.hash !== h) history.pushState(null, "", h || location.pathname);
}

function viewFromHash(){
  return Object.keys(VIEW_HASH).find(k => VIEW_HASH[k] === location.hash) || "home";
}
window.addEventListener("hashchange", () => {
  const v = viewFromHash();
  if (v === "dest" && !state.dests.length) return showView("home", false);
  if (v === "plans" && !state.plans.length) return showView("home", false);
  if (v === "plan" && state.planIdx == null)
    return showView(state.plans.length ? "plans" : "home", false);
  showView(v, false);
});

const ICONS = { pin:"◎", scan:"◈", rank:"✦", search:"⌕", judge:"⚖", rail:"⇄", plan:"▤", brain:"❖", pen:"✎", think:"✧", tool:"⚙", observe:"◉" };

function toast(msg, ms = 2600){
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), ms);
}

const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

/* ── 初始化 ── */
async function init(){
  bindForm();
  showView("home", false);
  if (location.hash) history.replaceState(null, "", location.pathname);
  try{
    const st = await fetch(API_BASE + "/api/status").then(r => r.json());
    const dot = $("#dotAmap"), lbl = $("#lblAmap");
    if (st.amap){ dot.classList.add("ok"); lbl.textContent = "高德API"; }
    else { dot.classList.add("warn"); lbl.textContent = "高德(未配Key)"; }
    const ld = $("#dotLlm"), ll = $("#lblLlm");
    if (st.llm){ ld.classList.add("ok"); ll.textContent = "AI决策 · " + (st.llm_model || ""); }
    else { ld.classList.add("warn"); ll.textContent = "规则模式"; }
  }catch(e){}
  try{
    const {provinces} = await fetch(API_BASE + "/api/geo/provinces").then(r => r.json());
    const ps = $("#selProv");
    ps.innerHTML = provinces.map(p => `<option>${p}</option>`).join("");
    ps.value = "北京";
    ps.onchange = () => loadCities(ps.value);
    await loadCities("北京");
  }catch(e){}
  locateByGPS(true);
}

/* 省份 → 城市联动：城市列表来自高德行政区接口（后端 /api/geo/cities） */
async function loadCities(prov){
  const sel = $("#selCity");
  sel.innerHTML = `<option>加载中…</option>`;
  try{
    const {cities} = await fetch(API_BASE + "/api/geo/cities?province=" + encodeURIComponent(prov))
      .then(r => r.json());
    sel.innerHTML = cities.map(c =>
      `<option value="${c.name}" data-lat="${c.lat ?? ""}" data-lng="${c.lng ?? ""}">${c.name}</option>`
    ).join("") || `<option>${prov}</option>`;
  }catch(e){ sel.innerHTML = `<option>${prov}</option>`; }
  queueScope();
}

/* ── 出行范围：按出行方式/天数/预算算出各省份可达城市数，用户可圈选 ── */
let _scopeTimer = null;
function queueScope(){
  clearTimeout(_scopeTimer);
  _scopeTimer = setTimeout(loadScope, 350);
}
async function loadScope(){
  const opt = $("#selCity").selectedOptions[0];
  if (!opt || !opt.value) return;
  const body = collectParams();
  try{
    const r = await fetch(API_BASE + "/api/agent/scope", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(body)
    }).then(r => r.json());
    const box = $("#chipsScope");
    state.provinces = state.provinces.filter(p =>
      (r.provinces || []).some(x => x.name === p));
    box.innerHTML = (r.provinces || []).slice(0, 28).map(p =>
      `<button type="button" class="chip scope ${state.provinces.includes(p.name) ? "on" : ""}"
        data-v="${p.name}">${p.name} <i>${p.n}城·最快${p.min_hours}h</i></button>`
    ).join("");
    $("#scopeNote").textContent =
      `共 ${r.total} 城可达` + (state.provinces.length
        ? ` · 已圈定 ${state.provinces.length} 省` : "（不选 = 全部可达）");
    $("#scopeRow").style.display = "";
  }catch(e){ $("#scopeRow").style.display = "none"; }
}

function locateByGPS(silent){
  const note = $("#locNote");
  if (!navigator.geolocation){
    if (!silent) toast("浏览器不支持定位，请手动选择出发城市");
    return;
  }
  note.textContent = "定位中…";
  navigator.geolocation.getCurrentPosition(async pos => {
    try{
      const r = await fetch(API_BASE + "/api/locate", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({lat: pos.coords.latitude, lng: pos.coords.longitude})
      }).then(r => r.json());
      const ps = $("#selProv"), sel = $("#selCity");
      const hit = [...ps.options].find(o => r.province && r.province.startsWith(o.value.slice(0,2)));
      if (hit){ ps.value = hit.value; await loadCities(hit.value); }
      if (![...sel.options].some(o => o.value === r.name)){
        sel.insertAdjacentHTML("afterbegin",
          `<option value="${r.name}" data-lat="${r.lat||""}" data-lng="${r.lng||""}">${r.name}</option>`);
      }
      sel.value = r.name;
      state.origin = r;
      note.textContent = `已定位：${r.name}（${r.note||"GPS"}）`;
      queueScope();
    }catch(e){ note.textContent = "定位失败，请手动选择"; }
  }, () => {
    note.textContent = "定位被拒，请手动选择出发城市";
    if (!silent) toast("已拒绝定位权限，请手动选择城市");
  }, {timeout: 6000});
}

/* ── 表单交互 ── */
function bindForm(){
  const budget = $("#budget"), out = $("#budgetOut");
  const sync = () => {
    out.textContent = "¥" + Number(budget.value).toLocaleString();
    budget.style.setProperty("--fill",
      ((budget.value - budget.min) / (budget.max - budget.min) * 100) + "%");
  };
  budget.addEventListener("input", sync); sync();
  budget.addEventListener("change", queueScope);

  let days = 3;
  const dv = $("#daysVal");
  $("#dayMinus").onclick = () => { days = Math.max(1, days - 1); dv.textContent = days; queueScope(); };
  $("#dayPlus").onclick  = () => { days = Math.min(10, days + 1); dv.textContent = days; queueScope(); };
  state.getDays = () => days;

  $("#chipsTransport").addEventListener("click", e => {
    const c = e.target.closest(".chip"); if (!c) return;
    $("#chipsTransport").querySelectorAll(".chip").forEach(x => x.classList.remove("on"));
    c.classList.add("on");
    queueScope();
  });
  $("#chipsScope").addEventListener("click", e => {
    const c = e.target.closest(".chip"); if (!c) return;
    c.classList.toggle("on");
    state.provinces = $$("#chipsScope .chip.on").map(x => x.dataset.v);
    $("#scopeNote").textContent = $("#scopeNote").textContent.replace(
      /（.*）| · 已圈定 .*省/, "") +
      (state.provinces.length ? ` · 已圈定 ${state.provinces.length} 省` : "（不选 = 全部可达）");
  });
  $("#selCity").addEventListener("change", queueScope);
  $("#chipsPrefs").addEventListener("click", e => {
    const c = e.target.closest(".chip"); if (c) c.classList.toggle("on");
  });
  $("#btnGeo").onclick = () => locateByGPS(false);
  $("#btnGo").onclick = runRecommend;
  /* 页面返回条：data-back 指目标视图 */
  document.addEventListener("click", e => {
    const b = e.target.closest(".back-btn"); if (!b) return;
    const v = b.dataset.back;
    showView(v === "dest" && !state.dests.length ? "home" : v);
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape") history.back(); });
}

function collectParams(){
  const opt = $("#selCity").selectedOptions[0];
  return {
    city: opt ? opt.value : null,
    lat: opt && opt.dataset.lat ? +opt.dataset.lat : null,
    lng: opt && opt.dataset.lng ? +opt.dataset.lng : null,
    budget: +$("#budget").value,
    days: state.getDays(),
    transport: $("#chipsTransport .chip.on").dataset.v,
    prefs: $$("#chipsPrefs .chip.on").map(c => c.dataset.v),
    provinces: state.provinces,
  };
}

/* ── Agent 时间线：SSE 流式实时渲染 ── */
function beginTrace(title){
  showView("trace");
  const sec = $("#stageTrace"), list = $("#traceList");
  $("#traceTitle").textContent = title || "Agent 正在思考";
  $("#traceSpinner").classList.remove("done");
  list.innerHTML = "";
}
function addTraceStep(s){
  const li = document.createElement("li");
  li.innerHTML = `<div class="t-title"><span style="color:var(--teal)">${ICONS[s.icon]||"•"}</span> ${s.title}</div>
                  ${s.detail ? `<div class="t-detail">${s.detail}</div>` : ""}`;
  $("#traceList").appendChild(li);
  li.scrollIntoView({behavior:"smooth", block:"nearest"});
}
function endTrace(){ $("#traceSpinner").classList.add("done"); }

/* POST + SSE 流读取：每个 step 事件立刻上屏，done 返回完整结果 */
async function streamPost(url, body, onStep){
  const resp = await fetch(API_BASE + url, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  });
  if (!resp.ok || !resp.body) throw new Error("HTTP " + resp.status);
  const reader = resp.body.getReader(), dec = new TextDecoder();
  let buf = "", final = null;
  while (true){
    const {done, value} = await reader.read();
    if (done) break;
    buf += dec.decode(value, {stream: true});
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0){
      const raw = buf.slice(0, i); buf = buf.slice(i + 2);
      const line = raw.split("\n").find(l => l.startsWith("data:"));
      if (!line) continue;
      const msg = JSON.parse(line.slice(5).trim());
      if (msg.type === "step") onStep(msg);
      else if (msg.type === "done") final = msg.data;
      else if (msg.type === "error") throw new Error(msg.message || "agent error");
    }
  }
  if (!final) throw new Error("流已结束但未收到结果");
  return final;
}

/* ── 阶段一：目的地推荐 ── */
async function runRecommend(){
  const btn = $("#btnGo"); btn.disabled = true; btn.textContent = "规划中…";
  const params = collectParams();
  const direct = ($("#inpDest").value || "").trim();
  if (direct){                       // 指定目的地 → 跳过推荐，直接出方案
    btn.disabled = false;
    btn.innerHTML = '开始规划 <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M5 12h14M13 6l6 6-6 6"/></svg>';
    runPlans(direct.replace(/市$/, ""));
    return;
  }
  beginTrace("Agent → 目的地推荐");
  try{
    const r = await streamPost("/api/agent/destinations/stream", params, addTraceStep);
    endTrace();
    state.origin = r.origin; state.dests = r.destinations || [];
    renderDestinations(r);
    if (!state.dests.length) toast(r.message || "没有可行目的地");
  }catch(e){
    endTrace();
    showView("home");
    toast("服务异常：" + e.message);
  }finally{
    btn.disabled = false;
    btn.innerHTML = '开始规划 <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M5 12h14M13 6l6 6-6 6"/></svg>';
  }
}

const MEDALS = ["NO.1","NO.2","NO.3","NO.4","NO.5","NO.6"];

function renderDestinations(r){
  const sec = $("#stageDest"), grid = $("#destGrid");
  $("#destMeta").textContent =
    `从 ${r.origin.name} 出发 · 精选 ${state.dests.length} 个`
    + (r.total_feasible ? ` · 共 ${r.total_feasible} 个可达` : "")
    + ` · 按综合得分排序`
    + (state.provinces.length ? ` ｜ 范围：${state.provinces.join("、")}` : "")
    + (r.verdict && r.verdict.why ? ` ｜ AI复核：${r.verdict.why}` : "");
  grid.innerHTML = "";
  state.dests.forEach((d, i) => {
    const card = document.createElement("div");
    card.className = "dest-card"; card.style.animationDelay = (i * 90) + "ms";
    const tr = d.transport_est;
    card.innerHTML = `
      <div class="dest-rank">${MEDALS[i]}</div>
      ${d.llm_pick ? `<div class="dest-pick">❖ AI 首推</div>` : ""}
      <div class="dest-city">${d.city}<i>${d.province} · ${Math.round(d.km)}km</i></div>
      <div class="dest-tags">${d.in_kb === false ? `<span class="tag niche">小众</span>` : ""}${d.tags.map(t => `<span class="tag">${t}</span>`).join("")}</div>
      ${d.ai_comment ? `<div class="dest-ai">${d.ai_comment}</div>` : ""}
      <div class="dest-score">
        <div class="ring">${scoreRing(d.score)}<span class="num">${d.score}</span></div>
        <ul class="dest-reasons">${d.reasons.slice(0,3).map(x => `<li>${x}</li>`).join("")}</ul>
      </div>
      <div class="dest-foot">
        <div><b>¥${d.rough_total}</b> 估 / ${tr.desc} · ${tr.hours}h 单程
          ${d.rough_total > collectParams().budget ? `<span class="budget-bad">略超预算，可降档</span>` : ""}</div>
        <button class="pick-btn">选它 →</button>
      </div>`;
    card.querySelector(".pick-btn").onclick = () => runPlans(d.city);
    grid.appendChild(card);
  });

  /* 查看更多：其余可达候选（规则评分序，紧凑卡片） */
  const wrap = $("#moreWrap"); wrap.innerHTML = "";
  const more = r.more || [];
  if (more.length){
    const total = r.total_feasible || (state.dests.length + more.length);
    const btn = document.createElement("button");
    btn.className = "btn ghost more-btn";
    btn.innerHTML = `还有 ${more.length} 个可达目的地（共 ${total} 个满足条件） <b>展开 ↓</b>`;
    const mg = document.createElement("div");
    mg.className = "more-grid hidden";
    more.forEach(d => {
      const tr = d.transport_est || {};
      const mc = document.createElement("div");
      mc.className = "mini-card";
      mc.innerHTML = `
        <div class="mini-top"><b>${d.city}</b><i>${d.province} · ${Math.round(d.km)}km</i>
          <span class="mini-score">${d.score}分</span></div>
        ${(d.in_kb === false) ? `<div class="dest-tags"><span class="tag niche">小众</span>${(d.tags||[]).slice(0,2).map(t=>`<span class="tag">${t}</span>`).join("")}</div>` : ""}
        <div class="mini-reason">${(d.reasons || []).slice(0,2).join("；")}</div>
        <div class="mini-foot"><span>¥${d.rough_total} 估 · ${tr.desc || ""} ${tr.hours ?? ""}h</span>
          <button class="pick-btn mini-pick">选它 →</button></div>`;
      mc.querySelector(".mini-pick").onclick = () => runPlans(d.city);
      mg.appendChild(mc);
    });
    btn.onclick = () => {
      const hidden = mg.classList.toggle("hidden");
      btn.querySelector("b").textContent = hidden ? "展开 ↓" : "收起 ↑";
      if (!hidden) mg.scrollIntoView({behavior:"smooth", block:"nearest"});
    };
    wrap.appendChild(btn); wrap.appendChild(mg);
  }

  $("#traceReplayDest").innerHTML = $("#traceList").innerHTML;
  $("#traceReplayDest").closest("details").querySelector("summary")
    .textContent = `Agent 思考回放 · ${$("#traceList").children.length} 步`;
  showView("dest");
  renderMap(r.origin, state.dests, more);
}

function scoreRing(score){
  const pct = Math.min(100, score) / 100, r = 21, c = 2 * Math.PI * r;
  return `<svg width="52" height="52"><circle cx="26" cy="26" r="${r}" fill="none" stroke="var(--line)" stroke-width="5"/>
    <circle cx="26" cy="26" r="${r}" fill="none" stroke="var(--teal)" stroke-width="5"
    stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - pct)}" stroke-linecap="round"/></svg>`;
}

/* ── 地图（Leaflet，失败静默降级） ── */
function renderMap(origin, dests, more){
  const box = $("#mapBox");
  try{
    if (typeof L === "undefined") { box.style.display = "none"; return; }
    box.style.display = "";
    if (state.map){ state.map.remove(); state.map = null; }
    const map = L.map(box, {scrollWheelZoom:false}).setView([origin.lat, origin.lng], 4);
    state.map = map;
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      {maxZoom: 12, attribution: "© OpenStreetMap"}).addTo(map);
    L.circleMarker([origin.lat, origin.lng],
      {radius:9, color:"#e0654f", fillColor:"#e0654f", fillOpacity:.9})
      .addTo(map).bindTooltip(`出发 · ${origin.name}`, {permanent:true, direction:"top"});
    (more || []).forEach(d => {           // 其余可达候选：灰色小点
      if (d.lat == null || d.lng == null) return;
      L.circleMarker([d.lat, d.lng],
        {radius:4, color:"#9aa4ad", fillColor:"#9aa4ad", fillOpacity:.55, weight:1.5})
        .addTo(map).bindTooltip(`${d.city} · ${d.score}分`).on("click", () => runPlans(d.city));
    });
    dests.forEach(d => {
      L.circleMarker([d.lat, d.lng],
        {radius:7, color:"#0fa3a8", fillColor:"#0fa3a8", fillOpacity:.8, weight:2})
        .addTo(map).bindTooltip(`${d.city} · ${d.score}分`).on("click", () => runPlans(d.city));
      L.polyline([[origin.lat, origin.lng],[d.lat, d.lng]],
        {color:"#0fa3a8", weight:1.5, opacity:.35, dashArray:"5 6"}).addTo(map);
    });
  }catch(e){ box.style.display = "none"; }
}

/* ── 阶段二：方案生成 ── */
async function runPlans(dest){
  const params = {...collectParams(), dest};
  beginTrace(`Agent → 正在研究「${dest}」`);
  try{
    const r = await streamPost("/api/agent/plans/stream", params, addTraceStep);
    endTrace();
    state.plans = r.plans; state.intel = r.intel; state.lastPlanResp = r;
    state.guide = r.guide;
    $("#traceTitle").textContent = `Agent → 「${dest}」方案已就绪`;
    renderPlans(r);
  }catch(e){ endTrace(); showView(state.dests.length ? "dest" : "home");
             toast("方案生成失败：" + e.message); }
}

function renderPlans(r){
  const sec = $("#stagePlans"), list = $("#planList");
  $("#plansTitle").innerHTML = `${r.dest} 出游方案
    <small>${r.origin.name} 出发 · ${Math.round(r.km)}km · ${r.plans.length} 套可选</small>`;

  const ib = $("#intelBar");
  const srcBadge = r.intel.sources.map(s => `<span class="intel-pill">数据源 · ${s}</span>`).join("");
  ib.innerHTML = srcBadge +
    `<span class="intel-pill ${r.judge_engine && r.judge_engine.startsWith("llm") ? "" : "warn"}">取舍引擎 · ${
      {llm:"LLM 决策", rule:"规则引擎", "rule-fallback":"规则兜底"}[r.judge_engine] || "规则引擎"}</span>` +
    (r.intel.weather ? `<span class="intel-pill">当地天气 · ${r.intel.weather.weather} ${r.intel.weather.temp}°C</span>` : "") +
    r.intel.notes.slice(0,3).map(n => `<span class="intel-pill">${n}</span>`).join("") +
    (r.intel.dropped.length ? `<span class="intel-pill warn">已剔除 ${r.intel.dropped.length} 项不合时令/排不下的项目</span>` : "");

  list.innerHTML = "";
  r.plans.forEach((p, i) => {
    const over = !p.fits_budget;
    const pct = Math.min(100, p.budget.total / collectParams().budget * 100);
    const card = document.createElement("div");
    card.className = "plan-card"; card.style.animationDelay = (i * 90) + "ms";
    card.innerHTML = `
      <div class="plan-badge pb-${i % 5}">${p.name[0]}</div>
      <div class="plan-mid">
        <div class="plan-name">${p.name}<span class="pace">${p.pace}</span></div>
        <div class="plan-desc">${p.desc}</div>
        ${p.ai_note ? `<div class="ai-note">❖ ${p.ai_note}</div>` : ""}
        <div class="plan-days">${p.days.map(d => `<span class="mini-day">D${d.day} ${d.title}</span>`).join("")}</div>
      </div>
      <div class="plan-right">
        <div class="plan-cost">¥${p.budget.total}<small> /人</small></div>
        <div class="cost-bar ${over ? "over" : ""}"><i style="width:${pct}%"></i></div>
        ${over ? `<div class="plan-over">超预算 ¥${p.budget.total - collectParams().budget}</div>` : ""}
        <div class="plan-cta">查看行程 ↗</div>
      </div>`;
    card.onclick = () => openPlan(i);
    list.appendChild(card);
  });

  /* 详细攻略：多源交叉验证组装，放在方案列表最下方 */
  $("#guideBox").innerHTML = guideHtml(r.guide);

  $("#traceReplayPlans").innerHTML = $("#traceList").innerHTML;
  $("#traceReplayPlans").closest("details").querySelector("summary")
    .textContent = `Agent 思考回放 · ${$("#traceList").children.length} 步`;
  showView("plans");
}

function guideHtml(g){
  if (!g || !g.sections || !g.sections.length) return "";
  return `<div class="guide">
    <h3 class="guide-title">${esc(g.dest)} · 数据来源与参考
      <small>方案由以下来源交叉验证组装：小红书笔记 · 去哪儿票价 · 高德POI · 12306</small></h3>
    ${g.sections.map(sec => `
      <div class="guide-sec">
        <h4>${sec.icon || ""} ${esc(sec.title)}</h4>
        <div class="guide-items">${sec.items.map(gItem).join("")}</div>
      </div>`).join("")}
  </div>`;
}

/* markdown-lite：###小节 / -列表 / **重点**（先整体转义再替换标记） */
function mdLite(s){
  if (!s) return "";
  const bold = t => t.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  let html = "", inUl = false;
  const closeUl = () => { if (inUl){ html += "</ul>"; inUl = false; } };
  for (const ln of esc(s).split(/\n+/).map(x => x.trim()).filter(Boolean)){
    if (ln.startsWith("###")){ closeUl(); html += `<h4>${bold(ln.replace(/^#+\s*/, ""))}</h4>`; }
    else if (ln.startsWith("- ") || ln.startsWith("* ")){
      if (!inUl){ html += "<ul>"; inUl = true; }
      html += `<li>${bold(ln.slice(2))}</li>`;
    } else { closeUl(); html += `<p>${bold(ln)}</p>`; }
  }
  closeUl();
  return html;
}

function gItem(it){
  const t = it.url
    ? `<a href="${esc(it.url)}" target="_blank" rel="noopener">${esc(it.title)}</a>`
    : `<b>${esc(it.title)}</b>`;
  return `<div class="g-item"><div class="g-head">${t}${it.meta ? `<i>${esc(it.meta)}</i>` : ""}</div>
    ${it.excerpt ? `<div class="g-ex">${esc(it.excerpt)}</div>` : ""}</div>`;
}

/* ── 方案详情（整页视图） ── */
function openPlan(i){
  state.planIdx = i;
  renderPlanDetail(state.plans[i], state.lastPlanResp);
  showView("plan");
}

function renderPlanDetail(p, r){
  const t = p.transport;
  const trainRows = (t.trains || []).map(tr => `
    <tr><td><b>${tr.code}</b></td><td>${tr.from}→${tr.to}</td>
    <td>${tr.dep}–${tr.arr}</td><td>${tr.hours}h</td>
    <td class="${tr.seats["二等座"] && tr.seats["二等座"] !== "无" ? "seat-ok" : "seat-no"}">二等${tr.seats["二等座"] ?? "—"}</td>
    <td class="${tr.seats["一等座"] && tr.seats["一等座"] !== "无" ? "seat-ok" : "seat-no"}">一等${tr.seats["一等座"] ?? "—"}</td></tr>`).join("");

  const itin = p.days.map(d => `
    <div class="day-block">
      <div class="day-head"><div class="day-num">${d.day}</div><div class="day-title">${d.title}</div></div>
      <div class="day-items">${d.items.map(it => `
        <div class="it ${it.type}">
          <span class="slot">${it.slot}</span>
          <span class="body"><span class="name">${it.name}</span><div class="note">${it.note||""}</div></span>
          <span class="cost">${it.cost ? "¥" + it.cost : ""}</span>
        </div>`).join("")}
      </div>
    </div>`).join("");

  const b = p.budget, rows = [
    ["往返交通", b.transport], ["住宿", b.hotel], ["餐饮", b.food],
    ["门票", b.tickets], ["市内交通", b.local],
  ];
  const maxB = Math.max(...rows.map(x => x[1]), 1);

  $("#detailBody").innerHTML = `
    <div class="d-title">${r.dest} · ${p.name}</div>
    <div class="d-sub">${p.desc} · ${p.pace}节奏 · ${r.origin.name}出发往返</div>

    <div class="train-box">
      <h4>往返交通
        <span class="src-badge ${t.src === "12306" ? "" : "est"}">${t.src === "12306" ? "12306 实时余票 · " + (t.query_date || "") : "估算价 · 仅供参考"}</span></h4>
      <div class="train-route"><b>${r.origin.name}</b><span class="arrow">⇄</span><b>${r.dest}</b>
        <span style="color:var(--ink3);font-size:12px">单程约 ${t.hours}h · ¥${t.cost}</span></div>
      <div style="font-size:12.5px;color:var(--ink2)">去 ${t.outbound}<br>回 ${t.back}</div>
      ${trainRows ? `<table class="train-table"><tr><th>车次</th><th>区间</th><th>时刻</th><th>历时</th><th>二等座</th><th>一等座</th></tr>${trainRows}</table>` : ""}
    </div>

    <div class="itin">${itin}</div>

    ${p.guide ? `<div class="pguide"><h4>📖 本套详细攻略</h4>
      <div class="pguide-body">${mdLite(p.guide)}</div></div>` : ""}

    <div class="budget-box">
      <h4>预算拆解（单人）</h4>
      ${rows.map(([l, v]) => `<div class="brow"><span class="lbl">${l}</span>
        <span class="bar"><i style="width:${Math.round(v / maxB * 100)}%"></i></span>
        <span class="val">¥${v}</span></div>`).join("")}
      <div class="btotal"><span style="font-size:12px;color:var(--ink3)">合计 · 日均 ¥${b.per_day}</span>
        <span class="num ${!p.fits_budget ? "over" : ""}">¥${b.total}</span></div>
      ${p.over_hint ? `<div style="font-size:12px;color:var(--coral);margin-top:6px">${p.over_hint}</div>` : ""}
    </div>

    <div class="tips-box"><h4>行遍锦囊</h4><ul>
      ${(state.intel.tips || []).map(x => `<li>${x}</li>`).join("")}
      ${(state.intel.notes || []).map(x => `<li>${x}</li>`).join("")}
      ${(state.intel.web || []).slice(0,2).map(w => `<li>攻略参考：<a href="${w.url}" target="_blank" style="color:var(--teal-d)">${w.title}</a></li>`).join("")}
    </ul></div>

    ${guideHtml(state.guide)}`;
}

init();
