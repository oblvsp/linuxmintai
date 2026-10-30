# linuxmintai

**Приватный AI-ассистент системного администрирования.** Всё считается локально: ни один запрос не покидает машину, API-ключей и облачных маршрутов в архитектуре нет.

![local-only](https://img.shields.io/badge/local--only-no%20cloud-2ea44f)
![Ollama](https://img.shields.io/badge/Ollama-0.40-000000)
![CrewAI](https://img.shields.io/badge/CrewAI-1.15-ff6b6b)
![VRAM](https://img.shields.io/badge/VRAM-%E2%89%A5%208%20GB-critical)
![Docker](https://img.shields.io/badge/Docker-optional-2496ed)

Ассистент настраивает и обслуживает ОС: диагностика, права, сервисы, память. Код этот ассистент **не пишет** — для программирования осознанно используются внешние облачные модели. Это разделение ролей зафиксировано в [PROMPT.md, шаг 9](PROMPT.md) и не является недоработкой.

---

## Как это работает

```mermaid
flowchart LR
    A["Терминал<br/><code>agent_demo.py</code>"] -->|OpenAI API| B
    C["Open WebUI<br/>Docker · :3000"] -->|host.docker.internal| B
    B["bastion-broker<br/>127.0.0.1:11434"] -->|VRAM-бюджет,<br/>очередь загрузки| D["Ollama<br/>127.0.0.1:11435"]
    D --> E[("RTX 2070 SUPER<br/>8 GB VRAM")]
```

Ключевая идея — **bastion-broker** между клиентами и Ollama. На 8 ГБ VRAM помещается ровно одна 8B-модель, поэтому все запросы проходят через один сериализованный шлюз: он ведёт учёт VRAM, не даёт двум моделям одновременно претендовать на память и отдаёт телеметрию на `/broker/status`.

## Компоненты

| Компонент | Где живёт | Адрес | Роль |
|---|---|---|---|
| **Ollama** | системный сервис | `127.0.0.1:11435` | движок инференса, flash-attention, KV-кэш q8_0 |
| **bastion-broker** | user-юнит (с linger) | `127.0.0.1:11434` | VRAM-бюджет, очередь, прокси к Ollama |
| **CrewAI-агент** | `~/ai-workspace/venv` | — | агент с инструментом `bash` |
| **Open WebUI** | Docker-контейнер | `127.0.0.1:3000` | чат-интерфейс (опционально) |
| **Мост WebUI → bastion** | `bastion-webui-bridge` | — | route_localnet + DNAT на `docker0` |
| **Панель состояния** | user-юнит `lmintai-status` | `127.0.0.1:11436` | GPU/VRAM/RAM и фаза LLM (опционально) |
| **Desklet** | Cinnamon | — | те же данные на рабочем столе |

Все слушатели — bastion, Ollama и WebUI — привязаны строго к loopback. Проверка после любой настройки:

```bash
ss -tlnp | grep -E ':1143[45]'   # в выводе должны быть только 127.0.0.1
```

## Модели

| Модель | Назначение | VRAM (ledger) | Примечание |
|---|---|---|---|
| `crewai-qwen3-8b` | агент с инструментами | 5.1 GB | Q4_K_M, столько же занимает на GPU |
| `qwen3.5:9b` | развёрнутые ответы без инструментов | 5.5 GB | `num_ctx` 24576: файлы до ~24k токенов не обрезаются, часть слоёв уходит в CPU |
| `qwen3:8b` | общие вопросы | 5.1 GB | базовая модель |

`vram_gb` — **замеренный runtime-VRAM** (`size_vram` из `/api/ps`), а не размер файла: размер файла занижает (не учитывает KV-кэш). Завышенное значение запирает модель в очереди по `nvidia-smi backstop`.

Установщик импортирует только `crewai-qwen3-8b`; `qwen3:8b` и `qwen3.5:9b` ставятся обычным `ollama pull`.

> **Добавляя модель, сразу пропишите её в `~/.config/bastion/broker.yaml`.** Незаявленная модель получает дефолтную оценку 10 GB, которая не влезает в бюджет 7.1 GB, — запрос зависнет на 300 с и вернёт 504. Оценку VRAM можно снять командой `bastion --detect-models`.

## Требования

| Что | Минимум | Зачем |
|---|---|---|
| GPU | NVIDIA, 8 GB VRAM, драйвер ≥ 535 | одна 8B-модель целиком на GPU |
| RAM / swap | ОЗУ 16 GB+ (на целевой машине 32 GB), swap ≥ 16 GB | защита от OOM-заморозок (шаг 2.5) |
| Python | 3.10 – 3.13 | требование CrewAI |
| root | sudo или pkexec | установка Ollama, drop-in, мост WebUI |

`uv`, `ollama`, `bastion-broker` и Python-стек ставит сам установщик.

## Быстрый старт

```bash
git clone https://github.com/oblvsp/linuxmintai && cd linuxmintai

./install.sh                # агентный стек: шаги 0–8
./install.sh --with-monitor # + панель состояния и desklet (порт 11436)
./install.sh --with-webui   # то же + Open WebUI в Docker (шаг 10)
```

Скрипт идемпотентен: настроенные блоки пропускаются, повторный запуск безопасен. В конце он сам проверяет, что слушатели только на loopback, и делает сквозной вызов модели через bastion.

## Использование

### Агент в терминале

```bash
~/ai-workspace/venv/bin/python ~/ai-workspace/agent_demo.py \
  "Покажи, сколько свободной памяти, и предложи одно безопасное действие"
```

Агент работает с `BashTool`: деструктивные команды блокируются чёрным списком, а в конце выводится, какая модель реально была резидентна и сколько VRAM занято.

Модель можно переопределить переменными окружения:

```bash
LMINTAI_AGENT_MODEL=ollama/qwen3:8b \
LMINTAI_THINK_MODEL=ollama/qwen3.5:9b \
  ~/ai-workspace/venv/bin/python ~/ai-workspace/agent_demo.py "вопрос"
```

### Чат в браузере

Откройте <http://127.0.0.1:3000>. При первом входе Open WebUI предлагает создать администратора — сделайте это сразу, контейнер доступен только с этой машины, но аккаунт всё равно ваш.

В **Settings → Model** для обеих thinking-моделей выставьте `max_tokens` ≥ 2048 (глобальный дефолт 4096 уже задан в `models.default_params`). `num_ctx` вручную не трогайте — его подставляет bastion из `default_num_ctx` (24576 для qwen3.5:9b). С меньшим бюджетом модель тратит его целиком на скрытое рассуждение и возвращает пустой ответ — это и есть причина «непонятного вывода» в прошлых прогонах.

### Напрямую через API

bastion отдаёт OpenAI-совместимый эндпоинт (только без стриминга — на `stream:true` он вернёт буферизованное тело, для чата используйте `/api/*`):

```bash
curl -s http://127.0.0.1:11434/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"crewai-qwen3-8b","max_tokens":2048,
       "messages":[{"role":"user","content":"Назови три команды для проверки места на диске"}]}' \
  | jq -r '.choices[0].message.content'
```

### Панель состояния

Откройте <http://127.0.0.1:11436/> — отдельная вкладка рядом с чатом. Панель обновляется раз в секунду и показывает фазу работы LLM, а не только «модель загружена»:

| Фаза | Когда |
|---|---|
| **простой** | запросов нет, модель резидентна |
| **очередь** | запрос ждёт освобождения VRAM (имя модели — из `queue_by_model`) |
| **загрузка модели** | идёт подкачка весов в VRAM |
| **генерация** | модель отвечает |
| **bastion недоступен** | брокер не отвечает, показана подсказка |

Плюс VRAM/GPU/температура, RAM и swap, число запросов и переключений моделей. Данные склеиваются из `/broker/status` и `/proc`, сервис — 150 строк на stdlib без зависимостей (`lmintai-status.service`, loopback).

Там же два API: `/api/status` (JSON) и `/api/text` (4 строки для desklet/Conky).

**Desklet Cinnamon:** установите через `./install.sh --with-monitor`, затем ПКМ по рабочему столу → **Desklets** → «linuxmintai · состояние». Виджет читает `/api/text` каждые 2 секунды (интервал меняется в настройках). После правки `desklet.js` оболочку нужно перезапустить: `cinnamon --replace`.

## Подводные камни

Всё ниже поймано при развёртывании, а не пересказано из документации.

| Симптом | Причина | Решение |
|---|---|---|
| 504 через 300 с, в логе `need 10240MB, available 0MB` | auto-detect VRAM возвращает 0 в systemd user-сервисе; неизвестная модель оценивается в 10 GB | `gpu.total_vram_gb: 8` явно + запись модели в `models:` |
| Очередь стоит 60 с, затем `nvidia-smi backstop: only X GB free, need Y + margin` | бюджет допуска задаёт `hardware_margin_gb` (по умолчанию 2 GB), а `headroom_gb` влияет только на `max_vram_gb`; `vram_gb` взят из размера файла и завышен | `hardware_margin_gb: 0.9` + `vram_gb` по замеру `size_vram` из `/api/ps` |
| Пустой ответ, `done_reason=length` | thinking-модель съела весь бюджет на скрытое рассуждение | `max_tokens` ≥ 2048, `num_predict` ≥ 1024; `thinking:false` не помогает |
| Длинный файл молча теряет хвост | Ollama обрезает промпт только когда он длиннее `num_ctx`, и тогда режет сразу вдвое (`limit = num_ctx − (num_ctx − keep)/2`) | `default_num_ctx: 24576` для qwen3.5:9b; файлы до ~24k токенов проходят целиком, но часть слоёв уходит в CPU |
| WebUI «висит» на ответе, в ответе `content-type: application/json` и тело `{"raw":"data: … [DONE]"}` | bastion 0.6.0 буферизует `/v1/*` и не отдаёт SSE на `stream:true` | подключать WebUI нативным Ollama-API (`ENABLE_OLLAMA_API=true` + `OLLAMA_BASE_URL`), OpenAI-провайдер выключить |
| В списке моделей дубли и ошибки соединения | остался включённым OpenAI-провайдер с base URL `.../v1` | Admin → Settings → Connections: очистить base URL и ключи OpenAI |
| `ollama create` падает без внятной ошибки | сервис работает от `ollama`, а домашний каталог с GGUF — `0750` | импорт через временный `ollama serve` от пользователя + копирование `blobs/` и `manifests-v2/` |
| WebUI: пустой список моделей, затем HTTP 400 | контейнер не видит loopback; bastion отбивает чужой Host | мост `route_localnet`/DNAT + `server.trusted_hosts` |
| Desklet показывает `Ð¾ÑÐµÑÐµÐ´Ñ` | GJS трактует байты curl как Latin-1 | декодировать UTF-8 вручную (см. `monitor/desklet/desklet.js`) |
| Демо пишет не ту модель, что загружена | поле `current_model` в `/broker/status` устаревает | читать `loaded_models` |

Полные обоснования и команды — в [PROMPT.md](PROMPT.md), по шагам.

## Безопасность

- **Только loopback.** bastion и Ollama слушают `127.0.0.1`; WebUI публикуется как `-p 127.0.0.1:3000:8080`, а не `0.0.0.0`.
- **Никаких секретов.** Облачных API-ключей в системе нет по определению; ключ WebUI к bastion — фиктивная строка.
- **Агент осторожен.** Чёрный список `BashTool` не ослаблять; подтверждения опасных команд в Open Interpreter не отключать.
- **OOM-защита обязательна.** swap ≥ 16 GB, earlyoom `-m 15 -s 60`, `vm.swappiness=10` — реальный инцидент с фризом разобран в PROMPT.md, шаг 2.5.

## Open Interpreter: почему не используется

Установлен (0.0.56), но на 8 ГБ VRAM непригоден: его codex-движок отправляет системный промпт и схемы инструментов на ~11 900 токенов. Контекст такого размера требует KV-кэш, не влезающий в 8 ГБ вместе с весами модели. Агентные задачи закрывает CrewAI-агент с явным набором инструментов. Расчёты и CLI-синтаксис — PROMPT.md, шаг 8.

## Структура репозитория

| Путь | Что это |
|---|---|
| [`install.sh`](install.sh) | идемпотентный установщик, флаги `--with-monitor`, `--with-webui` |
| [`PROMPT.md`](PROMPT.md) | пошаговый промпт развёртывания: команды, обоснования, ловушки |
| [`examples/config_llm.py`](examples/config_llm.py) | LLM-объекты CrewAI + `describe_now()` |
| [`examples/bash_tool.py`](examples/bash_tool.py) | bash-инструмент с чёрным списком |
| [`examples/agent_demo.py`](examples/agent_demo.py) | демо-агент |
| [`examples/Modelfile`](examples/Modelfile) | шаблон чата для импорта GGUF |
| [`examples/broker.yaml.example`](examples/broker.yaml.example) | эталонный конфиг bastion |
| [`examples/bastion.service`](examples/bastion.service) | user-юнит брокера |
| [`examples/bastion-webui-bridge.service`](examples/bastion-webui-bridge.service) | мост контейнер → loopback |
| [`monitor/status_server.py`](monitor/status_server.py) | панель состояния: JSON + HTML на `:11436` |
| [`monitor/lmintai-status.service`](monitor/lmintai-status.service) | user-юнит панели |
| [`monitor/desklet/`](monitor/desklet/) | desklet Cinnamon (метаданные, JS, стили, настройки) |

## Статус

Стек развёрнут и проверен в бою на целевой машине (Mint 22.3, i5-8400, 32 ГБ RAM, RTX 2070 SUPER 8 ГБ) 2026-10-30: демо-агент выполнял системные команды, модель резидентна на GPU, `bastion --validate` — 0 failed, WebUI и панель состояния отвечают по loopback, desklet показывает живые данные. Известные ограничения: одна модель в VRAM одновременно, переключение между моделями занимает 5–15 с, контекст больше ~16k токенов обрабатывается с вытеснением части слоёв в CPU (медленнее).
