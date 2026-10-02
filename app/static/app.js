const state = {
  locale: "de-DE",
  timezone: "Europe/Berlin",
  refreshSeconds: 60,
  refreshTimer: null,
  radarTimer: null,
  radarFrames: [],
  radarOrigin: null,
  slideIndex: 0,
  slideTimer: null,
  slideIntervalMs: 10000,
  slideAutoplay: true,
  slidePausedByMouse: false,
  mouseResumeTimer: null,
  toastTimer: null,
  selectedStocks: [],
  selectedClimateMetrics: [],
  selectedDeadlines: [],
  selectedEvents: [],
  weatherLocationResults: [],
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

function sortedTagEntries(tags = {}) {
  return Object.entries(tags).sort(([left], [right]) => left.localeCompare(right));
}

function climateMetricKey(item) {
  return JSON.stringify([item.measurement, item.field, sortedTagEntries(item.tags)]);
}

function formatInfluxTags(tags = {}) {
  return sortedTagEntries(tags).map(([key, value]) => `${key}=${value}`).join(" · ");
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
    : status === "stale"
      ? "Gespeichert"
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
  target.classList.toggle("metric-grid--dense", metrics.length >= 5);
  target.classList.toggle("metric-grid--packed", metrics.length >= 7);
  target.innerHTML = metrics.length
    ? metrics.map((metric, index) => `
      <section class="metric">
        <span class="metric-label" title="${escapeHtml(metric.label)}">${escapeHtml(metric.label)}</span>
        <div class="metric-value">${formatNumber(metric.value, metric.decimals ?? 1)}<span class="metric-unit">${escapeHtml(metric.unit)}</span></div>
        ${sparkline(metric.points || [], `climate-${index}`)}
      </section>`).join("")
    : '<div class="empty">Keine Klimawerte vorhanden</div>';
  target.classList.remove("loading-block");
}

function urgencyLabel(days) {
  if (days < 0) return ["abgelaufen", "critical"];
  if (days === 0) return ["heute", "critical"];
  if (days === 1) return ["morgen", "critical"];
  if (days <= 7) return [`in ${days} Tagen`, "critical"];
  if (days <= 14) return [`in ${days} Tagen`, "warning"];
  return [`in ${days} Tagen`, ""];
}

function renderDeadlines(section) {
  const items = section.items || [];
  $("#deadline-count").textContent = items.length;
  const target = $("#deadline-content");
  target.innerHTML = items.length
    ? items.slice(0, 7).map((item) => {
      const [urgency, urgencyClass] = urgencyLabel(Number(item.days_remaining));
      return `<div class="list-item deadline-item ${urgencyClass}">
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
    ? items.slice(0, 7).map((item) => {
      const [urgency, urgencyClass] = urgencyLabel(Number(item.days_remaining));
      return `<div class="event-item ${urgencyClass}">
        <div class="date-tile"><strong>${formatDate(item.start, { day: "2-digit" })}</strong><span>${formatDate(item.start, { month: "short" })}</span></div>
        <div class="item-main"><strong title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</strong><div class="item-meta"><span>${formatDate(item.start, { weekday: "short", hour: "2-digit", minute: "2-digit" })}</span>${item.location ? `<span>·</span><span>${escapeHtml(item.location)}</span>` : ""}${item.recurrence && item.recurrence !== "none" ? `<span>·</span><span>↻ ${escapeHtml(recurrenceLabel(item.recurrence))}</span>` : ""}</div></div>
        <span class="urgency ${urgencyClass}">${urgency}</span>
      </div>`;
    }).join("")
    : '<div class="empty">Keine bevorstehenden Termine</div>';
  target.classList.remove("loading-block");
  if (section.errors?.length) showToast(section.errors[0]);
}

function renderDepartureList(selector, items) {
  const target = $(selector);
  target.innerHTML = items.length
    ? items.map((item) => {
      const delay = Number(item.delay_minutes);
      const delayed = Number.isFinite(delay) && delay > 0;
      const platformChanged = item.platform && item.planned_platform && String(item.platform) !== String(item.planned_platform);
      const status = item.cancelled ? "Fällt aus" : delayed ? `+${delay} min` : delay === 0 ? "Pünktlich" : "Plan";
      return `<div class="departure-row ${item.cancelled ? "cancelled" : delayed ? "delayed" : "on-time"}">
        <div class="departure-time"><strong>${formatDate(item.realtime || item.planned, { hour: "2-digit", minute: "2-digit" })}</strong>${delayed ? `<span>${formatDate(item.planned, { hour: "2-digit", minute: "2-digit" })}</span>` : ""}</div>
        <span class="departure-line">${escapeHtml(item.line || "RE1")}</span>
        <div class="departure-destination"><strong>${escapeHtml(item.destination || "–")}</strong><span>${item.platform ? `Gleis ${escapeHtml(item.platform)}` : "Gleis –"}${platformChanged ? ` · geplant ${escapeHtml(item.planned_platform)}` : ""}</span></div>
        <span class="departure-delay">${status}</span>
      </div>`;
    }).join("")
    : '<div class="empty">Keine passende RE1-Abfahrt gefunden</div>';
  target.classList.remove("loading-block");
}

function renderDepartures(departures) {
  sourceState($("#departure-state"), departures.status);
  $("#departure-updated").textContent = departures.updated_at
    ? `${escapeHtml(departures.station || "Brandenburg Hbf")} · Stand ${formatDate(departures.updated_at, { hour: "2-digit", minute: "2-digit" })}`
    : escapeHtml(departures.station || "Brandenburg Hbf");
  $("#departure-message").textContent = departures.message || "Echtzeit inklusive Verspätungen und Ausfällen";
  renderDepartureList("#departures-magdeburg", departures.directions?.magdeburg || []);
  renderDepartureList("#departures-berlin", departures.directions?.berlin || []);
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
        <div class="stock-name"><strong>${escapeHtml(item.label || item.symbol)}</strong><span>${escapeHtml(item.symbol)} · ${item.asset_type === "crypto" ? "Krypto" : "Aktie"}</span></div>
        ${sparkline(item.points || [], `stock-${index}`)}
        <div class="stock-price">${formatNumber(item.price, 2)} ${escapeHtml(item.currency || "")}<span class="stock-change ${negative ? "negative" : ""}">${Number.isFinite(change) ? `${negative ? "" : "+"}${formatNumber(change, 2)} %` : "–"}</span></div>
      </div>`;
    }).join("")
    : '<div class="empty">Keine Aktien oder Kryptowährungen ausgewählt</div>';
  target.classList.remove("loading-block");
}

function radarMap(radar, location) {
  if (!radar?.frames?.length) {
    state.radarFrames = [];
    return '<div class="radar-map radar-unavailable"><span>Radar momentan nicht verfügbar</span></div>';
  }
  const latitude = Number(radar.latitude);
  const longitude = Number(radar.longitude);
  const zoom = Number(radar.zoom) || 7;
  const tileCount = 2 ** zoom;
  const latitudeRadians = latitude * Math.PI / 180;
  const tileX = ((longitude + 180) / 360) * tileCount;
  const tileY = (1 - Math.asinh(Math.tan(latitudeRadians)) / Math.PI) / 2 * tileCount;
  const firstX = Math.floor(tileX) - 2;
  const firstY = Math.floor(tileY) - 1;
  const markerX = (2 + tileX - Math.floor(tileX)) * 256;
  const markerY = (1 + tileY - Math.floor(tileY)) * 256;
  const originShift = 20037508.342789244;
  const tileMeters = (originShift * 2) / tileCount;
  const bbox = [
    -originShift + firstX * tileMeters,
    originShift - (firstY + 3) * tileMeters,
    -originShift + (firstX + 5) * tileMeters,
    originShift - firstY * tileMeters,
  ].join(",");
  const tiles = [];
  for (let row = 0; row < 3; row += 1) {
    for (let column = 0; column < 5; column += 1) {
      const x = (firstX + column + tileCount) % tileCount;
      const y = firstY + row;
      const baseUrl = `https://tile.openstreetmap.org/${zoom}/${x}/${y}.png`;
      tiles.push(`<div class="radar-tile"><img src="${escapeHtml(baseUrl)}" alt="" /></div>`);
    }
  }

  const endpoint = "https://maps.dwd.de/geoserver/dwd/wms";
  const layer = "dwd:Radar_rv_product_1x1km_ger";
  state.radarFrames = radar.frames.map((frame) => {
    const query = new URLSearchParams({
      service: "WMS",
      version: "1.1.1",
      request: "GetMap",
      layers: layer,
      styles: "",
      bbox,
      width: "1024",
      height: "614",
      srs: "EPSG:3857",
      format: "image/png",
      transparent: "true",
      time: frame.time,
    });
    return { ...frame, stale: Boolean(radar.stale), url: `${endpoint}?${query.toString()}` };
  });
  const first = state.radarFrames[0];
  const firstLabel = radar.stale ? "Radar gespeichert" : "Radar jetzt";
  const liveLabel = radar.stale ? "Letzter Stand" : "2 h Vorschau";
  return `<div class="radar-map" role="button" tabindex="0" aria-label="Regenradar für ${escapeHtml(location)} groß öffnen">
    <div class="radar-tile-grid" style="--radar-marker-x:${markerX.toFixed(1)}px;--radar-marker-y:${markerY.toFixed(1)}px;left:calc(50% - ${markerX.toFixed(1)}px);top:calc(50% - ${markerY.toFixed(1)}px)">${tiles.join("")}<img class="radar-frame" src="${escapeHtml(first.url)}" decoding="async" alt="Animierte Niederschlagsvorhersage" /></div>
    <span class="radar-marker" aria-label="${escapeHtml(location)}"></span>
    <span class="radar-time">${firstLabel} · ${formatDate(first.time, { hour: "2-digit", minute: "2-digit" })}</span>
    <span class="radar-live ${radar.stale ? "stale" : ""}"><i></i> ${liveLabel}</span>
    <span class="radar-expand-hint" aria-hidden="true">⛶</span>
    <span class="radar-attribution"><a href="https://www.dwd.de/" target="_blank" rel="noopener">DWD</a> · © OpenStreetMap</span>
  </div>`;
}

function fitRadarMap() {
  document.querySelectorAll(".radar-map").forEach((map) => {
    const grid = map.querySelector(".radar-tile-grid");
    if (!grid || !map.clientWidth || !map.clientHeight) return;
    const scale = Math.max(map.clientWidth / 1280, map.clientHeight / 768) * 1.03;
    grid.style.transform = `scale(${scale})`;
  });
}

function openRadarDialog(map) {
  const dialog = $("#radar-dialog");
  if (!map || dialog.open) return;
  state.radarOrigin = { parent: map.parentNode, nextSibling: map.nextSibling };
  map.classList.add("radar-map--expanded");
  $("#radar-dialog-content").append(map);
  dialog.showModal();
  window.requestAnimationFrame(fitRadarMap);
}

function restoreRadarMap() {
  const map = $("#radar-dialog-content .radar-map");
  if (!map || !state.radarOrigin) return;
  const { parent, nextSibling } = state.radarOrigin;
  map.classList.remove("radar-map--expanded");
  if (nextSibling?.parentNode === parent) parent.insertBefore(map, nextSibling);
  else parent.append(map);
  state.radarOrigin = null;
  window.requestAnimationFrame(fitRadarMap);
}

function startRadarAnimation() {
  window.clearInterval(state.radarTimer);
  state.radarTimer = null;
  if (!state.radarFrames.length) return;

  state.radarFrames.slice(1).forEach((frame) => {
    const preload = new Image();
    preload.decoding = "async";
    preload.src = frame.url;
  });
  if (state.radarFrames.length === 1) return;

  let index = 0;
  state.radarTimer = window.setInterval(() => {
    index = (index + 1) % state.radarFrames.length;
    const images = document.querySelectorAll(".radar-frame");
    const labels = document.querySelectorAll(".radar-time");
    if (!images.length || !labels.length) return;
    const frame = state.radarFrames[index];
    const offset = frame.stale
      ? "Letzter Stand"
      : Number(frame.minutes_ahead) > 0 ? `Radar +${frame.minutes_ahead} min` : "Radar jetzt";
    images.forEach((image) => { image.src = frame.url; });
    labels.forEach((label) => {
      label.textContent = `${offset} · ${formatDate(frame.time, { hour: "2-digit", minute: "2-digit" })}`;
    });
  }, 1400);
}

function renderWeather(weather) {
  const radarDialog = $("#radar-dialog");
  if (radarDialog.open) radarDialog.close();
  restoreRadarMap();
  window.clearInterval(state.radarTimer);
  state.radarTimer = null;
  state.radarFrames = [];
  sourceState($("#weather-state"), weather.status);
  const weatherLocation = weather.location || "Brandenburg an der Havel";
  $("#radar-dialog-title").textContent = `Regenradar ${weatherLocation}`;
  $("#weather-location").textContent = weatherLocation;
  $("#weather-updated").textContent = weather.updated_at
    ? `Stand ${formatDate(weather.updated_at, { hour: "2-digit", minute: "2-digit" })}`
    : "";
  const target = $("#weather-content");
  const current = weather.current;
  const hourly = weather.hourly || [];
  const daily = weather.daily || [];
  if (!current || !daily.length) {
    target.innerHTML = `<div class="empty">${escapeHtml(weather.message || "Keine Wetterdaten vorhanden")}</div>`;
  } else {
    target.innerHTML = `
      <div class="weather-top">
        <div class="weather-current">
          <div class="weather-current-icon" aria-hidden="true">${escapeHtml(current.icon)}</div>
          <div>
            <div class="weather-current-temp">${formatNumber(current.temperature, 1)}°</div>
            <div class="weather-current-copy">
              <strong>${escapeHtml(current.label)}</strong>
              <span>Gefühlt ${formatNumber(current.apparent_temperature, 1)} °C · Wind ${formatNumber(current.wind_speed, 0)} km/h</span>
            </div>
          </div>
        </div>
        <div class="weather-radar-preview" aria-label="Regenradar für ${escapeHtml(weatherLocation)}">
          ${radarMap(weather.radar, weatherLocation)}
        </div>
      </div>
      <div class="weather-forecast-block weather-hourly-block">
        <div class="weather-section-heading"><strong>Heute im Verlauf</strong><span>Regenwahrscheinlichkeit</span></div>
        <div class="weather-hourly" aria-label="Wetterverlauf heute">
          ${hourly.map((hour) => `<div class="weather-hour ${hour.is_current ? "current" : ""}" title="${escapeHtml(hour.label)} · ${formatNumber(hour.precipitation_probability, 0)} % Regen">
            <strong>${formatDate(hour.time, { hour: "2-digit", minute: "2-digit" })}</strong>
            <span class="weather-hour-icon" aria-hidden="true">${escapeHtml(hour.icon)}</span>
            <span class="weather-hour-temp">${formatNumber(hour.temperature, 0)}°</span>
            <span class="weather-hour-rain">${formatNumber(hour.precipitation_probability, 0)} %</span>
          </div>`).join("")}
        </div>
      </div>
      <div class="weather-forecast-block weather-days-block">
        <div class="weather-section-heading"><strong>5-Tage-Ausblick</strong><span>Höchst- / Tiefstwert</span></div>
        <div class="weather-days">
          ${daily.slice(0, 5).map((day) => `<div class="weather-day" title="${escapeHtml(day.label)}">
            <strong>${formatDate(`${day.date}T12:00:00`, { weekday: "short" })}</strong>
            <span class="weather-day-icon" aria-hidden="true">${escapeHtml(day.icon)}</span>
            <span class="weather-day-temp">${formatNumber(day.temperature_max, 0)}° / ${formatNumber(day.temperature_min, 0)}°</span>
            <span class="weather-day-rain">${formatNumber(day.precipitation_probability, 0)} %</span>
          </div>`).join("")}
        </div>
      </div>`;
  }
  target.classList.remove("loading-block");
  window.requestAnimationFrame(fitRadarMap);
  startRadarAnimation();
  if (weather.status === "error" && weather.message) showToast(weather.message);
}

function renderMensa(mensa) {
  const status = $("#mensa-state");
  sourceState(status, mensa.status);
  if (mensa.stale) status.textContent = "Gespeichert";
  const dayLabel = mensa.date
    ? formatDate(mensa.date + "T12:00:00", { weekday: "long", day: "2-digit", month: "long" })
    : mensa.weekday || "Heute";
  $("#mensa-date").textContent = dayLabel;
  $("#mensa-message").textContent = mensa.message || "Preise für Studierende · Angaben ohne Gewähr";
  const source = $("#mensa-source");
  if (/^https?:\/\//.test(mensa.source_url || "")) source.href = mensa.source_url;

  const target = $("#mensa-content");
  const items = mensa.items || [];
  target.classList.toggle("mensa-grid--compact", items.length > 6);
  target.innerHTML = items.length
    ? items.slice(0, 8).map((item) => {
      const badges = (item.badges || []).map((badge) => "<span>" + escapeHtml(badge) + "</span>").join("");
      const calories = item.calories != null && Number.isFinite(Number(item.calories))
        ? "<span>" + formatNumber(item.calories, 0) + " kcal</span>"
        : "";
      return '<article class="mensa-card">'
        + '<div class="mensa-card-top">'
        + '<span class="mensa-category">' + escapeHtml(item.category) + "</span>"
        + '<strong class="mensa-price">' + escapeHtml(item.price || "Preis vor Ort") + "</strong>"
        + "</div>"
        + "<h3>" + escapeHtml(item.name) + "</h3>"
        + '<div class="mensa-details">' + badges + calories + "</div>"
        + "</article>";
    }).join("")
    : '<div class="empty">' + escapeHtml(mensa.message || "Für diesen Tag ist kein Speiseplan verfügbar.") + "</div>";
  target.classList.remove("loading-block");
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

function dashboardSlides() {
  return Array.from(document.querySelectorAll(".dashboard-slide"));
}

function scheduleSlideChange() {
  window.clearTimeout(state.slideTimer);
  state.slideTimer = null;
  if (!state.slideAutoplay || state.slidePausedByMouse) return;
  state.slideTimer = window.setTimeout(() => {
    if (document.querySelector("dialog[open]")) {
      scheduleSlideChange();
      return;
    }
    showSlide(state.slideIndex + 1);
  }, state.slideIntervalMs);
}

function showSlide(index) {
  const slides = dashboardSlides();
  if (!slides.length) return;
  const movingBackward = index < state.slideIndex;
  $(".slide-deck").classList.toggle("is-backward", movingBackward);
  state.slideIndex = (index + slides.length) % slides.length;
  slides.forEach((slide, slideIndex) => {
    const active = slideIndex === state.slideIndex;
    slide.classList.toggle("is-active", active);
    slide.setAttribute("aria-hidden", String(!active));
  });
  document.querySelectorAll("[data-slide-to]").forEach((dot) => {
    const active = Number(dot.dataset.slideTo) === state.slideIndex;
    dot.classList.toggle("is-active", active);
    dot.setAttribute("aria-selected", String(active));
  });
  if (slides[state.slideIndex].querySelector(".radar-map")) {
    window.requestAnimationFrame(fitRadarMap);
  }
  scheduleSlideChange();
}

function setSlideAutoplay(enabled, reason = "") {
  window.clearTimeout(state.mouseResumeTimer);
  state.mouseResumeTimer = null;
  state.slidePausedByMouse = false;
  state.slideAutoplay = enabled;
  const button = $("#slide-autoplay");
  button.textContent = enabled ? "Ⅱ" : "▶";
  button.setAttribute(
    "aria-label",
    enabled ? "Automatischen Wechsel pausieren" : "Automatischen Wechsel fortsetzen",
  );
  $("#slide-status").textContent = enabled ? "Automatik · 10 s" : reason || "Automatik pausiert";
  scheduleSlideChange();
}

function initializeSlides() {
  const slides = dashboardSlides();
  $("#slide-dots").innerHTML = slides.map((slide, index) => `<button type="button" role="tab" data-slide-to="${index}" aria-label="${escapeHtml(slide.dataset.slideTitle)}" title="${escapeHtml(slide.dataset.slideTitle)}"></button>`).join("");
  showSlide(0);
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
    $("#refresh-interval").textContent = `alle ${state.refreshSeconds} s`;
    renderClimate(data.climate);
    renderDeadlines(data.deadlines);
    renderEvents(data.events);
    renderDepartures(data.departures || { status: "disabled", directions: {} });
    renderStocks(data.stocks);
    renderWeather(data.weather || { status: "disabled", daily: [] });
    renderMensa(data.mensa || { status: "disabled", items: [] });
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
      <div class="manage-copy"><strong>${escapeHtml(item.label || item.symbol)}</strong><span>${escapeHtml(item.symbol)} · ${item.asset_type === "crypto" ? "Krypto" : "Aktie"}</span></div>
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
      <div class="manage-copy"><strong>${escapeHtml(item.label)}</strong><span>${escapeHtml(item.measurement)} · ${escapeHtml(item.field)}${formatInfluxTags(item.tags) ? ` · ${escapeHtml(formatInfluxTags(item.tags))}` : ""}${item.unit ? ` · ${escapeHtml(item.unit)}` : ""}</span></div>
      <button class="remove-selection" type="button" data-remove-climate="${Number(item.id)}" aria-label="${escapeHtml(item.label)} entfernen">×</button>
    </div>`).join("")
    : '<div class="manage-message">Noch kein InfluxDB-Messwert ausgewählt.</div>';
}

async function refreshClimateSelections() {
  renderClimateSelections(await request("/api/climate/metrics"));
}

function manualDateLabel(value) {
  return formatDate(value, {
    weekday: "short",
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function recurrenceLabel(recurrence) {
  return {
    daily: "täglich",
    weekly: "wöchentlich",
    monthly: "monatlich",
    yearly: "jährlich",
  }[recurrence] || "einmalig";
}

function renderDeadlineSelections(items) {
  state.selectedDeadlines = items;
  const target = $("#deadline-selected");
  target.innerHTML = items.length
    ? items.map((item) => `<div class="manage-selection">
      <div class="manage-copy"><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.kind || "Deadline")} · ${manualDateLabel(item.due)}</span></div>
      <button class="remove-selection" type="button" data-remove-deadline="${Number(item.id)}" aria-label="${escapeHtml(item.title)} entfernen">×</button>
    </div>`).join("")
    : '<div class="manage-message">Noch keine Deadline eingetragen.</div>';
}

async function refreshDeadlineSelections() {
  renderDeadlineSelections(await request("/api/deadlines"));
}

function renderEventSelections(items) {
  state.selectedEvents = items;
  const target = $("#event-selected");
  target.innerHTML = items.length
    ? items.map((item) => `<div class="manage-selection">
      <div class="manage-copy"><strong>${escapeHtml(item.title)}</strong><span>${manualDateLabel(item.start)}${item.location ? ` · ${escapeHtml(item.location)}` : ""}${item.recurrence && item.recurrence !== "none" ? ` · ↻ ${escapeHtml(recurrenceLabel(item.recurrence))}${item.recurrence_until ? ` bis ${formatDate(`${item.recurrence_until}T12:00:00`, { day: "2-digit", month: "short", year: "numeric" })}` : ""}` : ""}</span></div>
      <button class="remove-selection" type="button" data-remove-event="${Number(item.id)}" aria-label="${escapeHtml(item.title)} entfernen">×</button>
    </div>`).join("")
    : '<div class="manage-message">Noch kein Termin eingetragen.</div>';
}

async function refreshEventSelections() {
  renderEventSelections(await request("/api/events"));
}

function renderCurrentWeatherLocation(location) {
  $("#weather-location-current").innerHTML = `<div class="manage-selection">
    <div class="manage-copy"><strong>${escapeHtml(location.name)}</strong><span>${escapeHtml(location.label || location.name)}</span></div>
  </div>`;
}

async function refreshCurrentWeatherLocation() {
  renderCurrentWeatherLocation(await request("/api/weather/location"));
}

$("#weather-location-button").addEventListener("click", async () => {
  $("#weather-location-dialog").showModal();
  $("#weather-location-search-results").innerHTML = '<div class="manage-message">Nach einem Ort oder einer Postleitzahl suchen.</div>';
  try {
    await refreshCurrentWeatherLocation();
    $("#weather-location-search-input").focus();
  } catch (error) {
    showToast(error.message);
  }
});

$("#weather-location-search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#weather-location-search-input");
  const query = input.value.trim();
  if (query.length < 2) return;
  const button = event.currentTarget.querySelector("button");
  const target = $("#weather-location-search-results");
  button.disabled = true;
  target.innerHTML = '<div class="manage-message">Ortssuche läuft …</div>';
  try {
    state.weatherLocationResults = await request(`/api/weather/locations/search?q=${encodeURIComponent(query)}`);
    target.innerHTML = state.weatherLocationResults.length
      ? state.weatherLocationResults.map((item, index) => `<button class="manage-result" type="button" data-weather-location-index="${index}">
        <span class="manage-copy"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.label)}</span></span>
        <span>Auswählen</span>
      </button>`).join("")
      : '<div class="manage-message">Kein passender Ort gefunden.</div>';
  } catch (error) {
    target.innerHTML = `<div class="manage-message">${escapeHtml(error.message)}</div>`;
  } finally {
    button.disabled = false;
  }
});

$("#weather-location-search-results").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-weather-location-index]");
  if (!button) return;
  const location = state.weatherLocationResults[Number(button.dataset.weatherLocationIndex)];
  if (!location) return;
  button.disabled = true;
  try {
    await request("/api/weather/location", {
      method: "PUT",
      body: JSON.stringify(location),
    });
    $("#weather-location-dialog").close();
    await loadDashboard();
    showToast(`Wetterort wurde auf ${location.name} geändert.`);
  } catch (error) {
    showToast(error.message);
    button.disabled = false;
  }
});

$("#stock-manage-button").addEventListener("click", async () => {
  $("#stock-dialog").showModal();
  $("#stock-search-results").innerHTML = '<div class="manage-message">Nach Aktie, Kryptowährung oder Kürzel suchen.</div>';
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
      ? items.map((item) => `<button class="manage-result" type="button" data-add-stock="${escapeHtml(item.symbol)}" data-stock-label="${escapeHtml(item.label)}" data-stock-type="${escapeHtml(item.asset_type || "stock")}">
        <span class="manage-copy"><strong>${escapeHtml(item.label)}</strong><span>${item.asset_type === "crypto" ? "Krypto" : "Aktie"} · ${escapeHtml(item.symbol)}${item.exchange ? ` · ${escapeHtml(item.exchange)}` : ""}</span></span>
        <span>Hinzufügen</span>
      </button>`).join("")
      : '<div class="manage-message">Keine passende Aktie oder Kryptowährung gefunden.</div>';
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
      body: JSON.stringify({
        symbol: button.dataset.addStock,
        label: button.dataset.stockLabel,
        asset_type: button.dataset.stockType || "stock",
      }),
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
    const selected = new Set(state.selectedClimateMetrics.map(climateMetricKey));
    const items = (data.items || []).filter((item) => !selected.has(climateMetricKey(item)));
    target.innerHTML = items.length
      ? items.map((item) => `<button class="manage-result" type="button" data-add-measurement="${escapeHtml(item.measurement)}" data-add-field="${escapeHtml(item.field)}" data-add-tags="${escapeHtml(JSON.stringify(item.tags || {}))}">
        <span class="manage-copy"><strong>${escapeHtml(item.field)}</strong><span>${escapeHtml(item.measurement)}${formatInfluxTags(item.tags) ? ` · ${escapeHtml(formatInfluxTags(item.tags))}` : ""} · ${escapeHtml(data.bucket)}</span></span>
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
      body: JSON.stringify({
        measurement: button.dataset.addMeasurement,
        field: button.dataset.addField,
        tags: JSON.parse(button.dataset.addTags || "{}"),
      }),
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

