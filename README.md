# ValueBet Engine

Сервис ищет **value bets** на футбольные матчи (топ-5 лиг + ЛЧ, тоталы):
статистическая модель (Dixon-Coles) против коэффициентов букмекеров.
Единственный источник правды по решениям — [SPEC.md](SPEC.md) и
[docs/decisions.md](docs/decisions.md) (decisions главнее при конфликте).

## Стек

Python 3.12 + `uv` · SQLAlchemy 2.0 + Alembic (SQLite) · FastAPI · Streamlit ·
structlog · pydantic-settings · ruff / mypy / pytest.

## Локальный запуск

```bash
cp .env.example .env          # заполнить ключи (см. docs/data-sources.md)
uv sync --dev                 # зависимости
uv run alembic upgrade head   # схема БД (SQLite ./valuebet.db)
uv run python scripts/seed_team_aliases.py   # сид команд из фазы 0

# сервисы-заглушки (фаза 1)
uv run uvicorn api.main:app --port 8000          # GET /health
uv run uvicorn core.scheduler.main:app --port 8001
uv run uvicorn mcp.main:app --port 8002
uv run streamlit run dashboard/app.py            # http://localhost:8501
```

## Загрузка данных (фаза 2)

```bash
uv run python -m core.ingest backfill    # история: Understat (6 сезонов) + football-data + закрывающие тоталы
uv run python -m core.ingest refresh     # текущий сезон (повторная загрузка)
uv run python -m core.ingest events      # TOA /events: кик-оффы, переносы (бесплатно)
uv run python -m core.ingest odds-snapshot --league premier_league --tier T-24      # тоталы, 1 кредит
uv run python -m core.ingest odds-snapshot --league premier_league --markets btts correct_score --tier T-3 --event-id <id>
uv run python -m core.ingest scores      # результаты ЛЧ (2 кредита)
uv run python -m core.ingest backfill --lineups-since 2026-10-22   # составы (1 запрос/матч)
```

Каждый платный вызов TOA проходит квот-гард (жёсткий стоп 450, pacing) и
пишется в `api_quota_usage`/`quota_counters`.

## Проверки

```bash
uv run ruff check .
uv run mypy
uv run pytest
```

## CI

GitHub Actions (`.github/workflows/ci.yml`):

- **lint-type-test** — ruff, mypy, pytest; затем миграция Alembic с нуля + сид
  team_aliases (одна база через `DATABASE_URL`) и `alembic check` — проверка,
  что миграция совпадает с ORM-метаданными.
- **docker-arm64** — сборка образа под `linux/arm64` (buildx + qemu) и
  smoke-прогон `python -c "import soccerdata, core.config"` в образе:
  проверяет нативные зависимости (tls-клиент soccerdata качает
  `tls-client-linux-arm64.so` в рантайме; cffi в lock без linux-колёс —
  компилируется из sdist, поэтому в builder-стейдж установлен gcc/libffi-dev).

## Docker (linux/arm64)

```bash
docker build --platform linux/arm64 -t valuebet-engine:dev .
docker compose up -d            # api/scheduler/mcp/dashboard с healthchecks
```

## Структура

```
core/       config, db, orm + ingest/model/value/news/llm/tracking/backtest/scheduler
api/        REST API (фаза 1: заглушка /health)
mcp/        MCP-сервер для Hermes (фаза 8)
dashboard/  Streamlit (фаза 9)
scripts/    recon-скрипты фазы 0 + сиды
reports/    артефакты-доказательства (включая фазу 0)
alembic/    миграции
docs/       data-sources.md, decisions.md
```

## Конфигурация

`config.yaml` (типизирован в `core/config.py`): лиги с кодами всех источников,
пороги, политика опроса коэффициентов, префильтр кандидатов, букмекеры
(Fortuna — единственный bettable), модели LLM. Секреты — только в `.env`.
