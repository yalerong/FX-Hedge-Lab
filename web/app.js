const fmt = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });
let dashboard = null;

function showStatus(message, type = "ok") {
  const box = document.getElementById("statusBar");
  box.textContent = message;
  box.className = `status-bar ${type === "ok" ? "" : type}`;
}

function money(value) {
  return fmt.format(Number(value || 0));
}

function today() {
  return new Date().toISOString().slice(0, 10);
}

function setDefaultDates() {
  document.querySelectorAll('input[type="date"]').forEach((input) => {
    if (!input.value) input.value = today();
  });
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `Request failed: ${path}`);
  }
  return data;
}

function formData(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  const numericKeys = [
    "amount",
    "probability",
    "locked_rate",
    "actual_rate",
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
  if (data.currency) data.currency = data.currency.toUpperCase();
  return data;
}

async function loadDashboard() {
  dashboard = await api("/api/state");
  renderDashboard(dashboard);
}

function renderDashboard(data) {
  renderRateStatus(data);
  renderSuggestions(data.suggestions || []);
  renderNetExposure(data.net_exposures || []);
  renderList("exposureRows", data.exposures || [], renderExposure);
  renderList("hedgeRows", data.hedges || [], renderHedge);
  renderScenarioRows(data.suggestions || []);
  renderList("backtestRows", data.backtest || [], renderBacktest);
  renderConfig(data.config || {});
}

function renderRateStatus(data) {
  const rates = data.rates || {};
  document.getElementById("rateStatus").textContent =
    `Status: ${rates.status || "-"}, updated: ${rates.fetched_at || "-"}`;
}

function renderSuggestions(items) {
  const box = document.getElementById("suggestions");
  box.innerHTML = "";
  if (!items.length) {
    box.innerHTML = '<div class="card">暂无建议。先添加敞口。</div>';
    return;
  }
  items.forEach((item) => {
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <strong>${item.period} ${item.currency}</strong>
      <p>${item.plain_text}</p>
      <p class="meta">剩余敞口：${money(item.net_exposure)}，目标套保比例：${ratioText(item.target_hedge_ratio)}，损益科目：${bucketName(item.accounting_bucket)}</p>
      <p class="meta">建议金额：${money(item.recommended_amount)}，交易汇率：${item.trade_rate}，人民币风险：${money(item.risk_cny)}</p>
      <button type="button">按建议填入锁汇单</button>
    `;
    card.querySelector("button").addEventListener("click", () => fillHedgeFromSuggestion(item));
    box.appendChild(card);
  });
}

function fillHedgeFromSuggestion(item) {
  const form = document.getElementById("hedgeForm");
  form.trade_date.value = today();
  form.due_date.value = `${item.period}-28`;
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
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${row.period}</td>
      <td>${row.currency}</td>
      <td>${riskCategoryName(row.risk_category)}</td>
      <td>${ratioText(row.target_hedge_ratio)}</td>
      <td>${money(row.business_exposure)}</td>
      <td>${money(row.locked_exposure)}</td>
      <td class="${row.net_exposure >= 0 ? "positive" : "negative"}">${money(row.net_exposure)}</td>
      <td>${row.current_rate}</td>
      <td>${money(row.cny_risk)}</td>
    `;
    body.appendChild(tr);
  });
}

function renderScenarioRows(suggestions) {
  const box = document.getElementById("scenarioRows");
  box.innerHTML = "";
  if (!suggestions.length) {
    box.innerHTML = '<div class="item">暂无推荐交易，因此没有预计损益场景。</div>';
    return;
  }
  suggestions.forEach((item) => {
    const rows = Object.entries(item.scenario_projection || {}).map(([name, row]) => {
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
    div.innerHTML = `
      <strong>${item.period} ${item.currency} 推荐交易预计损益</strong>
      <p class="meta">科目：${bucketName(item.accounting_bucket)}。中性、乐观、悲观、自定义场景按配置的汇率涨跌幅计算。</p>
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

function itemShell(row, title, detail, collection) {
  const div = document.createElement("div");
  div.className = "item";
  div.innerHTML = `
    <strong>${title}</strong>
    <p>${detail}</p>
    <p class="meta">${row.description || ""}</p>
    <button type="button" class="secondary">删除</button>
  `;
  div.querySelector("button").addEventListener("click", async () => {
    await api(`/api/${collection}/${row.id}`, { method: "DELETE" });
    await loadDashboard();
    showStatus("已删除。");
  });
  return div;
}

function renderExposure(row) {
  const direction = row.direction === "receipt" ? "未来收外币" : "未来付外币";
  return itemShell(
    row,
    `${row.due_date} ${row.currency} ${money(row.amount)}`,
    `${direction}，${riskCategoryName(row.category)}，概率 ${ratioText(row.probability || 1)}`,
    "exposures",
  );
}

function renderHedge(row) {
  const action = row.action === "sell_foreign" ? "卖出外币/远期结汇" : "买入外币/远期购汇";
  return itemShell(
    row,
    `${row.due_date} ${row.currency} ${money(row.amount)}`,
    `${action}，锁定汇率 ${row.locked_rate}`,
    "hedges",
  );
}

function renderBacktest(row) {
  const div = document.createElement("div");
  div.className = "item";
  const cls = row.hedge_effect_cny >= 0 ? "positive" : "negative";
  div.innerHTML = `
    <strong>${row.period} ${row.currency}</strong>
    <p>${row.plain_text}</p>
    <p>锁汇贡献：<span class="${cls}">${money(row.hedge_effect_cny)} CNY</span></p>
    <p class="meta">业务敞口 ${money(row.business_exposure)}，实际汇率 ${row.actual_rate}，参考汇率 ${row.reference_rate}</p>
  `;
  return div;
}

function renderConfig(config) {
  const form = document.getElementById("configForm");
  form.rate_api_url.value = config.rate_api_url || "";
  form.rate_cache_hours.value = config.rate_cache_hours || 24;
  form.risk_limit_cny.value = config.risk_limit_cny || 200000;
  form.enterprise_type.value = config.enterprise_type || "comprehensive";
  form.default_hedge_ratio.value = config.default_hedge_ratio ?? 0.8;
  form.optimistic_shift_pct.value = config.optimistic_shift_pct ?? 0.03;
  form.pessimistic_shift_pct.value = config.pessimistic_shift_pct ?? -0.03;
  form.custom_scenario_shift_pct.value = config.custom_scenario_shift_pct ?? 0.01;
}

function riskCategoryName(value) {
  return {
    balance_sheet: "资产负债表套保",
    cash_flow: "现金流套保",
    order_contract: "合同/订单套保",
  }[value] || value || "-";
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

function bindForms() {
  document.getElementById("exposureForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    await submitForm(event.currentTarget, "/api/exposures", "敞口已保存", () => {
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
    await submitForm(event.currentTarget, "/api/config", "配置已保存", null, false);
  });

  document.getElementById("refreshRatesBtn").addEventListener("click", async () => {
    await runAction("正在刷新汇率...", async () => {
      await api("/api/rates/refresh", { method: "POST", body: "{}" });
      await loadDashboard();
      showStatus("汇率已刷新");
    });
  });

  document.getElementById("resetDemoBtn").addEventListener("click", async () => {
    await runAction("正在恢复样例...", async () => {
      await api("/api/reset-demo", { method: "POST", body: "{}" });
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

async function submitForm(form, path, successMessage, afterSuccess = null, reset = true) {
  if (!form.reportValidity()) {
    showStatus("有必填项没有填完整，请检查表单。", "error");
    return;
  }
  const button = form.querySelector('button[type="submit"]');
  await runAction("正在保存...", async () => {
    if (button) button.disabled = true;
    await api(path, { method: "POST", body: JSON.stringify(formData(form)) });
    if (reset) {
      form.reset();
      setDefaultDates();
    }
    await loadDashboard();
    showStatus(successMessage);
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
setDefaultDates();
loadDashboard().catch((error) => {
  showStatus(error.message, "error");
});
