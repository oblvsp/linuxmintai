import json
import os
import urllib.request

from crewai import LLM

BROKER_URL = "http://localhost:11434"  # bastion -> Ollama (127.0.0.1:11435)

AGENT_MODEL_ID = os.environ.get("LMINTAI_AGENT_MODEL", "ollama/qwen3.5:9b")
THINK_MODEL_ID = os.environ.get("LMINTAI_THINK_MODEL", "ollama/qwen3.5:9b")

# Одна модель на все роли: в VRAM всё равно помещается одна, а любая смена модели —
# это выгрузка и загрузка 5–15 s. Разделение оставлено на случай экспериментов с env.
agent_llm = LLM(model=AGENT_MODEL_ID, base_url=BROKER_URL, temperature=0.2)
think_llm = LLM(model=THINK_MODEL_ID, base_url=BROKER_URL, temperature=0.4)


def describe_now() -> str:
    """Какая модель сейчас резидентна в VRAM: имя модели + локация, из /broker/status."""
    try:
        with urllib.request.urlopen(f"{BROKER_URL}/broker/status", timeout=2) as r:
            s = json.load(r)
        g = s["gpu"]
        loaded = [m.get("name", "?") for m in (s.get("loaded_models") or [])]
        cur = ", ".join(loaded) if loaded else "none loaded"
        return f"[local:ollama] {cur} | VRAM {g['vram_used_mb']}/{g['vram_total_mb']} MB"
    except Exception as e:
        return f"[local:ollama] broker недоступен: {e}"
