/* ═══════════ 旅图 TravelAgent 前端逻辑 ═══════════ */
const $ = s => document.querySelector(s);
const state = { origin: null, dests: [], plans: [], intel: null, map: null };

const ICONS = { pin:"◎", scan:"◈", rank:"✦", search:"⌕", judge:"⚖", rail:"⇄", plan:"▤", brain:"❖", pen:"✎" };

function toast(msg, ms = 2600){
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), ms);
}

/* ── 初始化 ── */
async function init(){
  bindForm();
  try{
    const st = await fetch("/api/status").then(r => r.json());
    const dot = $("#dotAmap"), lbl = $("#lblAmap");
    if (st.amap){ dot.classList.add("ok"); lbl.textContent = "高德API"; }
    else { dot.classList.add("warn"); lbl.textContent = "高德(未配Key)"; }
    const ld = $("#dotLlm"), ll = $("#lblLlm");
    if (st.llm){ ld.classList.add("ok"); ll.textContent = "AI决策 · " + (st.llm_model || ""); }
    else { ld.classList.add("warn"); ll.textContent = "规则模式"; }
  }catch(e){}
  try{
    const cities = await fetch("/api/cities").then(r => r.json());
    const sel = $("#selCity");
    sel.innerHTML = cities.map(c =>
      `<option value="${c.name}">${c.name} · ${c.province}</option>`).join("");
  }catch(e){}
  locateByGPS(true);
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
      const r = await fetch("/api/locate", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({lat: pos.coords.latitude, lng: pos.coords.longitude})
      }).then(r => r.json());
      $("#selCity").value = r.name;
      state.origin = r;
      note.textContent = `已定位：${r.name}（${r.note||"GPS"}）`;
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

  let days = 3;
  const dv = $("#daysVal");
  $("#dayMinus").onclick = () => { days = Math.max(1, days - 1); dv.textContent = days; };
  $("#dayPlus").onclick  = () => { days = Math.min(10, days + 1); dv.textContent = days; };
  state.getDays = () => days;

  $("#chipsTransport").addEventListener("click", e => {
    const c = e.target.closest(".chip"); if (!c) return;
    $("#chipsTransport").querySelectorAll(".chip").forEach(x => x.classList.remove("on"));
    c.classList.add("on");
  });
  $("#chipsPrefs").addEventListener("click", e => {
    const c = e.target.closest(".chip"); if (c) c.classList.toggle("on");
  });
  $("#btnGeo").onclick = () => locateByGPS(false);
  $("#btnGo").onclick = runRecommend;
  $("#drawerClose").onclick = $("#drawerMask").onclick = closeDrawer;
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeDrawer(); });
}

function collectParams(){
  return {
    city: $("#selCity").value || null,
    budget: +$("#budget").value,
    days: state.getDays(),
    transport: $("#chipsTransport .chip.on").dataset.v,
    prefs: [...$("#chipsPrefs .chip.on")].map(c => c.dataset.v),
  };
}

/* ── Agent 时间线渲染（逐步显现） ── */
function showTrace(trace, title){
  const sec = $("#stageTrace"), list = $("#traceList");
  $("#traceTitle").textContent = title || "Agent 正在思考";
  $("#traceSpinner").classList.remove("done");
  sec.classList.remove("hidden");
  list.innerHTML = "";
  sec.scrollIntoView({behavior: "smooth", block: "start"});
  trace.forEach((s, i) => {
    setTimeout(() => {
      const li = document.createElement("li");
      li.innerHTML = `<div class="t-title"><span style="color:var(--teal)">${ICONS[s.icon]||"•"}</span> ${s.title}</div>
                      ${s.detail ? `<div class="t-detail">${s.detail}</div>` : ""}`;
      list.appendChild(li);
      if (i === trace.length - 1) $("#traceSpinner").classList.add("done");
    }, 420 * i + 300);
  });
  return trace.length * 420 + 400;
}

/* ── 阶段一：目的地推荐 ── */
async function runRecommend(){
  const btn = $("#btnGo"); btn.disabled = true; btn.textContent = "规划中…";
  $("#stageDest").classList.add("hidden");
  $("#stagePlans").classList.add("hidden");
  const params = collectParams();
  try{
    const r = await fetch("/api/agent/destinations", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(params)
    }).then(r => r.json());
    if (r.error) throw new Error(r.error);
    state.origin = r.origin; state.dests = r.destinations || [];
    const wait = showTrace(r.trace, "Agent → 目的地推荐");
    setTimeout(() => renderDestinations(r), wait);
    if (!state.dests.length) setTimeout(() => toast(r.message || "没有可行目的地"), wait);
  }catch(e){
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
    `从 ${r.origin.name} 出发 · ${state.dests.length} 个候选 · 按综合得分排序`
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
      <div class="dest-tags">${d.tags.map(t => `<span class="tag">${t}</span>`).join("")}</div>
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
  sec.classList.remove("hidden");
  sec.scrollIntoView({behavior:"smooth"});
  renderMap(r.origin, state.dests);
}

function scoreRing(score){
  const pct = Math.min(100, score) / 100, r = 21, c = 2 * Math.PI * r;
  return `<svg width="52" height="52"><circle cx="26" cy="26" r="${r}" fill="none" stroke="var(--line)" stroke-width="5"/>
    <circle cx="26" cy="26" r="${r}" fill="none" stroke="var(--teal)" stroke-width="5"
    stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - pct)}" stroke-linecap="round"/></svg>`;
}

/* ── 地图（Leaflet，失败静默降级） ── */
function renderMap(origin, dests){
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
  $("#stagePlans").classList.add("hidden");
  const wait = showTrace([], `Agent → 正在研究「${dest}」`);
  $("#traceList").innerHTML = `<li><div class="t-title">正在调用搜索 / 判断 / 编排工具…</div></li>`;
  try{
    const r = await fetch("/api/agent/plans", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(params)
    }).then(r => r.json());
    if (r.error) throw new Error(r.error);
    state.plans = r.plans; state.intel = r.intel; state.lastPlanResp = r;
    showTrace(r.trace, `Agent → 「${dest}」方案已就绪`);
    setTimeout(() => renderPlans(r), r.trace.length * 420 + 400);
  }catch(e){ toast("方案生成失败：" + e.message); }
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
    card.onclick = () => openDrawer(p, r);
    list.appendChild(card);
  });
  sec.classList.remove("hidden");
  sec.scrollIntoView({behavior:"smooth"});
}

/* ── 方案详情抽屉 ── */
function openDrawer(p, r){
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

  $("#drawerBody").innerHTML = `
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
    </ul></div>`;
  $("#drawer").classList.add("open");
  $("#drawerMask").classList.remove("hidden");
}
function closeDrawer(){
  $("#drawer").classList.remove("open");
  $("#drawerMask").classList.add("hidden");
}

init();
