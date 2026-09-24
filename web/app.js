const fmt = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });
let dashboard = null;
let lastDeleted = null;
const FORM_TITLES = {
  exposureForm: "保存敞口",
  hedgeForm: "记录锁汇",
  settlementForm: "保存实际汇率",
};
const CONFIG_PERCENT_FIELDS = new Set([
  "default_hedge_ratio",
  "optimistic_shift_pct",
  "pessimistic_shift_pct",
  "custom_scenario_shift_pct",
]);
const TRADINGVIEW_CNY_CURRENCIES = new Set(["USD", "EUR", "JPY", "HKD", "GBP", "AUD", "SGD", "CAD", "CHF", "NZD"]);
const HEDGE_ACTION_LABELS = {
  sell_foreign: "卖出外币/远期结汇",
  buy_foreign: "买入外币/远期购汇",
};

function showStatus(message, type = "ok") {
  const box = document.getElementById("statusBar");
  box.textContent = message;
  box.className = `status-bar ${type === "ok" ? "" : type}`;
}

function money(value) {
  return fmt.format(Number(value || 0));
}

function hedgeActionLabel(action) {
  return HEDGE_ACTION_LABELS[action] || action || "—";
}

function localToday() {
  const now = new Date();
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 10);
}

function today() {
  return localToday();
}

function isoStamp() {
  return new Date().toISOString().slice(0, 10);
}

function setDefaultDates() {
  document.querySelectorAll('input[type="date"]').forEach((input) => {
    if (!input.value) input.value = today();
  });
}

async function api(path, options = {}) {
  const headers = {
    "Content-Type": "application/json",
    ...(options.headers || {}),
  };
  const response = await fetch(path, {
    ...options,
    headers,
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `Request failed: ${path}`);
  }
  return data;
}

function formData(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  // 未勾选的 checkbox 根本不会出现在 FormData 里，要显式补 false
  if (form.elements.booked) data.booked = form.elements.booked.checked;
  const numericKeys = [
    "amount",
    "probability",
    "locked_rate",
    "actual_rate",
    "actual_amount",
    "rate_cache_hours",
    "risk_limit_cny",
    "default_hedge_ratio",
    "optimistic_shift_pct",
    "pessimistic_shift_pct",
    "custom_scenario_shift_pct",
  ];
  for (const key of numericKeys) {
    if (data[key] !== undefined && data[key] !== "") data[key] = Number(data[key]);
  }
  if (data.probability !== undefined && data.probability !== "") {
    data.probability = Number(data.probability) / 100;
  }
  for (const key of CONFIG_PERCENT_FIELDS) {
    if (data[key] !== undefined && data[key] !== "") data[key] = Number(data[key]) / 100;
  }
  if (data.currency) data.currency = data.currency.toUpperCase();
  return data;
}

function parseConfigJsonFields(form) {
  const parsed = {};
  const errorBox = document.getElementById("configJsonError");
  if (errorBox) errorBox.textContent = "";
  for (const field of form.querySelectorAll("[data-json-field]")) {
    const raw = field.value.trim();
    if (!raw) {
      parsed[field.name] = {};
      continue;
    }
    try {
      const value = JSON.parse(raw);
      const expectsArray = field.dataset.jsonType === "array";
      const valid = expectsArray
        ? Array.isArray(value)
        : value && !Array.isArray(value) && typeof value === "object";
      if (!valid) {
        throw new Error(expectsArray ? "必须是 JSON 数组" : "必须是 JSON 对象");
      }
      parsed[field.name] = value;
    } catch (error) {
      const label = field.closest("label")?.childNodes[0]?.textContent?.trim() || field.name;
      const message = `${label} 解析失败：${error.message}`;
      if (errorBox) errorBox.textContent = message;
      field.focus();
      throw new Error(message);
    }
  }
  return parsed;
}

function configFormData(form) {
  const data = { ...formData(form), ...parseConfigJsonFields(form) };
  data.confirmed_parameters = Object.fromEntries(
    Array.from(form.querySelectorAll("[data-confirmed-key]"))
      .map((field) => [field.dataset.confirmedKey, field.checked]),
  );
  return data;
}

async function loadDashboard() {
  dashboard = await api("/api/state");
  renderDashboard(dashboard);
}

function scenarioAssumptionNote(uniform) {
  if (uniform === false) return "";
  return `
    <div class="item warn-item">
      <strong>情景假设：所有币种同幅同向变动</strong>
      <p class="meta">
        这等于假设币种之间相关性为 1。对"净收美元 + 净付欧元"这类组合，两边会天然对冲，
        把合计风险算小——真实风险来自交叉汇率。要按币种分别设涨跌幅，
        在配置的 <code>scenario_shifts</code> 里写，例如
        <code>{"EUR": {"optimistic": -0.01}}</code>。
      </p>
    </div>
  `;
}

function renderDashboard(data) {
  renderWorkspace(data.workspace || {});
  renderRateStatus(data);
  renderPortfolio(data.portfolio || {});
  renderSuggestions(data.suggestions || [], data.rate_trial_reasons || []);
  renderNetExposure(data.net_exposures || []);
  renderExposureTable(data.exposures || []);
  renderHedgeTable(data.hedges || []);
  renderSettlementTable(data.settlements || []);
  renderScenarioRows(
    data.scenario_rows || [],
    data.scenario_totals || {},
    data.scenario_uniform,
    data.rate_trial_reasons || [],
  );
  renderList("backtestRows", data.backtest || [], renderBacktest);
  renderPlanDrift(data.plan_drift || {});
  renderPlans(data.plans || []);
  renderAudit(data.audit || []);
  renderConfig(data.config || {});
}

function renderWorkspace(workspace) {
  const badge = document.getElementById("workspaceBadge");
  const setup = document.getElementById("setupPanel");
  const file = document.getElementById("dataFilePath");
  const backups = document.getElementById("backupStatus");
  const mode = workspace.data_mode || "unknown";
  if (badge) {
    badge.textContent = `${mode === "sample" ? "样例数据" : mode === "empty" ? "空白工作区" : "真实工作区"} · ${workspace.data_file || ""}`;
    badge.title = badge.textContent;
    badge.className = `workspace-badge workspace-${mode}`;
  }
  if (setup) setup.hidden = Boolean(workspace.setup_complete);
  if (file) file.textContent = workspace.data_file ? `当前文件：${workspace.data_file}` : "";
  if (backups) backups.textContent = `可用备份：${workspace.backup_count || 0} 个`;
}

function rateStatusText(status) {
  return {
    live: "实时",
    fallback: "内置兜底",
    cached_after_refresh_error: "刷新失败·用缓存",
  }[status] || status || "-";
}

function fmtTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("zh-CN", { hour12: false });
}

function ratioBar(ratio) {
  const pct = Math.max(0, Math.min(1, Number(ratio || 0))) * 100;
  return `<span class="bar"><span class="bar-fill" style="width:${pct.toFixed(1)}%"></span></span>`;
}

function kpiCard(label, value, note, tone) {
  return `
    <div class="kpi${tone ? " kpi-" + tone : ""}">
      <span class="kpi-label">${label}</span>
      <strong class="kpi-value">${value}</strong>
      <span class="kpi-note">${note || ""}</span>
    </div>
  `;
}

