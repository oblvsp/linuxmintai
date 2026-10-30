#!/usr/bin/env bash
# linuxmintai — идемпотентный установщик локального AI-воркспейса (local-only).
# Повторный запуск безопасен: каждый блок пропускается, если компонент уже настроен.
# Флаги: --with-monitor (панель 127.0.0.1:11436 + desklet), --with-webui (Open WebUI),
#        --with-search (SearXNG для web_search/fetch_page агента).
# Логика и обоснование каждого шага — в PROMPT.md.
set -u
cd "$(dirname "$0")"

ok()   { echo "[OK]   $*"; }
skip() { echo "[SKIP] $*"; }
run()   { echo "[RUN]  $*"; "$@" || die "команда не удалась: $*"; }
die()  { echo "[FAIL] $*" >&2; exit 1; }

SUDO=""
rootcmd() {
  if [ "$(id -u)" = 0 ]; then echo "";
  elif sudo -n true 2>/dev/null; then echo "sudo";
  elif command -v pkexec >/dev/null; then echo "pkexec";
  else die "нужен root (sudo или pkexec)"; fi
}

BROKER_YAML="$HOME/.config/bastion/broker.yaml"

WITH_WEBUI=0
WITH_MONITOR=0
WITH_SEARCH=0
for arg in "$@"; do
  case "$arg" in
    --with-webui) WITH_WEBUI=1 ;;
    --with-monitor) WITH_MONITOR=1 ;;
    --with-search) WITH_SEARCH=1 ;;
    *) echo "[WARN] неизвестный флаг: $arg" ;;
  esac
done

# ---------- Шаг 0/1: базовые зависимости ----------
SUDO="$(rootcmd)"

command -v python3 >/dev/null || die "python3 не найден (нужен >=3.10)"
PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
ok "python $PYVER"

if command -v uv >/dev/null; then skip "uv уже установлен"; else
  run bash -c "curl -LsSf https://astral.sh/uv/install.sh | sh"
  grep -q '.local/bin' "$HOME/.bashrc" || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
  export PATH="$HOME/.local/bin:$PATH"
fi

if command -v nvidia-smi >/dev/null; then
  VRAM_MB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1)
  ok "nvidia-smi: ${VRAM_MB} MiB VRAM"
else
  die "нет nvidia-smi — сначала установите драйвер NVIDIA (>=535), см. PROMPT.md шаг 1"
fi

# ---------- Шаг 2.5: OOM-защита (предупреждаем, не ломаем) ----------
SWAP_KB=$(awk '/^SwapTotal/{print $2}' /proc/meminfo)
if [ "$SWAP_KB" -lt 16000000 ]; then
  echo "[WARN] swap $((SWAP_KB/1024)) KB < 16 GB — до создания swap агентов не запускать (см. PROMPT.md шаг 2.5)"
fi
command -v earlyoom >/dev/null && ok "earlyoom установлен" || echo "[WARN] earlyoom не установлен (см. PROMPT.md шаг 2.5)"

# ---------- Шаг 2: Ollama + drop-in ----------
if command -v ollama >/dev/null; then skip "ollama уже установлена"; else
  [ -n "$SUDO" ] || die "установка Ollama требует root: curl -fsSL https://ollama.com/install.sh | sh"
  run $SUDO bash -c "curl -fsSL https://ollama.com/install.sh | sh"
fi

DROPIN=/etc/systemd/system/ollama.service.d/override.conf
# OLLAMA_CONTEXT_LENGTH — серверный дефолт контекста. Нужен потому, что CrewAI/LiteLLM
# ходят в OpenAI-совместимый /v1, где инъекция num_ctx от bastion теряется, и Ollama
# брала бы свои 4096. Держать в согласии с default_num_ctx модели в broker.yaml.
WANT_ENVS=(OLLAMA_HOST=127.0.0.1:11435 OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_NUM_PARALLEL=1 OLLAMA_KEEP_ALIVE=30m OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 OLLAMA_CONTEXT_LENGTH=24576)
need_dropin=1
# читаем drop-in без $SUDO: иначе каждая из 8 проверок при pkexec даёт отдельный диалог
if [ -r "$DROPIN" ]; then
  need_dropin=0
  for e in "${WANT_ENVS[@]}"; do
    grep -qF "$e" "$DROPIN" || need_dropin=1
  done
