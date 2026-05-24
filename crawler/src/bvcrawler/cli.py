"""Typer CLI for the crawler."""

import typer
from rich.console import Console

from bvcrawler import __version__
from bvcrawler.connectors import CONNECTORS

app = typer.Typer(help="bitvision phoenix — public DICOM archive crawler (admin only)")
console = Console()


@app.command()
def version() -> None:
    """Print crawler version."""
    console.print(f"bvcrawler [bold]{__version__}[/bold]")


@app.command("list-sources")
def list_sources() -> None:
    """List available source connectors."""
    if not CONNECTORS:
        console.print("[yellow]No connectors registered yet.[/yellow]")
        return
    for name, conn in CONNECTORS.items():
        console.print(f"- [bold]{name}[/bold] — {conn.description}")


@app.command()
def run(
    source: str = typer.Option(..., "--source", help="Connector name (see list-sources)"),
    collection: str = typer.Option(..., "--collection", help="Source-specific collection id"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Discover only, do not ingest"),
) -> None:
    """Run a crawler connector against a public archive."""
    if source not in CONNECTORS:
        console.print(f"[red]Unknown source:[/red] {source}")
        raise typer.Exit(code=2)
    connector = CONNECTORS[source]
    console.print(
        f"[green]Running[/green] {source} / {collection} "
        f"(dry_run={dry_run}) — {connector.description}"
    )
    # Actual execution is implemented per-connector in future phases.
    raise typer.Exit(code=0)


if __name__ == "__main__":
    app()