function renderPortfolio(portfolio) {
  const box = document.getElementById("portfolioCards");
  const scope = document.getElementById("portfolioScope");
  const tbody = document.getElementById("portfolioByCurrency");
  const note = document.getElementById("portfolioNote");
  const badge = document.getElementById("todoCount");
  if (!box) return;

  const rows = portfolio.by_currency || [];
  if (!rows.length) {
    box.innerHTML = '<div class="kpi"><span class="kpi-label">暂无敞口</span><strong class="kpi-value">—</strong><span class="kpi-note">先在「录入」里添加一笔</span></div>';
    tbody.innerHTML = "";
    scope.textContent = "";
    note.textContent = "";
    if (badge) badge.textContent = "";
    return;
  }

  scope.textContent = `${portfolio.currency_count} 个币种 · ${portfolio.period_count} 个期间 · ${portfolio.leg_count} 组敞口`;
  box.innerHTML = [
    kpiCard("业务敞口合计", money(portfolio.gross_exposure_cny) + " CNY", "各币种取绝对值后相加"),
    kpiCard("已锁合计", money(portfolio.locked_cny) + " CNY", `已锁比例 ${ratioText(portfolio.locked_ratio)}`),
    kpiCard("剩余敞口", money(portfolio.net_exposure_cny) + " CNY",
      `币种间天然对冲 ${money(portfolio.natural_offset_cny)} 后净额 ${money(portfolio.net_after_offset_cny)}`, "warn"),
    kpiCard("待锁建议", money(portfolio.recommended_cny) + " CNY", `${portfolio.pending_count} 条建议待处理`, "todo"),
  ].join("");

  tbody.innerHTML = rows.map((row) => `
    <tr>
      <td>${escapeHtml(row.currency)}</td>
      <td>${money(row.gross_cny)}</td>
      <td>${money(row.locked_cny)}</td>
      <td>${money(row.net_cny)}</td>
      <td>${ratioBar(row.locked_ratio)} ${ratioText(row.locked_ratio)}</td>
      <td>${money(row.recommended_cny)}</td>
    </tr>
  `).join("");

  const missing = portfolio.rate_missing || [];
  const offsetNote = ` 净额口径（假设各币种同向变动、允许天然对冲）为 ${money(portfolio.net_after_offset_cny)} CNY，` +
    "两个数答的是不同问题：绝对值口径问「风险量级多大」，净额口径问「真正还敞着多少」。";
  note.textContent = missing.length
    ? `各币种取绝对值后相加，不同币种不互相抵消。${missing.join("、")} 暂无汇率，未计入合计。` + offsetNote
    : "各币种取绝对值后相加，不同币种不互相抵消——净收美元和净付欧元是两个独立的风险。" + offsetNote;

  if (badge) badge.textContent = portfolio.pending_count ? String(portfolio.pending_count) : "";
}

function setupSideNav() {
  const links = Array.from(document.querySelectorAll(".sidenav a[data-nav]"));
  if (!links.length) return;
  const sections = links
    .map((link) => document.getElementById(link.dataset.nav))
    .filter(Boolean);

  const activate = (id) => {
    links.forEach((link) => {
      link.classList.toggle("active", link.dataset.nav === id);
    });
  };

  if ("IntersectionObserver" in window) {
    const seen = new Map();
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => seen.set(entry.target.id, entry));
        const visible = Array.from(seen.values())
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible.length) activate(visible[0].target.id);
      },
      { rootMargin: "-72px 0px -55% 0px", threshold: 0 },
    );
    sections.forEach((section) => observer.observe(section));
  }

  links.forEach((link) => {
    link.addEventListener("click", () => activate(link.dataset.nav));
  });
  activate(links[0].dataset.nav);
}

function renderRateStatus(data) {
  const rates = data.rates || {};
  document.getElementById("rateStatus").textContent =
    `汇率：${rateStatusText(rates.status)} · 更新于 ${fmtTime(rates.fetched_at)}`;
}

function tradingViewTrendUrl(currency) {
  const code = String(currency || "").trim().toUpperCase();
  if (!TRADINGVIEW_CNY_CURRENCIES.has(code)) return "";
  return `https://www.tradingview.com/symbols/${encodeURIComponent(`${code}CNY`)}/`;
}

function trendLink(item) {
  const href = tradingViewTrendUrl(item.currency);
  if (!href) return "";
  return `
    <p class="meta trend-link">
      <a href="${href}" target="_blank" rel="noopener noreferrer">查看汇率走势</a>
      <span>行情为参考价，真正可执行价格以银行远期报价为准。</span>
    </p>
  `;
}

function renderSuggestions(items, rateTrialReasons = []) {
  const box = document.getElementById("suggestions");
  box.innerHTML = "";
  if (!items.length) {
    const reason = rateTrialReasons.length
      ? `暂无可执行建议：${rateTrialReasons.map(escapeHtml).join("；")}。请先刷新实时汇率。`
      : "暂无建议。先添加敞口。";
    box.innerHTML = `<div class="card">${reason}</div>`;
    return;
  }
  items.forEach((item) => {
    const card = document.createElement("div");
    card.className = "card";
    const ratioLine = item.effective_hedge_ratio !== undefined && item.effective_hedge_ratio !== item.target_hedge_ratio
      ? `${ratioText(item.target_hedge_ratio)} → 实际 ${ratioText(item.effective_hedge_ratio)}`
      : ratioText(item.target_hedge_ratio);
    card.innerHTML = `
      <strong>${escapeHtml(item.period)} ${escapeHtml(item.currency)}</strong>${item.past_due ? ' <span class="warn-tag" title="到期日已过，这笔敞口还挂在建议里，说明没有被处理掉">已过期</span>' : ""}${item.direction_unexpected ? ' <span class="warn-tag" title="净敞口方向和配置的企业类型相反，多半是录入有误，方向仍按净敞口走">方向异常</span>' : ""}
      <p>${escapeHtml(item.plain_text)}</p>
      ${renderForecastBlock(item)}
      ${item.trial ? `<p class="notice notice-warn">试算建议：${(item.trial_reasons || []).map(escapeHtml).join("；")}</p>` : ""}
      <p class="meta">剩余敞口：${money(item.net_exposure)}，目标套保比例：${ratioLine}，损益科目：${bucketName(item.accounting_bucket)}</p>
      <p class="meta">建议金额：${money(item.recommended_amount)}，交易汇率：${item.trade_rate}${forwardTag(item)}，人民币风险：${money(item.risk_cny)}</p>
      ${forwardLine(item)}
      ${trendLink(item)}
      ${item.past_due
        ? '<p class="notice notice-warn">到期日已过，按今天的交易日已经下不了这张远期单：请到「结算明细」登记实际结果，或把敞口的到期日改到未来。</p><button type="button" disabled>按建议填入锁汇单</button>'
        : '<button type="button">按建议填入锁汇单</button>'}
    `;
    // 过期敞口预填出来的锁汇单 trade_date > due_date，后端必然拒绝，
    // 按钮点了只会得到一个 400，不如直接禁用并指到结算流程。
    if (!item.past_due) {
      card.querySelector("button").addEventListener("click", () => fillHedgeFromSuggestion(item));
    }
    box.appendChild(card);
  });
}

const FORWARD_BASIS = {
  quote: "银行报价",
  cip: "利差推算",
  spot: "即期兜底",
};

function forwardTag(item) {
  if (!item.forward_basis) return "";
  const cls = item.forward_basis === "spot" ? "warn-tag" : "chip";
  return ` <span class="${cls}" title="${escapeHtml(item.forward_note || "")}">${FORWARD_BASIS[item.forward_basis] || item.forward_basis}</span>`;
}

// 远期结汇不是按即期价成交的，远期点是利差决定的，不是对走势的判断。
function forwardLine(item) {
  if (!item.forward_basis || item.spot_rate === undefined) return "";
  if (item.forward_basis === "spot") {
    return `<p class="meta">即期 ${item.spot_rate}，未取到远期价：${escapeHtml(item.forward_note || "")}</p>`;
  }
  const points = Number(item.forward_points || 0);
  const word = points === 0 ? "持平" : points > 0 ? "升水" : "贴水";
  return `<p class="meta">即期 ${item.spot_rate} → 远期 ${item.forward_rate}（${word} ${Math.abs(points).toFixed(6)}，期限 ${item.tenor_years} 年）</p>`;
}

