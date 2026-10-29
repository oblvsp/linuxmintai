Роль: Ты — опытный DevOps-инженер и специалист по локальным AI-системам. Твоя задача — развернуть на Linux Mint гибридный AI-воркспейс с двумя маршрутами: (1) primary — бесплатные модели OpenRouter через ollama-free-model-proxy от aljazceru (с фильтром tool use); (2) fallback — локальная crewai-qwen3-8b через Ollama, защищённая bastion-broker. Все обращения CrewAI идут сначала в OpenRouter, при ошибке — автоматический откат на локальную модель. В ответе каждого вызова должно быть видно имя модели и локация (cloud:openrouter / local:ollama). Целевое железо: i5-8400, 32 GB RAM, RTX 2070 SUPER (8 GB VRAM). Критическое ограничение: 8 ГБ VRAM. Выполняй шаги последовательно.

ШАГ 0: ПРЕДВАРИТЕЛЬНАЯ ПРОВЕРКА (обязательно перед любыми установками).

Проверь наличие каждого компонента. Если компонент уже есть — пропускай его установку. Используй command -v для проверки исполняемых файлов , dpkg -l | grep для пакетов, ollama list для моделей.

# 0.1 Python (требование CrewAI: >=3.10, <3.14) [citation:1]
python3 --version

# 0.2 uv (менеджер пакетов)
command -v uv >/dev/null 2>&1 && echo "uv: OK" || echo "uv: MISSING"

# 0.3 NVIDIA driver (должен быть >= 535) 
nvidia-smi

# 0.4 Docker
docker -v

# 0.5 NVIDIA Container Toolkit 
dpkg -l | grep nvidia-container-toolkit

# 0.6 Ollama
command -v ollama >/dev/null 2>&1 && ollama -v || echo "ollama: MISSING"

# 0.7 bastion-broker (Python-пакет)
python3 -c "import bastion" 2>/dev/null && echo "bastion: OK" || echo "bastion: MISSING"

# 0.8 ollama-free-model-proxy (наличие репозитория и бинарника)
test -d ~/ollama-free-model-proxy && echo "proxy repo: OK" || echo "proxy repo: MISSING"

# 0.9 Модель crewai-qwen3-8b в Ollama
ollama list | grep -q "crewai-qwen3-8b" && echo "model: OK" || echo "model: MISSING"

# 0.10 Open Interpreter
command -v interpreter >/dev/null 2>&1 && interpreter --version || echo "interpreter: MISSING"

# 0.11 CrewAI
command -v crewai >/dev/null 2>&1 && crewai version || echo "crewai: MISSING"

Правило: для каждого MISSING — выполни соответствующий шаг ниже. Для каждого OK — пропусти установку и переходи к следующей проверке. Не переустанавливай то, что уже работает.

Шаг 1: Системные зависимости (только если MISSING). Если nvidia-smi отсутствует или версия драйвера < 535 — установи драйвер. Если docker отсутствует — установи Docker. Если nvidia-container-toolkit отсутствует — установи его .

# Только для отсутствующих компонентов:
sudo apt update
# Драйвер (если nvidia-smi не работает):
sudo apt install -y nvidia-driver-580 nvidia-cuda-toolkit
# Docker (если docker -v не работает):
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# NVIDIA Container Toolkit (если dpkg не находит):
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
# Проверка:
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi

Шаг 2: Ollama (только если MISSING). Если ollama -v не работает:

curl -fsSL https://ollama.com/install.sh | sh
echo 'export OLLAMA_MAX_LOADED_MODELS=1' >> ~/.bashrc
echo 'export OLLAMA_NUM_PARALLEL=1' >> ~/.bashrc
echo 'export OLLAMA_KEEP_ALIVE=30m' >> ~/.bashrc
source ~/.bashrc
sudo systemctl restart ollama

Если Ollama уже установлена — проверь, что переменные окружения заданы, но не переустанавливай.

Шаг 3: bastion-broker (только если MISSING). Если import bastion не работает:

pip install bastion-broker
# Перенастрой Ollama на внутренний порт (если ещё не сделано):
sudo mkdir -p /etc/systemd/system/ollama.service.d/
sudo tee /etc/systemd/system/ollama.service.d/override.conf > /dev/null << 'EOF'
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11435"
EOF
sudo systemctl daemon-reload && sudo systemctl restart ollama
bastion --init-config && bastion --detect-models && bastion --validate

bastion слушает 11434 и проксирует к Ollama на 11435 .

Шаг 4: Модель crewai-qwen3-8b (только если MISSING). Если ollama list не содержит модель:

