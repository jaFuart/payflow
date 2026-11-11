"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = {
  key: sessionStorage.getItem("mcc.basis.apiKey") || "",
  view: "overview",
  filter: "all",
  search: "",
  offset: 0,
  limit: 8,
  payments: [],
  total: 0,
  stats: null,
  selected: null,
  detail: null,
  events: [],
  receipts: [],
  tab: "events",
  busy: false,
  connected: false,
  tableSignature: "",
  detailSignature: "",
  launching: false,
};
const scenarios = {
  success: {
    name: "Подписка Basis Pro",
    amount: "2490.00",
    label: "Всё по плану",
  },
  decline: {
    name: "Оплата корпоративного тарифа",
    amount: "8900.00",
    label: "Отказ шлюза",
  },
  retry: {
    name: "Заказ #1042 · повтор webhook",
    amount: "1490.00",
    label: "С третьей попытки",
  },
  dlq: {
    name: "Заказ #1043 · проверка DLQ",
    amount: "3790.00",
    label: "До последнего",
  },
};
const eventLabels = {
  "payment.created": "Запрос принят, платёж сохранён",
  "outbox.published": "Событие передано в RabbitMQ",
  "processing.started": "Consumer начал обработку",
  "gateway.succeeded": "Платёжный шлюз подтвердил оплату",
  "gateway.failed": "Платёжный шлюз отклонил платёж",
  "webhook.failed": "Не удалось доставить webhook",
  "retry.scheduled": "Назначена повторная попытка",
  "webhook.delivered": "Webhook доставлен получателю",
  "payment.dead_lettered": "Лимит попыток исчерпан · DLQ",
  "webhook.redelivery_requested": "Доставка запрошена повторно",
};
const views = {
  overview: {
    title: "Платежи под контролем",
    subtitle: "Весь путь платежа — от первого запроса до последнего webhook.",
    label: "Обзор",
  },
  payments: {
    title: "Каждый платёж — на виду",
    subtitle: "Результат оплаты и состояние доставки — всегда отдельно.",
    label: "Платежи",
  },
  architecture: {
    title: "События вместо догадок",
    subtitle: "Как устроена обработка, которая переживает ошибки и повторы.",
    label: "Как это работает",
  },
  api: {
    title: "Небольшой API. Чёткие гарантии",
    subtitle: "Те же операции, которые использует этот интерфейс.",
    label: "API reference",
  },
};
const escape = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        char
      ],
  );
