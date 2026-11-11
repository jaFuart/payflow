# Payflow · API и эксплуатация

[← Вернуться к обзору проекта](../README.md)

## Авторизация и настройки

Все API endpoints, включая `/health`, `/docs` и `/openapi.json`, требуют заголовок `X-API-Key`. Отсутствующий или неверный ключ возвращает `401`. HTML, стили и скрипты панели доступны без ключа; данные загружаются после авторизации. Панель хранит ключ в `sessionStorage` текущей вкладки; выход удаляет его.

Встроенный приёмник `/demo/webhooks/{payment_id}/{token}` проверяет HMAC-SHA256-токен, привязанный к платежу, вместо API-заголовка. Создание демоплатежей и приёмник выключены при `DEMO_ENABLED=false` и возвращают `404`.

Для примеров ниже задайте ключ из вашего `.env`:

```bash
export API_KEY=payflow-local-demo-key
```

| Настройка | Назначение |
| --- | --- |
| `API_KEY` | Общий статический ключ, не менее 16 символов. |
| `API_PORT` | Опубликованный порт API; по умолчанию `8000`. |
| `DEMO_ENABLED` | `false` по умолчанию в приложении; `.env.example` включает локальные сценарии. |
| `DEMO_CALLBACK_BASE_URL` | Адрес API из consumer; в Compose `http://api:8000`. |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Учётная запись и имя базы. |
| `RABBITMQ_USER`, `RABBITMQ_PASSWORD` | Учётная запись брокера. |
| `RABBITMQ_MANAGEMENT_PORT` | Порт RabbitMQ Management; по умолчанию `15672`. |
| `APP_IMAGE` | Образ приложения; по умолчанию `payflow:local`. |

Значения `.env.example` предназначены для локальной демонстрации. Для внешнего размещения задайте собственные ключ и пароли. Опубликованные API и RabbitMQ Management привязаны к `127.0.0.1`; PostgreSQL доступен только в сети Compose. Учётная запись RabbitMQ из примера: `mcc` / `mcc-local-password`.

При запуске Python-процессов вне Compose доступны настройки приложения: `DATABASE_URL`, `RABBITMQ_URL`, `GATEWAY_DELAY_MIN=2`, `GATEWAY_DELAY_MAX=5`, `GATEWAY_SUCCESS_RATE=0.9`, `WEBHOOK_TIMEOUT=10`, `RETRY_BASE_DELAY=1`, `OUTBOX_POLL_INTERVAL=0.5` и `OUTBOX_BATCH_SIZE=100`. Приведены значения по умолчанию. Стандартный Compose передаёт параметры подключения, API-ключ и настройки деморежима; остальные параметры можно добавить в `environment` сервисов.

## Создание платежа

`POST /api/v1/payments` принимает платёж и возвращает `202 Accepted` после сохранения платежа и первоначального события одной транзакцией.

```bash
curl -i http://localhost:8000/api/v1/payments \
  -H "X-API-Key: $API_KEY" \
  -H 'Idempotency-Key: invoice-2026-001' \
  -H 'Content-Type: application/json' \
  --data '{
    "amount": "1500.00",
    "currency": "RUB",
    "description": "Оплата заказа 001",
    "metadata": {"order_id": "001"},
    "webhook_url": "https://example.com/payments/webhook"
  }'
```

`webhook_url` должен вести на ваш доступный HTTP(S) endpoint; адрес выше показывает формат. Внутри контейнера `localhost` указывает на сам контейнер. Для webhook на хосте Docker Desktop используйте `http://host.docker.internal:PORT/path`, а для сервиса Compose — его сетевое имя.

```json
{
  "payment_id": "6ac4bca7-4705-4795-8f79-6335c57d794a",
  "status": "pending",
  "created_at": "2026-11-11T12:00:00Z"
}
```

| Поле | Правило |
| --- | --- |
| `amount` | Положительный Decimal: до 18 цифр всего и до 2 после запятой, максимум `9999999999999999.99`. Строка сохраняет точность денежного значения. |
| `currency` | `RUB`, `USD` или `EUR`. |
| `description` | Необязательная строка до 1000 символов; по умолчанию `""`. |
| `metadata` | Необязательный JSON-объект; по умолчанию `{}`. |
| `webhook_url` | Обязательный HTTP(S) URL до 2048 символов. |
| `Idempotency-Key` | Обязательный непустой HTTP-заголовок до 255 символов. |

Пропущенный ключ идемпотентности, неизвестные поля и некорректные данные возвращают `422`.

Ключ идемпотентности уникален в базе. Повтор с тем же ключом и нормализованным телом возвращает `202`, исходные `payment_id` и `created_at`, а также текущий статус платежа. Повтор после завершения сохраняет результат обработки. Тот же ключ с другим телом возвращает `409`. Порядок ключей JSON, суммы `"1500"` / `"1500.00"` и явно переданные значения полей по умолчанию эквивалентны после валидации. Уникальное ограничение PostgreSQL защищает и от одновременных повторных запросов.