function renderForecastBlock(item) {
  const s = item.forecast_signal;
  if (!s) return "";
  const tier = s.tier || "reject";
  const dir = item.forecast_direction || s.direction || "flat";
  const chips = [];
  chips.push(`<span class="chip dir-${dir}">${dirText(dir)}</span>`);
  if (s.mape !== null && s.mape !== undefined) {
    chips.push(`<span class="chip">MAPE ${(s.mape * 100).toFixed(1)}%</span>`);
  }
  if (s.direction_accuracy !== null && s.direction_accuracy !== undefined) {
    chips.push(`<span class="chip">方向准确 ${(s.direction_accuracy * 100).toFixed(0)}%</span>`);
  }
  if (s.interval_coverage !== null && s.interval_coverage !== undefined) {
    chips.push(`<span class="chip">区间覆盖 ${(s.interval_coverage * 100).toFixed(0)}%</span>`);
  }
  if (s.trend && (s.trend.direction === "up" || s.trend.direction === "down")) {
    const arrow = s.trend.direction === "up" ? "↑" : "↓";
    chips.push(`<span class="chip">势能 ${arrow} ${s.trend.alignment}/6</span>`);
  }
  chips.push(`<span class="chip tier-${tier}">${tierText(tier)}</span>`);
  const notes = [];
  if (item.forecast_reason) notes.push(item.forecast_reason);
  if (s.tier_reasons && s.tier_reasons.length) notes.push(...s.tier_reasons);
  const reason = notes.length ? `<p class="meta forecast-reason">${notes.map(escapeHtml).join("；")}</p>` : "";
  return `
    <div class="forecast-block">
      <div class="forecast-chips">${chips.join("")}</div>
      ${sparkline(s.forecast, s.current)}
      ${reason}
    </div>
  `;
}

function dirText(dir) {
  return { up: "预测 ↑", down: "预测 ↓", flat: "预测 →" }[dir] || dir;
}

function tierText(tier) {
  return { support: "模型支持", caution: "模型谨慎", reject: "模型不达标" }[tier] || tier;
}

function sparkline(points, currentRate) {
  if (!points || !points.length) return "";
  const vals = (currentRate != null ? [Number(currentRate)] : []).concat(points.map((p) => Number(p.rate)));
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const range = max - min || 1;
  const w = 180;
  const h = 38;
  const stepX = vals.length > 1 ? w / (vals.length - 1) : w;
  const yAt = (v) => (h - ((v - min) / range) * (h - 4)) - 2;
  const path = vals.map((v, i) => `${i === 0 ? "M" : "L"}${(i * stepX).toFixed(1)},${yAt(v).toFixed(1)}`).join(" ");
  return `<svg class="forecast-spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true">
    <path d="${path}" fill="none" stroke="currentColor" stroke-width="1.5"/>
    <circle cx="0" cy="${yAt(vals[0]).toFixed(1)}" r="2.4" fill="currentColor"/>
  </svg>`;
}

function fillHedgeFromSuggestion(item) {
  const form = document.getElementById("hedgeForm");
  form.trade_date.value = today();
  form.due_date.value = item.due_date || `${item.period}-28`;
  form.currency.value = item.currency;
  form.action.value = item.action;
  form.amount.value = item.recommended_amount;
  form.locked_rate.value = item.trade_rate || item.current_rate;
  form.description.value = item.plain_text;
  form.scrollIntoView({ behavior: "smooth", block: "center" });
  showStatus("已按建议填入锁汇单，请确认后保存。");
}

function renderNetExposure(rows) {
  const body = document.getElementById("netExposureRows");
  body.innerHTML = "";
  rows.forEach((row) => {
    const net = Number(row.net_exposure);
    const side = net > 0 ? { label: "净收", cls: "in" } : net < 0 ? { label: "净付", cls: "out" } : { label: "持平", cls: "flat" };
    const rateCell = row.rate_available
      ? `${row.current_rate}`
      : '<span class="warn-tag" title="该币种暂无汇率，人民币金额无法估算">汇率缺失</span>';
    const riskCell = row.rate_available
      ? `${money(row.cny_risk)}${row.over_risk_limit ? ' <span class="warn-tag" title="超过配置的风险阈值，仅作提示">超阈值</span>' : ""}`
      : "—";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(row.period)}${row.past_due ? ' <span class="warn-tag" title="到期日已过">已过期</span>' : ""}${row.direction_unexpected ? ' <span class="warn-tag" title="净敞口方向和配置的企业类型相反，多半是录入有误；方向仍按净敞口走">方向异常</span>' : ""}</td>
      <td>${escapeHtml(row.currency)}</td>
      <td>${riskCategoryCell(row.risk_category, row.risk_category_known)}</td>
      <td>${ratioText(row.target_hedge_ratio)}</td>
      <td>${money(row.business_exposure)}</td>
      <td>${money(row.locked_exposure)}</td>
      <td><span class="net-tag net-${side.cls}">${side.label}</span> ${money(Math.abs(net))}</td>
      <td>${rateCell}</td>
      <td>${riskCell}</td>
    `;
    body.appendChild(tr);
  });
}

function renderScenarioTotals(totals, legCount) {
  const names = ["neutral", "optimistic", "pessimistic", "custom"].filter((n) => totals[n]);
  if (!names.length) return "";
  const rows = names.map((name) => {
    const row = totals[name];
    return `
      <tr>
        <td>${scenarioName(name)}</td>
        <td>${money(row.unrealized_exchange_gain_loss)}</td>
        <td>${money(row.hedge_pnl)}</td>
        <td class="total-cell">${money(row.total_projected_gain_loss)}</td>
      </tr>
    `;
  }).join("");
  const best = totals.optimistic && totals.pessimistic
    ? Math.abs(totals.optimistic.total_projected_gain_loss - totals.pessimistic.total_projected_gain_loss)
    : null;
  const spread = best === null ? "" : `乐观与悲观两端相差 ${money(best)} CNY。`;
  return `
    <div class="item total-item">
      <strong>全部敞口合计（${legCount} 组期间 × 币种）</strong>
      <p class="meta">各币种损益已折成人民币，可直接相加；不同到期日按名义金额相加，未做贴现。${spread}</p>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>场景</th>
              <th>未实现汇兑损益</th>
              <th>套保损益</th>
              <th>合计预计损益</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>
  `;
}

function renderScenarioRows(entries, totals, uniform, rateTrialReasons = []) {
  const box = document.getElementById("scenarioRows");
  box.innerHTML = "";
  if (!entries.length) {
    const reason = rateTrialReasons.length
      ? `暂无可执行情景测算：${rateTrialReasons.map(escapeHtml).join("；")}。请先刷新实时汇率。`
      : "暂无敞口，因此没有预计损益场景。";
    box.innerHTML = `<div class="item">${reason}</div>`;
    return;
  }
  // 先给组合层面的总账，再给逐个期间/币种的明细。
  box.insertAdjacentHTML("beforeend", renderScenarioTotals(totals || {}, entries.length));
  box.insertAdjacentHTML("beforeend", scenarioAssumptionNote(uniform));
  entries.forEach((item) => {
    const rows = Object.entries(item.projection || {}).map(([name, row]) => {
      const bucketValue = row[item.accounting_bucket] || 0;
      return `
        <tr>
          <td>${scenarioName(name)}</td>
          <td>${row.scenario_rate}</td>
          <td>${money(row.unrealized_exchange_gain_loss)}</td>
          <td>${money(bucketValue)}</td>
          <td>${money(row.total_projected_gain_loss)}</td>
        </tr>
      `;
    }).join("");
    const div = document.createElement("div");
    div.className = "item";
    // 建议金额为 0 时套保腿为 0，但敞口本身的浮动损益依然要显示。
    const title = item.has_recommendation
      ? `${escapeHtml(item.period)} ${escapeHtml(item.currency)} 推荐交易预计损益`
      : `${escapeHtml(item.period)} ${escapeHtml(item.currency)} 当前敞口预计损益`;
    const note = item.has_recommendation
      ? `建议金额 ${money(item.recommended_amount)}。`
      : "无新增建议（已达目标套保比例），下表只反映剩余敞口本身的浮动损益。";
    div.innerHTML = `
      <strong>${title}</strong>
      <p class="meta">${note}科目：${bucketName(item.accounting_bucket)}。中性、乐观、悲观、自定义场景按配置的汇率涨跌幅计算。</p>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>场景</th>
              <th>情景汇率</th>
              <th>未实现汇兑损益</th>
              <th>${bucketName(item.accounting_bucket)}</th>
              <th>合计预计损益</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    `;
    box.appendChild(div);
  });
}

function renderList(id, rows, renderItem) {
  const box = document.getElementById(id);
  box.innerHTML = "";
  if (!rows.length) {
    box.innerHTML = '<div class="item">暂无数据。</div>';
    return;
  }
  rows.forEach((row) => box.appendChild(renderItem(row)));
}

function escapeHtml(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function byDueDate(a, b) {
  const left = `${a.due_date || ""}${a.currency || ""}`;
  const right = `${b.due_date || ""}${b.currency || ""}`;
  return left.localeCompare(right);
}

// 明细用表格：字段各占一列，能横向比对，不再把信息塞进句子里。
function renderDetailTable(tbodyId, countId, rows, columns, collection, emptyText) {
  const body = document.getElementById(tbodyId);
  const count = document.getElementById(countId);
  if (!body) return;
  if (count) count.textContent = rows.length ? `共 ${rows.length} 条` : "";
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="${columns.length + 1}" class="empty-row">${emptyText}</td></tr>`;
    return;
  }
  body.innerHTML = "";
  rows.slice().sort(byDueDate).forEach((row) => {
    const tr = document.createElement("tr");
    const cells = columns.map((col) => {
      const value = col.render(row);
      return `<td${col.cls ? ` class="${col.cls}"` : ""}>${value}</td>`;
    }).join("");
    tr.innerHTML = `${cells}<td class="col-action"><button type="button" class="row-edit">编辑</button> <button type="button" class="row-copy">复制</button> <button type="button" class="row-delete">删除</button></td>`;
    const title = `${row.due_date || ""} ${row.currency || ""} ${money(row.amount)}`;
    tr.querySelector(".row-edit").addEventListener("click", () => startEdit(collection, row));
    tr.querySelector(".row-copy").addEventListener("click", () => copyRow(collection, row));
    tr.querySelector(".row-delete").addEventListener("click", async () => {
      if (!window.confirm(`确认删除这条记录？\n${title}`)) return;
      await runAction("正在删除...", async () => {
        await api(`/api/${collection}/${row.id}`, { method: "DELETE" });
        lastDeleted = { collection, row: snapshotRecord(row) };
        updateUndoButton();
        await loadDashboard();
        showStatus("已删除。可在「数据管理」里撤销最近一次删除。");
      });
    });
    body.appendChild(tr);
  });
}