fi
if [ "$need_dropin" = 1 ]; then
  TMPD=$(mktemp -d)
  { echo "[Service]"; printf 'Environment="%s"\n' "${WANT_ENVS[@]}"; } > "$TMPD/override.conf"
  run $SUDO mkdir -p /etc/systemd/system/ollama.service.d
  run $SUDO cp "$TMPD/override.conf" "$DROPIN"
  run $SUDO systemctl daemon-reload
  run $SUDO systemctl restart ollama
  ok "ollama drop-in применён (127.0.0.1:11435, 1 модель, q8_0 KV)"
else
  skip "ollama drop-in уже корректен"
fi
sleep 2
curl -s -m 5 http://127.0.0.1:11435/api/version >/dev/null || die "ollama не отвечает на 127.0.0.1:11435"

# ---------- Шаг 3: bastion ----------
if command -v bastion >/dev/null; then skip "bastion CLI установлен"; else
  run uv tool install 'bastion-broker[dashboard]'
  export PATH="$HOME/.local/bin:$PATH"
fi
# богаче любой проверки импортов: /broker/status ниже вернёт 500/ошибку, если extra [dashboard] не установлен

mkdir -p "$HOME/.config/systemd/user"
UNIT="$HOME/.config/systemd/user/bastion.service"
if [ -f "$UNIT" ]; then skip "bastion.service существует"; else
  cat > "$UNIT" << 'EOF'
[Unit]
Description=BASTION local LLM broker
After=default.target

[Service]
ExecStart=%h/.local/bin/bastion
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
  ok "создан $UNIT"
fi

# broker.yaml: создать, затем пропаттировать опасные дефолты и добавить модели
if [ ! -f "$BROKER_YAML" ]; then
  mkdir -p "$HOME/.config/bastion"
  bastion --init-config >/dev/null 2>&1 || cat > "$BROKER_YAML" << 'EOF'
ollama:
  host: "127.0.0.1"
  port: 11435
server:
  host: "127.0.0.1"
  port: 11434
gpu:
  total_vram_gb: 0
  headroom_gb: 0.9
  max_temperature_c: 83
scheduler:
  cooldown_seconds: 2.0
  max_queue_size: 512
models: {}
EOF
fi
VRAM_GB=$(( VRAM_MB / 1024 ))
python3 - "$BROKER_YAML" "$VRAM_GB" << 'PYEOF'
import re, sys
path, vram_gb = sys.argv[1], sys.argv[2]
src = open(path).read()
orig = src
# 1) broker и ollama — только loopback; trusted_hosts — для Open WebUI из контейнера
src = re.sub(r'(?s)(server:\n)(.*?)(\n\S|\Z)',
             r'\1  host: "127.0.0.1"\n  port: 11434\n  trusted_hosts: ["localhost", "127.0.0.1", "host.docker.internal"]\3', src)
src = re.sub(r'(?s)(ollama:\n)(.*?)(\n\S|\Z)',
             r'\1  host: "127.0.0.1"\n  port: 11435\3', src)
# 2) GPU: явный total_vram_gb (auto-detect=0 ломает бюджет в user-сервисе).
#    headroom_gb влияет только на max_vram_gb в телеметрии; допуск считает
#    hardware_margin_gb (дефолт 2.0 -> бюджет 6144MB, модели 9B не влезают).
src = re.sub(r'^(\s*)total_vram_gb:.*$', rf'\1total_vram_gb: {vram_gb}  # явное значение: auto-detect не работает в systemd user-сервисе', src, flags=re.M)
src = re.sub(r'^(\s*)headroom_gb:.*$', r'\1headroom_gb: 0.9', src, flags=re.M)
if re.search(r'^\s*hardware_margin_gb:', src, flags=re.M):
    src = re.sub(r'^(\s*)hardware_margin_gb:.*$', r'\1hardware_margin_gb: 0.9', src, flags=re.M)
elif re.search(r'^\s*headroom_gb: 0\.9', src, flags=re.M):
    src = re.sub(r'^(\s*)headroom_gb: 0\.9.*$',
                 r'\1headroom_gb: 0.9\n\1hardware_margin_gb: 0.9', src, flags=re.M, count=1)
else:
    src = src.replace('gpu:\n', 'gpu:\n  hardware_margin_gb: 0.9\n', 1)
