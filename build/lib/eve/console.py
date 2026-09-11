"""Shared console styling for every eve command."""

from __future__ import annotations

from rich.console import Console
from rich.theme import Theme

EVE_THEME = Theme(
    {
        "info": "cyan",
        "success": "bold green",
        "warning": "bold yellow",
        "error": "bold red",
        "accent": "bold cyan",
        "step": "bold magenta",
    }
)

console = Console(theme=EVE_THEME)


def info(message):
    console.print(f"[info]·[/info] {message}")


def success(message):
    console.print(f"[success]✓[/success] {message}")


def warning(message):
    console.print(f"[warning]![/warning] {message}")


def error(message):
    console.print(f"[error]✗[/error] {message}")


def step(title):
    console.print()
    console.print(f"[step]▸[/step] [bold]{title}[/bold]")