function cloneRecord(row) {
  const clone = { ...row };
  delete clone.id;
  delete clone.created_at;
  return clone;
}

function snapshotRecord(row) {
  return JSON.parse(JSON.stringify(row));
}

async function copyRow(collection, row) {
  await runAction("正在复制记录...", async () => {
    await api(`/api/${collection}`, { method: "POST", body: JSON.stringify(cloneRecord(row)) });
    await loadDashboard();
    showStatus("已复制为一条新记录。");
  });
}

function updateUndoButton() {
  const button = document.getElementById("undoDeleteBtn");
  if (button) button.disabled = !lastDeleted;
}

function clearUndoState() {
  lastDeleted = null;
  updateUndoButton();
}

// 导入/恢复/清空/换样例之后，旧工作区的编辑态不能留着：替换后的工作区
// 常常带着同样的稳定 ID（恢复备份、重载样例都是），此时提交一张陈旧的
// 编辑表单会静默覆盖新工作区里的那条记录。
function resetActiveEdits() {
  for (const id of ["exposureForm", "hedgeForm", "settlementForm"]) {
    const form = document.getElementById(id);
    if (form && form.dataset.editId) resetEdit(form);
  }
}

function afterWorkspaceReplaced() {
  clearUndoState();
  resetActiveEdits();
}

async function undoDeleted() {
  if (!lastDeleted) {
    showStatus("没有可撤销的删除。", "error");
    return;
  }
  const { collection, row } = lastDeleted;
  await runAction("正在撤销删除...", async () => {
    await api(`/api/${collection}`, { method: "POST", body: JSON.stringify(row) });
    lastDeleted = null;
    updateUndoButton();
    await loadDashboard();
    showStatus("已撤销最近一次删除。");
  });
}

function startEdit(collection, row) {
  const formId = { exposures: "exposureForm", hedges: "hedgeForm", settlements: "settlementForm" }[collection];
  const form = document.getElementById(formId);
  if (!form) return;
  form.reset();
  setDefaultDates();
  form.dataset.editId = row.id;
  Object.entries(row).forEach(([key, value]) => {
    if (!form.elements[key]) return;
    if (form.elements[key].type === "checkbox") {
      form.elements[key].checked = Boolean(value);
    } else if (key === "probability") {
      form.elements[key].value = Math.round(Number(value == null ? 1 : value) * 100);
    } else {
      form.elements[key].value = value == null ? "" : value;
    }
  });
  const button = form.querySelector('button[type="submit"]');
  if (button) button.textContent = "保存修改";
  ensureCancelEditButton(form);
  form.scrollIntoView({ behavior: "smooth", block: "center" });
  showStatus("已载入记录，修改后点保存。", "busy");
}

function ensureCancelEditButton(form) {
  if (form.querySelector(".cancel-edit")) return;
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "secondary cancel-edit";
  cancel.textContent = "取消编辑";
  cancel.addEventListener("click", () => resetEdit(form));
  form.appendChild(cancel);
}

function resetEdit(form) {
  delete form.dataset.editId;
  const button = form.querySelector('button[type="submit"]');
  if (button) button.textContent = FORM_TITLES[form.id] || button.textContent;
  const cancel = form.querySelector(".cancel-edit");
  if (cancel) cancel.remove();
  form.reset();
  setDefaultDates();
  if (form.id === "exposureForm") {
    categoryTouched = false;
    updateCategoryHint();
  }
}

// ---- category:begin ---- 与 web_app.suggest_category 逐行对应，改动必须两侧同步
function suggestCategory(row) {
  if (row.booked) {
    return ["balance_sheet", "已入账/已开票的外币资产负债，属于资产负债表套保"];
  }
  var probability = row.probability;
  probability = (probability === null || probability === undefined) ? 1 : Number(probability);
  if (!isFinite(probability)) probability = 1;
  if (probability >= 1) {
    return ["order_contract", "金额已确定但尚未入账，属于合同/订单套保"];
  }
  return ["cash_flow", "发生概率 " + Math.round(probability * 100) + "%，属于高度可能的预期交易，走现金流套保"];
}
// ---- category:end ----

// 让财务自己在下拉框里选，他多半选不对——不是因为不认真，而是这个分类
// 本来就该由凭证形态推出来，不该靠人记规则。
// 这里只给推荐和理由，不自动改选择——分歧要看得见，不是悄悄替人做主。
let categoryTouched = false;