## Чтение, список и история

```bash
curl http://localhost:8000/api/v1/payments/6ac4bca7-4705-4795-8f79-6335c57d794a \
  -H "X-API-Key: $API_KEY"
```

Ответ `200` содержит `payment_id`, `amount`, `currency`, `description`, `metadata`, `idempotency_key`, `webhook_url`, `status`, `created_at`, `processed_at`, `processing_attempts`, `delivery_generation`, `demo_scenario`, `webhook_delivered_at`, `dead_lettered_at` и `last_error`. Сумма сериализуется строкой; даты хранятся в UTC. Неизвестный UUID возвращает `404`, некорректный UUID — `422`.

Статус платежа изменяется `pending → succeeded | failed` и отражает результат шлюза. Ошибка webhook сохраняет уже полученный результат; состояние доставки видно в отдельных полях. `processing_attempts` — номер текущей попытки обработки/доставки от 0 до 3. `delivery_generation` начинается с 0 и увеличивается при ручном восстановлении доставки.

| Endpoint | Ответ |
| --- | --- |
| `GET /api/v1/payments` | `items`, `total`, `limit`, `offset`; новые платежи первыми. |
| `GET /api/v1/payments/{id}/events` | `items`: `id`, `event_type`, `attempt`, `details`, `created_at`; события по времени. |
| `GET /api/v1/payments/{id}/webhooks` | `items`: `id`, `delivery_generation`, `attempt`, `status_code`, `payload`, `created_at`; все серии доставки. |
| `GET /api/v1/stats` | Счётчики платежей, доставки и outbox, `demo_enabled` и UTC-время `as_of`. |

HTTP-попытки сохраняет встроенный демоприёмник; для внешних webhook endpoint `/webhooks` возвращает пустую историю. Результат внешней доставки виден в событиях обработки и полях платежа.

Параметры списка:

| Параметр | Допустимые значения |
| --- | --- |
| `status` | `pending`, `succeeded`, `failed`. |
| `delivery` | `pending`, `retrying`, `delivered`, `dead_lettered`. |
| `search` | Поиск по UUID или описанию без учёта регистра; до 100 символов. |
| `limit` | От 1 до 100; по умолчанию 20. |
| `offset` | От 0 до 100000; по умолчанию 0. |

`delivery=pending` означает отсутствие отметок доставки, DLQ и текущей ошибки; `retrying` — наличие ошибки без завершения доставки. Фильтр доставки независим от результата шлюза.

```bash
curl 'http://localhost:8000/api/v1/payments?delivery=dead_lettered&limit=20' \
  -H "X-API-Key: $API_KEY"
```

Статистика содержит `total`, `pending`, `succeeded`, `failed`, `delivered`, `dead_lettered`, `retrying`, `outbox_pending`, `outbox_published`, `demo_enabled`, `as_of`.

## Демосценарии

```bash
curl http://localhost:8000/api/v1/demo/payments \
  -H "X-API-Key: $API_KEY" \
  -H 'Idempotency-Key: demo-retry-001' \
  -H 'Content-Type: application/json' \
  --data '{"scenario": "retry", "amount": "1490.00", "currency": "RUB"}'
```

Обязателен `scenario`: `success`, `decline`, `retry` или `dlq`. Необязательные поля: `amount` (по умолчанию `"1490.00"`), `currency` (`"RUB"`) и `description` (`"Демонстрационный платёж"`). Денежные ограничения совпадают с обычным платежом. Адрес webhook назначается приложением. Ответ — `202` с `payment_id`, `status`, `created_at`; повтор ключа сохраняет платёж, другой сценарий или тело с тем же ключом возвращает `409`.

Сценарии задают результат в отдельном поле платежа; пользовательские metadata не управляют шлюзом. `success` доставляет успешную оплату с HTTP `204`, `decline` доставляет отказ с HTTP `204`, `retry` отвечает `503`, `503`, `204`, `dlq` отвечает `503` на всех трёх попытках первой серии. После ручного восстановления DLQ-сценарий отвечает `204`.

Приёмник проверяет HMAC-токен, соответствие `payment_id` и, если переданы, заголовков `Idempotency-Key` / `X-Webhook-ID`. Каждый прошедший проверку HTTP-запрос сохраняется с номером попытки и поколения, в том числе повторный запрос той же попытки. Выбор ответа зависит от сохранённого номера попытки, поэтому дубли HTTP-запросов не обходят сценарий повторов.

## Webhook, повторы и восстановление

Consumer отправляет HTTP POST на `webhook_url`. Любой `2xx` означает успешную доставку; остальные коды, ошибки соединения и таймауты приводят к повтору. Redirects автоматически не выполняются. Таймаут по умолчанию — 10 секунд.