$("#deadline-manage-button").addEventListener("click", async () => {
  $("#deadline-dialog").showModal();
  try {
    await refreshDeadlineSelections();
    $("#deadline-title").focus();
  } catch (error) {
    showToast(error.message);
  }
});

$("#deadline-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    await request("/api/deadlines", {
      method: "POST",
      body: JSON.stringify({
        title: $("#deadline-title").value.trim(),
        due: $("#deadline-due").value,
        kind: $("#deadline-kind").value.trim() || "Deadline",
      }),
    });
    event.currentTarget.reset();
    await refreshDeadlineSelections();
    await loadDashboard();
    $("#deadline-title").focus();
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
  }
});

$("#deadline-selected").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-remove-deadline]");
  if (!button) return;
  try {
    await request(`/api/deadlines/${button.dataset.removeDeadline}`, { method: "DELETE" });
    await refreshDeadlineSelections();
    await loadDashboard();
  } catch (error) {
    showToast(error.message);
  }
});

$("#event-manage-button").addEventListener("click", async () => {
  $("#event-dialog").showModal();
  try {
    await refreshEventSelections();
    $("#event-title").focus();
  } catch (error) {
    showToast(error.message);
  }
});

$("#event-recurrence").addEventListener("change", (event) => {
  const until = $("#event-recurrence-until");
  until.disabled = event.currentTarget.value === "none";
  if (until.disabled) until.value = "";
});