function updateCategoryHint() {
  const form = document.getElementById("exposureForm");
  const hint = document.getElementById("categoryHint");
  if (!form || !hint) return;
  const row = {
    booked: form.booked.checked,
    probability: form.probability.value === "" ? 1 : Number(form.probability.value),
  };
  const [suggested, reason] = suggestCategory(row);
  // 用户没动过下拉框就跟着推荐走；一旦手动改过就再也不替他改，
  // 只把分歧显示出来。默认值和推荐打架是没必要的噪音。
  if (!categoryTouched && form.category.value !== suggested) {
    form.category.value = suggested;
  }
  const chosen = form.category.value;
  if (chosen === suggested) {
    hint.innerHTML = `推荐：<b>${escapeHtml(riskCategoryName(suggested))}</b>——${escapeHtml(reason)}`;
    hint.className = "notice";
  } else {
    hint.innerHTML = `按录入的信息，推荐是<b>${escapeHtml(riskCategoryName(suggested))}</b>（${escapeHtml(reason)}），` +
      `你选的是<b>${escapeHtml(riskCategoryName(chosen))}</b>。会计科目会按你选的走。`;
    hint.className = "notice notice-warn";
  }
}

function renderExposureTable(rows) {
  renderDetailTable("exposureRows", "exposureCount", rows, [
    { render: (row) => escapeHtml(row.due_date) },
    { render: (row) => escapeHtml(row.currency) },
    {
      render: (row) => row.direction === "receipt"
        ? '<span class="net-tag net-in">收</span> 未来收外币'
        : '<span class="net-tag net-out">付</span> 未来付外币',
    },
    { render: (row) => money(row.amount), cls: "num" },
    { render: (row) => ratioText(row.probability == null ? 1 : row.probability), cls: "num" },
    {
      render: (row) => {
        const cell = riskCategoryCell(row.category);
        const suggested = row.suggested_category;
        if (!suggested || suggested === row.category) return cell;
        return `${cell} <span class="warn-tag" title="${escapeHtml(row.suggestion_reason || "")}">推荐 ${escapeHtml(riskCategoryName(suggested))}</span>`;
      },
    },
    { render: (row) => (row.booked ? "是" : "否") },
    { render: (row) => escapeHtml(row.description || "—"), cls: "col-note" },
  ], "exposures", "还没有敞口，先到「录入」里添加一笔。");
}

function renderHedgeTable(rows) {
  renderDetailTable("hedgeRows", "hedgeCount", rows, [
    { render: (row) => escapeHtml(row.trade_date) },
    { render: (row) => escapeHtml(row.due_date) },
    { render: (row) => escapeHtml(row.currency) },
    {
      render: (row) => escapeHtml(hedgeActionLabel(row.action)),
    },
    { render: (row) => money(row.amount), cls: "num" },
    { render: (row) => escapeHtml(row.locked_rate), cls: "num" },
    { render: (row) => escapeHtml(row.description || "—"), cls: "col-note" },
  ], "hedges", "还没有锁汇记录。");
}

function renderSettlementTable(rows) {
  renderDetailTable("settlementRows", "settlementCount", rows, [
    { render: (row) => escapeHtml(row.due_date) },
    { render: (row) => escapeHtml(row.currency) },
    { render: (row) => escapeHtml(row.actual_rate), cls: "num" },
    { render: (row) => row.actual_amount == null ? "—" : money(row.actual_amount), cls: "num" },
    { render: (row) => escapeHtml(row.description || "—"), cls: "col-note" },
  ], "settlements", "还没有结算记录。");
}

function renderBacktest(row) {
  const div = document.createElement("div");
  div.className = "item";
  const cls = row.hedge_effect_cny >= 0 ? "positive" : "negative";
  const settled = row.settled !== false;
  const tag = settled ? "" : ' <span class="warn-tag" title="尚未录入到期实际汇率，按当前市场价试算">试算</span>';
  const rateLine = settled
    ? `实际汇率 ${row.actual_rate}，${row.reference_rate == null ? "当前市场参考汇率缺失" : `参考汇率 ${row.reference_rate}`}`
    : `未录入实际汇率，按当前市场价 ${row.reference_rate} 试算`;
  div.innerHTML = `
    <strong>${escapeHtml(row.period)} ${escapeHtml(row.currency)}</strong>${tag}
    <p>${escapeHtml(row.plain_text)}</p>
    <p>锁汇贡献${settled ? "" : "（试算）"}：<span class="${cls}">${money(row.hedge_effect_cny)} CNY</span>
      <span class="meta">（对到期即期，交易员口径）</span></p>
    <p class="meta">业务敞口 ${money(row.business_exposure)}，${rateLine}</p>
    ${renderBenchmark(row.benchmark)}
  `;
  return div;
}

const AUDIT_ACTIONS = {
  create: "新增",
  delete: "删除",
  update: "修改",
  reset: "恢复样例",
  freeze: "冻结方案",
  clear: "清空",
  import: "导入",
  restore: "恢复备份",
};

const AUDIT_COLLECTIONS = {
  exposures: "敞口",
  hedges: "锁汇",
  settlements: "到期汇率",
  config: "配置",
  workspace: "整个工作区",
  plans: "方案",
};

// 配置里有三个字典型的键（interest_rates / forward_overrides / scenario_shifts），
// 直接 String() 会渲染成 [object Object]——审计日志号称是唯一能回答
// "把哪条改成了什么"的地方，偏偏对新加的这几个键答不上来。
function auditValue(value) {
  if (value === null || value === undefined) return "空";
  if (typeof value === "object") {
    const text = JSON.stringify(value);
    return text.length > 120 ? text.slice(0, 117) + "…" : text;
  }
  return String(value);
}

function auditSummary(row) {
  if (row.collection === "config") {
    const changes = Object.entries(row.after || {})
      .map(([key, value]) => `${escapeHtml(key)}：${escapeHtml(auditValue(value.from))} → ${escapeHtml(auditValue(value.to))}`);
    return changes.length ? changes.join("；") : "无实际变化";
  }
  if (row.collection === "workspace") {
    if (row.action === "clear") return "业务数据已清空";
    if (row.action === "import") return "工作区已从 JSON 导入";
    if (row.action === "restore") return "工作区已从备份恢复";
    if (row.action === "reset") {
      const mode = (row.after || {}).metadata?.data_mode;
      return mode === "empty" ? "工作区已重置为空白数据" : "工作区已重置为样例数据";
    }
    return "工作区已更新";
  }
  if (row.collection === "plans") {
    // 方案的载荷里没有 due_date / currency / amount，走下面那个分支会得到空白，
    // 于是"变更记录"答不出删掉的是哪一份方案。
    const body = row.after || row.before || {};
    const label = body.label ? escapeHtml(body.label) : "(未命名)";
    const count = body.rows === undefined ? "" : `，${escapeHtml(auditValue(body.rows))} 条建议`;
    return `${label}${count}`;
  }
  const body = row.after || row.before || {};
  const parts = [body.due_date, body.currency, body.amount == null ? null : money(body.amount)]
    .filter(Boolean)
    .map(escapeHtml);
  const note = body.description ? `（${escapeHtml(body.description)}）` : "";
  return parts.join(" ") + note;
}

function renderAudit(rows) {
  const body = document.getElementById("auditRows");
  const scope = document.getElementById("auditScope");
  if (!body) return;
  if (scope) scope.textContent = rows.length ? `最近 ${rows.length} 条` : "";
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="4" class="empty-row">还没有任何改动。</td></tr>';
    return;
  }
  body.innerHTML = rows.map((row) => `
    <tr>
      <td>${escapeHtml(fmtTime(row.at))}</td>
      <td>${escapeHtml(AUDIT_ACTIONS[row.action] || row.action)}</td>
      <td>${escapeHtml(AUDIT_COLLECTIONS[row.collection] || row.collection)}</td>
      <td class="col-note" title="${auditSummary(row).replace(/<[^>]*>/g, "")}">${auditSummary(row)}</td>
    </tr>
  `).join("");
}

