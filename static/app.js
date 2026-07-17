/* TokenLens 前端逻辑 */
(function () {
  "use strict";

  /* ---------- 状态 ---------- */
  const state = {
    records: [],
    prices: {},
    agents: [],
    view: "overview",
    range: 30,        // 0 = 全部
    agent: "all",
    model: "all",
    metric: "tokens", // tokens | cost
  };

  const PALETTE = ["#7c5cff", "#38bdf8", "#ff8a5c", "#34d399", "#f472b6", "#fbbf24", "#a3e635", "#c084fc", "#22d3ee", "#fb7185"];
  const colorCache = {};
  function colorOf(name) {
    if (!colorCache[name]) {
      let h = 0;
      for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
      colorCache[name] = PALETTE[h % PALETTE.length];
    }
    return colorCache[name];
  }

  /* ---------- 工具 ---------- */
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  function fmtNum(n) {
    if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
    if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
    return String(Math.round(n));
  }
  function fmtFull(n) { return Math.round(n).toLocaleString("en-US"); }
  function fmtCost(c) {
    if (c >= 100) return "$" + c.toFixed(1);
    if (c >= 1) return "$" + c.toFixed(2);
    return "$" + c.toFixed(3);
  }
  function totalTokens(r) { return r.input + r.output + r.cached; }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(t._timer);
    t._timer = setTimeout(() => t.classList.remove("show"), 2200);
  }

  /* ---------- 数据 ---------- */
  async function loadData() {
    const res = await fetch("/api/records");
    const data = await res.json();
    state.records = data.records;
    state.prices = data.prices;
    state.agents = data.agents;
    buildFilterOptions();
    render();
  }

  function filtered() {
    let list = state.records;
    if (state.range > 0) {
      const from = new Date();
      from.setDate(from.getDate() - state.range);
      const fromStr = from.toISOString().slice(0, 10);
      list = list.filter((r) => r.date >= fromStr);
    }
    if (state.agent !== "all") list = list.filter((r) => r.agent === state.agent);
    if (state.model !== "all") list = list.filter((r) => r.model === state.model);
    return list;
  }

  function aggBy(list, key) {
    const map = {};
    for (const r of list) {
      const k = r[key];
      if (!map[k]) map[k] = { name: k, input: 0, output: 0, cached: 0, cost: 0, requests: 0, days: new Set() };
      const m = map[k];
      m.input += r.input; m.output += r.output; m.cached += r.cached;
      m.cost += r.cost; m.requests += r.requests; m.days.add(r.date);
    }
    return Object.values(map)
      .map((m) => ({ ...m, tokens: m.input + m.output + m.cached, days: m.days.size }))
      .sort((a, b) => b.tokens - a.tokens);
  }

  function seriesByDate(list, days) {
    const dates = [];
    const today = new Date();
    for (let i = days - 1; i >= 0; i--) {
      const d = new Date(today);
      d.setDate(d.getDate() - i);
      dates.push(d.toISOString().slice(0, 10));
    }
    const map = {};
    for (const r of list) map[r.date] = (map[r.date] || 0) + totalTokens(r);
    return { dates, values: dates.map((d) => map[d] || 0) };
  }

  /* ---------- 图表 ---------- */
  function chartOf(el) {
    let inst = echarts.getInstanceByDom(el);
    if (!inst) inst = echarts.init(el, null, { renderer: "canvas" });
    return inst;
  }

  const AXIS_STYLE = {
    axisLine: { lineStyle: { color: "rgba(255,255,255,0.12)" } },
    axisLabel: { color: "#8b93a7", fontSize: 11 },
    splitLine: { lineStyle: { color: "rgba(255,255,255,0.06)" } },
  };
  const TOOLTIP_STYLE = {
    backgroundColor: "rgba(16,22,42,0.95)",
    borderColor: "rgba(255,255,255,0.12)",
    textStyle: { color: "#e8ecf4", fontSize: 12 },
  };

  function renderTrend(list) {
    const days = state.range === 7 ? 7 : 30;
    const dates = [];
    const today = new Date();
    for (let i = days - 1; i >= 0; i--) {
      const d = new Date(today);
      d.setDate(d.getDate() - i);
      dates.push(d.toISOString().slice(0, 10));
    }
    const byModel = {};
    for (const r of list) {
      if (!byModel[r.model]) byModel[r.model] = {};
      const v = state.metric === "cost" ? r.cost : totalTokens(r);
      byModel[r.model][r.date] = (byModel[r.model][r.date] || 0) + v;
    }
    const series = Object.keys(byModel).sort().map((model) => ({
      name: model,
      type: "line",
      stack: "total",
      smooth: true,
      symbol: "none",
      lineStyle: { width: 1.5, color: colorOf(model) },
      areaStyle: { opacity: 0.18, color: colorOf(model) },
      itemStyle: { color: colorOf(model) },
      emphasis: { focus: "series" },
      data: dates.map((d) => +(byModel[model][d] || 0).toFixed(4)),
    }));
    chartOf($("#chart-trend")).setOption({
      backgroundColor: "transparent",
      tooltip: { trigger: "axis", ...TOOLTIP_STYLE, valueFormatter: (v) => state.metric === "cost" ? fmtCost(v) : fmtFull(v) },
      legend: { textStyle: { color: "#8b93a7", fontSize: 11 }, icon: "roundRect", itemWidth: 12, itemHeight: 6, top: 0 },
      grid: { left: 8, right: 12, top: 34, bottom: 4, containLabel: true },
      xAxis: {
        type: "category", data: dates.map((d) => d.slice(5)), boundaryGap: false,
        axisLine: AXIS_STYLE.axisLine, axisLabel: AXIS_STYLE.axisLabel, axisTick: { show: false },
      },
      yAxis: {
        type: "value",
        axisLabel: { ...AXIS_STYLE.axisLabel, formatter: (v) => state.metric === "cost" ? "$" + v : fmtNum(v) },
        splitLine: AXIS_STYLE.splitLine,
      },
      series,
    }, true);
  }

  function renderModelPie(list) {
    const agg = aggBy(list, "model");
    chartOf($("#chart-model-pie")).setOption({
      backgroundColor: "transparent",
      tooltip: {
        trigger: "item", ...TOOLTIP_STYLE,
        formatter: (p) => `${p.name}<br/>${fmtFull(p.value)} tokens (${p.percent}%)`,
      },
      legend: { orient: "vertical", right: 4, top: "middle", textStyle: { color: "#8b93a7", fontSize: 11 }, icon: "circle", itemWidth: 8 },
      series: [{
        type: "pie",
        radius: ["58%", "80%"],
        center: ["36%", "50%"],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: "#0a0e17", borderWidth: 2, borderRadius: 6 },
        label: { show: false },
        emphasis: { scaleSize: 6, label: { show: true, color: "#e8ecf4", fontSize: 13, fontWeight: 600, formatter: "{b}\n{d}%" } },
        data: agg.map((m) => ({ name: m.name, value: m.tokens, itemStyle: { color: colorOf(m.name) } })),
      }],
    }, true);
    chartOf($("#chart-model-pie")).off("click");
    chartOf($("#chart-model-pie")).on("click", (p) => openDrawer("model", p.name));
  }

  function renderAgentBar(list) {
    const agg = aggBy(list, "agent");
    chartOf($("#chart-agent-bar")).setOption({
      backgroundColor: "transparent",
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, ...TOOLTIP_STYLE, valueFormatter: fmtFull },
      legend: { textStyle: { color: "#8b93a7", fontSize: 11 }, icon: "roundRect", itemWidth: 12, itemHeight: 6, top: 0 },
      grid: { left: 8, right: 12, top: 30, bottom: 4, containLabel: true },
      xAxis: { type: "category", data: agg.map((a) => a.name), axisLine: AXIS_STYLE.axisLine, axisLabel: { ...AXIS_STYLE.axisLabel, interval: 0, rotate: agg.length > 5 ? 18 : 0 }, axisTick: { show: false } },
      yAxis: { type: "value", axisLabel: { ...AXIS_STYLE.axisLabel, formatter: fmtNum }, splitLine: AXIS_STYLE.splitLine },
      series: [
        { name: "输入", type: "bar", stack: "t", barWidth: 22, itemStyle: { color: "#7c5cff", borderRadius: [0, 0, 0, 0] }, data: agg.map((a) => a.input) },
        { name: "输出", type: "bar", stack: "t", itemStyle: { color: "#38bdf8" }, data: agg.map((a) => a.output) },
        { name: "缓存", type: "bar", stack: "t", itemStyle: { color: "#2dd4bf", borderRadius: [5, 5, 0, 0] }, data: agg.map((a) => a.cached) },
      ],
    }, true);
    chartOf($("#chart-agent-bar")).off("click");
    chartOf($("#chart-agent-bar")).on("click", (p) => openDrawer("agent", p.name));
  }

  /* ---------- 渲染：总览 ---------- */
  function renderStats(list) {
    const tokens = list.reduce((s, r) => s + totalTokens(r), 0);
    const input = list.reduce((s, r) => s + r.input, 0);
    const output = list.reduce((s, r) => s + r.output, 0);
    const cached = list.reduce((s, r) => s + r.cached, 0);
    const cost = list.reduce((s, r) => s + r.cost, 0);
    const requests = list.reduce((s, r) => s + r.requests, 0);
    const models = new Set(list.map((r) => r.model)).size;
    const agents = new Set(list.map((r) => r.agent)).size;
    const days = Math.max(1, new Set(list.map((r) => r.date)).size);

    const cards = [
      { label: "总消耗 Tokens", value: fmtNum(tokens), sub: `输入 ${fmtNum(input)} · 输出 ${fmtNum(output)} · 缓存 ${fmtNum(cached)}`, color: "rgba(124,92,255,0.3)" },
      { label: "总费用（估算）", value: fmtCost(cost), sub: `日均 ${fmtCost(cost / days)}`, color: "rgba(0,212,255,0.28)" },
      { label: "总请求数", value: fmtFull(requests), sub: `日均 ${fmtFull(Math.round(requests / days))} 次`, color: "rgba(52,211,153,0.28)" },
      { label: "活跃模型 / Agent", value: `${models} <small>/</small> ${agents}`, sub: `${days} 天有使用记录`, color: "rgba(251,191,36,0.26)" },
    ];
    $("#stat-cards").innerHTML = cards.map((c) => `
      <div class="stat-card" style="--sc:${c.color}">
        <div class="stat-label">${c.label}</div>
        <div class="stat-value">${c.value}</div>
        <div class="stat-sub">${c.sub}</div>
      </div>`).join("");
  }

  function renderRanks(list) {
    for (const [key, elId, type] of [["model", "#model-rank", "model"], ["agent", "#agent-rank", "agent"]]) {
      const agg = aggBy(list, key).slice(0, 6);
      const max = agg.length ? agg[0].tokens : 1;
      $(elId).innerHTML = agg.map((m, i) => `
        <div class="rank-item" data-type="${type}" data-name="${esc(m.name)}">
          <span class="rank-no">${i + 1}</span>
          <span class="rank-dot" style="background:${colorOf(m.name)}"></span>
          <span class="rank-name">${esc(m.name)}</span>
          <span class="rank-bar-wrap"><span class="rank-bar" style="display:block;width:${(m.tokens / max * 100).toFixed(1)}%;background:${colorOf(m.name)}"></span></span>
          <span class="rank-val">${fmtNum(m.tokens)}</span>
        </div>`).join("") || `<div class="empty">暂无数据</div>`;
    }
    $$(".rank-item").forEach((el) => el.addEventListener("click", () => openDrawer(el.dataset.type, el.dataset.name)));
  }

  /* ---------- 渲染：模型 / Agent 卡片 ---------- */
  function renderEntityCards(list, key, containerId, type) {
    const agg = aggBy(list, key);
    const el = $(containerId);
    el.innerHTML = agg.map((m, i) => {
      const c = colorOf(m.name);
      const share = (() => {
        const all = agg.reduce((s, x) => s + x.tokens, 0);
        return all ? (m.tokens / all * 100).toFixed(1) : 0;
      })();
      return `
      <div class="entity-card" data-type="${type}" data-name="${esc(m.name)}">
        <div class="entity-head">
          <div class="entity-badge" style="background:linear-gradient(135deg, ${c}, ${c}88)">${esc(m.name[0])}</div>
          <div>
            <div class="entity-name">${esc(m.name)}</div>
            <div class="entity-sub">${m.days} 天活跃 · 占比 ${share}%</div>
          </div>
        </div>
        <div class="entity-stats">
          <div><div class="es-label">总 Tokens</div><div class="es-value">${fmtNum(m.tokens)}</div></div>
          <div><div class="es-label">费用</div><div class="es-value">${fmtCost(m.cost)}</div></div>
          <div><div class="es-label">输入 / 输出</div><div class="es-value">${fmtNum(m.input)} / ${fmtNum(m.output)}</div></div>
          <div><div class="es-label">请求数</div><div class="es-value">${fmtFull(m.requests)}</div></div>
        </div>
        <div class="entity-spark" id="spark-${type}-${i}"></div>
      </div>`;
    }).join("") || `<div class="empty">当前筛选条件下暂无数据</div>`;

    agg.forEach((m, i) => {
      const sub = list.filter((r) => r[key] === m.name);
      const { dates, values } = seriesByDate(sub, 14);
      const sparkEl = document.getElementById(`spark-${type}-${i}`);
      if (!sparkEl) return;
      chartOf(sparkEl).setOption({
        backgroundColor: "transparent",
        grid: { left: 0, right: 0, top: 4, bottom: 0 },
        xAxis: { type: "category", show: false, data: dates, boundaryGap: false },
        yAxis: { type: "value", show: false, min: 0 },
        tooltip: { trigger: "axis", ...TOOLTIP_STYLE, formatter: (ps) => `${ps[0].name}<br/>${fmtFull(ps[0].value)} tokens` },
        series: [{
          type: "line", data: values, smooth: true, symbol: "none",
          lineStyle: { width: 2, color: colorOf(m.name) },
          areaStyle: {
            color: {
              type: "linear", x: 0, y: 0, x2: 0, y2: 1,
              colorStops: [{ offset: 0, color: colorOf(m.name) + "55" }, { offset: 1, color: colorOf(m.name) + "00" }],
            },
          },
        }],
      }, true);
    });

    el.querySelectorAll(".entity-card").forEach((card) =>
      card.addEventListener("click", () => openDrawer(card.dataset.type, card.dataset.name)));
  }

  /* ---------- 渲染：记录表 ---------- */
  function renderRecords(list) {
    const rows = [...list].sort((a, b) => b.date.localeCompare(a.date) || b.id - a.id);
    $("#records-count").textContent = `共 ${rows.length} 条`;
    $("#records-tbody").innerHTML = rows.map((r) => `
      <tr>
        <td>${r.date}</td>
        <td><span class="tag"><i style="background:${colorOf(r.agent)}"></i>${esc(r.agent)}</span></td>
        <td><span class="tag"><i style="background:${colorOf(r.model)}"></i>${esc(r.model)}</span></td>
        <td class="num">${fmtFull(r.requests)}</td>
        <td class="num">${fmtFull(r.input)}</td>
        <td class="num">${fmtFull(r.output)}</td>
        <td class="num">${fmtFull(r.cached)}</td>
        <td class="num"><b>${fmtFull(totalTokens(r))}</b></td>
        <td class="num">${fmtCost(r.cost)}</td>
        <td class="num">${r.source === "auto" ? '<span class="auto-badge">自动</span>' : `<button class="row-del" data-id="${r.id}" title="删除">✕</button>`}</td>
      </tr>`).join("") || `<tr><td colspan="10" class="empty">暂无数据：点击左下「同步本地数据」扫描本机 AI 工具日志，或「录入数据」手动添加</td></tr>`;

    $$("#records-tbody .row-del").forEach((btn) => btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm("确定删除这条记录吗？")) return;
      const res = await fetch(`/api/records?id=${btn.dataset.id}`, { method: "DELETE" });
      if (res.ok) { toast("已删除"); await loadData(); }
      else toast("删除失败");
    }));
  }

  /* ---------- 抽屉 ---------- */
  function openDrawer(type, name) {
    const list = state.records.filter((r) => r[type] === name);
    if (!list.length) return;
    const tokens = list.reduce((s, r) => s + totalTokens(r), 0);
    const cost = list.reduce((s, r) => s + r.cost, 0);
    const requests = list.reduce((s, r) => s + r.requests, 0);
    const input = list.reduce((s, r) => s + r.input, 0);
    const output = list.reduce((s, r) => s + r.output, 0);
    const cached = list.reduce((s, r) => s + r.cached, 0);
    const others = new Set(list.map((r) => r[type === "model" ? "agent" : "model"]));

    $("#drawer-tag").textContent = type === "model" ? "模型详情" : "Agent 详情";
    $("#drawer-title").textContent = name;
    $("#drawer-col-extra").textContent = type === "model" ? "Agent" : "模型";
    $("#drawer-stats").innerHTML = [
      ["总 Tokens", fmtNum(tokens)],
      ["费用", fmtCost(cost)],
      ["请求数", fmtFull(requests)],
      ["输入", fmtNum(input)],
      ["输出", fmtNum(output)],
      ["缓存", fmtNum(cached)],
    ].map(([l, v]) => `<div class="ds"><div class="ds-label">${l}</div><div class="ds-value">${v}</div></div>`).join("")
      + (type === "model" && state.prices[name]
        ? `<div class="ds"><div class="ds-label">单价（入/出 每百万）</div><div class="ds-value">$${state.prices[name].input} / $${state.prices[name].output}</div></div>`
        : `<div class="ds"><div class="ds-label">关联${type === "model" ? "Agent" : "模型"}数</div><div class="ds-value">${others.size}</div></div>`);

    $("#drawer-mask").classList.add("show");
    $("#drawer").classList.add("show");

    const { dates, values } = seriesByDate(list, 30);
    chartOf($("#chart-drawer")).setOption({
      backgroundColor: "transparent",
      tooltip: { trigger: "axis", ...TOOLTIP_STYLE, formatter: (ps) => `${dates[ps[0].dataIndex]}<br/>${fmtFull(ps[0].value)} tokens` },
      grid: { left: 8, right: 12, top: 14, bottom: 4, containLabel: true },
      xAxis: { type: "category", data: dates.map((d) => d.slice(5)), boundaryGap: false, axisLine: AXIS_STYLE.axisLine, axisLabel: AXIS_STYLE.axisLabel, axisTick: { show: false } },
      yAxis: { type: "value", axisLabel: { ...AXIS_STYLE.axisLabel, formatter: fmtNum }, splitLine: AXIS_STYLE.splitLine },
      series: [{
        type: "line", data: values, smooth: true, symbol: "none",
        lineStyle: { width: 2.5, color: colorOf(name) },
        areaStyle: {
          color: {
            type: "linear", x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [{ offset: 0, color: colorOf(name) + "66" }, { offset: 1, color: colorOf(name) + "00" }],
          },
        },
      }],
    }, true);

    const extraKey = type === "model" ? "agent" : "model";
    const rows = [...list].sort((a, b) => b.date.localeCompare(a.date) || b.id - a.id).slice(0, 20);
    $("#drawer-tbody").innerHTML = rows.map((r) => `
      <tr>
        <td>${r.date}</td>
        <td><span class="tag"><i style="background:${colorOf(r[extraKey])}"></i>${esc(r[extraKey])}</span></td>
        <td class="num">${fmtFull(totalTokens(r))}</td>
        <td class="num">${fmtCost(r.cost)}</td>
      </tr>`).join("");
  }

  function closeDrawer() {
    $("#drawer-mask").classList.remove("show");
    $("#drawer").classList.remove("show");
  }

  /* ---------- 录入弹窗 ---------- */
  async function syncData() {
    const btn = $("#btn-sync");
    btn.disabled = true;
    btn.textContent = "⟳ 扫描中…";
    try {
      const res = await fetch("/api/collect", { method: "POST" });
      const data = await res.json();
      if (!res.ok) { toast(data.error || "同步失败"); return; }
      const parts = Object.entries(data.summary || {})
        .map(([agent, s]) => `${agent}：${s.files} 个日志，新增 ${s.events} 条`);
      toast(parts.length ? `扫描完成 ｜ ${parts.join("；")}` : "未发现本机 AI 工具日志");
      await loadData();
    } catch (e) {
      toast("同步失败");
    } finally {
      btn.disabled = false;
      btn.textContent = "⟳ 同步本地数据";
    }
  }

  function openModal() {
    buildDatalists();
    $("#modal-mask").classList.add("show");
  }
  function closeModal() { $("#modal-mask").classList.remove("show"); }

  function buildDatalists() {
    const agents = [...new Set([...state.agents, ...state.records.map((r) => r.agent)])];
    const models = [...new Set([...Object.keys(state.prices), ...state.records.map((r) => r.model)])];
    $("#dl-agents").innerHTML = agents.map((a) => `<option value="${esc(a)}">`).join("");
    $("#dl-models").innerHTML = models.map((m) => `<option value="${esc(m)}">`).join("");
  }

  function updateCostTip() {
    const f = $("#add-form");
    const model = f.model.value.trim();
    const p = state.prices[model];
    const input = +f.input.value || 0, output = +f.output.value || 0, cached = +f.cached.value || 0;
    if (!p) {
      $("#cost-tip").textContent = model ? "该模型暂无价格信息，费用将记为 0，可手动填写。" : "";
      return;
    }
    const cost = (input * p.input + output * p.output + cached * p.input * 0.1) / 1e6;
    $("#cost-tip").textContent = `按 ${model} 价目估算费用：${fmtCost(cost)}`;
  }

  async function submitForm(e) {
    e.preventDefault();
    const f = e.target;
    const payload = {
      date: f.date.value,
      agent: f.agent.value.trim(),
      model: f.model.value.trim(),
      requests: +f.requests.value || 0,
      input: +f.input.value || 0,
      output: +f.output.value || 0,
      cached: +f.cached.value || 0,
      cost: f.cost.value === "" ? null : +f.cost.value,
    };
    const res = await fetch("/api/records", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (res.ok) {
      toast("已保存");
      closeModal();
      f.reset();
      f.date.value = new Date().toISOString().slice(0, 10);
      $("#cost-tip").textContent = "";
      await loadData();
    } else {
      const err = await res.json().catch(() => ({}));
      toast(err.error || "保存失败");
    }
  }

  /* ---------- 筛选 & 视图切换 ---------- */
  function buildFilterOptions() {
    const agents = [...new Set(state.records.map((r) => r.agent))].sort();
    const models = [...new Set(state.records.map((r) => r.model))].sort();
    const keepA = state.agent, keepM = state.model;
    $("#filter-agent").innerHTML = `<option value="all">全部 Agent</option>` + agents.map((a) => `<option ${a === keepA ? "selected" : ""} value="${esc(a)}">${esc(a)}</option>`).join("");
    $("#filter-model").innerHTML = `<option value="all">全部模型</option>` + models.map((m) => `<option ${m === keepM ? "selected" : ""} value="${esc(m)}">${esc(m)}</option>`).join("");
  }

  const VIEW_TITLES = { overview: "总览", models: "模型", agents: "Agent", records: "明细记录" };

  function switchView(view) {
    state.view = view;
    $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
    $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + view));
    $("#page-title").textContent = VIEW_TITLES[view];
    render();
    // 视图切换后图表需要按新尺寸重绘
    setTimeout(() => $$(".chart, .entity-spark").forEach((el) => {
      const inst = echarts.getInstanceByDom(el);
      if (inst) inst.resize();
    }), 30);
  }

  function render() {
    const list = filtered();
    const autoCount = state.records.filter((r) => r.source === "auto").length;
    $("#foot-tip").textContent = `共 ${state.records.length} 条` + (autoCount ? ` · 自动采集 ${autoCount} 条` : " 记录");
    if (state.view === "overview") {
      renderStats(list);
      renderTrend(list);
      renderModelPie(list);
      renderAgentBar(list);
      renderRanks(list);
    } else if (state.view === "models") {
      renderEntityCards(list, "model", "#model-cards", "model");
    } else if (state.view === "agents") {
      renderEntityCards(list, "agent", "#agent-cards", "agent");
    } else if (state.view === "records") {
      renderRecords(list);
    }
  }

  /* ---------- 事件绑定 ---------- */
  function bind() {
    $$(".nav-item").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));

    $$("#range-seg button").forEach((b) => b.addEventListener("click", () => {
      state.range = +b.dataset.range;
      $$("#range-seg button").forEach((x) => x.classList.toggle("active", x === b));
      render();
    }));

    $$("#metric-seg button").forEach((b) => b.addEventListener("click", () => {
      state.metric = b.dataset.metric;
      $$("#metric-seg button").forEach((x) => x.classList.toggle("active", x === b));
      render();
    }));

    $("#filter-agent").addEventListener("change", (e) => { state.agent = e.target.value; render(); });
    $("#filter-model").addEventListener("change", (e) => { state.model = e.target.value; render(); });

    $("#btn-add").addEventListener("click", openModal);
    $("#btn-sync").addEventListener("click", syncData);
    $("#modal-close").addEventListener("click", closeModal);
    $("#modal-mask").addEventListener("click", (e) => { if (e.target.id === "modal-mask") closeModal(); });
    $("#drawer-close").addEventListener("click", closeDrawer);
    $("#drawer-mask").addEventListener("click", closeDrawer);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeDrawer(); closeModal(); } });

    const f = $("#add-form");
    f.addEventListener("submit", submitForm);
    ["input", "output", "cached", "model"].forEach((n) => f[n].addEventListener("input", updateCostTip));
    f.date.value = new Date().toISOString().slice(0, 10);

    window.addEventListener("resize", () => $$(".chart, .entity-spark").forEach((el) => {
      const inst = echarts.getInstanceByDom(el);
      if (inst) inst.resize();
    }));
  }

  /* ---------- 启动 ---------- */
  bind();
  loadData().then(() => {
    const h = location.hash.slice(1);
    if (VIEW_TITLES[h] && h !== state.view) switchView(h);
  });
})();
