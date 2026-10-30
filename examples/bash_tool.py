"""Безопасный bash-инструмент для локального системного ассистента linuxmintai."""
import re
import subprocess

from crewai.tools import BaseTool

DENY = re.compile(
    r"\b(rm\s+-rf\s+/|mkfs|dd\s+if=|wipefs|shred|partprobe|fdisk|pvremove|"
    r"vgremove|lvremove|zfs\s+destroy|format)\b|>\s*/dev/sd|:\(\)\{",
    re.IGNORECASE,
)
DENY_SUFFIX = re.compile(r"\brm\b.*\s-[a-z]*[rf][a-z]*\s+/(\s|$)")

MAX_OUTPUT = 6000
TIMEOUT = 60


class BashTool(BaseTool):
    name: str = "bash"
    description: str = (
        "Выполняет одну shell-команду в системе пользователя и возвращает её вывод. "
        "Аргумент — строка команды. Деструктивные команды блокируются."
    )

    def _run(self, command: str) -> str:
        if DENY.search(command) or DENY_SUFFIX.search(command):
            return f"ОТКАЗАНО: команда в чёрном списке рискованных операций: {command!r}"
        try:
            proc = subprocess.run(
                ["bash", "-lc", command],
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return f"ТАЙМАУТ {TIMEOUT}s: {command!r}"
        out = (proc.stdout + ("\n" + proc.stderr if proc.stderr else "")).strip()
        if len(out) > MAX_OUTPUT:
            out = out[:MAX_OUTPUT] + f"\n...[обрезано, всего {len(out)} символов]"
        return f"exit={proc.returncode}\n{out or '(нет вывода)'}"