const icon = (name) =>
  `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const time = (value) =>
  value
    ? new Date(value).toLocaleTimeString("ru-RU", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
    : "—";
const shortDate = (value) =>
  value
    ? new Date(value)
        .toLocaleString("ru-RU", {
          day: "2-digit",
          month: "short",
          hour: "2-digit",
          minute: "2-digit",
        })
        .replace(" г.", "")
    : "—";
const money = (amount) => {
  const [whole, fraction = "00"] = String(amount).split(".");
  return `${whole.replace(/\B(?=(\d{3})+(?!\d))/g, "\u202f")},${fraction.padEnd(2, "0")}`;
};

function errorMessage(payload) {
  if (typeof payload?.detail === "string") return payload.detail;
  if (Array.isArray(payload?.detail))
    return payload.detail.map((item) => item.msg).join(". ");
  return "Не удалось выполнить запрос. Попробуйте ещё раз.";
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-API-Key", state.key);
  if (options.body) headers.set("Content-Type", "application/json");
  const response = await fetch(path, {
    ...options,
    headers,
    cache: "no-store",
  });
  if (response.status === 401) {
    state.key = "";
    state.connected = false;
    sessionStorage.removeItem("mcc.basis.apiKey");
    showAuth();
    throw new Error("API-ключ не принят. Проверьте значение API_KEY в .env.");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(errorMessage(payload));
  return payload;
}

function connection(online) {
  state.connected = online;
  $("#connection-status").innerHTML =
    `<i class="live-dot"></i>${online ? "API подключён" : "Нет соединения"}`;
}

function toast(message, isError = false) {
  const element = document.createElement("div");
  element.className = `toast${isError ? " error" : ""}`;
  const close = document.createElement("button");
  close.textContent = "×";
  close.setAttribute("aria-label", "Закрыть уведомление");
  close.addEventListener("click", () => element.remove());
  element.append(close, document.createTextNode(message));
  $("#toast-region").append(element);
  setTimeout(() => element.remove(), 6500);
}

function showAuth() {
  if (!$("#auth-modal").open) $("#auth-modal").showModal();
  $("#api-key-input").focus();
}

function setView(view) {
  state.view = views[view] ? view : "overview";
  const current = views[state.view];
  $("#page-title").innerHTML =
    `${escape(current.title)}<span class="title-dot">.</span>`;
  $("#page-subtitle").textContent = current.subtitle;
  $("#breadcrumb-current").textContent = current.label;
  $("#overview-view").hidden = state.view !== "overview";
  $("#payments-section").hidden = !["overview", "payments"].includes(
    state.view,
  );
  $("#architecture-view").hidden = state.view !== "architecture";
  $("#api-view").hidden = state.view !== "api";
  $$(".nav-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.view === state.view);
    if (item.dataset.view === state.view)
      item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
}

function paymentBadge(payment) {
  const labels = {
    pending: "В обработке",
    succeeded: "Успешно",
    failed: "Отклонён",
  };
  return `<span class="badge ${escape(payment.status)}">${labels[payment.status] || escape(payment.status)}</span>`;
}

function deliveryState(payment) {
  if (payment.webhook_delivered_at)
    return { key: "delivered", label: "Доставлен" };
  if (payment.dead_lettered_at)
    return { key: "dead_lettered", label: "В очереди DLQ" };
  if (payment.last_error) return { key: "retrying", label: "Повтор доставки" };
  return {
    key: "pending",
    label: payment.processing_attempts ? "Отправляется" : "Ожидает",
  };
}

function updateStats(stats) {
  state.stats = stats;
  $("#metric-total").textContent = stats.total.toLocaleString("ru-RU");
  $("#metric-success").textContent = stats.succeeded.toLocaleString("ru-RU");
  $("#metric-delivered").textContent = stats.delivered.toLocaleString("ru-RU");
  $("#metric-dlq").textContent = stats.dead_lettered.toLocaleString("ru-RU");
  $("#metric-pending").textContent = stats.pending
    ? `${stats.pending} в обработке`
    : "Все запросы обработаны";
  const processed = stats.succeeded + stats.failed;
  $("#metric-rate").textContent = processed
    ? `${Math.round((stats.succeeded / processed) * 100)}% от завершённых оплат`
    : "Пока нет завершённых оплат";
  $("#metric-retry").textContent = stats.retrying
    ? `${stats.retrying} ожидают повтора`
    : "Подтверждено получателем";
  $("#nav-count").textContent = stats.total;
  $("#filter-dlq-count").textContent = stats.dead_lettered;
  $("#demo-disabled").hidden = stats.demo_enabled;
  $("#run-all").disabled = !stats.demo_enabled || state.launching;
  $$("[data-scenario]").forEach(
    (button) => (button.disabled = !stats.demo_enabled || state.launching),
  );
  $$("select[name=scenario] option").forEach(
    (option) =>
      (option.disabled = !stats.demo_enabled && option.value !== "custom"),
  );
  if (!stats.demo_enabled) $("select[name=scenario]").value = "custom";
}

function renderTable(result) {
  state.payments = result.items;
  state.total = result.total;
  $("#payment-count").textContent = result.total;
  if (!result.items.length) {
    const hasFilter = state.filter !== "all" || state.search;
    $("#payments-body").innerHTML =
      `<tr><td colspan="6"><div class="empty-state"><span>${hasFilter ? "⌕" : "↗"}</span><strong>${hasFilter ? "По этому запросу платежей нет" : "Здесь начнётся история платежей"}</strong><p>${hasFilter ? "Измените фильтр или поисковый запрос." : "Запустите демо выше или создайте свой платёж."}</p></div></td></tr>`;
  } else {
    $("#payments-body").innerHTML = result.items
      .map((payment) => {
        const delivery = deliveryState(payment);
        return `<tr data-id="${escape(payment.payment_id)}" tabindex="0" aria-label="Открыть платёж ${escape(payment.description || payment.payment_id)}"><td><div class="payment-identity"><span class="payment-row-icon">${icon("pay")}</span><div><span class="payment-name">${escape(payment.description || "Без описания")}</span><span class="payment-id">${escape(payment.payment_id.slice(0, 8))}…${escape(payment.payment_id.slice(-4))}</span></div></div></td><td><span class="amount">${escape(money(payment.amount))}<small>${escape(payment.currency)}</small></span></td><td>${paymentBadge(payment)}</td><td><span class="badge ${delivery.key}">${delivery.label}</span>${payment.processing_attempts > 1 ? `<span class="attempt-note">Попытка ${payment.processing_attempts} из 3</span>` : ""}</td><td><time class="table-date" datetime="${escape(payment.created_at)}">${escape(shortDate(payment.created_at))}</time></td><td class="row-arrow">↗</td></tr>`;
      })
      .join("");
  }
  $("#pagination-label").textContent = result.total
    ? `${result.offset + 1}–${result.offset + result.items.length} из ${result.total} платежей`
    : "Нет платежей";
  $("#previous-page").disabled = state.offset === 0;
  $("#next-page").disabled = state.offset + state.limit >= result.total;
}

async function refresh() {
  if (!state.key || state.busy || document.hidden) return;
  state.busy = true;
  const selection = state.selected;
  const params = new URLSearchParams({
    limit: String(state.limit),
    offset: String(state.offset),
  });
  if (state.filter === "dead_lettered") params.set("delivery", "dead_lettered");
  else if (state.filter !== "all") params.set("status", state.filter);
  if (state.search) params.set("search", state.search);
  try {
    const calls = [api("/api/v1/stats"), api(`/api/v1/payments?${params}`)];
    if (selection)
      calls.push(
        api(`/api/v1/payments/${selection}`),
        api(`/api/v1/payments/${selection}/events`),
        api(`/api/v1/payments/${selection}/webhooks`),
      );
    const [stats, list, detail, events, receipts] = await Promise.all(calls);
    connection(true);
    updateStats(stats);
    const signature = JSON.stringify(list);
    if (signature !== state.tableSignature) {
      renderTable(list);
      state.tableSignature = signature;
    }
    if (selection && state.selected === selection) {
      state.detail = detail;
      state.events = events.items;
      state.receipts = receipts.items;
      const detailSignature = JSON.stringify([
        detail,
        events,
        receipts,
        state.tab,
      ]);
      if (detailSignature !== state.detailSignature) {
        renderDetail();
        state.detailSignature = detailSignature;
      }
    }
  } catch (error) {
    connection(false);
    if (!state.key) {
      $("#auth-error").textContent = error.message;
      $("#auth-error").hidden = false;
    }
  } finally {
    state.busy = false;
  }
}

async function createDemo(scenario) {
  const config = scenarios[scenario];
  if (!config) return;
  const request = {
    scenario,
    amount: config.amount,
    currency: "RUB",
    description: config.name,
  };
  return api("/api/v1/demo/payments", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(request),
  });
}

async function launchScenario(scenario) {
  if (state.launching) return;
  state.launching = true;
  if (state.stats) updateStats(state.stats);
  try {
    await createDemo(scenario);
    state.offset = 0;
    setFilter("all", false);
    toast(
      `Сценарий «${scenarios[scenario].label}» запущен. Платёж передан в обработку.`,
    );
    await refresh();
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.launching = false;
    if (state.stats) updateStats(state.stats);
  }
}

async function launchAll() {
  if (state.launching) return;
  state.launching = true;
  if (state.stats) updateStats(state.stats);
  const button = $("#run-all");
  button.innerHTML = `Создаём платежи ${icon("clock")}`;
  try {
    for (const scenario of Object.keys(scenarios)) await createDemo(scenario);
    state.offset = 0;
    setFilter("all", false);
    toast(
      "Четыре сценария запущены. Consumer обрабатывает их по очереди — следите за статусами.",
    );
    await refresh();
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.launching = false;
    button.innerHTML = `Запустить демо ${icon("arrow")}`;
    if (state.stats) updateStats(state.stats);
  }
}

function setFilter(filter, fetchNow = true) {
  state.filter = filter;
  state.offset = 0;
  $$("[data-filter]").forEach((button) =>
    button.classList.toggle("active", button.dataset.filter === filter),
  );
  state.tableSignature = "";
  if (fetchNow) refresh();
}

let drawerReturnFocus = null;
function openPayment(id) {
  drawerReturnFocus = document.activeElement;
  state.selected = id;
  state.detail = null;
  state.tab = "events";
  state.detailSignature = "";
  $("#drawer-title").textContent = `Платёж #${id.slice(0, 8)}`;
  $("#drawer-content").innerHTML =
    '<div class="loading-note">Загружаем историю платежа…</div>';
  $("#payment-drawer").hidden = false;
  $("#drawer-backdrop").hidden = false;
  document.body.style.overflow = "hidden";
  $("#close-drawer").focus();
  refresh();
}