// 司库口径：结汇均价 vs 当月月均汇率。企业财务按月均记账和考核，
// 所以这才是甲方真正会问的比法；上面那个锁汇贡献比的是到期即期价。
// 敞口本身也会不准。订单黄了一半的损失跟汇率没关系，
// 混在价差里看就会把"订单缩水"记到套保决策头上。
function renderVariance(v) {
  if (!v) {
    return '<p class="meta">未录入实际发生额，只能算价差，量差无从拆分。</p>';
  }
  const gap = Number(v.volume_gap);
  const word = gap === 0 ? "与计划一致" : gap > 0 ? "多于计划" : "少于计划";
  const over = v.over_hedged
    ? `<span class="warn-tag" title="敞口没发生但远期已经锁了，远期照样要交割">超额套保 ${money(v.over_hedged_notional)}</span>`
    : "";
  return `
    <p class="meta">
      价量分解：实际发生 ${money(v.actual_notional)}（计划 ${money(v.planned_notional)}，${word}
      ${ratioText(Math.abs(v.volume_gap_pct))}）——
      <b>量差 ${money(v.volume_variance_cny)}</b>（敞口没按计划发生，跟汇率无关）、
      <b>价差 ${money(v.price_variance_cny)}</b>（结汇均价相对月均），合计 ${money(v.total_variance_cny)} CNY。 ${over}
    </p>
  `;
}

function renderBenchmark(bench) {
  if (!bench) {
    return '<p class="meta">无月均基准：这个币种没有本地行情序列，也没在配置里录入财务月均汇率。</p>';
  }
  const total = Number(bench.vs_benchmark_cny);
  const cls = total >= 0 ? "positive" : "negative";
  const word = total >= 0 ? "好于" : "差于";
  const provisional = bench.settled === false
    ? ' <span class="warn-tag" title="到期实际汇率还没录入，结汇均价和归因都是按当前市场价试算的">试算</span>'
    : "";
  return `
    <div class="bench-block">
      <p>司库口径${provisional}：结汇均价 <b>${bench.realized_avg_rate}</b>，当月月均 <b>${bench.average_rate}</b>，
        合计<span class="${cls}">${word} ${money(Math.abs(total))} CNY</span></p>
      <p class="meta">
        拆开看：套保效应 ${money(bench.hedge_effect_cny)}（锁汇把价格从到期即期挪到了结汇均价）、
        择时效应 ${money(bench.timing_effect_cny)}（市场自己从月均走到到期即期，跟锁不锁无关）。
        套保覆盖 ${ratioText(bench.hedge_coverage)}，基准来源：${escapeHtml(bench.average_source || "-")}。
      </p>
      ${renderVariance(bench.variance)}
    </div>
  `;
}

const DECISION_LABELS = {
  enterprise_type: "企业类型",
  default_hedge_ratio: "默认套保比例",
  month_currency_hedge_ratios: "分月份/币种套保比例",
  interest_rates: "利率（影响远期价）",
  forward_overrides: "远期报价",
  supported_currencies: "支持币种",
  strategy_type: "策略类型",
  optimistic_shift_pct: "乐观涨跌幅",
  pessimistic_shift_pct: "悲观涨跌幅",
  custom_scenario_shift_pct: "自定义涨跌幅",
  scenario_shifts: "分币种情景",
};

// 快照真正的用处不是存档，是回头告诉你"参数已经漂了"。
// 只有影响建议金额的参数变了才算过期；改情景涨跌幅只影响损益模拟。
function renderPlanDrift(drift) {
  const box = document.getElementById("planDrift");
  if (!box) return;
  if (!drift.has_plan) {
    box.innerHTML = "";
    return;
  }
  const parts = [];
  const decision = Object.entries(drift.decision_changed || {});
  const scenario = Object.entries(drift.scenario_changed || {});
  const recommendation = Object.entries(drift.recommendation_changed || {});
  const rates = Object.entries(drift.rate_moved || {});
  const isStale = Boolean(drift.stale) || Boolean(recommendation.length);

  if (decision.length) {
    parts.push(`<p><b>影响建议金额的参数已改：</b>${decision.map(([key, change]) =>
      `${escapeHtml(DECISION_LABELS[key] || key)} ${escapeHtml(auditValue(change.from))} → ${escapeHtml(auditValue(change.to))}`
    ).join("；")}</p>`);
  }
  if (scenario.length) {
    parts.push(`<p class="meta">只影响损益模拟、不影响建议金额的改动：${scenario.map(([key]) =>
      escapeHtml(DECISION_LABELS[key] || key)).join("、")}</p>`);
  }
  if (recommendation.length) {
    const labels = {
      status: "建议状态",
      action: "操作方向",
      forecast_multiplier: "预测折扣",
      effective_hedge_ratio: "有效套保比例",
      recommended_amount: "建议金额",
    };
    parts.push(`<p><b>冻结方案里的建议已变：</b>${recommendation.map(([rowKey, changes]) => {
      const fields = Object.entries(changes || {}).map(([field, change]) => {
        const from = field === "action" ? hedgeActionLabel(change.from) : auditValue(change.from);
        const to = field === "action" ? hedgeActionLabel(change.to) : auditValue(change.to);
        return `${escapeHtml(labels[field] || field)} ${escapeHtml(from)} → ${escapeHtml(to)}`;
      });
      return `${escapeHtml(rowKey)}：${fields.join("、")}`;
    }).join("；")}</p>`);
  }
  const signals = Object.entries(drift.signal_changed || {});
  if (signals.length) {
    parts.push(`<p><b>预测信号已变（折扣会跟着变）：</b>${signals.map(([currency, change]) =>
      `${escapeHtml(currency)} ${escapeHtml(auditValue(change.from))} → ${escapeHtml(auditValue(change.to))}`
    ).join("；")}</p>`);
  }
  if (rates.length) {
    parts.push(`<p class="meta">汇率相对快照的变动：${rates.map(([currency, move]) =>
      `${escapeHtml(currency)} ${(move.move * 100).toFixed(2)}%`).join("、")}</p>`);
  }
  if (!parts.length) {
    box.innerHTML = `<div class="item">最近方案「${escapeHtml(drift.label || "-")}」的参数与当前一致。</div>`;
    return;
  }
  box.innerHTML = `
    <div class="item ${isStale ? "warn-item" : ""}">
      <strong>${isStale ? "当前建议已经不是方案里的那份" : "方案与当前建议一致"}</strong>
      <p class="meta">对比对象：「${escapeHtml(drift.label || "-")}」，冻结于 ${escapeHtml(fmtTime(drift.created_at))}</p>
      ${parts.join("")}
    </div>
  `;
}

function renderPlans(rows) {
  const box = document.getElementById("planRows");
  const scope = document.getElementById("plansScope");
  if (!box) return;
  if (scope) scope.textContent = rows.length ? `最近 ${rows.length} 份` : "";
  if (!rows.length) {
    box.innerHTML = '<div class="item">还没有冻结过方案。在「待锁汇」里点「冻结为方案」。</div>';
    return;
  }
  box.innerHTML = "";
  rows.forEach((plan) => {
    const div = document.createElement("div");
    div.className = "item";
    div.dataset.planId = plan.id || "";
    const lines = (plan.rows || []).map((row) => `
      <tr>
        <td>${escapeHtml(row.period)} ${escapeHtml(row.currency)}</td>
        <td>${escapeHtml(hedgeActionLabel(row.action))}</td>
        <td class="num">${ratioText(row.target_hedge_ratio)}</td>
        <td class="num">${Number(row.forecast_multiplier).toFixed(2)}×</td>
        <td class="num">${money(row.recommended_amount)}</td>
        <td class="num">${escapeHtml(row.trade_rate)}</td>
      </tr>
    `).join("");
    div.innerHTML = `
      <strong>${escapeHtml(plan.label)}</strong>
      <p class="meta">冻结于 ${escapeHtml(fmtTime(plan.created_at))}，
        默认套保比例 ${ratioText((plan.config || {}).default_hedge_ratio)}，
        汇率取自 ${escapeHtml((plan.rate_snapshot || {}).status || "-")}</p>
      <p class="notice plan-print-note">方案为冻结快照，行情与参数仅用于当时建议复盘；交易执行仍以银行远期报价、内部授权和实际成交为准。</p>
      <div class="table-wrap">
        <table>
          <thead><tr><th>期间/币种</th><th>建议动作</th><th>目标比例</th><th>折扣</th><th>建议金额</th><th>交易汇率</th></tr></thead>
          <tbody>${lines}</tbody>
        </table>
      </div>
      <div class="plan-row-actions">
        <button type="button" class="secondary plan-print-btn">打印/导出 PDF</button>
        <button type="button" class="secondary plan-delete-btn">删除这份方案</button>
      </div>
    `;
    div.querySelector(".plan-print-btn").addEventListener("click", () => printPlan(plan.id));
    div.querySelector(".plan-delete-btn").addEventListener("click", async () => {
      if (!window.confirm(`确认删除方案「${plan.label}」？此操作无法撤销。`)) return;
      await runAction("正在删除...", async () => {
        await api(`/api/plans/${plan.id}`, { method: "DELETE" });
        await loadDashboard();
        showStatus("方案已删除。");
      });
    });
    box.appendChild(div);
  });
}