$("#event-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button[type="submit"]');
  button.disabled = true;
  try {
    const end = $("#event-end").value;
    await request("/api/events", {
      method: "POST",
      body: JSON.stringify({
        title: $("#event-title").value.trim(),
        start: $("#event-start").value,
        end: end || null,
        location: $("#event-location").value.trim(),
        recurrence: $("#event-recurrence").value,
        recurrence_until: $("#event-recurrence-until").value || null,
      }),
    });
    event.currentTarget.reset();
    $("#event-recurrence-until").disabled = true;
    await refreshEventSelections();
    await loadDashboard();
    $("#event-title").focus();
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
  }
});

$("#event-selected").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-remove-event]");
  if (!button) return;
  try {
    await request(`/api/events/${button.dataset.removeEvent}`, { method: "DELETE" });
    await refreshEventSelections();
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

$("#radar-dialog").addEventListener("close", restoreRadarMap);

$("#weather-content").addEventListener("click", (event) => {
  if (event.target.closest("a")) return;
  const map = event.target.closest(".radar-map:not(.radar-unavailable)");
  if (map) openRadarDialog(map);
});

$("#weather-content").addEventListener("keydown", (event) => {
  if (event.target.closest("a") || !["Enter", " "].includes(event.key)) return;
  const map = event.target.closest(".radar-map:not(.radar-unavailable)");
  if (!map) return;
  event.preventDefault();
  openRadarDialog(map);
});

$("#slide-previous").addEventListener("click", () => showSlide(state.slideIndex - 1));
$("#slide-next").addEventListener("click", () => showSlide(state.slideIndex + 1));
$("#slide-dots").addEventListener("click", (event) => {
  const button = event.target.closest("[data-slide-to]");
  if (button) showSlide(Number(button.dataset.slideTo));
});
$("#slide-autoplay").addEventListener("click", () => {
  setSlideAutoplay(!state.slideAutoplay);
});

document.addEventListener("keydown", (event) => {
  if (document.querySelector("dialog[open]") || event.target.matches("input, textarea, select")) return;
  if (event.key === "ArrowRight") {
    event.preventDefault();
    showSlide(state.slideIndex + 1);
  } else if (event.key === "ArrowLeft") {
    event.preventDefault();
    showSlide(state.slideIndex - 1);
  }
});

window.addEventListener("mousemove", () => {
  if (!state.slideAutoplay) return;
  state.slidePausedByMouse = true;
  window.clearTimeout(state.slideTimer);
  state.slideTimer = null;
  window.clearTimeout(state.mouseResumeTimer);
  $("#slide-status").textContent = "Maus bewegt · pausiert";
  state.mouseResumeTimer = window.setTimeout(() => {
    state.slidePausedByMouse = false;
    $("#slide-status").textContent = "Automatik · 10 s";
    scheduleSlideChange();
  }, 3000);
}, { passive: true });
window.addEventListener("resize", () => window.requestAnimationFrame(fitRadarMap));

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
initializeSlides();
loadDashboard();