mkdir -p ~/models/crewai && cd ~/models/crewai
wget https://huggingface.co/rchow93/crewai-qwen3-8b-gguf/resolve/main/crewai-qwen3-8b-thinking-v2-q4_k_m.gguf
cat > Modelfile << 'EOF'
FROM ./crewai-qwen3-8b-thinking-v2-q4_k_m.gguf
TEMPLATE """{{- if .System }}<|im_start|>system
{{ .System }}<|im_end|>
{{ end }}<|im_start|>user
{{ .Prompt }}<|im_end|>
<|im_start|>assistant
"""
SYSTEM """You are a CrewAI code generation expert."""
PARAMETER temperature 0.7
PARAMETER num_ctx 8192
PARAMETER stop "<|im_end|>"
PARAMETER stop "<|im_start|>"
EOF
ollama create crewai-qwen3-8b -f Modelfile
ollama run crewai-qwen3-8b "test"

Шаг 5: ollama-free-model-proxy (только если MISSING). Если репозиторий отсутствует:

git clone https://github.com/aljazceru/ollama-free-model-proxy.git ~/ollama-free-model-proxy
cd ~/ollama-free-model-proxy
# ⚠️ Сверься с README репозитория: команды установки и порт могут отличаться.
# Согласно README, прокси слушает 11434 по умолчанию, но у нас этот порт занят bastion.
# Запусти прокси на 11436 (или другом свободном порту):
export OPENAI_API_KEY="your-openrouter-key"
export TOOL_USE_ONLY=true   # фильтр только tool-capable free-моделей [citation:3][citation:6]
# Запуск: см. README (может быть ./ollama-proxy или иная команда)
# Установщик должен запросить OPENROUTER_API_KEY в интерактивном режиме.

Ключ получить на https://openrouter.ai/keys. Важно: bastion уже занял 11434, поэтому прокси должен слушать другой порт (например, 11436). Уточни в README, как задать порт.

Шаг 6: Python-стек (только для MISSING компонентов). Если interpreter или crewai отсутствуют:

# uv (если отсутствует):
curl -LsSf https://astral.sh/uv/install.sh | sh
# venv и зависимости:
uv venv ~/ai-workspace/venv && source ~/ai-workspace/venv/bin/activate
uv pip install 'crewai[litellm]' crewai-tools
# Open Interpreter (если interpreter отсутствует):
curl -fsSL https://www.openinterpreter.com/install | sh

Шаг 7: Маршрутизатор с видимой моделью. Создай ~/ai-workspace/config_llm.py (перезаписывай только если хочешь обновить логику):

import os
from crewai import LLM

CLOUD_BASE_URL = "http://localhost:11436"   # порт ollama-free-model-proxy (уточни в README)
LOCAL_BASE_URL = "http://localhost:11434"   # порт bastion -> Ollama

CLOUD_MODEL_ID = "ollama/auto"   # auto-selected free-модель с tool use
LOCAL_MODEL_ID = "ollama/crewai-qwen3-8b"

cloud_llm = LLM(model=CLOUD_MODEL_ID, base_url=CLOUD_BASE_URL, temperature=0.2)
local_llm = LLM(model=LOCAL_MODEL_ID, base_url=LOCAL_BASE_URL, temperature=0.2)

class VisibleRouterLLM:
    def __init__(self):
        self.last_used = None

    def call(self, messages, tools=None, **kwargs):
        try:
            resp = cloud_llm.call(messages=messages, tools=tools, **kwargs)
            self.last_used = {"model": CLOUD_MODEL_ID, "location": "cloud:openrouter", "reason": None}
            return resp
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            print(f"[router] OpenRouter недоступен → fallback. Причина: {reason}")
        resp = local_llm.call(messages=messages, tools=tools, **kwargs)
        self.last_used = {"model": LOCAL_MODEL_ID, "location": "local:ollama", "reason": reason}
        return resp

    def describe_last(self) -> str:
        if not self.last_used:
            return "no calls yet"
        m = self.last_used
        tag = f"[{m['location']}] {m['model']}"
        return f"{tag}  (fallback: {m['reason']})" if m["reason"] else tag

router = VisibleRouterLLM()

В agent_crew.py после crew.kickoff():

from config_llm import router
result = crew.kickoff(inputs=inputs)
print(f"\nОтветила модель: {router.describe_last()}")

Шаг 8: Проверка Open Interpreter (локально, приватные задачи).

interpreter --local --os --model ollama/crewai-qwen3-8b

Шаг 9: Политика и безопасность.

По умолчанию — OpenRouter (primary). Локальный fallback срабатывает при 429, timeout, отсутствии tool-capable free-модели.

Приватные данные — только локально: используй отдельный Agent с llm=local_llm или Open Interpreter с --local.

Не отправляй секреты, проприетарный код и личные файлы через ollama-free-model-proxy — запросы уходят на сервер OpenRouter.

Rate limits OpenRouter: ~20/мин, 50/день при балансе <10 кредитов (до 1000/день при ≥10). При частых 429 роутер будет уходить в fallback, и локальная 8B быстро упрётся в потолок.

Open Interpreter: не отключай подтверждение опасных команд.

Выполни Шаг 0, зафиксируй результаты проверки, затем выполни только те шаги, где компоненты отсутствуют. Сообщи итоговый статус каждого компонента и результат тестового вызова CrewAI с указанием ответившей модели и локации.