function printPlan(planId) {
  document.querySelectorAll("#planRows .item").forEach((item) => {
    item.classList.toggle("print-target", item.dataset.planId === String(planId || ""));
  });
  document.body.dataset.printPlan = String(planId || "");
  const clear = () => {
    delete document.body.dataset.printPlan;
    document.querySelectorAll("#planRows .item").forEach((item) => item.classList.remove("print-target"));
    window.removeEventListener("afterprint", clear);
  };
  window.addEventListener("afterprint", clear);
  window.print();
  window.setTimeout(clear, 60000);
}

function renderConfig(config) {
  const form = document.getElementById("configForm");
  form.rate_api_url.value = config.rate_api_url || "";
  form.rate_cache_hours.value = config.rate_cache_hours || 24;
  form.risk_limit_cny.value = config.risk_limit_cny ?? 200000;
  form.enterprise_type.value = config.enterprise_type || "comprehensive";
  for (const key of CONFIG_PERCENT_FIELDS) {
    form.elements[key].value = config[key] == null ? "" : Number(config[key]) * 100;
  }
  for (const field of form.querySelectorAll("[data-json-field]")) {
    const fallback = field.dataset.jsonType === "array" ? [] : {};
    field.value = JSON.stringify(config[field.name] || fallback, null, 2);
  }
  const confirmed = config.confirmed_parameters || {};
  for (const field of form.querySelectorAll("[data-confirmed-key]")) {
    field.checked = Boolean(confirmed[field.dataset.confirmedKey]);
  }
}

const RISK_CATEGORY_NAMES = {
  balance_sheet: "资产负债表套保",
  cash_flow: "现金流套保",
  order_contract: "合同/订单套保",
};

function riskCategoryName(value) {
  return RISK_CATEGORY_NAMES[value] || value || "-";
}

// 表外类目是历史数据留下的，会计科目只能兜底到公允价值变动，
// 这里显式标出来而不是装作认识它。
function riskCategoryCell(value, known) {
  const known_ = known === undefined ? value in RISK_CATEGORY_NAMES : known;
  if (known_) return escapeHtml(riskCategoryName(value));
  return `${escapeHtml(value || "-")} <span class="warn-tag" title="不是合法的风险类型，会计科目按公允价值变动兜底；建议删除后重新录入">未知类目</span>`;
}

function bucketName(value) {
  return {
    derivative_investment_income: "衍生品投资收益",
    fair_value_change_gain_loss: "衍生品公允价值变动损益",
    realized_exchange_gain_loss: "已实现汇兑损益",
  }[value] || value || "-";
}

function scenarioName(value) {
  return {
    neutral: "中性",
    optimistic: "乐观",
    pessimistic: "悲观",
    custom: "自定义",
  }[value] || value;
}

function ratioText(value) {
  return `${Math.round(Number(value || 0) * 10000) / 100}%`;
}

function bindSetupPanel() {
  const empty = document.getElementById("startEmptyBtn");
  const sample = document.getElementById("startSampleBtn");
  if (empty) empty.addEventListener("click", async () => {
    await runAction("正在创建空白工作区...", async () => {
      await api("/api/workspace/empty", { method: "POST", body: "{}" });
      afterWorkspaceReplaced();
      await loadDashboard();
      showStatus("空白工作区已就绪。");
    });
  });
  if (sample) sample.addEventListener("click", async () => {
    await runAction("正在加载样例...", async () => {
      await api("/api/workspace/sample", {
        method: "POST",
        body: JSON.stringify({ today: today() }),
      });
      afterWorkspaceReplaced();
      await loadDashboard();
      showStatus("样例数据已加载。");
    });
  });
}

function bindDataManagement() {
  const exportBtn = document.getElementById("exportWorkspaceBtn");
  const importFile = document.getElementById("importWorkspaceFile");
  const exportCsvBtn = document.getElementById("exportCsvBtn");
  const importCsvFile = document.getElementById("importCsvFile");
  const exportXlsxBtn = document.getElementById("exportXlsxBtn");
  const importXlsxFile = document.getElementById("importXlsxFile");
  const restoreBtn = document.getElementById("restoreLatestBackupBtn");
  const clearBtn = document.getElementById("clearBusinessBtn");
  const undoBtn = document.getElementById("undoDeleteBtn");
  if (exportBtn) exportBtn.addEventListener("click", exportWorkspace);
  if (importFile) importFile.addEventListener("change", importWorkspace);
  if (exportCsvBtn) exportCsvBtn.addEventListener("click", exportCsv);
  if (importCsvFile) importCsvFile.addEventListener("change", importCsv);
  if (exportXlsxBtn) exportXlsxBtn.addEventListener("click", exportXlsx);
  if (importXlsxFile) importXlsxFile.addEventListener("change", importXlsx);
  bindCsvCollectionSelectors();
  if (restoreBtn) restoreBtn.addEventListener("click", restoreLatestBackup);
  if (clearBtn) clearBtn.addEventListener("click", clearBusinessData);
  if (undoBtn) {
    undoBtn.addEventListener("click", undoDeleted);
    updateUndoButton();
  }
}

function bindCsvCollectionSelectors() {
  const selects = Array.from(document.querySelectorAll("[data-csv-collection-select]"));
  selects.forEach((select) => {
    select.addEventListener("change", () => {
      selects.forEach((other) => {
        if (other !== select) other.value = select.value;
      });
    });
  });
}

function selectedCsvCollection() {
  const select = document.querySelector("[data-csv-collection-select]");
  return select ? select.value : "exposures";
}