# 3) записи моделей: vram_gb — замеренный runtime-VRAM, не размер файла.
#    Завышение запирает модель в очереди (nvidia-smi backstop), занижение — риск OOM.
#    default_num_ctx — потолок промпта: выше 16k скорость падает на треть, зато файлы
#    до 24k токенов доходят целиком (Ollama режет промпт вдвое, как только не влезает).
models = {
    "qwen3.5:9b": ("5.5", 24576, "general"),
}
for name, (gb, ctx, tag) in models.items():
    pattern = re.compile(r'("' + re.escape(name) + r'":\s*\n\s*vram_gb:\s*)[0-9.]+([^\n]*\n\s*default_num_ctx:\s*)[0-9]+')
    src = pattern.sub(lambda m: m.group(1) + gb + m.group(2) + str(ctx), src)
if re.search(r'^models:\s*\{\}', src, flags=re.M):
    src = re.sub(r'^models:\s*\{\}', 'models:', src, flags=re.M)
block = ""
for name, (gb, ctx, tag) in models.items():
    if f'"{name}"' not in src:
        block += (f'\n  "{name}":\n'
                  f'    vram_gb: {gb}\n'
                  f'    default_num_ctx: {ctx}\n'
                  f'    tags: ["{tag}"]\n')
if 'models:' not in src:
    src += '\nmodels:\n'
src = src.rstrip('\n') + block + '\n' if block else src
if src != orig:
    open(path, "w").write(src)
    print("broker.yaml пропатчен: loopback, total_vram_gb, margins, models")
else:
    print("broker.yaml уже корректен")
PYEOF

systemctl --user daemon-reload
systemctl --user enable bastion >/dev/null 2>&1
# bastion — user-юнит; без linger он гаснет после logout
if [ "$(loginctl show-user "$USER" 2>/dev/null | grep -c '^Linger=yes')" != 1 ]; then
  if $SUDO loginctl enable-linger "$USER" 2>/dev/null; then ok "linger включён — bastion переживёт logout"; else echo "[WARN] linger не включён (выполните: pkexec loginctl enable-linger $USER)"; fi
else
  skip "linger уже включён"
fi
systemctl --user restart bastion
sleep 3
systemctl --user is-active --quiet bastion || die "bastion не запустился: journalctl --user -u bastion"
curl -s -m 5 http://127.0.0.1:11434/broker/status >/dev/null || die "/broker/status не отвечает"
ok "bastion активен на 127.0.0.1:11434"

# ---------- Шаг 4: модель qwen3.5:9b ----------
# Единственная модель на все роли: две в 8 GB VRAM всё равно не помещаются,
# а каждая смена модели — выгрузка и загрузка на 5–15 s.
if ollama list | grep -q "qwen3.5:9b"; then
  skip "модель qwen3.5:9b уже в Ollama"
else
  echo "[RUN] скачиваю qwen3.5:9b (~6.6 GB); повторный запуск докачивает"
  OLLAMA_HOST=127.0.0.1:11435 run ollama pull qwen3.5:9b
fi
ollama list | grep -q "qwen3.5:9b" || die "модель так и не появилась в ollama list"
systemctl --user restart bastion; sleep 2

# ---------- Шаг 5/6/7: Python-стек и файлы воркспейса ----------
VENV="$HOME/ai-workspace/venv"
if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c 'import crewai' 2>/dev/null; then
  skip "venv с crewai существует"
else
  mkdir -p "$HOME/ai-workspace"
  run uv venv "$VENV"
  run uv pip install --python "$VENV/bin/python" 'crewai[litellm]' crewai-tools
fi
for f in config_llm.py bash_tool.py web_tools.py agent_demo.py project_memory.py; do
  if [ -f "$HOME/ai-workspace/$f" ]; then echo "[SKIP] ~/ai-workspace/$f (не перезаписываю; свежие эталоны — examples/)"; else
    run cp "examples/$f" "$HOME/ai-workspace/$f"
  fi
done
# PLAN.md правится пользователем, поэтому не перезаписывается; LOG.md создаётся агентом
if [ -f "$HOME/ai-workspace/PLAN.md" ]; then
  echo "[SKIP] ~/ai-workspace/PLAN.md (память проекта)"
