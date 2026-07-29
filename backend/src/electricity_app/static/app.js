"use strict";

const DATA_INSUFFICIENT = "数据不足";
const dashboardElement = document.querySelector("#dashboard");
const trendStatus = document.querySelector("#trend-status");
const hourlyStatus = document.querySelector("#hourly-status");
const detailStatus = document.querySelector("#detail-status");
const pageError = document.querySelector("#page-error");
const chartError = document.querySelector("#chart-error");
const detailDate = document.querySelector("#detail-date");
const dayCache = new Map();

let dashboardData = null;
let trendChart = null;
let hourlyChart = null;
let activeDayRequest = 0;

function setText(selector, value) {
  const element = document.querySelector(selector);
  if (element) {
    element.textContent = value;
  }
}

function finiteNumber(value) {
  if (value === null || value === undefined || value === "") {
    return null;
  }
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function formatNumber(value, digits = 2) {
  const number = finiteNumber(value);
  if (number === null) {
    return DATA_INSUFFICIENT;
  }
  return new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(number);
}

async function fetchJson(path) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });
  if (response.status === 401) {
    window.location.assign("/wechat/entry");
    throw new Error("unauthorized");
  }
  if (!response.ok) {
    throw new Error(`request failed: ${response.status}`);
  }
  return response.json();
}

function renderSummary(data) {
  const balance = finiteNumber(data.balance);
  const estimate = finiteNumber(data.estimated_days_remaining);
  setText(
    "#balance-value",
    balance === null ? DATA_INSUFFICIENT : `¥ ${formatNumber(balance)}`,
  );
  setText("#today-energy-value", `${formatNumber(data.today_energy)} kWh`);
  setText("#today-cost-value", `¥ ${formatNumber(data.today_cost)}`);
  setText(
    "#estimated-days-value",
    estimate === null ? DATA_INSUFFICIENT : `${formatNumber(estimate, 1)} 天`,
  );

  const comparison = finiteNumber(data.day_change_percent);
  if (comparison === null) {
    setText("#comparison-text", DATA_INSUFFICIENT);
  } else if (comparison === 0) {
    setText("#comparison-text", "今日用电与昨日持平");
  } else {
    const direction = comparison > 0 ? "增加" : "减少";
    setText(
      "#comparison-text",
      `今日用电较昨日${direction} ${formatNumber(Math.abs(comparison), 1)}%`,
    );
  }

  const recentComparison = finiteNumber(
    data.recent_seven_day_change_percent,
  );
  if (recentComparison === null) {
    setText("#recent-mean-comparison", DATA_INSUFFICIENT);
  } else if (recentComparison === 0) {
    setText("#recent-mean-comparison", "今日用电与近 7 日均值持平");
  } else {
    const direction = recentComparison > 0 ? "增加" : "减少";
    setText(
      "#recent-mean-comparison",
      `今日用电较近 7 日均值${direction} ${formatNumber(
        Math.abs(recentComparison),
        1,
      )}%`,
    );
  }

  const typicalHour = finiteNumber(data.typical_historical_peak_hour);
  if (typicalHour === null) {
    setText("#typical-peak-text", DATA_INSUFFICIENT);
  } else {
    const start = String(typicalHour).padStart(2, "0");
    const end = String((typicalHour + 1) % 24).padStart(2, "0");
    setText("#typical-peak-text", `典型高峰 ${start}:00–${end}:00`);
  }

  const staleBanner = document.querySelector("#stale-banner");
  staleBanner.hidden = !data.is_stale;
  if (data.is_stale) {
    staleBanner.textContent = "数据超过 90 分钟未更新";
  }

  if (data.last_successful_sync) {
    const syncDate = new Date(data.last_successful_sync);
    setText(
      "#last-sync",
      Number.isNaN(syncDate.getTime())
        ? "同步时间不可用"
        : `上次同步 ${syncDate.toLocaleString("zh-CN", {
            month: "numeric",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
          })}`,
    );
  } else {
    setText("#last-sync", "暂无同步记录");
  }
}