```json
{
  "event": "payment.processed",
  "payment_id": "6ac4bca7-4705-4795-8f79-6335c57d794a",
  "amount": "1500.00",
  "currency": "RUB",
  "description": "Оплата заказа 001",
  "metadata": {"order_id": "001"},
  "status": "succeeded",
  "created_at": "2026-11-11T12:00:00+00:00",
  "processed_at": "2026-11-11T12:00:03+00:00"
}
```

Заголовки запроса:

```text
Idempotency-Key: <payment_id>
X-Webhook-ID: <payment_id>
X-Webhook-Event: payment.processed
X-Webhook-Attempt: <1..3>
X-Webhook-Generation: <0..>
```

Стабильный `payment_id` / `Idempotency-Key` предназначен для дедупликации бизнес-события. Номер попытки и поколение помогают различать доставку.

В одной серии — максимум три попытки: первая сразу, вторая через 1 секунду после ошибки, третья через 2 секунды после следующей ошибки. К задержке добавляются время доставки и опроса outbox. Отказ шлюза — терминальный `failed`, о котором тоже отправляется webhook; случайный отказ не перезапускает шлюз. Исключения шлюза повторяются в пределах того же лимита.

После исчерпания попыток заполняются `dead_lettered_at` и `last_error`, а событие публикуется в `payments.dlq` через `payments.dlx`. Основная очередь — `payments.new`, exchange — `payments`. Неизвестные платежи и повреждённые сообщения также попадают в DLQ. Когда PostgreSQL недоступен, сообщения повторяются через durable TTL-очереди RabbitMQ `payments.infrastructure-retry.2` и `.3`.

```bash
curl -X POST http://localhost:8000/api/v1/payments/PAYMENT_UUID/redeliver \
  -H "X-API-Key: $API_KEY"
```

Ручное восстановление возвращает `202` и создаёт новое событие через outbox. Оно доступно только после исчерпания попыток при терминальном результате платежа; в остальных состояниях — `409`, для неизвестного UUID — `404`. Увеличивается `delivery_generation`, сбрасываются счётчик попыток, отметка DLQ и текущая ошибка. `status` и `processed_at` сохраняются; сообщения предыдущего поколения игнорируются. Блокировка строки защищает от нескольких одновременно запущенных серий.

Для внешнего webhook его доступность восстанавливается отдельно. Встроенный DLQ-сценарий переключается на успешный ответ. Исходное сообщение в RabbitMQ DLQ остаётся для диагностики; повторная доставка не очищает очередь.

## Сохранность и границы гарантий

Платёж и первоначальное событие создаются одной транзакцией. Поэтому принятый платёж остаётся в базе при недоступном брокере и будет опубликован позже. Relay отправляет persistent сообщения с publisher confirms и обязательной маршрутизацией; после подтверждения RabbitMQ фиксирует `published_at`. Неудачная публикация остаётся в outbox и повторяется с экспоненциальной задержкой до 60 секунд без ограничения числа попыток. Это отдельная политика от трёх попыток обработки/доставки.

Основная очередь, инфраструктурные TTL-очереди и DLQ используют quorum queues. Перенос сообщений через настроенный dead-letter exchange использует `at-least-once`: исходная очередь сохраняет сообщение до подтверждения приёма целевой очередью. Compose поднимает один узел RabbitMQ; высокая доступность потребует кластера.

Consumer подтверждает сообщение после фиксации результата либо записи повтора/DLQ. Блокировки строк сериализуют обработку одного платежа. Результат шлюза сохраняется отдельной транзакцией **до** HTTP webhook, поэтому повтор доставки использует завершённый результат шлюза.

Гарантия — **at least once**. Если процесс упадёт после публикации в RabbitMQ, но до фиксации `published_at`, событие может прийти повторно. Если webhook уже принял POST, а consumer упал до записи `webhook_delivered_at`, POST может повториться. Получатель дедуплицирует события по `payment_id` или стабильным `Idempotency-Key` / `X-Webhook-ID`. Эмулятор не выполняет реальных списаний; настоящий шлюз должен поддерживать собственный ключ идемпотентности для промежутка между списанием и фиксацией результата в PostgreSQL.

## Health и OpenAPI

```bash
curl http://localhost:8000/health -H "X-API-Key: $API_KEY"
curl http://localhost:8000/openapi.json -H "X-API-Key: $API_KEY"
```

`/health` проверяет подключение к PostgreSQL и возвращает `{"status":"ok"}`. Swagger UI доступен на `/docs`: браузер должен отправлять `X-API-Key` для страницы и схемы, а Authorize задаёт ключ для последующих API-запросов. Полная спецификация доступна через защищённый `/openapi.json`.

## Запуск и состояние данных

