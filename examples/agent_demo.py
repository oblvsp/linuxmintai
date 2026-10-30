"""Демо системного ассистента linuxmintai: агент с bash-инструментом на локальной модели.

Запуск: ~/ai-workspace/venv/bin/python ~/ai-workspace/agent_demo.py "почини права у ~/.bashrc"
Модель агента: LMINTAI_AGENT_MODEL (по умолчанию ollama/qwen3.5:9b).
"""
import sys

from crewai import Agent, Crew, Task

from bash_tool import BashTool
from config_llm import agent_llm, describe_now
from project_memory import append_log, context_block


def main() -> None:
    question = " ".join(sys.argv[1:]) or "Покажи, сколько памяти свободно, и предложи одно действие."

    agent = Agent(
        role="Системный администратор Linux",
        goal="Помочь пользователю настроить и починить систему безопасными обратимыми командами.",
        backstory=(
            "Опытный администратор. Перед деструктивными действиями останавливается и спрашивает. "
            "Предпочитает диагностику до лечения. Память проекта (PLAN.md и последние записи LOG.md) "
            "приходит в конце задачи — учитывай её и не повторяй уже сделанное."
        ),
        llm=agent_llm,
        tools=[BashTool()],
        verbose=True,
        max_iter=6,
    )

    memory = context_block()
    task = Task(
        description=question if not memory else f"{question}\n\n{memory}",
        expected_output="Краткий отчёт: что сделано/найдено, риски, следующий шаг.",
        agent=agent,
    )

    crew = Crew(agents=[agent], tasks=[task], verbose=True)
    result = crew.kickoff()
    append_log(f"запрос: {question} | итог: {str(result).strip()[:160]}")

    print("\n" + "=" * 60)
    print("РЕЗУЛЬТАТ:")
    print(result)
    print("=" * 60)
    print(f"Ответила модель: {describe_now()}")


if __name__ == "__main__":
    main()
