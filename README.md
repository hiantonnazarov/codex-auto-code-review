# Codex Auto Code Review

Автоматическое независимое ревью **только изменений кода**, полученных из события `turn/diff/updated` Codex App Server.

> **Важное ограничение:** одного глобального `Stop` hook недостаточно. Нужен Codex-клиент, который запускает `appserver-proxy` вместо `codex app-server`, либо который передаёт исходные события через `appserver-ingest`. Codex Desktop не подключается к уже работающему App Server автоматически. Если событий нет, ревью завершается ошибкой, **без резервного Git diff**.

## Что проверяется

- Только последний агрегированный `diff` конкретного `threadId + turnId` из App Server.
- Только код: `.py`, `.js`, `.ts`, `.go`, `.rs`, `.java` и другие программные расширения, а также `Dockerfile` и `Makefile`. Markdown, README и остальные документы не анализируются.
- Только настраиваемые исключения `exclude_globs`: нет особых правил для `auth`, `security`, `api` и подобных папок, нет списка включений.
- Режим `significant`: не менее **30 изменённых строк кода или 2 файлов кода**. Режим `all`: любое изменение кода.
- Новые/старые committed или staged изменения, которых **нет** в diff App Server, не анализируются. История Git, HEAD, index и локальный общий diff **не читаются** для определения области ревью.

**Нельзя гарантировать исключение изменений, закоммиченных внутри текущего хода:** App Server не содержит флага «коммит уже создан». Без дополнительного чтения Git такая гарантия технически недостижима, а Git-проверки исключены по требованиям.

## Как подключить

```text
Codex-клиент с поддержкой пользовательской команды запуска App Server
        │ JSON-RPC stdio
        ▼
codex-auto-review appserver-proxy
        │ неизменный JSON-RPC
        ▼
codex app-server --listen stdio://
        │ turn/started + turn/diff/updated
        ▼
Локальное состояние по threadId/turnId
        │
Codex Stop hook ──> фильтр кода ──> отдельный codex exec (read-only)
        │
        └──> отчёт JSON / замечания в исходный ход
```

`appserver-proxy` — прозрачный посредник, **не** самостоятельный клиент. Он работает, только если ваш Codex-клиент позволяет указать вместо запуска `codex app-server` команду `codex-auto-review appserver-proxy`. Он не перехватывает чужие или уже запущенные процессы Codex Desktop.

### Windows PowerShell

```powershell
py -m pip install --user git+https://github.com/hiantonnazarov/codex-auto-code-review.git
py -m codex_auto_review.cli install
py -m codex_auto_review.cli doctor
```

Затем настройте *в своём App Server клиенте* запуск сервера командой:

```text
codex-auto-review appserver-proxy
```

Способ настройки зависит от клиента; не все клиенты это поддерживают. Разрешите установленные `UserPromptSubmit` и `Stop` hooks в Codex.

Если вы разрабатываете собственный клиент App Server, можно передавать реальные JSONL-уведомления на stdin:

```powershell
py -m codex_auto_review.cli appserver-ingest
```

По одной JSON-RPC notification на строку, с `method` и `params`. Данные из Git, журнала Codex, вызовов инструментов и текста LLM не принимаются как источник diff.

## CLI

| Команда | Назначение |
|---|---|
| `install` / `uninstall` | Добавить / удалить только собственные хуки |
| `appserver-proxy` | Пересылать JSON-RPC stdio и сохранять App Server diff |
| `appserver-ingest` | Принимать уведомления App Server по JSONL |
| `doctor` | Проверить зависимости, установку и факт доставки hooks |
| `config` | Показать действующие настройки |
| `status --session ID` | Показать состояние сессии |
| `review --cwd . --session THREAD_ID --turn TURN_ID` | Проверить ранее сохранённый App Server diff |

Ручной `review` никогда не обращается к `HEAD` и не собирает незакоммиченные изменения.

Настройки в [config.toml](config.toml); приоритет: встроенные значения → глобальный `~/.codex-auto-code-review/config.toml` → локальный устаревший `.codex-auto-review.toml` → корневой `config.toml` с таблицей `[review]`.

## Что нельзя гарантировать без дополнительного источника

1. App Server публикует `turn/diff/updated` после соответствующих `fileChange`, но некоторые изменения через скрипты могут не попасть в событие (см. [openai/codex#41109](https://github.com/openai/codex/issues/41109)). Нет события — нет возможности узнать о такой правке.
2. Даже если `turn/started` пришёл, отсутствие `turn/diff/updated` означает только **отсутствие наблюдаемых изменений**, а не доказательство, что файлы на диске не менялись.
3. Исключить уже закоммиченные внутри хода фрагменты невозможно без Git-сверки. Такая сверка намеренно не реализована.
4. Сессии других агентов не анализируются, их изменения не сопоставляются. Нет конфликт-детектора и нет worktrees.
5. Возврат замечаний в тот же ход по `Stop` зависит от поддержки событий выбранным Codex-хостом. Регистрация hook не гарантирует его выполнения.
6. Reviewer вправе читать локальный код для проверки контекста и строк, но не получает Git diff и не расширяет список файлов для ревью.

## Тестирование

```powershell
py -m pip install -e . pytest
py -m pytest -q
```

Unit-тесты используют поддельный reviewer. Реальный smoke test требует авторизованного Codex CLI, клиента с подключённым App Server relay и работающего `Stop` hook.

Лицензия MIT.