API выполняет `alembic upgrade head` перед Uvicorn. Consumer стартует после готовности API и брокера. API и consumer используют один образ с разными командами запуска. Данные PostgreSQL и RabbitMQ находятся в именованных volumes.

```bash
docker compose logs -f api consumer
docker compose stop
docker compose up -d --wait --wait-timeout 180
```

`docker compose down` сохраняет volumes; `docker compose down --volumes` удаляет базу и сообщения.

При обновлении существующей установки прежнее имя Compose-проекта позволяет сохранить доступ к её volumes. Для установки, ранее называвшейся MCC Basis, добавьте в прежний `.env`:

```dotenv
COMPOSE_PROJECT_NAME=mcc-basis
```

Сохраните текущие учётные данные базы, брокера и `API_KEY`. Новый проект по умолчанию называется `payflow`; технические имена базы и пользователей из примера сохранены для совместимости.

## Проверка перезапуска и outbox

Модульные и API-тесты:

```bash
uv sync --frozen
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Основная интеграционная проверка использует запущенные PostgreSQL и RabbitMQ. Она поднимает внутри API временный webhook на порту 8090, доступный consumer как `http://api:8090`; дополнительные порты на хост не открываются.

```bash
docker compose exec -T api python scripts/smoke.py
docker compose exec -T api python scripts/demo_smoke.py
```

Первый скрипт проверяет авторизацию, контракт, валидацию, восемь конкурентных идемпотентных запросов, конфликт ключа, ответы webhook `500 → 500 → 204`, задержки, повтор брокерного сообщения для завершённого платежа и три ошибки до DLQ. Вероятностный ответ шлюза допустим: принимаются `succeeded` и `failed`.

Второй проверяет четыре заданных демосценария, события, HTTP-попытки, список, статистику, фактическое сообщение DLQ и восстановление доставки без изменения результата шлюза. Нужен `DEMO_ENABLED=true`. Скрипты используют уникальные тестовые ключи и сохраняют платежи для просмотра; просмотренные сообщения DLQ возвращаются в очередь.

После полного основного smoke test:

```bash
docker compose restart postgres rabbitmq api consumer
docker compose up -d --no-recreate --wait --wait-timeout 180
docker compose exec -T api python scripts/smoke.py --verify-only
```

Проверяются сохранённые платежи, даты, результат, ключ идемпотентности и сообщение DLQ. Checkpoint лежит в `/tmp` контейнера API и сохраняется при `restart`. `up --force-recreate` пересоздаёт контейнер и удаляет временный checkpoint; volumes базы и брокера сохраняются.

Для проверки принятого платежа при простое consumer:

```bash
docker compose stop consumer
docker compose exec -T api python scripts/smoke.py --seed-pending
docker compose exec -T api python scripts/smoke.py --resume-pending
```

`--seed-pending` создаёт платёж и напрямую проверяет в PostgreSQL, что он остался `pending`, а outbox-запись сохранена и не опубликована. `--resume-pending` запускает webhook и печатает `READY`. Во втором терминале после этого выполните:

```bash
docker compose start consumer
```

Первый терминал проверит доставку, исходную дату создания и `published_at` события. Для проверки недоступного брокера после consumer остановите `rabbitmq`; при восстановлении используйте `docker compose up -d --wait rabbitmq consumer`. Для долгого старта можно задать `--resume-pending --timeout 180`.

## Готовый образ и GHCR

Публичный [релиз 1.1.1](https://github.com/jaFuart/payflow/releases/tag/v1.1.1) содержит `Payflow.zip`, `Payflow-container.tar.gz` и `Payflow.sha256`. Загрузите файлы одного релиза в один каталог и проверьте суммы перед распаковкой:

```bash
sha256sum -c Payflow.sha256
```

Распакуйте `Payflow.zip`, поместите контейнерный архив рядом с `compose.yaml` и из этого каталога выполните:

```bash
docker load -i Payflow-container.tar.gz
cp .env.example .env
docker compose up -d --no-build --wait --wait-timeout 180
```

Архив содержит образ `payflow:local`, соответствующий умолчанию Compose. Если `.env` задаёт другой `APP_IMAGE`, укажите образ из загруженного архива.

GitHub Actions проверяет исходники и интеграцию перед публикацией. Push в `main` публикует `ghcr.io/jafuart/payflow` с тегами `latest`, версии и SHA коммита; pull request выполняет проверки без публикации.

Пакет GHCR требует авторизации GitHub-токеном с `read:packages`:

```bash
docker login ghcr.io
```

Укажите в `.env`:

```dotenv
APP_IMAGE=ghcr.io/jafuart/payflow:1.1.1
```

```bash
docker compose pull api consumer
docker compose up -d --no-build --wait --wait-timeout 180
```

Публичный контейнерный архив позволяет запустить тот же релиз без доступа к закрытому GHCR-пакету.