function closePayment() {
  state.selected = null;
  state.detail = null;
  $("#payment-drawer").hidden = true;
  $("#drawer-backdrop").hidden = true;
  document.body.style.overflow = "";
  if (drawerReturnFocus?.isConnected) drawerReturnFocus.focus();
}

function eventDescription(event) {
  const parts = [];
  if (event.attempt) parts.push(`Попытка ${event.attempt}`);
  if (event.details.generation > 0)
    parts.push(`Цикл доставки ${event.details.generation + 1}`);
  if (event.details.delay_seconds)
    parts.push(`Ожидание ${event.details.delay_seconds} с`);
  if (event.details.error) parts.push(event.details.error);
  return parts.join(" · ");
}

function renderTimeline() {
  if (!state.events.length)
    return '<div class="empty-state"><strong>Пока нет записанных событий</strong><p>История доступна для платежей, созданных после обновления стенда.</p></div>';
  return `<ol class="timeline">${state.events
    .map((event) => {
      const positive = ["gateway.succeeded", "webhook.delivered"].includes(
        event.event_type,
      );
      const negative = [
        "gateway.failed",
        "webhook.failed",
        "payment.dead_lettered",
      ].includes(event.event_type);
      return `<li class="timeline-item ${positive ? "positive" : negative ? "negative" : ""}"><span class="timeline-dot"></span><div class="timeline-title"><span>${escape(eventLabels[event.event_type] || event.event_type)}</span><time datetime="${escape(event.created_at)}">${escape(time(event.created_at))}</time></div><p class="timeline-info">${escape(eventDescription(event) || event.event_type)}</p></li>`;
    })
    .join("")}</ol>`;
}