async function exportWorkspace() {
  await runAction("正在导出工作区...", async () => {
    const exported = await api("/api/workspace/export");
    const blob = new Blob([JSON.stringify(exported, null, 2)], { type: "application/json" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `fx-workspace-${isoStamp()}.json`;
    link.click();
    URL.revokeObjectURL(link.href);
    showStatus("工作区 JSON 已导出。");
  });
}

async function importWorkspace(event) {
  const file = event.currentTarget.files && event.currentTarget.files[0];
  if (!file) return;
  try {
    const parsed = JSON.parse(await file.text());
    if (!window.confirm("导入会覆盖当前工作区；系统会先自动备份当前数据。确认继续？")) return;
    await runAction("正在导入工作区...", async () => {
      await api("/api/import", { method: "POST", body: JSON.stringify(parsed) });
      afterWorkspaceReplaced();
      await loadDashboard();
      showStatus("工作区已导入。");
    });
  } catch (error) {
    showStatus(`导入失败：${error.message}`, "error");
  } finally {
    event.currentTarget.value = "";
  }
}

async function exportCsv() {
  const collection = selectedCsvCollection();
  await runAction("正在导出明细 CSV...", async () => {
    const response = await fetch(`/api/csv/export?collection=${encodeURIComponent(collection)}`);
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try {
        const payload = await response.json();
        message = payload.error || message;
      } catch (_) {
        message = await response.text() || message;
      }
      throw new Error(message);
    }
    const blob = new Blob([await response.text()], { type: "text/csv;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${collection}-${isoStamp()}.csv`;
    link.click();
    URL.revokeObjectURL(link.href);
    showStatus("明细 CSV 已导出。");
  });
}

async function importCsv(event) {
  const file = event.currentTarget.files && event.currentTarget.files[0];
  if (!file) return;
  const collection = selectedCsvCollection();
  try {
    const csv = await file.text();
    if (!window.confirm("导入 CSV 会追加到所选类型的当前明细；系统会先全量校验，失败不会落盘。确认继续？")) return;
    await runAction("正在导入明细 CSV...", async () => {
      await api("/api/csv/import", { method: "POST", body: JSON.stringify({ collection, csv }) });
      await loadDashboard();
      showStatus("明细 CSV 已导入。");
    });
  } catch (error) {
    showStatus(`CSV 导入失败：${error.message}`, "error");
  } finally {
    event.currentTarget.value = "";
  }
}

async function exportXlsx() {
  const collection = selectedCsvCollection();
  await runAction("正在导出明细 Excel...", async () => {
    const response = await fetch(`/api/xlsx/export?collection=${encodeURIComponent(collection)}`);
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try {
        const payload = await response.json();
        message = payload.error || message;
      } catch (_) {
        message = await response.text() || message;
      }
      throw new Error(message);
    }
    const blob = await response.blob();
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${collection}-${isoStamp()}.xlsx`;
    link.click();
    URL.revokeObjectURL(link.href);
    showStatus("明细 Excel 已导出。");
  });
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let index = 0; index < bytes.length; index += 1) {
    binary += String.fromCharCode(bytes[index]);
  }
  return btoa(binary);
}

async function importXlsx(event) {
  const file = event.currentTarget.files && event.currentTarget.files[0];
  if (!file) return;
  const collection = selectedCsvCollection();
  try {
    if (!window.confirm("导入 Excel 会追加到所选类型的当前明细；系统会先全量校验，失败不会落盘。确认继续？")) return;
    const data_b64 = arrayBufferToBase64(await file.arrayBuffer());
    await runAction("正在导入明细 Excel...", async () => {
      await api("/api/xlsx/import", { method: "POST", body: JSON.stringify({ collection, data_b64 }) });
      await loadDashboard();
      showStatus("明细 Excel 已导入。");
    });
  } catch (error) {
    showStatus(`Excel 导入失败：${error.message}`, "error");
  } finally {
    event.currentTarget.value = "";
  }
}

async function restoreLatestBackup() {
  if (!window.confirm("恢复最近备份会覆盖当前工作区；系统会先自动备份当前数据。确认继续？")) return;
  await runAction("正在恢复最近备份...", async () => {
    await api("/api/backups/latest/restore", { method: "POST", body: "{}" });
    afterWorkspaceReplaced();
    await loadDashboard();
    showStatus("最近备份已恢复。");
  });
}

async function clearBusinessData() {
  if (!window.confirm("清空会删除当前敞口、锁汇和结算记录；系统会先自动备份当前数据。确认继续？")) return;
  await runAction("正在清空业务数据...", async () => {
    await api("/api/clear-business", { method: "POST", body: "{}" });
    afterWorkspaceReplaced();
    await loadDashboard();
    showStatus("业务数据已清空。");
  });
}

function bindForms() {
  const exposureForm = document.getElementById("exposureForm");
  bindDataManagement();
  bindSetupPanel();
  // 顺序要紧：category 的处理必须先把 categoryTouched 置上再刷新提示。
  // 反过来注册的话，用户第一次改下拉框时 updateCategoryHint 先跑，
  // 那时 categoryTouched 还是 false，选择会被"跟随推荐"的逻辑弹回去——
  // 用户得改两次才生效，正好跟"手动改过就再也不改"相反。
  ["probability", "booked"].forEach((name) => {
    const field = exposureForm.elements[name];
    if (field) field.addEventListener("change", updateCategoryHint);
  });
  exposureForm.elements.category.addEventListener("change", () => {
    categoryTouched = true;
    updateCategoryHint();
  });
  exposureForm.elements.probability.addEventListener("input", updateCategoryHint);
  updateCategoryHint();

  document.getElementById("freezePlanBtn").addEventListener("click", async () => {
    const label = document.getElementById("planLabel").value.trim();
    await runAction("正在冻结方案...", async () => {
      await api("/api/plans", { method: "POST", body: JSON.stringify({ label }) });
      document.getElementById("planLabel").value = "";
      await loadDashboard();
      showStatus("方案已冻结。之后改配置不会再动它。");
    });
  });

  document.getElementById("exposureForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    await submitForm(event.currentTarget, "/api/exposures", "敞口已保存", () => {
      // submitForm 里会 form.reset()，把下拉框打回第一项、概率打回 1。
      // 不同步重算的话，下一笔敞口会被静默存成 balance_sheet，
      // 而页面上还挂着上一笔的提示。
      categoryTouched = false;
      updateCategoryHint();
      document.getElementById("exposureListPanel").scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });

  document.getElementById("hedgeForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    await submitForm(event.currentTarget, "/api/hedges", "锁汇记录已保存");
  });

  document.getElementById("settlementForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    await submitForm(event.currentTarget, "/api/settlements", "实际汇率已保存");
  });

  document.getElementById("configForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    await submitForm(event.currentTarget, "/api/config", "配置已保存", null, false, configFormData);
  });

  document.getElementById("refreshRatesBtn").addEventListener("click", async () => {
    await runAction("正在刷新汇率...", async () => {
      await api("/api/rates/refresh", { method: "POST", body: "{}" });
      await loadDashboard();
      showStatus("汇率已刷新");
    });
  });

  document.getElementById("resetDemoBtn").addEventListener("click", async () => {
    if (!window.confirm("恢复样例会覆盖当前敞口、锁汇、结算记录和配置参数；系统会先自动备份当前工作区。确认继续？")) return;
    await runAction("正在恢复样例...", async () => {
      await api("/api/reset-demo", {
        method: "POST",
        body: JSON.stringify({ today: today() }),
      });
      afterWorkspaceReplaced();
      await loadDashboard();
      showStatus("样例数据已恢复");
    });
  });

  document.querySelectorAll("form").forEach((form) => {
    form.addEventListener(
      "invalid",
      () => showStatus("有必填项没有填完整，请检查表单。", "error"),
      true,
    );
  });
}

async function submitForm(form, path, successMessage, afterSuccess = null, reset = true, payloadBuilder = formData) {
  if (!form.reportValidity()) {
    showStatus("有必填项没有填完整，请检查表单。", "error");
    return;
  }
  const button = form.querySelector('button[type="submit"]');
  await runAction("正在保存...", async () => {
    if (button) button.disabled = true;
    const editId = form.dataset.editId;
    const target = editId ? `${path}/${encodeURIComponent(editId)}` : path;
    await api(target, { method: editId ? "PUT" : "POST", body: JSON.stringify(payloadBuilder(form)) });
    if (reset) {
      form.reset();
      setDefaultDates();
    }
    if (editId) resetEdit(form);
    await loadDashboard();
    showStatus(editId ? "修改已保存" : successMessage);
    if (afterSuccess) afterSuccess();
    if (button) button.disabled = false;
  }, () => {
    if (button) button.disabled = false;
  });
}

async function runAction(busyMessage, action, finallyAction = null) {
  try {
    showStatus(busyMessage, "busy");
    await action();
  } catch (error) {
    showStatus(error.message, "error");
  } finally {
    if (finallyAction) finallyAction();
  }
}

bindForms();
setupSideNav();
setDefaultDates();
showStatus("正在加载数据...", "busy");
loadDashboard()
  .then(() => showStatus("数据已就绪。"))
  .catch((error) => {
    showStatus(error.message, "error");
  });
