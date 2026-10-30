"""Bash-инструмент локального ассистента linuxmintai.

Опасные команды не блокируются насмерть, а спрашивают разрешение у человека:
чёрный список из 12 замеренных опасных команд пропускал 10, поэтому решение
передаётся тому, кто видит команду. Без терминала (systemd, скрипт, агент в
фоне) подтверждения получить не у кого — такие команды не выполняются.
"""
import re
import subprocess
import sys

from crewai.tools import BaseTool

# Легитимных сценариев нет, ошибка стоит системы — не выполняется никогда.
FORBID = [
    (re.compile(r":\(\)\s*\{.*:\s*\|\s*:"), "fork bomb"),
]

# Необратимые или массовые изменения: показываем команду и ждём «да».
RISKY = [
    (re.compile(r"\brm\b"), "удаление файлов"),
    (re.compile(r"\b(mkfs|mke2fs|wipefs|fdisk|parted|sfdisk|pvremove|vgremove|lvremove)\b"
                r"|zfs\s+destroy|btrfs\s+subvolume\s+delete"), "разметка или файловая система"),
    (re.compile(r"\bdd\b"), "прямая запись на устройство"),
    (re.compile(r"\b(shred|truncate)\b"), "затирание или обрезка данных"),
    (re.compile(r">\s*/dev/(sd|nvme|hd|vd|mmcblk)"), "запись в блочное устройство"),
    (re.compile(r">\s*/(etc|boot|usr|var/lib)/"), "перезапись системного файла"),
    (re.compile(r"\bmv\b"), "перемещение файлов (молча затирает цель)"),
    (re.compile(r"\bcp\b.*\s/(etc|boot|usr|bin|sbin|lib|var/lib)(/|\s|$)"),
     "копирование в системный путь"),
    (re.compile(r">>?\s*\S*\.(bashrc|profile|bash_profile|zshrc|bash_aliases)\b"
                r"|>\s*\S*(authorized_keys|\.ssh/)"), "перезапись конфига оболочки или ключей"),
    (re.compile(r"\b(chmod|chown|chgrp)\b.*(-[a-zA-Z]*R[a-zA-Z]*\b|/(etc|usr|boot|bin|sbin|lib|var|home|root)\b)"),
     "смена прав или владельца"),
    (re.compile(r"\b(systemctl|service)\b.*\b(stop|disable|mask|kill|restart|poweroff|reboot|halt)\b"),
     "остановка или отключение службы"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"), "выключение или перезагрузка"),
    (re.compile(r"\b(kill|killall|pkill)\b"), "завершение процессов"),
    (re.compile(r"\b(apt|apt-get|dnf|yum|pacman|zypper|snap)\b.*\b"
                r"(install|remove|purge|autoremove|upgrade|dist-upgrade)\b"), "изменение пакетов"),
    (re.compile(r"\b(crontab|at)\b"), "задачи по расписанию"),
    (re.compile(r"\b(iptables|ip6tables|nft|ufw|firewall-cmd)\b"), "правила файрвола"),
    (re.compile(r"\bgit\b.*\b(reset\s+--hard|clean\s+-[a-zA-Z]*f|push\b.*--force)"),
     "потеря незакоммиченных изменений"),
    (re.compile(r"\b(curl|wget)\b.*\|\s*(sudo|bash|sh)\b"), "выполнение скачанного кода"),
    (re.compile(r"\b(sudo|pkexec|su)\b"), "повышение привилегий"),
]

MAX_OUTPUT = 6000
TIMEOUT = 60


def classify(command: str) -> tuple[str, str | None]:
    """('forbid' | 'confirm' | 'safe', причина) — решение по одной команде."""
    for pattern, why in FORBID:
        if pattern.search(command):
            return "forbid", why
    for pattern, why in RISKY:
        if pattern.search(command):
            return "confirm", why
    return "safe", None


def _confirm(command: str, reason: str) -> bool:
    if not sys.stdin.isatty():
        return False
    print("\n" + "!" * 70, file=sys.stderr)
    print(f"АГЕНТ ПРОСИТ ВЫПОЛНИТЬ: {command}", file=sys.stderr)
    print(f"Почему спрашивает: {reason}", file=sys.stderr)
    try:
        answer = input("Выполнить? введите 'да' для разрешения, Enter — отказ: ")
    except EOFError:
        return False
    return answer.strip().lower() in {"да", "д", "yes", "y"}


class BashTool(BaseTool):
    name: str = "bash"
    description: str = (
        "Выполняет одну shell-команду в системе пользователя и возвращает её вывод. "
        "Аргумент — строка команды. Опасные команды требуют подтверждения пользователя "
        "в терминале; если подтверждения нет, команда не выполняется — предложи безопасную "
        "альтернативу или попроси пользователя выполнить её самому."
    )

    def _run(self, command: str) -> str:
        level, reason = classify(command)
        if level == "forbid":
            return f"ЗАПРЕЩЕНО ({reason}): команда не выполняется ни при каком подтверждении."
        if level == "confirm" and not _confirm(command, reason):
            return (
                f"НЕ ВЫПОЛНЕНО: команда {command!r} требует подтверждения пользователя "
                f"({reason}), но подтверждение не получено. Не повторяй её — предложи "
                "безопасную альтернативу или попроси пользователя выполнить вручную."
            )
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
