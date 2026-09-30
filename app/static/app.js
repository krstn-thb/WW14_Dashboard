const state = {
  locale: "de-DE",
  timezone: "Europe/Berlin",
  refreshSeconds: 60,
  refreshTimer: null,
  toastTimer: null,
  selectedStocks: [],
  selectedClimateMetrics: [],
};

const $ = (selector) => document.querySelector(selector);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("visible");
  window.clearTimeout(state.toastTimer);
  state.toastTimer = window.setTimeout(() => toast.classList.remove("visible"), 4500);
}

function updateClock() {
  const now = new Date();
  $("#clock-time").textContent = new Intl.DateTimeFormat(state.locale, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    timeZone: state.timezone,
  }).format(now);
  $("#clock-date").textContent = new Intl.DateTimeFormat(state.locale, {
    weekday: "short",
    day: "2-digit",
    month: "long",
    timeZone: state.timezone,
  }).format(now);
}

function formatDate(value, options = {}) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "–";
  return new Intl.DateTimeFormat(state.locale, { timeZone: state.timezone, ...options }).format(date);
}

function formatNumber(value, decimals = 1) {
  if (!Number.isFinite(Number(value))) return "–";
  return new Intl.NumberFormat(state.locale, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  }).format(value);
}

function sparkline(values, id, extraClass = "") {
  const clean = values.map((item) => Number(item?.value ?? item)).filter(Number.isFinite);
  if (clean.length < 2) return '<span aria-hidden="true"></span>';
  const width = 180;
  const height = 42;
  const min = Math.min(...clean);
  const max = Math.max(...clean);
  const span = max - min || 1;
  const coords = clean.map((value, index) => {
    const x = (index / (clean.length - 1)) * width;
    const y = height - 3 - ((value - min) / span) * (height - 8);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const polygon = `0,${height} ${coords.join(" ")} ${width},${height}`;
  return `<svg class="sparkline ${extraClass}" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
    <defs><linearGradient id="spark-${escapeHtml(id)}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#83e6b9"/><stop offset="1" stop-color="#83e6b9" stop-opacity="0"/></linearGradient></defs>
    <polygon points="${polygon}" fill="url(#spark-${escapeHtml(id)})" opacity=".18"></polygon>
    <polyline points="${coords.join(" ")}"></polyline>
  </svg>`;
}

function sourceState(element, status) {
  element.className = `source-state ${status || ""}`;
  element.textContent = status === "live"
    ? "Live"
    : status === "error"
      ? "Störung"
      : status === "disabled"
        ? "Aus"
        : "Demo";
}

function renderClimate(climate) {
  sourceState($("#climate-state"), climate.status);
  $("#climate-message").textContent = climate.message || "InfluxDB verbunden";
  $("#climate-updated").textContent = climate.updated_at
    ? `Stand ${formatDate(climate.updated_at, { hour: "2-digit", minute: "2-digit" })}`
    : "";
  const target = $("#climate-content");
  const metrics = climate.metrics || [];
  target.innerHTML = metrics.length
    ? metrics.map((metric, index) => `
      <section class="metric">
        <span class="metric-label">${escapeHtml(metric.label)}</span>
        <div class="metric-value">${formatNumber(metric.value, metric.decimals ?? 1)}<span class="metric-unit">${escapeHtml(metric.unit)}</span></div>
        ${sparkline(metric.points || [], `climate-${index}`)}
      </section>`).join("")
    : '<div class="empty">Keine Klimawerte vorhanden</div>';
  target.classList.remove("loading-block");
}

function urgencyLabel(days) {
  if (days < 0) return ["abgelaufen", "now"];
  if (days === 0) return ["heute", "now"];
  if (days === 1) return ["morgen", "soon"];
  if (days <= 7) return [`in ${days} Tagen`, "soon"];
  return [`in ${days} Tagen`, ""];
}

function renderDeadlines(section) {
  const items = section.items || [];
  $("#deadline-count").textContent = items.length;
  const target = $("#deadline-content");
  target.innerHTML = items.length
    ? items.slice(0, 7).map((item) => {
      const [urgency, urgencyClass] = urgencyLabel(Number(item.days_remaining));
      return `<div class="list-item">
        <div class="date-tile"><strong>${formatDate(item.due, { day: "2-digit" })}</strong><span>${formatDate(item.due, { month: "short" })}</span></div>
        <div class="item-main"><strong title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</strong><div class="item-meta"><span>${escapeHtml(item.kind || "Deadline")}</span><span>·</span><span>${formatDate(item.due, { hour: "2-digit", minute: "2-digit" })}</span></div></div>
        <span class="urgency ${urgencyClass}">${urgency}</span>
      </div>`;
    }).join("")
    : '<div class="empty">Keine bevorstehenden Deadlines</div>';
  target.classList.remove("loading-block");
  if (section.errors?.length) showToast(section.errors[0]);
}

function renderEvents(section) {
  const items = section.items || [];
  $("#event-count").textContent = items.length;
  const target = $("#event-content");
  target.innerHTML = items.length
    ? items.slice(0, 7).map((item) => `<div class="event-item">
      <div class="date-tile"><strong>${formatDate(item.start, { day: "2-digit" })}</strong><span>${formatDate(item.start, { month: "short" })}</span></div>
      <div class="item-main"><strong title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</strong><div class="item-meta"><span>${formatDate(item.start, { weekday: "short", hour: "2-digit", minute: "2-digit" })}</span>${item.location ? `<span>·</span><span>${escapeHtml(item.location)}</span>` : ""}</div></div>
    </div>`).join("")
    : '<div class="empty">Keine bevorstehenden Termine</div>';
  target.classList.remove("loading-block");
  if (section.errors?.length) showToast(section.errors[0]);
}

function renderStocks(stocks) {
  sourceState($("#stock-state"), stocks.status);
  $("#stock-message").textContent = stocks.message || "Kurse zeitverzögert";
  const target = $("#stock-content");
  const items = stocks.items || [];
  target.innerHTML = items.length
    ? items.map((item, index) => {
      const change = Number(item.change_percent);
      const negative = change < 0;
      return `<div class="stock-row ${negative ? "negative" : ""}">
        <div class="stock-name"><strong>${escapeHtml(item.label || item.symbol)}</strong><span>${escapeHtml(item.symbol)}</span></div>
        ${sparkline(item.points || [], `stock-${index}`)}
        <div class="stock-price">${formatNumber(item.price, 2)} ${escapeHtml(item.currency || "")}<span class="stock-change ${negative ? "negative" : ""}">${Number.isFinite(change) ? `${negative ? "" : "+"}${formatNumber(change, 2)} %` : "–"}</span></div>
      </div>`;
    }).join("")
    : '<div class="empty">Keine Aktien ausgewählt</div>';
  target.classList.remove("loading-block");
}

function renderWeather(weather) {
  sourceState($("#weather-state"), weather.status);
  $("#weather-location").textContent = weather.location || "Brandenburg an der Havel";
  $("#weather-updated").textContent = weather.updated_at
    ? `Stand ${formatDate(weather.updated_at, { hour: "2-digit", minute: "2-digit" })}`
    : "";
  const target = $("#weather-content");
  const current = weather.current;
  const daily = weather.daily || [];
  if (!current || !daily.length) {
    target.innerHTML = `<div class="empty">${escapeHtml(weather.message || "Keine Wetterdaten vorhanden")}</div>`;
  } else {
    target.innerHTML = `
      <div class="weather-current">
        <div class="weather-current-icon" aria-hidden="true">${escapeHtml(current.icon)}</div>
        <div class="weather-current-temp">${formatNumber(current.temperature, 1)}°</div>
        <div class="weather-current-copy">
          <strong>${escapeHtml(current.label)}</strong>
          <span>Gefühlt ${formatNumber(current.apparent_temperature, 1)} °C · Wind ${formatNumber(current.wind_speed, 0)} km/h</span>
        </div>
      </div>
      <div class="weather-days">
        ${daily.slice(0, 5).map((day) => `<div class="weather-day" title="${escapeHtml(day.label)}">
          <strong>${formatDate(`${day.date}T12:00:00`, { weekday: "short" })}</strong>
          <span class="weather-day-icon" aria-hidden="true">${escapeHtml(day.icon)}</span>
          <span class="weather-day-temp">${formatNumber(day.temperature_max, 0)}° / ${formatNumber(day.temperature_min, 0)}°</span>
          <span class="weather-day-rain">${formatNumber(day.precipitation_probability, 0)} %</span>
        </div>`).join("")}
      </div>`;
  }
  target.classList.remove("loading-block");
  if (weather.status === "error" && weather.message) showToast(weather.message);
}

function renderTodos(items) {
  const openCount = items.filter((item) => !item.done).length;
  $("#todo-count").textContent = openCount;
  const target = $("#todo-content");
  target.innerHTML = items.length
    ? items.map((item) => `<div class="todo-item ${item.done ? "done" : ""}" data-id="${Number(item.id)}">
      <input type="checkbox" ${item.done ? "checked" : ""} aria-label="Aufgabe als erledigt markieren" />
      <span title="${escapeHtml(item.text)}">${escapeHtml(item.text)}</span>
      <button class="todo-delete" type="button" aria-label="Aufgabe löschen">×</button>
    </div>`).join("")
    : '<div class="empty">Alles erledigt – oder eine neue Aufgabe hinzufügen.</div>';
  target.classList.remove("loading-block");
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    let detail = `Fehler ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* no body */ }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

async function loadDashboard() {
  window.clearTimeout(state.refreshTimer);
  try {
    const data = await request("/api/dashboard");
    state.locale = data.settings.locale || state.locale;
    state.timezone = data.settings.timezone || state.timezone;
    state.refreshSeconds = Math.max(15, Number(data.settings.refresh_seconds) || 60);
    document.title = data.settings.title;
    $("#dashboard-title").textContent = data.settings.title;
    $("#dashboard-subtitle").textContent = data.settings.subtitle;
    $("#refresh-interval").textContent = `alle ${state.refreshSeconds} s`;
    renderClimate(data.climate);
    renderDeadlines(data.deadlines);
    renderEvents(data.events);
    renderStocks(data.stocks);
    renderWeather(data.weather || { status: "disabled", daily: [] });
    renderTodos(data.todos);
    const connection = $("#connection");
    connection.className = "connection online";
    connection.lastElementChild.textContent = "Verbunden";
    $("#last-refresh").textContent = `Aktualisiert ${formatDate(data.generated_at, { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
  } catch (error) {
    const connection = $("#connection");
    connection.className = "connection offline";
    connection.lastElementChild.textContent = "Offline";
    showToast(`Dashboard konnte nicht aktualisiert werden: ${error.message}`);
  } finally {
    state.refreshTimer = window.setTimeout(loadDashboard, state.refreshSeconds * 1000);
  }
}

function renderStockSelections(items) {
  state.selectedStocks = items;
  const target = $("#stock-selected");
  target.innerHTML = items.length
    ? items.map((item) => `<div class="manage-selection">
      <div class="manage-copy"><strong>${escapeHtml(item.label || item.symbol)}</strong><span>${escapeHtml(item.symbol)}</span></div>
      <button class="remove-selection" type="button" data-remove-stock="${escapeHtml(item.symbol)}" aria-label="${escapeHtml(item.label || item.symbol)} entfernen">×</button>
    </div>`).join("")
    : '<div class="manage-message">Noch keine Aktie ausgewählt.</div>';
}

async function refreshStockSelections() {
  renderStockSelections(await request("/api/stocks"));
}

function renderClimateSelections(items) {
  state.selectedClimateMetrics = items;
  const target = $("#climate-selected");
  target.innerHTML = items.length
    ? items.map((item) => `<div class="manage-selection">
      <div class="manage-copy"><strong>${escapeHtml(item.label)}</strong><span>${escapeHtml(item.measurement)} · ${escapeHtml(item.field)}${item.unit ? ` · ${escapeHtml(item.unit)}` : ""}</span></div>
      <button class="remove-selection" type="button" data-remove-climate="${Number(item.id)}" aria-label="${escapeHtml(item.label)} entfernen">×</button>
    </div>`).join("")
    : '<div class="manage-message">Noch kein InfluxDB-Messwert ausgewählt.</div>';
}

async function refreshClimateSelections() {
  renderClimateSelections(await request("/api/climate/metrics"));
}

$("#stock-manage-button").addEventListener("click", async () => {
  $("#stock-dialog").showModal();
  $("#stock-search-results").innerHTML = '<div class="manage-message">Nach Unternehmen oder Börsenkürzel suchen.</div>';
  try {
    await refreshStockSelections();
    $("#stock-search-input").focus();
  } catch (error) {
    showToast(error.message);
  }
});

$("#stock-search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#stock-search-input");
  const query = input.value.trim();
  if (query.length < 2) return;
  const button = event.currentTarget.querySelector("button");
  const target = $("#stock-search-results");
  button.disabled = true;
  target.innerHTML = '<div class="manage-message">Suche läuft …</div>';
  try {
    const items = await request(`/api/stocks/search?q=${encodeURIComponent(query)}`);
    target.innerHTML = items.length
      ? items.map((item) => `<button class="manage-result" type="button" data-add-stock="${escapeHtml(item.symbol)}" data-stock-label="${escapeHtml(item.label)}">
        <span class="manage-copy"><strong>${escapeHtml(item.label)}</strong><span>${escapeHtml(item.symbol)}${item.exchange ? ` · ${escapeHtml(item.exchange)}` : ""}</span></span>
        <span>Hinzufügen</span>
      </button>`).join("")
      : '<div class="manage-message">Keine passende Aktie gefunden.</div>';
  } catch (error) {
    target.innerHTML = `<div class="manage-message">${escapeHtml(error.message)}</div>`;
  } finally {
    button.disabled = false;
  }
});

$("#stock-search-results").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-add-stock]");
  if (!button) return;
  button.disabled = true;
  try {
    await request("/api/stocks", {
      method: "POST",
      body: JSON.stringify({ symbol: button.dataset.addStock, label: button.dataset.stockLabel }),
    });
    await refreshStockSelections();
    await loadDashboard();
    showToast(`${button.dataset.stockLabel} wurde hinzugefügt.`);
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
  }
});

