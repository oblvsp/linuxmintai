#!/usr/bin/env bash
# linuxmintai — идемпотентный установщик локального AI-воркспейса (local-only).
# Повторный запуск безопасен: каждый блок пропускается, если компонент уже настроен.
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

HF_URL="https://huggingface.co/rchow93/crewai-qwen3-8b-gguf/resolve/main/crewai-qwen3-8b-thinking-v2-q4_k_m.gguf"
HF_SIZE=5027783936
BROKER_YAML="$HOME/.config/bastion/broker.yaml"

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
WANT_ENVS=(OLLAMA_HOST=127.0.0.1:11435 OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_NUM_PARALLEL=1 OLLAMA_KEEP_ALIVE=30m OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0)
need_dropin=1
if $SUDO test -f "$DROPIN" 2>/dev/null; then
  need_dropin=0
  for e in "${WANT_ENVS[@]}"; do
    $SUDO grep -qF "$e" "$DROPIN" || need_dropin=1
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
  headroom_gb: 6
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
# 1) broker и ollama — только loopback
src = re.sub(r'(?s)(server:\n)(.*?)(\n\S|\Z)',
             r'\1  host: "127.0.0.1"\n  port: 11434\3', src)
src = re.sub(r'(?s)(ollama:\n)(.*?)(\n\S|\Z)',
             r'\1  host: "127.0.0.1"\n  port: 11435\3', src)
# 2) GPU: явный total_vram_gb (auto-detect=0 ломает бюджет в user-сервисе) и headroom
src = re.sub(r'^(\s*)total_vram_gb:.*$', rf'\1total_vram_gb: {vram_gb}  # явное значение: auto-detect не работает в systemd user-сервисе', src, flags=re.M)
src = re.sub(r'^(\s*)headroom_gb:.*$', r'\1headroom_gb: 1.5', src, flags=re.M)
# 3) записи моделей: каждая обязана иметь vram_gb, иначе дефолт 10GB -> 504
models = {
    "qwen3:8b": ("4.9", "general"),
    "qwen3.5:9b": ("6.1", "general"),
    "crewai-qwen3-8b": ("5.0", "agent"),
}
if re.search(r'^models:\s*\{\}', src, flags=re.M):
    src = re.sub(r'^models:\s*\{\}', 'models:', src, flags=re.M)
block = ""
for name, (gb, tag) in models.items():
    if f'"{name}"' not in src:
        block += (f'\n  "{name}":\n'
                  f'    vram_gb: {gb}\n'
                  f'    default_num_ctx: 4096\n'
                  f'    tags: ["{tag}"]\n')
if 'models:' not in src:
    src += '\nmodels:\n'
src = src.rstrip('\n') + block + '\n' if block else src
if src != orig:
    open(path, "w").write(src)
    print("broker.yaml пропатчен: loopback, total_vram_gb, headroom, models")
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

# ---------- Шаг 4: модель crewai-qwen3-8b ----------
if ollama list | grep -q "crewai-qwen3-8b"; then
  skip "модель crewai-qwen3-8b уже в Ollama"
else
  mkdir -p "$HOME/models/crewai"
  GGUF="$HOME/models/crewai/crewai-qwen3-8b.gguf"
  if [ ! -f "$GGUF" ] || [ "$(stat -c %s "$GGUF")" != "$HF_SIZE" ]; then
    echo "[RUN] скачиваю GGUF (4.7 GB, HF throttle 2–6 MB/s) — можно перезапустить, curl resume'ится"
    run curl -sSL -C - -o "$GGUF" "$HF_URL"
    [ "$(stat -c %s "$GGUF")" = "$HF_SIZE" ] || die "размер GGUF не совпадает с ожидаемым"
  fi
  cp -n examples/Modelfile "$HOME/models/crewai/Modelfile" 2>/dev/null || true
  [ -f "$HOME/models/crewai/Modelfile" ] || die "нет ~/models/crewai/Modelfile"

  if ! ollama create crewai-qwen3-8b -f "$HOME/models/crewai/Modelfile" >/dev/null 2>&1; then
    echo "[WARN] прямой create не удался (обычно сервис ollama не может прочитать GGUF из домашней папки) — обход через временный экземпляр"
    OLLAMA_HOST=127.0.0.1:11437 OLLAMA_MODELS="$HOME/.ollama/models" ollama serve >/tmp/ollama_tmp.log 2>&1 &
    TMP_PID=$!
    for i in $(seq 1 30); do curl -s -m 2 http://127.0.0.1:11437/api/version >/dev/null && break; sleep 1; done
    OLLAMA_HOST=127.0.0.1:11437 ollama create crewai-qwen3-8b -f "$HOME/models/crewai/Modelfile" || die "create на временном экземпляре не удался"
    kill "$TMP_PID" 2>/dev/null
    SVC_MODELS=/usr/share/ollama/.ollama/models
    run $SUDO mkdir -p "$SVC_MODELS/blobs" "$SVC_MODELS/manifests-v2"
    run $SUDO cp -a "$HOME/.ollama/models/blobs/." "$SVC_MODELS/blobs/"
    run $SUDO cp -a "$HOME/.ollama/models/manifests-v2/." "$SVC_MODELS/manifests-v2/"
    run $SUDO chown -R ollama:ollama /usr/share/ollama/.ollama/models
    rm -rf "$HOME/.ollama/models"
  fi
  ollama list | grep -q "crewai-qwen3-8b" || die "модель так и не появилась в ollama list"
  ok "crewai-qwen3-8b импортирована"
fi
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
for f in config_llm.py bash_tool.py agent_demo.py; do
  if [ -f "$HOME/ai-workspace/$f" ]; then echo "[SKIP] ~/ai-workspace/$f (не перезаписываю; свежие эталоны — examples/)"; else
    run cp "examples/$f" "$HOME/ai-workspace/$f"
  fi
done

# ---------- Шаг 8: Open Interpreter ----------
if command -v interpreter >/dev/null; then
  echo "[WARN] interpreter 0.0.56 установлен, но на 8 GB VRAM непригоден (системный промпт движка ~11.9k токенов, см. PROMPT.md шаг 8) — основной путь: CrewAI-агент"
else
  echo "[SKIP] Open Interpreter не устанавливается: codex-движок 0.0.56 требует контекст, не влезающий в 8 GB VRAM (PROMPT.md шаг 8)"
fi

# ---------- Финальная проверка ----------
echo; echo "=== Итоговая проверка ==="
bastion --validate 2>&1 | tail -3
ss -tlnp 2>/dev/null | grep -E '1143[45]' | grep -v '127.0.0.1' && die "найден не-loopback слушатель!" || ok "слушатели только на 127.0.0.1"
curl -s -m 60 http://127.0.0.1:11434/api/generate -d '{"model":"crewai-qwen3-8b","prompt":"ping","stream":false,"options":{"num_predict":8}}' >/dev/null && ok "сквозной вызов crewai-qwen3-8b через bastion прошёл"
"$VENV/bin/python" -c "import sys; sys.path.insert(0,'$HOME/ai-workspace'); from config_llm import describe_now; print('Модель:', describe_now())"
echo "Готово. Демо: $VENV/bin/python ~/ai-workspace/agent_demo.py \"почини права у ~/.bashrc\""