function renderReceipts() {
  if (!state.receipts.length)
    return '<div class="empty-state"><strong>Пока нет ответов получателя</strong><p>Здесь сохраняются HTTP-ответы встроенного демоприёмника.</p></div>';
  return state.receipts
    .map(
      (receipt) =>
        `<article class="receipt-card"><div class="receipt-heading"><span class="http-response ${receipt.status_code >= 400 ? "error" : ""}">HTTP ${receipt.status_code}</span><span>Попытка ${receipt.attempt} · цикл ${receipt.delivery_generation + 1}</span><time>${escape(time(receipt.created_at))}</time></div><pre>${escape(JSON.stringify(receipt.payload, null, 2))}</pre></article>`,
    )
    .join("");
}

function renderDetail() {
  const payment = state.detail;
  if (!payment) return;
  const scroll = $("#drawer-content").scrollTop;
  const delivery = deliveryState(payment);
  $("#drawer-content").innerHTML =
    `<div class="detail-summary"><div class="detail-amount">${escape(money(payment.amount))} <small>${escape(payment.currency)}</small></div>${paymentBadge(payment)}</div><p class="detail-description">${escape(payment.description || "Без описания")}</p><div class="detail-grid"><div><label>Доставка webhook</label><span class="badge ${delivery.key}">${delivery.label}</span></div><div><label>Попытки · цикл ${payment.delivery_generation + 1}</label><strong>${payment.processing_attempts} / 3</strong></div><div><label>Создан</label><span>${escape(shortDate(payment.created_at))}</span></div><div><label>Результат шлюза</label><span>${escape(shortDate(payment.processed_at))}</span></div><div><label>Payment ID</label><code>${escape(payment.payment_id)}</code></div><div><label>Idempotency-Key</label><code>${escape(payment.idempotency_key)}</code></div></div><div class="detail-actions">${payment.demo_scenario ? `<button class="button secondary" data-action="repeat">${icon("copy")}Повторить запрос</button>` : ""}${payment.dead_lettered_at ? `<button class="button primary" data-action="redeliver">${icon("retry")}Возобновить доставку</button>` : ""}<button class="button secondary" data-action="copy-id">${icon("copy")}ID</button></div>${payment.last_error ? `<div class="detail-error">${escape(payment.last_error)}</div>` : ""}<div class="detail-tabs" role="tablist"><button class="detail-tab ${state.tab === "events" ? "active" : ""}" role="tab" aria-selected="${state.tab === "events"}" data-tab="events">История · ${state.events.length}</button><button class="detail-tab ${state.tab === "webhooks" ? "active" : ""}" role="tab" aria-selected="${state.tab === "webhooks"}" data-tab="webhooks">Webhook · ${state.receipts.length}</button><button class="detail-tab ${state.tab === "payload" ? "active" : ""}" role="tab" aria-selected="${state.tab === "payload"}" data-tab="payload">JSON</button></div><div role="tabpanel">${state.tab === "events" ? renderTimeline() : state.tab === "webhooks" ? renderReceipts() : `<pre class="detail-payload">${escape(JSON.stringify(payment, null, 2))}</pre>`}</div>`;
  $("#drawer-content").scrollTop = scroll;
}