else
  run cp examples/PLAN.md "$HOME/ai-workspace/PLAN.md"
fi

# ---------- Шаг 8: Open Interpreter ----------
if command -v interpreter >/dev/null; then
  echo "[WARN] interpreter 0.0.56 установлен, но на 8 GB VRAM непригоден (системный промпт движка ~11.9k токенов, см. PROMPT.md шаг 8) — основной путь: CrewAI-агент"
else
  echo "[SKIP] Open Interpreter не устанавливается: codex-движок 0.0.56 требует контекст, не влезающий в 8 GB VRAM (PROMPT.md шаг 8)"
fi

# ---------- Монитор состояния (опция --with-monitor) ----------
if [ "$WITH_MONITOR" = 1 ]; then
  DESKLET_DIR="$HOME/.local/share/cinnamon/desklets/lmintai-status@vsp"
  mkdir -p "$DESKLET_DIR"
  run cp monitor/status_server.py "$HOME/ai-workspace/status_server.py"
  run cp monitor/lmintai-status.service "$HOME/.config/systemd/user/"
  run cp monitor/desklet/metadata.json monitor/desklet/desklet.js \
        monitor/desklet/stylesheet.css monitor/desklet/settings-schema.json "$DESKLET_DIR/"
  systemctl --user daemon-reload
  systemctl --user enable --now lmintai-status
  sleep 1
  curl -s -m 3 http://127.0.0.1:11436/api/status >/dev/null \
    || die "панель не отвечает: journalctl --user -u lmintai-status"
  ok "панель состояния: http://127.0.0.1:11436/"
  echo "[HINT] desklet установлен; включить: ПКМ по рабочему столу → Desklets → «linuxmintai · состояние»"
  echo "[HINT] после правки desklet.js оболочку нужно перезапустить: cinnamon --replace"
fi

# ---------- Шаг 10 (опция --with-webui): Open WebUI в Docker ----------
if [ "$WITH_WEBUI" = 1 ]; then
  if ! docker info >/dev/null 2>&1; then
    run $SUDO systemctl enable --now docker
  fi
  # юнит-мост контейнер→loopback брокера (route_localnet + DNAT на docker0)
  if ! $SUDO systemctl is-active --quiet bastion-webui-bridge; then
    run $SUDO cp examples/bastion-webui-bridge.service /etc/systemd/system/
    run $SUDO systemctl daemon-reload
    run $SUDO systemctl enable --now bastion-webui-bridge
    ok "bastion-webui-bridge активен"
  else
    skip "bastion-webui-bridge уже активен"
  fi
  if docker ps --format '{{.Names}}' | grep -qx open-webui; then
    skip "контейнер open-webui запущен"
  else
    docker rm -f open-webui 2>/dev/null
    run docker run -d --name open-webui --restart unless-stopped \
      --add-host=host.docker.internal:host-gateway \
      -p 127.0.0.1:3000:8080 \
      -v open-webui:/app/backend/data \
      -e ENABLE_OLLAMA_API=true \
      -e OLLAMA_BASE_URL=http://host.docker.internal:11434 \
      -e SCARF_NO_ANALYTICS=true -e DO_NOT_TRACK=true -e ANONYMIZED_TELEMETRY=false \
      ghcr.io/open-webui/open-webui:main
    ok "Open WebUI: http://127.0.0.1:3000 (первый вход создаёт администратора)"
  fi

  # --- настройки в webui.db ---
  # БД перекрывает env-переменные контейнера, поэтому нужные ключи выставляются явно.
  # Иначе повторная установка/ручная правка в UI тихо возвращают дефолты: RAG режет
  # файлы на чанки, а выключенный ollama.enable даёт «Нет доступных моделей».
  for _ in $(seq 1 60); do
    docker exec open-webui test -f /app/backend/data/webui.db 2>/dev/null && break
    sleep 2
  done
  WEBUI_DIFF=$(docker exec -i open-webui python - <<'PYEOF'
import json, sqlite3

want = {
    "ollama.enable": True,                          # без него WebUI не видит модели
    "rag.full_context": True,                       # файл целиком в промпт, без нарезки на чанки
    "models.default_params": {"max_tokens": 4096},  # reasoning-модель: <2048 даёт пустой content
}

con = sqlite3.connect("/app/backend/data/webui.db")
diff = []
for key, val in want.items():
    row = con.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    cur = json.loads(row[0]) if row else None
    if cur != val:
        con.execute(
            "INSERT INTO config(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(val)),
        )
        diff.append(f"{key}: {cur!r} -> {val!r}")
con.commit()
print("; ".join(diff))
PYEOF
)
  if [ -n "$WEBUI_DIFF" ]; then
    echo "[RUN]  webui.db: $WEBUI_DIFF"
    run docker restart open-webui
    for _ in $(seq 1 60); do
      [ "$(docker inspect -f '{{.State.Health.Status}}' open-webui 2>/dev/null)" = healthy ] && break
      sleep 2
    done
    ok "настройки WebUI применены (конфиг читается при старте — отсюда перезапуск)"
  else
    skip "настройки webui.db уже корректны"
  fi
