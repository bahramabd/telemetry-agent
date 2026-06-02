from __future__ import annotations

from rich.console import Console

from src.config import get_settings
from src.db import ping_database


def run() -> None:
    console = Console()
    settings = get_settings()

    console.print("[bold cyan]Telemetry Intelligence Agent[/bold cyan]")

    if ping_database(settings):
        console.print("[green]MongoDB connection successful.[/green]")
    else:
        console.print("[red]MongoDB connection failed.[/red]")
        console.print(
            "[yellow]Check MONGO_URI and make sure MongoDB is running.[/yellow]"
        )
        return

    console.print("Telemetry CLI ready. Type your question or 'exit' to quit.")

    while True:
        user_input = input("> ").strip()

        if user_input.lower() in {"exit", "quit", "q"}:
            console.print("Goodbye.")
            break

        if not user_input:
            continue

        console.print(
            "Analysis not implemented yet. Next step is telemetry profiling."
        )


if __name__ == "__main__":
    run()