async function detailAction(action, button) {
  const payment = state.detail;
  if (!payment) return;
  button.disabled = true;
  try {
    if (action === "copy-id") {
      await navigator.clipboard.writeText(payment.payment_id);
      toast("ID платежа скопирован.");
    } else if (action === "repeat") {
      const repeated = await api("/api/v1/demo/payments", {
        method: "POST",
        headers: { "Idempotency-Key": payment.idempotency_key },
        body: JSON.stringify({
          scenario: payment.demo_scenario,
          amount: payment.amount,
          currency: payment.currency,
          description: payment.description,
        }),
      });
      if (repeated.payment_id !== payment.payment_id)
        throw new Error("Получен другой ID платежа.");
      toast("Тот же ключ — тот же платёж. Новый платёж и событие не созданы.");
    } else if (action === "redeliver") {
      await api(`/api/v1/payments/${payment.payment_id}/redeliver`, {
        method: "POST",
      });
      toast(
        "Доставка возобновлена. Результат оплаты сохранён, шлюз повторно не вызывается.",
      );
      await refresh();
    }
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function renderApiReference() {
  const operations = [
    ["POST", "/api/v1/payments"],
    ["GET", "/api/v1/payments/{id}"],
    ["GET", "/api/v1/payments"],
    ["GET", "/api/v1/stats"],
    ["GET", "/api/v1/payments/{id}/events"],
    ["GET", "/api/v1/payments/{id}/webhooks"],
    ["POST", "/api/v1/payments/{id}/redeliver"],
    ["POST", "/api/v1/demo/payments"],
  ];
  $("#api-operations").innerHTML = operations
    .map(
      ([method, path]) =>
        `<article class="api-operation"><span class="http-method ${method === "POST" ? "post" : ""}">${method}</span><code>${path}</code></article>`,
    )
    .join("");
  $("#curl-example").textContent =
    `curl -X POST ${location.origin}/api/v1/payments \\\n  -H 'X-API-Key: YOUR_API_KEY' \\\n  -H 'Idempotency-Key: order-1042' \\\n  -H 'Content-Type: application/json' \\\n  --data '{\n    "amount": "1490.00",\n    "currency": "RUB",\n    "description": "Оплата заказа",\n    "webhook_url": "https://your-service.example/webhook"\n  }'`;
}

$("#auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  state.key = $("#api-key-input").value.trim();
  $("#auth-error").hidden = true;
  try {
    await api("/health");
    sessionStorage.setItem("mcc.basis.apiKey", state.key);
    $("#auth-modal").close();
    $("#api-key-input").value = "";
    await refresh();
  } catch (error) {
    $("#auth-error").textContent = error.message;
    $("#auth-error").hidden = false;
  } finally {
    button.disabled = false;
  }
});
$("#auth-modal").addEventListener("cancel", (event) => event.preventDefault());
for (const button of [$("#logout"), $("#mobile-logout")]) {
  button.addEventListener("click", () => {
    closePayment();
    state.key = "";
    sessionStorage.removeItem("mcc.basis.apiKey");
    connection(false);
    showAuth();
  });
}
$("#new-payment").addEventListener("click", () => {
  $("#form-error").hidden = true;
  $("#custom-webhook-field").hidden =
    $("select[name=scenario]").value !== "custom";
  $("#payment-modal").showModal();
});
$("#close-modal").addEventListener("click", () => $("#payment-modal").close());
$("select[name=scenario]").addEventListener("change", (event) => {
  const custom = event.target.value === "custom";
  $("#custom-webhook-field").hidden = !custom;
  $("input[name=webhook_url]").required = custom;
});
$("#payment-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  $("#form-error").hidden = true;
  try {
    const custom = data.get("scenario") === "custom";
    const body = {
      amount: data.get("amount"),
      currency: data.get("currency"),
      description: data.get("description"),
    };
    if (custom) body.webhook_url = data.get("webhook_url");
    else body.scenario = data.get("scenario");
    await api(custom ? "/api/v1/payments" : "/api/v1/demo/payments", {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(body),
    });
    $("#payment-modal").close();
    state.offset = 0;
    setFilter("all", false);
    toast("Платёж создан и передан в обработку.");
    await refresh();
  } catch (error) {
    $("#form-error").textContent = error.message;
    $("#form-error").hidden = false;
  } finally {
    button.disabled = false;
  }
});
$("#run-all").addEventListener("click", launchAll);
$$("[data-scenario]").forEach((button) =>
  button.addEventListener("click", () =>
    launchScenario(button.dataset.scenario),
  ),
);
$$("[data-filter]").forEach((button) =>
  button.addEventListener("click", () => setFilter(button.dataset.filter)),
);
let searchTimeout;
$("#payment-search").addEventListener("input", (event) => {
  clearTimeout(searchTimeout);
  searchTimeout = setTimeout(() => {
    state.search = event.target.value.trim();
    state.offset = 0;
    state.tableSignature = "";
    refresh();
  }, 250);
});
$("#previous-page").addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - state.limit);
  refresh();
});
$("#next-page").addEventListener("click", () => {
  state.offset += state.limit;
  refresh();
});
$("#payments-body").addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-id]");
  if (row) openPayment(row.dataset.id);
});
$("#payments-body").addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    const row = event.target.closest("tr[data-id]");
    if (row) {
      event.preventDefault();
      openPayment(row.dataset.id);
    }
  }
});
$("#close-drawer").addEventListener("click", closePayment);
$("#drawer-backdrop").addEventListener("click", closePayment);
$("#drawer-content").addEventListener("click", (event) => {
  const tab = event.target.closest("[data-tab]");
  if (tab) {
    state.tab = tab.dataset.tab;
    state.detailSignature = "";
    renderDetail();
    return;
  }
  const button = event.target.closest("[data-action]");
  if (button) detailAction(button.dataset.action, button);
});
document.addEventListener("keydown", (event) => {
  if (!state.selected || $("#payment-modal").open || $("#auth-modal").open)
    return;
  if (event.key === "Escape") closePayment();
  if (event.key === "Tab") {
    const focusable = [
      ...$("#payment-drawer").querySelectorAll(
        "button:not(:disabled),a[href],input,select,[tabindex='0']",
      ),
    ];
    const first = focusable[0],
      last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
});
$("#download-schema").addEventListener("click", async () => {
  try {
    const schema = await api("/openapi.json");
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(schema, null, 2)], { type: "application/json" }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = "mcc-basis-openapi.json";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) {
    toast(error.message, true);
  }
});
$("#copy-example").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("#curl-example").textContent);
    toast("Пример запроса скопирован.");
  } catch {
    toast("Браузер не разрешил доступ к буферу обмена.", true);
  }
});
window.addEventListener("hashchange", () => setView(location.hash.slice(1)));
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});
setView(location.hash.slice(1));
renderApiReference();
if (state.key) refresh();
else showAuth();
setInterval(refresh, 1600);
