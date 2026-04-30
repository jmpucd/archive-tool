import typer

app = typer.Typer(
    add_completion=False,
    help="Archive a finished digitization project to the library archives.",
)


@app.command()
def main() -> None:
    """Run the archive orchestrator."""
    typer.echo("archive-tool: Step 0 skeleton. Pickers and transfer not yet implemented.")