$("#stock-selected").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-remove-stock]");
  if (!button) return;
  try {
    await request(`/api/stocks/${encodeURIComponent(button.dataset.removeStock)}`, { method: "DELETE" });
    await refreshStockSelections();
    await loadDashboard();
  } catch (error) {
    showToast(error.message);
  }
});

$("#climate-manage-button").addEventListener("click", async () => {
  $("#climate-dialog").showModal();
  $("#influx-discovery-results").innerHTML = '<div class="manage-message">„InfluxDB durchsuchen“ lädt verfügbare Messfelder.</div>';
  try {
    await refreshClimateSelections();
  } catch (error) {
    showToast(error.message);
  }
});

$("#influx-discover-button").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const target = $("#influx-discovery-results");
  button.disabled = true;
  target.innerHTML = '<div class="manage-message">InfluxDB wird durchsucht …</div>';
  try {
    const data = await request("/api/influx/fields");
    const selected = new Set(state.selectedClimateMetrics.map((item) => `${item.measurement}\u0000${item.field}`));
    const items = (data.items || []).filter((item) => !selected.has(`${item.measurement}\u0000${item.field}`));
    target.innerHTML = items.length
      ? items.map((item) => `<button class="manage-result" type="button" data-add-measurement="${escapeHtml(item.measurement)}" data-add-field="${escapeHtml(item.field)}">
        <span class="manage-copy"><strong>${escapeHtml(item.field)}</strong><span>${escapeHtml(item.measurement)} · ${escapeHtml(data.bucket)}</span></span>
        <span>Hinzufügen</span>
      </button>`).join("")
      : '<div class="manage-message">Keine weiteren Messfelder mit aktuellen Daten gefunden.</div>';
  } catch (error) {
    target.innerHTML = `<div class="manage-message">${escapeHtml(error.message)}</div>`;
  } finally {
    button.disabled = false;
  }
});

