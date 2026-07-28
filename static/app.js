/* TokenLens 前端逻辑 */
(function () {
  "use strict";

  /* ---------- 状态 ---------- */
  const state = {
    records: [],
    prices: {},
    unpriced: [],
    agents: [],
    view: "overview",
    range: 30,        // 0 = 全部, "today" = 今天, "custom" = 自定义
    customFrom: "",
    customTo: "",
    agent: "all",
    model: "all",
    project: "all",
    metric: "tokens", // tokens | cost
    recGran: "day",   // day | hour | minute（明细记录粒度）
    recDate: "",
    recHour: 0,
    trendGran: "day", // day | hour | minute（趋势图时间粒度）
    trendDate: "",
    trendHour: 0,
    settings: { multi_device: "off", sync_dir: "", device: {}, peers: [] },
  };

  // 多端模式说明：off 单机；local_only 多端未同步（官方百分比仍准，绝对 token 数不可信）；
  // sync 多端已合并（绝对值恢复可信）
  const MULTI_TIPS = {
    off: "只有这一台电脑在用这个账号。官方百分比与本机日志同口径，估算的绝对额度可信。",
    local_only: "官方给的百分比是账号级的，本机日志只有本机那一份，两者相除会按本机用量占比系统性低估绝对额度。因此这个模式下只显示百分比与重置时间，隐藏所有绝对 token 估算。",
    sync: "各设备把采集结果写进同一个共享目录（每台只写自己的文件，不会冲突）。OneDrive / 坚果云 / Syncthing / SMB 共享都可以。汇总后绝对额度重新可信。注意：如果你把 AI 工具自身的日志目录（如 ~/.claude）也放进了网盘同步，两边会扫到同一批事件导致重复计数，那种情况请勿开启本模式。",
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
  function localDateStr(d) {
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }
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
    state.unpriced = data.unpriced || [];
    state.agents = data.agents;
    buildFilterOptions();
    render();
  }

  function shiftDate(dateStr, days) {
    const d = new Date(dateStr + "T00:00:00");
    d.setDate(d.getDate() + days);
    return localDateStr(d);
  }

  /* 当前时间范围的统一口径 {from, to, days}；「全部」没有边界，返回 null。
     统计卡、趋势图 X 轴、环比对比都从这里取，避免三处各算各的对不上。 */
  function periodBounds() {
    const today = localDateStr(new Date());
    if (state.range === "today") return { from: today, to: today, days: 1 };
    if (state.range === "custom") {
      if (!state.customFrom || !state.customTo || state.customFrom > state.customTo) return null;
      const days = Math.round(
        (new Date(state.customTo + "T00:00:00") - new Date(state.customFrom + "T00:00:00")) / 86400000) + 1;
      return { from: state.customFrom, to: state.customTo, days };
    }
    if (state.range > 0) return { from: shiftDate(today, 1 - state.range), to: today, days: state.range };
    return null;
  }

  /* 与当前周期等长、紧邻在前的一段，用于环比 */
  function previousBounds(cur) {
    if (!cur) return null;
    const to = shiftDate(cur.from, -1);
    return { from: shiftDate(to, 1 - cur.days), to, days: cur.days };
  }

  function inBounds(list, b) {
    return b ? list.filter((r) => r.date >= b.from && r.date <= b.to) : list;
  }

  function byFilters(list) {
    if (state.agent !== "all") list = list.filter((r) => r.agent === state.agent);
    if (state.model !== "all") list = list.filter((r) => r.model === state.model);
    if (state.project !== "all") list = list.filter((r) => (r.project || "") === state.project);
    return list;
  }

  function filtered() {
    return byFilters(inBounds(state.records, periodBounds()));
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
      dates.push(localDateStr(d));
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
  const TREND_HINT = "小时/分钟粒度来自本机原始日志，仅含自动采集数据，顶部时间范围不生效";

  const hourOptionsHtml = () => Array.from({ length: 24 }, (_, h) =>
    `<option value="${h}">${String(h).padStart(2, "0")} 时</option>`).join("");

  /* 趋势图 X 轴的日期序列。「全部」没有固定边界，按数据里的最早/最晚一天铺开。 */
  function rangeDates() {
    let b = periodBounds();
    if (!b) {
      const days = state.records.map((r) => r.date).filter(Boolean).sort();
      if (!days.length) return [localDateStr(new Date())];
      b = { from: days[0], to: days[days.length - 1] };
    }
    const dates = [];
    const d = new Date(b.from + "T00:00:00");
    const end = new Date(b.to + "T00:00:00");
    while (d <= end && dates.length < 800) {
      dates.push(localDateStr(d));
      d.setDate(d.getDate() + 1);
    }
    return dates.length ? dates : [localDateStr(new Date())];
  }

  /* 把 {模型: {桶: 值}} 铺成按 keys 对齐的堆叠面积系列 */
  function trendSeries(byModel, keys) {
    return Object.keys(byModel).sort().map((model) => ({
      name: model,
      type: "line",
      stack: "total",
      smooth: true,
      symbol: "none",
      lineStyle: { width: 1.5, color: colorOf(model) },
      areaStyle: { opacity: 0.18, color: colorOf(model) },
      itemStyle: { color: colorOf(model) },
      emphasis: { focus: "series" },
      data: keys.map((k) => +(byModel[model][k] || 0).toFixed(4)),
    }));
  }

  function setTrendOption(labels, series) {
    chartOf($("#chart-trend")).setOption({
      backgroundColor: "transparent",
      tooltip: { trigger: "axis", ...TOOLTIP_STYLE, valueFormatter: (v) => state.metric === "cost" ? fmtCost(v) : fmtFull(v) },
      legend: { textStyle: { color: "#8b93a7", fontSize: 11 }, icon: "roundRect", itemWidth: 12, itemHeight: 6, top: 0 },
      grid: { left: 8, right: 12, top: 34, bottom: 4, containLabel: true },
      xAxis: {
        type: "category", data: labels, boundaryGap: false,
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

  function renderTrend(list) {
    if (state.trendGran !== "day") { renderTrendFine(); return; }
    const dates = rangeDates();
    const byModel = {};
    for (const r of list) {
      if (!byModel[r.model]) byModel[r.model] = {};
      const v = state.metric === "cost" ? r.cost : totalTokens(r);
      byModel[r.model][r.date] = (byModel[r.model][r.date] || 0) + v;
    }
    setTrendOption(dates.map((d) => d.slice(5)), trendSeries(byModel, dates));
  }

  /* 小时/分钟粒度：现场重扫原始日志（/api/timeline），只含自动采集数据 */
  let trendSeq = 0;
  async function renderTrendFine() {
    const seq = ++trendSeq;
    const inst = chartOf($("#chart-trend"));
    const hint = $("#trend-gran-hint");
    const params = new URLSearchParams({ day: state.trendDate });
    if (state.trendGran === "minute") params.set("hour", state.trendHour);
    hint.textContent = TREND_HINT + " · 加载中…";
    inst.showLoading("default", { text: "", color: "#7c5cff", maskColor: "rgba(0,0,0,0)" });
    let data;
    try {
      const res = await fetch("/api/timeline?" + params.toString());
      data = await res.json();
      if (!res.ok) throw new Error(data.error || "加载失败");
    } catch (e) {
      if (seq !== trendSeq) return;   // 已被更新的请求取代
      inst.hideLoading();
      hint.textContent = TREND_HINT + " · " + (e.message || "加载失败");
      return;
    }
    if (seq !== trendSeq) return;
    inst.hideLoading();

    let rows = data.rows || [];
    if (state.agent !== "all") rows = rows.filter((r) => r.agent === state.agent);
    if (state.model !== "all") rows = rows.filter((r) => r.model === state.model);

    // 桶键与后端 breakdown() 一致：按小时是 "13"，按分钟是 "13:42"；补齐空桶保证轴连续
    const hh = String(state.trendHour).padStart(2, "0");
    const keys = state.trendGran === "hour"
      ? Array.from({ length: 24 }, (_, h) => String(h).padStart(2, "0"))
      : Array.from({ length: 60 }, (_, m) => `${hh}:${String(m).padStart(2, "0")}`);
    const labels = state.trendGran === "hour" ? keys.map((h) => h + ":00") : keys;

    const byModel = {};
    for (const r of rows) {
      if (!byModel[r.model]) byModel[r.model] = {};
      const v = state.metric === "cost" ? (r.cost || 0) : totalTokens(r);
      byModel[r.model][r.bucket] = (byModel[r.model][r.bucket] || 0) + v;
    }
    setTrendOption(labels, trendSeries(byModel, keys));
    const scope = data.day + (state.trendGran === "minute" ? ` ${hh} 时` : "");
    hint.textContent = rows.length
      ? `${TREND_HINT} · ${scope}`
      : `${TREND_HINT} · ${scope}没有采集到数据`;
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
  const sumBy = (list, f) => list.reduce((s, r) => s + f(r), 0);

  /* 环比徽标：与紧邻的等长上一周期比。上期为 0 时不算百分比（除零没意义），
     只标「新增」；两期都为 0 就什么都不显示。 */
  function deltaHtml(cur, prev) {
    if (prev == null) return "";
    if (!prev && !cur) return "";
    if (!prev) return `<span class="delta up">新增</span>`;
    const d = (cur - prev) / prev * 100;
    const cls = d > 1 ? "up" : d < -1 ? "down" : "flat";
    const arrow = d > 1 ? "↑" : d < -1 ? "↓" : "≈";
    return `<span class="delta ${cls}">${arrow} ${Math.abs(d) >= 999 ? "999+" : Math.abs(d).toFixed(0)}%</span>`;
  }

  function renderStats(list) {
    const tokens = sumBy(list, totalTokens);
    const input = sumBy(list, (r) => r.input);
    const output = sumBy(list, (r) => r.output);
    const cached = sumBy(list, (r) => r.cached);
    const cost = sumBy(list, (r) => r.cost);
    const requests = sumBy(list, (r) => r.requests);
    const models = new Set(list.map((r) => r.model)).size;
    const agents = new Set(list.map((r) => r.agent)).size;
    const projects = new Set(list.map((r) => r.project || "")).size;
    const days = Math.max(1, new Set(list.map((r) => r.date)).size);
    const hitRate = input + cached > 0 ? (cached / (input + cached)) * 100 : 0;

    // 上一周期（同样长度、紧挨在前），「全部」范围下没有可比对象
    const prevB = previousBounds(periodBounds());
    const prev = prevB ? byFilters(inBounds(state.records, prevB)) : null;
    const p = prev && {
      tokens: sumBy(prev, totalTokens), cost: sumBy(prev, (r) => r.cost),
      requests: sumBy(prev, (r) => r.requests),
    };
    const vs = prevB ? `较上期（${prevB.from.slice(5)}~${prevB.to.slice(5)}）` : "";

    const cards = [
      { label: "总消耗 Tokens", value: fmtNum(tokens), delta: p && deltaHtml(tokens, p.tokens),
        sub: p ? `${vs} ${fmtNum(p.tokens)}` : `输入 ${fmtNum(input)} · 输出 ${fmtNum(output)} · 缓存 ${fmtNum(cached)}`,
        color: "rgba(124,92,255,0.3)" },
      { label: "总费用（估算）", value: fmtCost(cost), delta: p && deltaHtml(cost, p.cost),
        sub: p ? `日均 ${fmtCost(cost / days)} · ${vs} ${fmtCost(p.cost)}` : `日均 ${fmtCost(cost / days)}`,
        color: "rgba(0,212,255,0.28)" },
      { label: "总请求数", value: fmtFull(requests), delta: p && deltaHtml(requests, p.requests),
        sub: p ? `日均 ${fmtFull(Math.round(requests / days))} 次 · ${vs} ${fmtFull(p.requests)}` : `日均 ${fmtFull(Math.round(requests / days))} 次`,
        color: "rgba(52,211,153,0.28)" },
      { label: "活跃模型 / Agent", value: `${models} <small>/</small> ${agents}`,
        sub: `${projects} 个项目 · ${days} 天有使用记录`, color: "rgba(251,191,36,0.26)" },
      { label: "缓存命中率", value: hitRate.toFixed(1) + "%",
        sub: `缓存 ${fmtNum(cached)} / 总输入 ${fmtNum(input + cached)}`, color: "rgba(45,212,191,0.28)" },
    ];
    $("#stat-cards").innerHTML = cards.map((c) => `
      <div class="stat-card" style="--sc:${c.color}">
        <div class="stat-label">${c.label}</div>
        <div class="stat-value">${c.value}${c.delta || ""}</div>
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
          ${type === "model" && state.unpriced.includes(String(m.name).toLowerCase()) ? `<span class="price-badge">未定价</span>` : ""}
          ${type === "model" ? `<button class="icon-btn price-edit" data-name="${esc(m.name)}" title="编辑价格">✎</button>` : ""}
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
    el.querySelectorAll(".price-edit").forEach((btn) =>
      btn.addEventListener("click", (e) => { e.stopPropagation(); openPriceModal(btn.dataset.name); }));
  }

  /* ---------- 渲染：记录表 ---------- */
  function hitRate(r) {
    const total = r.input + r.cached;
    return total > 0 ? ((r.cached / total) * 100).toFixed(1) + "%" : "—";
  }

  /* 记录来源：本机自动采集 / 其他设备同步而来 / 手动录入（可删） */
  function sourceCell(r) {
    if (r.source === "auto") return '<span class="auto-badge">自动</span>';
    if (r.source === "remote") return `<span class="remote-badge" title="来自其他设备">⇄ ${esc(r.device || "其他设备")}</span>`;
    return `<button class="row-del" data-id="${r.id}" title="删除">✕</button>`;
  }

  function renderRecords(list) {
    const rows = [...list].sort((a, b) => b.date.localeCompare(a.date) || b.id - a.id);
    $("#records-count").textContent = `共 ${rows.length} 条`;
    $("#records-tbody").innerHTML = rows.map((r) => `
      <tr>
        <td>${esc(r.date)}</td>
        <td><span class="tag"><i style="background:${colorOf(r.agent)}"></i>${esc(r.agent)}</span></td>
        <td><span class="tag"><i style="background:${colorOf(r.model)}"></i>${esc(r.model)}</span></td>
        <td>${r.project ? `<span class="tag"><i style="background:${colorOf(r.project)}"></i>${esc(r.project)}</span>` : "—"}</td>
        <td class="num">${fmtFull(r.requests)}</td>
        <td class="num">${fmtFull(r.input)}</td>
        <td class="num">${fmtFull(r.output)}</td>
        <td class="num">${fmtFull(r.cached)}</td>
        <td class="num">${hitRate(r)}</td>
        <td class="num"><b>${fmtFull(totalTokens(r))}</b></td>
        <td class="num">${fmtCost(r.cost)}</td>
        <td class="num">${sourceCell(r)}</td>
      </tr>`).join("") || `<tr><td colspan="12" class="empty">暂无数据：点击左下「同步本地数据」扫描本机 AI 工具日志，或「录入数据」手动添加</td></tr>`;

    $$("#records-tbody .row-del").forEach((btn) => btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm("确定删除这条记录吗？")) return;
      const res = await fetch(`/api/records?id=${btn.dataset.id}`, { method: "DELETE" });
      if (res.ok) { toast("已删除"); await loadData(); }
      else toast("删除失败");
    }));
  }

  /* ---------- 渲染：小时/分钟时间线 ---------- */
  async function renderTimeline() {
    const tbody = $("#records-tbody");
    const params = new URLSearchParams({ day: state.recDate });
    if (state.recGran === "minute") params.set("hour", state.recHour);
    $("#records-count").textContent = "加载中…";
    tbody.innerHTML = `<tr><td colspan="12" class="empty">加载中…</td></tr>`;
    let data;
    try {
      const res = await fetch("/api/timeline?" + params.toString());
      data = await res.json();
      if (!res.ok) throw new Error(data.error || "加载失败");
    } catch (e) {
      $("#records-count").textContent = "";
      tbody.innerHTML = `<tr><td colspan="12" class="empty">${esc(e.message || "加载失败")}</td></tr>`;
      return;
    }
    let rows = data.rows || [];
    if (state.agent !== "all") rows = rows.filter((r) => r.agent === state.agent);
    if (state.model !== "all") rows = rows.filter((r) => r.model === state.model);
    $("#records-count").textContent = `${data.day}${state.recGran === "minute" ? " " + String(state.recHour).padStart(2, "0") + " 时" : ""} · 共 ${rows.length} 条`;
    tbody.innerHTML = rows.map((r) => {
      const label = state.recGran === "hour" ? `${r.bucket}:00` : r.bucket;
      return `
      <tr>
        <td>${label}</td>
        <td><span class="tag"><i style="background:${colorOf(r.agent)}"></i>${esc(r.agent)}</span></td>
        <td><span class="tag"><i style="background:${colorOf(r.model)}"></i>${esc(r.model)}</span></td>
        <td class="dim" title="小时/分钟粒度不带项目维度">—</td>
        <td class="num">${fmtFull(r.requests)}</td>
        <td class="num">${fmtFull(r.input)}</td>
        <td class="num">${fmtFull(r.output)}</td>
        <td class="num">${fmtFull(r.cached)}</td>
        <td class="num">${hitRate(r)}</td>
        <td class="num"><b>${fmtFull(totalTokens(r))}</b></td>
        <td class="num">${fmtCost(r.cost || 0)}</td>
        <td class="num"><span class="auto-badge">自动</span></td>
      </tr>`;
    }).join("") || `<tr><td colspan="12" class="empty">该时段没有采集到数据（小时/分钟粒度仅含自动采集数据，手动记录请切回「按天」）</td></tr>`;
  }

  /* ---------- 抽屉 ---------- */
  // 抽屉里「相关记录」那一列展示的是另一个维度
  const DRAWER_EXTRA = { model: "agent", agent: "model", project: "model" };
  const DRAWER_TAG = { model: "模型详情", agent: "Agent 详情", project: "项目详情" };
  const DIM_LABEL = { model: "模型", agent: "Agent", project: "项目" };

  function openDrawer(type, name) {
    const list = state.records.filter((r) => r[type] === name);
    if (!list.length) return;
    const tokens = list.reduce((s, r) => s + totalTokens(r), 0);
    const cost = list.reduce((s, r) => s + r.cost, 0);
    const requests = list.reduce((s, r) => s + r.requests, 0);
    const input = list.reduce((s, r) => s + r.input, 0);
    const output = list.reduce((s, r) => s + r.output, 0);
    const cached = list.reduce((s, r) => s + r.cached, 0);
    const extraKey = DRAWER_EXTRA[type] || "model";
    const others = new Set(list.map((r) => r[extraKey]));

    $("#drawer-tag").textContent = DRAWER_TAG[type] || "详情";
    $("#drawer-title").textContent = name;
    $("#drawer-col-extra").textContent = DIM_LABEL[extraKey];
    $("#drawer-stats").innerHTML = [
      ["总 Tokens", fmtNum(tokens)],
      ["费用", fmtCost(cost)],
      ["请求数", fmtFull(requests)],
      ["输入", fmtNum(input)],
      ["输出", fmtNum(output)],
      ["缓存", fmtNum(cached)],
    ].map(([l, v]) => `<div class="ds"><div class="ds-label">${l}</div><div class="ds-value">${v}</div></div>`).join("")
      + (type === "model" && state.prices[String(name).toLowerCase()]
        ? `<div class="ds"><div class="ds-label">单价（入/出 每百万）</div><div class="ds-value">$${state.prices[String(name).toLowerCase()].input} / $${state.prices[String(name).toLowerCase()].output}</div></div>`
        : `<div class="ds"><div class="ds-label">关联${DIM_LABEL[extraKey]}数</div><div class="ds-value">${others.size}</div></div>`);

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

    const rows = [...list].sort((a, b) => b.date.localeCompare(a.date) || b.id - a.id).slice(0, 20);
    $("#drawer-tbody").innerHTML = rows.map((r) => `
      <tr>
        <td>${esc(r.date)}</td>
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

  function openPriceModal(model) {
    const f = $("#price-form");
    const p = state.prices[String(model).toLowerCase()] || {};
    f.model.value = model;
    f.input.value = p.input != null ? p.input : "";
    f.output.value = p.output != null ? p.output : "";
    f.cache_read.value = p.cache_read != null ? p.cache_read : "";
    $("#price-modal-mask").classList.add("show");
  }

  function buildDatalists() {
    const agents = [...new Set([...state.agents, ...state.records.map((r) => r.agent)])];
    const models = [...new Set([...Object.keys(state.prices), ...state.records.map((r) => r.model)])];
    $("#dl-agents").innerHTML = agents.map((a) => `<option value="${esc(a)}">`).join("");
    $("#dl-models").innerHTML = models.map((m) => `<option value="${esc(m)}">`).join("");
    $("#dl-projects").innerHTML = projectNames().map((p) => `<option value="${esc(p)}">`).join("");
  }

  function updateCostTip() {
    const f = $("#add-form");
    const model = f.model.value.trim();
    const p = state.prices[model.toLowerCase()];
    const input = +f.input.value || 0, output = +f.output.value || 0, cached = +f.cached.value || 0;
    if (!p) {
      $("#cost-tip").textContent = model ? "该模型暂无价格信息，费用将记为 0，可在「模型」页点 ✎ 录入价格。" : "";
      return;
    }
    const cachePrice = p.cache_read != null ? p.cache_read : p.input * 0.1;
    const cost = (input * p.input + output * p.output + cached * cachePrice) / 1e6;
    $("#cost-tip").textContent = `按 ${model} 价目估算费用：${fmtCost(cost)}`;
  }

  async function submitForm(e) {
    e.preventDefault();
    const f = e.target;
    const payload = {
      date: f.date.value,
      agent: f.agent.value.trim(),
      model: f.model.value.trim(),
      project: f.project.value.trim(),
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
    const opts = (sel, all, values, keep) => {
      $(sel).innerHTML = `<option value="all">${all}</option>` + values.map((v) =>
        `<option ${v === keep ? "selected" : ""} value="${esc(v)}">${esc(v)}</option>`).join("");
    };
    opts("#filter-agent", "全部 Agent", [...new Set(state.records.map((r) => r.agent))].sort(), state.agent);
    opts("#filter-model", "全部模型", [...new Set(state.records.map((r) => r.model))].sort(), state.model);
    opts("#filter-project", "全部项目", projectNames(), state.project);
  }

  function projectNames() {
    return [...new Set(state.records.map((r) => r.project).filter(Boolean))].sort();
  }

  const VIEW_TITLES = { overview: "总览", models: "模型", agents: "Agent", projects: "项目", records: "明细记录", plans: "Plan 用量" };

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

  const UNKNOWN_PROJECT = "未知项目";

  function renderProjectsHint() {
    const unknown = state.records.filter((r) => r.project === UNKNOWN_PROJECT).length;
    $("#projects-hint").textContent =
      "项目名取自各工具记录的工作目录（Kimi 取会话目录名，Cline 系不记录工作目录）"
      + (unknown ? ` · ${unknown} 条归入「${UNKNOWN_PROJECT}」，多为升级前采集的老数据，可点右侧重建` : "");
  }

  function renderModelsHint() {
    const n = state.unpriced.length;
    $("#models-hint").textContent =
      "订阅制模型按官方 API 参考价估算，不代表真实扣费；点卡片上的 ✎ 可改价"
      + (n ? ` · ${n} 个模型未定价，点 ✎ 设置` : "");
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
      renderModelsHint();
      renderEntityCards(list, "model", "#model-cards", "model");
    } else if (state.view === "agents") {
      renderEntityCards(list, "agent", "#agent-cards", "agent");
    } else if (state.view === "projects") {
      renderProjectsHint();
      renderEntityCards(list, "project", "#project-cards", "project");
    } else if (state.view === "records") {
      if (state.recGran === "day") renderRecords(list);
      else renderTimeline();
    } else if (state.view === "plans") {
      renderPlans();
    }
  }

  /* ---------- Plan 用量 ---------- */
  function planColor(pct) { return pct >= 85 ? "#fb7185" : pct >= 60 ? "#fbbf24" : "#34d399"; }

  function fmtReset(resetsAt) {
    let s = Math.max(0, Math.round(resetsAt - Date.now() / 1000));
    const d = Math.floor(s / 86400); s -= d * 86400;
    const h = Math.floor(s / 3600); s -= h * 3600;
    const m = Math.floor(s / 60);
    const parts = d ? `${d} 天` : h ? `${h} 小时` : `${m} 分钟`;
    return `${parts}后重置`;
  }

  /* actions：窗口右上角的操作区 HTML（目前只有额度估计开关），手动配额不传 */
  function planWinHtml(pct, title, sub, actions) {
    pct = Math.min(100, pct);
    return `
      <div class="plan-win">
        <div class="plan-win-head">
          <span>${title}</span>
          <span class="plan-win-right">${actions || ""}<span style="color:${planColor(pct)}">已用 ${pct}%</span></span>
        </div>
        <div class="plan-bar"><i style="width:${pct}%;background:${planColor(pct)}"></i></div>
        <div class="plan-sub">${sub}</div>
      </div>`;
  }

  /* 额度估计开关：多端时本机观测口径未必干净，让用户自己决定何时锚定、何时干脆不看 */
  function estToggleHtml(w) {
    if (!w.cal_key || w.absolute_hidden) return "";  // 已隐藏绝对值时开关没有意义
    const on = !!w.est_enabled;
    return `<button class="est-toggle${on ? "" : " off"}" data-key="${esc(w.cal_key)}"
      data-enabled="${on ? 1 : 0}"
      title="${on ? "停止后只显示官方百分比与重置时间" : "从当前时刻重新锚定，丢弃之前的校准值"}"
      >${on ? "停止估计" : "开始估计"}</button>`;
  }

  function fmtAgo(ts) {
    if (!ts) return "时间未知";
    const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (s < 90) return "刚刚";
    if (s < 5400) return `${Math.round(s / 60)} 分钟前`;
    if (s < 172800) return `${Math.round(s / 3600)} 小时前`;
    return `${Math.round(s / 86400)} 天前`;
  }

  function renderMultiBanner(data) {
    const box = $("#multi-banner");
    const mode = data.multi_device || "off";
    if (mode === "off") { box.innerHTML = ""; return; }
    if (mode === "local_only") {
      box.innerHTML = `<div class="multi-note warn">⚠ 多端使用但未同步：百分比与重置时间来自官方、仍然准确；由本机日志换算的绝对 token 数会系统性低估，已隐藏。配置共享目录后可恢复。</div>`;
      return;
    }
    const peers = data.peers || [];
    const list = peers.length
      ? peers.map((p) => `${esc(p.device_name)}（${fmtAgo(p.updated)}${p.stale ? " · 可能已离线" : ""}）`).join("、")
      : "还没读到其他设备的数据 —— 确认另一台也已开启合并并指向同一个目录";
    box.innerHTML = `<div class="multi-note ok">⇄ 多端合并已开启 · 其他设备：${list}</div>`;
  }

  async function renderPlans() {
    const box = $("#plan-cards");
    let data;
    try {
      const res = await fetch("/api/plans");
      data = await res.json();
    } catch (e) {
      box.innerHTML = `<div class="card"><div class="card-hint">加载失败，请重试</div></div>`;
      return;
    }
    const cards = [];
    for (const a of data.auto || []) {
      if (a.error) {
        cards.push(`
        <div class="card plan-card">
          <div class="card-head"><div class="card-title">${esc(a.agent)}</div><span class="card-hint">官方接口</span></div>
          <div class="plan-sub">${esc(a.error)}</div>
        </div>`);
        continue;
      }
      const wins = (a.windows || []).map((w) => {
        const subs = [];
        if (w.resets_at) subs.push(fmtReset(w.resets_at));
        const scope = w.measured_remote
          ? `本窗口合计 ${fmtNum(w.measured_tokens)} tokens（本机 ${fmtNum(w.measured_local)} + 其他设备 ${fmtNum(w.measured_remote)}）`
          : `本窗口采集 ${fmtNum(w.measured_tokens)} tokens${w.account_wide ? "" : "（仅本机）"}`;
        subs.push(scope);
        if (w.absolute_hidden) {
          subs.push("多端未同步，绝对额度不可估（百分比与重置时间仍然准确）");
        } else if (w.est_paused) {
          subs.push("额度估计已停止 · 百分比与重置时间仍来自官方，依然准确");
        } else if (w.est_quota) {
          const tag = w.est_carried ? "（沿用上次校准值）" : "";
          subs.push(`估算总额 ≈ ${fmtNum(w.est_quota)}${tag} · 剩余 ≈ ${fmtNum(w.est_remaining)}`);
        } else if (w.est_calibrating) {
          subs.push(`正在重新校准（${fmtAgo(w.est_since)}开始）· 官方百分比再涨 1% 即可给出估算`);
        } else {
          subs.push("暂无足够数据估算绝对额度");
        }
        return planWinHtml(w.used_percent, esc(w.label), subs.join(" · "), estToggleHtml(w));
      }).join("");
      const updated = (a.updated || "").replace("T", " ").slice(5, 16);
      cards.push(`
      <div class="card plan-card">
        <div class="card-head"><div class="card-title">${esc(a.agent)} · ${esc(a.plan_type)}</div><span class="card-hint">${esc(a.note || "官方数据")} · ${esc(updated)}</span></div>
        ${wins}
      </div>`);
    }
    for (const p of data.manual || []) {
      let sub;
      if (p.mode === "percent") {
        if (p.absolute_hidden) {
          sub = `已用约 ${p.pct}%（按 ${p.anchor_pct}% 锚定）· 多端未同步，绝对 token 数不可估`;
        } else {
          sub = p.est_quota
            ? `已用 ${fmtFull(p.used)} · 估算总额 ≈ ${fmtNum(p.est_quota)} · 剩余 ≈ ${fmtNum(p.remaining)}（按 ${p.anchor_pct}% 锚定）`
            : `锚定时本窗口无采集数据，暂无法估算总额（当前已用 ${p.pct}%）`;
        }
      } else {
        sub = `已用 ${fmtFull(p.used)} / 配额 ${fmtFull(p.quota)} · 剩余 ${fmtFull(p.remaining)}`;
        // 固定配额是唯一朝危险方向偏的路径：配额是账号级的，已用却只有本机
        if (p.understated) sub += " ⚠ 已用仅含本机，实际消耗更多、剩余更少";
      }
      cards.push(`
      <div class="card plan-card">
        <div class="card-head"><div class="card-title">${esc(p.name)}</div><button class="icon-btn plan-del" data-id="${p.id}" title="删除配额">✕</button></div>
        ${planWinHtml(p.pct, `${esc(p.agent)} · ${esc(p.period_label)}`, sub)}
      </div>`);
    }
    renderMultiBanner(data);
    if (!cards.length) {
      cards.push(`<div class="card"><div class="card-hint">暂无 Plan 数据：未发现 Codex 限额记录，可点右上「添加配额」手动跟踪 Kimi / Claude 等套餐。</div></div>`);
    }
    box.innerHTML = cards.join("");
  }

  /* ---------- 事件绑定 ---------- */
  function bind() {
    $$(".nav-item").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));

    $$("#range-seg button").forEach((b) => b.addEventListener("click", () => {
      const v = b.dataset.range;
      state.range = v === "today" || v === "custom" ? v : +v;
      $$("#range-seg button").forEach((x) => x.classList.toggle("active", x === b));
      $("#custom-range").hidden = state.range !== "custom";
      if (state.range === "custom") {
        const today = localDateStr(new Date());
        if (!$("#date-to").value) $("#date-to").value = today;
        if (!$("#date-from").value) {
          const d = new Date();
          d.setDate(d.getDate() - 6);
          $("#date-from").value = localDateStr(d);
        }
        state.customFrom = $("#date-from").value;
        state.customTo = $("#date-to").value;
      }
      render();
    }));

    $("#date-from").addEventListener("change", (e) => { state.customFrom = e.target.value; render(); });
    $("#date-to").addEventListener("change", (e) => { state.customTo = e.target.value; render(); });

    $$("#metric-seg button").forEach((b) => b.addEventListener("click", () => {
      state.metric = b.dataset.metric;
      $$("#metric-seg button").forEach((x) => x.classList.toggle("active", x === b));
      render();
    }));

    $("#filter-agent").addEventListener("change", (e) => { state.agent = e.target.value; render(); });
    $("#filter-model").addEventListener("change", (e) => { state.model = e.target.value; render(); });
    $("#filter-project").addEventListener("change", (e) => { state.project = e.target.value; render(); });

    $("#btn-rebuild").addEventListener("click", async (e) => {
      if (!confirm("将清空采集状态并重新扫描全部本机日志（原文件会先备份）。\n手动录入的记录不受影响。继续？")) return;
      const btn = e.currentTarget;
      btn.disabled = true;
      btn.textContent = "⟲ 重扫中…";
      try {
        const res = await fetch("/api/collect/rebuild", { method: "POST" });
        const data = await res.json();
        if (!res.ok) { toast(data.error || "重建失败"); return; }
        const parts = Object.entries(data.summary || {})
          .map(([agent, s]) => `${agent} ${s.events} 条`);
        toast(parts.length ? `重建完成 ｜ ${parts.join("；")}` : "未发现本机 AI 工具日志");
        await loadData();
      } catch (err) {
        toast("重建失败");
      } finally {
        btn.disabled = false;
        btn.textContent = "⟲ 重建采集数据";
      }
    });

    $("#btn-add").addEventListener("click", openModal);
    $("#btn-sync").addEventListener("click", syncData);

    // 自动同步：按选定间隔静默采集一次并刷新（后台本来也有 10 分钟扫描，这里让页面数据更及时）
    let autoSyncTimer = null;
    const INTERVAL_KEY = "tokenlens.syncInterval";
    const selInterval = $("#sel-sync-interval");
    const savedInterval = localStorage.getItem(INTERVAL_KEY);
    if (savedInterval && [...selInterval.options].some((o) => o.value === savedInterval)) {
      selInterval.value = savedInterval;
    }
    const intervalLabel = () => selInterval.options[selInterval.selectedIndex].text;

    function restartAutoSync() {
      clearInterval(autoSyncTimer);
      autoSyncTimer = null;
      if (!$("#chk-auto-sync").checked) return;
      autoSyncTimer = setInterval(async () => {
        try {
          await fetch("/api/collect", { method: "POST" });
          await loadData();
        } catch (err) { /* 静默失败，下轮再试 */ }
      }, Number(selInterval.value));
    }

    $("#chk-auto-sync").addEventListener("change", (e) => {
      restartAutoSync();
      toast(e.target.checked ? `已开启自动同步（每 ${intervalLabel()}）` : "已关闭自动同步");
    });
    selInterval.addEventListener("change", () => {
      localStorage.setItem(INTERVAL_KEY, selInterval.value);
      restartAutoSync();  // 已开启时立即按新间隔生效
      if ($("#chk-auto-sync").checked) toast(`自动同步间隔已调整为 ${intervalLabel()}`);
    });
    $("#modal-close").addEventListener("click", closeModal);
    $("#modal-mask").addEventListener("click", (e) => { if (e.target.id === "modal-mask") closeModal(); });

    // Plan 配额弹窗与删除
    // 多端设置
    const multiMask = $("#multi-modal-mask");
    const multiForm = $("#multi-form");
    const closeMultiModal = () => multiMask.classList.remove("show");
    function syncMultiUI() {
      const mode = $("#multi-mode").value;
      $("#multi-dir-row").hidden = mode !== "sync";
      $("#multi-tip").textContent = MULTI_TIPS[mode] || "";
    }
    function renderMultiPeers(s) {
      const peers = s.peers || [];
      const self = `本机：${esc(s.device?.name || "未命名")}`;
      $("#multi-peers").innerHTML = s.multi_device === "sync"
        ? `<div class="form-tip">${self}；读到 ${peers.length} 台其他设备${peers.length ? "：" + peers.map((p) => `${esc(p.device_name)}（${fmtAgo(p.updated)}，${p.days} 条日聚合）`).join("、") : ""}</div>`
        : `<div class="form-tip">${self}</div>`;
    }
    async function openMultiModal() {
      try {
        const res = await fetch("/api/settings");
        state.settings = await res.json();
      } catch (e) { toast("读取设置失败"); return; }
      const s = state.settings;
      multiForm.device_name.value = s.device?.name || "";
      $("#multi-mode").value = s.multi_device || "off";
      multiForm.sync_dir.value = s.sync_dir || "";
      syncMultiUI();
      renderMultiPeers(s);
      multiMask.classList.add("show");
    }
    $("#btn-multi").addEventListener("click", openMultiModal);
    $("#multi-modal-close").addEventListener("click", closeMultiModal);
    multiMask.addEventListener("click", (e) => { if (e.target === multiMask) closeMultiModal(); });
    $("#multi-mode").addEventListener("change", syncMultiUI);
    multiForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const payload = {
        multi_device: $("#multi-mode").value,
        sync_dir: multiForm.sync_dir.value.trim(),
        device_name: multiForm.device_name.value.trim(),
      };
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { toast(data.error || "保存失败"); return; }
      state.settings = data;
      toast(payload.multi_device === "sync"
        ? `已开启多端合并，导出 ${data.shared} 条`
        : "多端设置已保存");
      closeMultiModal();
      await loadData();
      renderPlans();
    });

    const planMask = $("#plan-modal-mask");
    const closePlanModal = () => planMask.classList.remove("show");
    $("#btn-add-plan").addEventListener("click", () => { buildDatalists(); planMask.classList.add("show"); });
    $("#plan-modal-close").addEventListener("click", closePlanModal);
    planMask.addEventListener("click", (e) => { if (e.target === planMask) closePlanModal(); });
    $("#plan-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      const payload = {
        name: f.pname.value.trim(),
        agent: f.agent.value.trim(),
        period: f.period.value,
        mode: f.mode.value,
      };
      if (payload.mode === "percent") payload.pct = +f.pct.value || 0;
      else payload.quota = +f.quota.value || 0;
      const res = await fetch("/api/plans", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        toast("已保存配额");
        closePlanModal();
        f.reset();
        togglePlanMode();
        renderPlans();
      } else {
        const err = await res.json().catch(() => ({}));
        toast(err.error || "保存失败");
      }
    });
    function togglePlanMode() {
      const percent = $("#plan-mode").value === "percent";
      $("#plan-pct-row").hidden = !percent;
      $("#plan-quota-row").hidden = percent;
      $("#plan-tip").textContent = percent
        ? "百分比可在工具的用量页查看（如 Kimi CLI 输入 /usage）；填入后按本机采集数据锚定估算总额与剩余，之后自动跟踪。"
        : "用量按该 Agent 在当前周期内采集到的 Tokens 合计（含缓存）计算，百分比 = 已用 ÷ 配额。";
    }
    $("#plan-mode").addEventListener("change", togglePlanMode);
    $("#plan-cards").addEventListener("click", async (e) => {
      const del = e.target.closest(".plan-del");
      if (del) {
        const res = await fetch(`/api/plans?id=${del.dataset.id}`, { method: "DELETE" });
        if (res.ok) { toast("已删除配额"); renderPlans(); }
        return;
      }
      const est = e.target.closest(".est-toggle");
      if (!est) return;
      const enable = est.dataset.enabled !== "1";
      if (enable && !confirm("将丢弃已有校准值，从当前时刻重新锚定估算。继续？")) return;
      est.disabled = true;
      try {
        const res = await fetch("/api/quota/estimate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ key: est.dataset.key, enabled: enable }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { toast(data.error || "操作失败"); est.disabled = false; return; }
        toast(enable ? "已从当前时刻重新开始估计" : "已停止额度估计");
        renderPlans();
      } catch (err) {
        toast("操作失败");
        est.disabled = false;
      }
    });
    $("#drawer-close").addEventListener("click", closeDrawer);
    $("#drawer-mask").addEventListener("click", closeDrawer);

    // 消耗趋势：时间粒度切换（按天 / 按小时 / 按分钟）
    const trendDate = $("#trend-date"), trendHour = $("#trend-hour"), trendHint = $("#trend-gran-hint");
    trendHour.innerHTML = hourOptionsHtml();
    state.trendDate = localDateStr(new Date());
    state.trendHour = new Date().getHours();
    trendDate.value = state.trendDate;
    trendHour.value = String(state.trendHour);
    $$("#trend-gran-seg button").forEach((b) => b.addEventListener("click", () => {
      state.trendGran = b.dataset.gran;
      $$("#trend-gran-seg button").forEach((x) => x.classList.toggle("active", x === b));
      trendDate.hidden = state.trendGran === "day";
      trendHour.hidden = state.trendGran !== "minute";
      trendHint.hidden = state.trendGran === "day";
      render();
    }));
    trendDate.addEventListener("change", () => {
      state.trendDate = trendDate.value || localDateStr(new Date());
      render();
    });
    trendHour.addEventListener("change", () => { state.trendHour = +trendHour.value; render(); });

    // 明细记录：时间粒度切换（按天 / 按小时 / 按分钟）
    const recDate = $("#rec-date"), recHour = $("#rec-hour"), recHint = $("#rec-gran-hint");
    recHour.innerHTML = hourOptionsHtml();
    state.recDate = localDateStr(new Date());
    state.recHour = new Date().getHours();
    recDate.value = state.recDate;
    recHour.value = String(state.recHour);
    $$("#rec-gran-seg button").forEach((b) => b.addEventListener("click", () => {
      state.recGran = b.dataset.gran;
      $$("#rec-gran-seg button").forEach((x) => x.classList.toggle("active", x === b));
      recDate.hidden = state.recGran === "day";
      recHour.hidden = state.recGran !== "minute";
      recHint.hidden = state.recGran === "day";
      render();
    }));
    recDate.addEventListener("change", () => {
      state.recDate = recDate.value || localDateStr(new Date());
      render();
    });
    recHour.addEventListener("change", () => { state.recHour = +recHour.value; render(); });

    // 价格编辑弹窗 + 官方价格导入
    const priceMask = $("#price-modal-mask");
    const closePriceModal = () => priceMask.classList.remove("show");
    $("#price-modal-close").addEventListener("click", closePriceModal);
    priceMask.addEventListener("click", (e) => { if (e.target === priceMask) closePriceModal(); });
    $("#price-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      const payload = { model: f.model.value.trim(), input: +f.input.value, output: +f.output.value };
      if (f.cache_read.value !== "") payload.cache_read = +f.cache_read.value;
      const res = await fetch("/api/prices", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        toast("价格已保存，费用已按新价重算");
        closePriceModal();
        await loadData();
      } else {
        const err = await res.json().catch(() => ({}));
        toast(err.error || "保存失败");
      }
    });
    $("#btn-import-prices").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      btn.textContent = "⇓ 正在拉取 models.dev…";
      try {
        const res = await fetch("/api/prices/import", { method: "POST" });
        const data = await res.json();
        if (!res.ok) { toast(data.error || "导入失败"); return; }
        const n = Object.keys(data.matched || {}).length;
        const un = (data.unmatched || []).join("、");
        toast(`已导入 ${n} 个模型的官方价格` + (un ? `；未匹配：${un}` : ""));
        await loadData();
      } catch (err) {
        toast("导入失败：网络异常");
      } finally {
        btn.disabled = false;
        btn.textContent = "⇓ 从 models.dev 导入官方价格";
      }
    });

    document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeDrawer(); closeModal(); closePlanModal(); closePriceModal(); closeMultiModal(); } });

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
