from __future__ import annotations

from datetime import datetime

from rich.console import Console

from src.config import get_settings
from src.db import get_database, ping_database
from src.telemetry.profile import get_dataset_profile


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

    db = get_database(settings)
    profile = get_dataset_profile(db)

    counts = profile["counts"]
    time_bounds = profile["time_bounds"]
    services = profile["services"]
    metric_names = profile["metric_names"]
    status_code_counts = profile["status_code_counts"]
    top_span_names = profile["top_span_names"]

    def format_datetime(value: datetime | None) -> str:
        if value is None:
            return "N/A"
        return value.isoformat()

    console.print("\n[bold]Dataset Profile[/bold]")
    console.print(
        f"- Collection counts: spans={counts['spans']}, logs={counts['logs']}, metrics={counts['metrics']}"
    )
    console.print(
        f"- Dataset time range: {format_datetime(time_bounds['start'])} -> {format_datetime(time_bounds['end'])}"
    )
    console.print(
        "[dim]Note: relative windows such as 'last hour' use the latest dataset timestamp, not the real current time.[/dim]"
    )
    console.print(f"- Services: {', '.join(services) if services else 'None'}")
    console.print(
        f"- Metric names: {', '.join(metric_names) if metric_names else 'None'}"
    )

    console.print("- Status code counts:")
    if status_code_counts:
        for item in status_code_counts:
            console.print(f"  - {item['status_code']}: {item['count']}")
    else:
        console.print("  - None")

    console.print("- Top 10 span names:")
    if top_span_names:
        for item in top_span_names:
            console.print(f"  - {item['name']}: {item['count']}")
    else:
        console.print("  - None")

    console.print("Telemetry CLI ready. Type your question or 'exit' to quit.")

    while True:
        user_input = input("> ").strip()

        if user_input.lower() in {"exit", "quit", "q"}:
            console.print("Goodbye.")
            break

        if not user_input:
            continue

        console.print("Analysis not implemented yet. Next step is telemetry profiling.")


if __name__ == "__main__":
    run()