function renderAnomalies(anomalies) {
  const list = document.querySelector("#anomaly-list");
  list.replaceChildren();
  const messages = {
    high_vs_baseline: "今日用电明显高于近 7 天同期",
    continuous_night_load: "夜间检测到持续用电",
  };

  if (!Array.isArray(anomalies) || anomalies.length === 0) {
    const item = document.createElement("li");
    item.className = "empty-state";
    item.textContent = "暂未发现异常";
    list.append(item);
    return;
  }

  anomalies.forEach((anomaly) => {
    const item = document.createElement("li");
    item.className = "anomaly-item";
    item.textContent = messages[anomaly] || "检测到用电异常";
    list.append(item);
  });
}

function chartBaseOption() {
  const reduceMotion = (
    typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
  return {
    animation: !reduceMotion,
    animationDuration: reduceMotion ? 0 : 350,
    textStyle: {
      color: "#9eabc0",
      fontFamily: 'Inter, "PingFang SC", "Microsoft YaHei", sans-serif',
    },
    grid: { top: 26, right: 12, bottom: 31, left: 45 },
    tooltip: {
      trigger: "axis",
      backgroundColor: "#172236",
      borderColor: "#4e6a69",
      textStyle: { color: "#f6f8fc" },
    },
    xAxis: {
      type: "category",
      axisLine: { lineStyle: { color: "#42516b" } },
      axisLabel: { color: "#9eabc0", hideOverlap: true },
    },
    yAxis: {
      type: "value",
      name: "kWh",
      nameTextStyle: { color: "#9eabc0" },
      splitLine: { lineStyle: { color: "#243047" } },
      axisLabel: { color: "#9eabc0" },
    },
  };
}

function renderTrend(labels, energyValues, costValues) {
  const hasValues = [...energyValues, ...costValues].some(
    (value) => value !== 0,
  );
  trendStatus.textContent = hasValues ? "" : "所选时段暂无用电记录";
  if (!trendChart) {
    return;
  }
  const base = chartBaseOption();
  trendChart.setOption({
    ...base,
    grid: { ...base.grid, right: 48 },
    legend: {
      data: ["用电量", "费用"],
      textStyle: { color: "#9eabc0" },
      top: 0,
    },
    xAxis: { ...base.xAxis, data: labels },
    yAxis: [
      base.yAxis,
      {
        ...base.yAxis,
        name: "元",
        position: "right",
        splitLine: { show: false },
      },
    ],
    series: [
      {
        name: "用电量",
        type: "line",
        yAxisIndex: 0,
        data: energyValues,
        smooth: true,
        showSymbol: energyValues.length <= 7,
        symbolSize: 7,
        lineStyle: { color: "#5ee6be", width: 3 },
        itemStyle: { color: "#5ee6be" },
        areaStyle: { color: "rgba(94, 230, 190, 0.13)" },
      },
      {
        name: "费用",
        type: "line",
        yAxisIndex: 1,
        data: costValues,
        smooth: true,
        showSymbol: costValues.length <= 7,
        symbolSize: 7,
        lineStyle: { color: "#f4bd5b", width: 2 },
        itemStyle: { color: "#f4bd5b" },
      },
    ],
  }, true);
}

function localIsoDate(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function dashboardDay() {
  const buckets = dashboardData && Array.isArray(dashboardData.recent_buckets)
    ? dashboardData.recent_buckets
    : [];
  const latest = buckets.at(-1);
  const latestDate = latest ? new Date(latest.start) : new Date();
  return Number.isNaN(latestDate.getTime()) ? new Date() : latestDate;
}

async function getDay(day) {
  if (!dayCache.has(day)) {
    dayCache.set(day, fetchJson(`/api/day/${day}`));
  }
  try {
    return await dayCache.get(day);
  } catch (error) {
    dayCache.delete(day);
    throw error;
  }
}

function selectedRangeData(range) {
  const key = range === "24h" ? "range_24h" : `range_${range}`;
  const value = dashboardData ? dashboardData[key] : null;
  return value && Array.isArray(value.points) ? value : null;
}

function renderRange(range) {
  const rangeData = selectedRangeData(range);
  if (!rangeData) {
    setText("#range-energy-total", DATA_INSUFFICIENT);
    setText("#range-cost-total", DATA_INSUFFICIENT);
    setText("#highest-day-text", DATA_INSUFFICIENT);
    renderTrend([], [], []);
    return;
  }

  setText(
    "#range-energy-total",
    `${formatNumber(rangeData.total_energy)} kWh`,
  );
  setText(
    "#range-cost-total",
    `¥ ${formatNumber(rangeData.total_cost)}`,
  );
  const highestEnergy = finiteNumber(rangeData.highest_use_day_energy);
  setText(
    "#highest-day-text",
    rangeData.highest_use_day && highestEnergy !== null
      ? `最高用电日 ${rangeData.highest_use_day} · ${formatNumber(
        highestEnergy,
      )} kWh`
      : DATA_INSUFFICIENT,
  );

  const labels = rangeData.points.map((point) => {
    if (range === "24h") {
      const instant = new Date(point.label);
      return Number.isNaN(instant.getTime())
        ? "—"
        : instant.toLocaleTimeString("zh-CN", {
            hour: "2-digit",
            minute: "2-digit",
            hour12: false,
          });
    }
    return typeof point.label === "string"
      ? point.label.slice(5)
      : "—";
  });
  const energyValues = rangeData.points.map(
    (point) => finiteNumber(point.energy) || 0,
  );
  const costValues = rangeData.points.map(
    (point) => finiteNumber(point.cost) || 0,
  );
  renderTrend(labels, energyValues, costValues);
}

function selectRange(button) {
  const range = button.dataset.range;
  document.querySelectorAll(".range-button").forEach((candidate) => {
    const selected = candidate === button;
    candidate.classList.toggle("is-active", selected);
    candidate.setAttribute("aria-pressed", String(selected));
  });
  renderRange(range);
}

function renderHourly(data) {
  const buckets = Array.isArray(data.hourly_profile)
    ? data.hourly_profile
    : [];
  const labels = buckets.map((bucket, hour) => `${String(hour).padStart(2, "0")}时`);
  const values = buckets.map((bucket) => finiteNumber(bucket.energy) || 0);
  hourlyStatus.textContent = values.some((value) => value > 0)
    ? ""
    : "今日暂无用电记录";
  if (!hourlyChart) {
    return;
  }
  hourlyChart.setOption({
    ...chartBaseOption(),
    xAxis: {
      ...chartBaseOption().xAxis,
      data: labels,
      axisLabel: { color: "#9eabc0", interval: 3 },
    },
    series: [{
      name: "用电量",
      type: "bar",
      data: values,
      itemStyle: {
        color: "#5ee6be",
        borderRadius: [4, 4, 0, 0],
      },
    }],
  }, true);
}

function appendDetailEmpty(message) {
  const row = document.createElement("tr");
  const cell = document.createElement("td");
  cell.colSpan = 3;
  cell.className = "empty-state";
  cell.textContent = message;
  row.append(cell);
  document.querySelector("#detail-table-body").append(row);
}

function renderDayDetail(detail) {
  const body = document.querySelector("#detail-table-body");
  body.replaceChildren();
  const buckets = Array.isArray(detail.buckets) ? detail.buckets : [];
  setText(
    "#detail-summary",
    `${detail.day} · ${formatNumber(detail.total_energy)} kWh · ¥ ${formatNumber(detail.total_cost)}`,
  );
  detailStatus.textContent = "";
  if (buckets.length === 0) {
    appendDetailEmpty("当日暂无用电记录");
    return;
  }

  buckets.forEach((bucket) => {
    const row = document.createElement("tr");
    const timeCell = document.createElement("td");
    const energyCell = document.createElement("td");
    const costCell = document.createElement("td");
    const instant = new Date(bucket.start);
    timeCell.textContent = Number.isNaN(instant.getTime())
      ? "—"
      : instant.toLocaleTimeString("zh-CN", {
          hour: "2-digit",
          minute: "2-digit",
          hour12: false,
        });
    energyCell.textContent = `${formatNumber(bucket.energy)} kWh`;
    costCell.textContent = `¥ ${formatNumber(bucket.cost)}`;
    row.append(timeCell, energyCell, costCell);
    body.append(row);
  });
}

async function loadDayDetail(day) {
  const requestId = ++activeDayRequest;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) {
    return;
  }
  detailStatus.textContent = "正在加载明细…";
  try {
    const detail = await getDay(day);
    if (requestId === activeDayRequest) {
      renderDayDetail(detail);
    }
  } catch (error) {
    if (
      requestId === activeDayRequest
      && error.message !== "unauthorized"
    ) {
      detailStatus.textContent = "明细加载失败，请稍后重试";
      document.querySelector("#detail-table-body").replaceChildren();
      appendDetailEmpty("暂时无法读取明细");
    }
  }
}

function renderDashboardFailure() {
  setText("#balance-value", "暂时无法读取");
  setText("#today-energy-value", "暂时无法读取");
  setText("#today-cost-value", "暂时无法读取");
  setText("#estimated-days-value", "暂时无法读取");
  setText("#comparison-text", DATA_INSUFFICIENT);
  setText("#trend-status", "趋势暂时无法读取");
  setText("#hourly-status", "小时分布暂时无法读取");
  setText("#detail-summary", DATA_INSUFFICIENT);
  setText("#detail-status", "明细暂时无法读取");

  const anomalyList = document.querySelector("#anomaly-list");
  anomalyList.replaceChildren();
  const anomalyState = document.createElement("li");
  anomalyState.className = "empty-state";
  anomalyState.textContent = "异常信息暂时无法读取";
  anomalyList.append(anomalyState);

  document.querySelector("#detail-table-body").replaceChildren();
  appendDetailEmpty("明细暂时无法读取");
}

async function initializeDashboard() {
  try {
    dashboardData = await fetchJson("/api/dashboard");
    renderSummary(dashboardData);
    renderAnomalies(dashboardData.anomalies);
    try {
      if (!window.echarts) {
        throw new Error("chart library unavailable");
      }
      trendChart = window.echarts.init(
        document.querySelector("#trend-chart"),
      );
      hourlyChart = window.echarts.init(
        document.querySelector("#hourly-chart"),
      );
    } catch (error) {
      chartError.textContent = "图表组件加载失败，文字数据仍可查看。";
      chartError.hidden = false;
      trendChart = null;
      hourlyChart = null;
    }
    selectRange(document.querySelector('[data-range="24h"]'));
    renderHourly(dashboardData);

    const initialDay = localIsoDate(dashboardDay());
    detailDate.max = initialDay;
    detailDate.value = initialDay;
    await loadDayDetail(initialDay);
  } catch (error) {
    if (error.message !== "unauthorized") {
      pageError.hidden = false;
      renderDashboardFailure();
    }
  } finally {
    dashboardElement.setAttribute("aria-busy", "false");
  }
}

document.querySelectorAll(".range-button").forEach((button) => {
  button.addEventListener("click", () => selectRange(button));
});

detailDate.addEventListener("change", () => loadDayDetail(detailDate.value));

window.addEventListener("resize", () => {
  if (trendChart) {
    trendChart.resize();
  }
  if (hourlyChart) {
    hourlyChart.resize();
  }
});

window.addEventListener("DOMContentLoaded", initializeDashboard);