$("#influx-discovery-results").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-add-measurement]");
  if (!button) return;
  button.disabled = true;
  try {
    await request("/api/climate/metrics", {
      method: "POST",
      body: JSON.stringify({ measurement: button.dataset.addMeasurement, field: button.dataset.addField }),
    });
    button.remove();
    await refreshClimateSelections();
    await loadDashboard();
  } catch (error) {
    showToast(error.message);
  }
});

$("#climate-selected").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-remove-climate]");
  if (!button) return;
  try {
    await request(`/api/climate/metrics/${button.dataset.removeClimate}`, { method: "DELETE" });
    await refreshClimateSelections();
    await loadDashboard();
  } catch (error) {
    showToast(error.message);
  }
});

document.querySelectorAll("[data-close-dialog]").forEach((button) => {
  button.addEventListener("click", () => $(`#${button.dataset.closeDialog}`).close());
});

document.querySelectorAll("dialog").forEach((dialog) => {
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
});

$("#todo-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#todo-input");
  const text = input.value.trim();
  if (!text) return;
  input.disabled = true;
  try {
    await request("/api/todos", { method: "POST", body: JSON.stringify({ text }) });
    input.value = "";
    await loadDashboard();
  } catch (error) {
    showToast(error.message);
  } finally {
    input.disabled = false;
    input.focus();
  }
});

$("#todo-content").addEventListener("change", async (event) => {
  if (!event.target.matches('input[type="checkbox"]')) return;
  const row = event.target.closest(".todo-item");
  try {
    await request(`/api/todos/${row.dataset.id}`, {
      method: "PATCH",
      body: JSON.stringify({ done: event.target.checked }),
    });
    await loadDashboard();
  } catch (error) {
    showToast(error.message);
  }
});

$("#todo-content").addEventListener("click", async (event) => {
  if (!event.target.matches(".todo-delete")) return;
  const row = event.target.closest(".todo-item");
  try {
    await request(`/api/todos/${row.dataset.id}`, { method: "DELETE" });
    await loadDashboard();
  } catch (error) {
    showToast(error.message);
  }
});

$("#fullscreen-button").addEventListener("click", async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
  } catch (error) {
    showToast("Vollbild konnte nicht aktiviert werden.");
  }
});

window.addEventListener("online", loadDashboard);
setInterval(updateClock, 1000);
updateClock();
loadDashboard();

