"""Демо системного ассистента linuxmintai: агент с bash-инструментом на локальной модели.

Запуск: ~/ai-workspace/venv/bin/python ~/ai-workspace/agent_demo.py "почини права у ~/.bashrc"
Модель агента: LMINTAI_AGENT_MODEL (по умолчанию ollama/crewai-qwen3-8b).
"""
import sys

from crewai import Agent, Crew, Task

from bash_tool import BashTool
from config_llm import agent_llm, describe_now


def main() -> None:
    question = " ".join(sys.argv[1:]) or "Покажи, сколько памяти свободно, и предложи одно действие."

    agent = Agent(
        role="Системный администратор Linux",
        goal="Помочь пользователю настроить и починить систему безопасными обратимыми командами.",
        backstory=(
            "Опытный администратор. Перед деструктивными действиями останавливается и спрашивает. "
            "Предпочитает диагностику до лечения."
        ),
        llm=agent_llm,
        tools=[BashTool()],
        verbose=True,
        max_iter=6,
    )

    task = Task(
        description=question,
        expected_output="Краткий отчёт: что сделано/найдено, риски, следующий шаг.",
        agent=agent,
    )

    crew = Crew(agents=[agent], tasks=[task], verbose=True)
    result = crew.kickoff()

    print("\n" + "=" * 60)
    print("РЕЗУЛЬТАТ:")
    print(result)
    print("=" * 60)
    print(f"Ответила модель: {describe_now()}")


if __name__ == "__main__":
    main()