fi

# ---------- Шаг 10.5 (опция --with-search): SearXNG для веб-инструментов ----------
# Единственный канал наружу: текст поискового запроса уходит в поисковые системы.
# Всё остальное (модель, чат, агент) остаётся на машине.
if [ "$WITH_SEARCH" = 1 ]; then
  if ! docker info >/dev/null 2>&1; then
    run $SUDO systemctl enable --now docker
  fi
  SEARX_DIR="$HOME/.config/searxng"
  mkdir -p "$SEARX_DIR"
  if docker ps -a --format '{{.Names}}' | grep -qx searxng; then
    skip "контейнер searxng существует"
    docker start searxng >/dev/null 2>&1 || true
  else
    run docker run -d --name searxng --restart unless-stopped \
      -p 127.0.0.1:8888:8080 \
      -v "$SEARX_DIR:/etc/searxng" \
      -e SEARXNG_SECRET="$(openssl rand -hex 32)" \
      -e SEARXNG_BASE_URL="http://127.0.0.1:8888/" \
      searxng/searxng:latest
  fi
  for _ in $(seq 1 60); do
    [ "$(curl -s -m 3 -o /dev/null -w '%{http_code}' http://127.0.0.1:8888/ 2>/dev/null)" = 200 ] && break
    sleep 2
  done
  # settings.yml принадлежит uid контейнера: снаружи без root не перезаписать, патчим изнутри
  if docker exec searxng sh -c 'grep -q "json" /etc/searxng/settings.yml' 2>/dev/null; then
    skip "SearXNG: JSON API уже включён"
  else
    docker exec -i -u 0 searxng sh -c 'cat > /etc/searxng/settings.yml' <<'EOF'
use_default_settings: true

server:
  image_proxy: true
  limiter: false
  public_instance: false
  method: "GET"

search:
  safe_search: 1
  formats:
    - html
    - json
EOF
    run docker restart searxng
    sleep 3
    ok "SearXNG: JSON API включён (secret_key берётся из SEARXNG_SECRET)"
  fi
  curl -s -m 20 "http://127.0.0.1:8888/search?q=linux+loopback&format=json" | grep -q '"results"' \
    || die "SearXNG не отдаёт JSON: docker logs searxng"
  ok "поиск работает: http://127.0.0.1:8888/"
fi

# ---------- Финальная проверка ----------
echo; echo "=== Итоговая проверка ==="
bastion --validate 2>&1 | tail -3
ss -tlnp 2>/dev/null | grep -E '1143[456]|:3000|:8888' | grep -v '127.0.0.1' && die "найден не-loopback слушатель!" || ok "слушатели только на 127.0.0.1"
curl -s -m 300 http://127.0.0.1:11434/api/generate -d '{"model":"qwen3.5:9b","prompt":"ping","stream":false,"options":{"num_predict":8}}' >/dev/null && ok "сквозной вызов qwen3.5:9b через bastion прошёл"
if [ "$WITH_MONITOR" = 1 ]; then
  curl -s -m 3 http://127.0.0.1:11436/api/status >/dev/null && ok "панель состояния отвечает" || die "панель не отвечает: journalctl --user -u lmintai-status"
fi
"$VENV/bin/python" -c "import sys; sys.path.insert(0,'$HOME/ai-workspace'); from config_llm import describe_now; print('Модель:', describe_now())"
echo "Готово. Демо: $VENV/bin/python ~/ai-workspace/agent_demo.py \"почини права у ~/.bashrc\""
