"""Память проекта для локального агента: PLAN.md (что важно знать) и LOG.md (что делали).

Файлы, а не встроенная память CrewAI: векторный store требует эмбеддингов на CPU,
а у нас один GPU и 8 ГБ. Текстовые файлы дешевле, их видно и можно править руками.
"""
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLAN = HERE / "PLAN.md"
LOG = HERE / "LOG.md"

PLAN_LIMIT = 2500
LOG_TAIL = 20
MAX_LOG_BYTES = 64_000


def context_block() -> str:
    parts = []
    if PLAN.exists():
        plan = PLAN.read_text(encoding="utf-8").strip()
        if plan:
            parts.append("### PLAN.md\n" + plan[:PLAN_LIMIT])
    if LOG.exists():
        lines = LOG.read_text(encoding="utf-8").splitlines()[-LOG_TAIL:]
        if lines:
            parts.append("### LOG.md (последние действия)\n" + "\n".join(lines))
    if not parts:
        return ""
    return "--- Память проекта ---\n" + "\n\n".join(parts) + "\n--- конец памяти ---\n"


def append_log(text: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"- {stamp} {text.strip()}\n")
    _trim()


def _trim() -> None:
    if LOG.stat().st_size <= MAX_LOG_BYTES:
        return
    lines = LOG.read_text(encoding="utf-8").splitlines()[-100:]
    LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
