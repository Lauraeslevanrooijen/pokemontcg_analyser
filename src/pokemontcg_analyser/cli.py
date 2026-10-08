from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import cards, desktop, recorder, storage

app = typer.Typer(help="Record and analyse your own Pokémon TCG Live matches.")
matches_app = typer.Typer(help="Log and review match results.")
cards_app = typer.Typer(help="Manage the local card database cache.")
app.add_typer(matches_app, name="matches")
app.add_typer(cards_app, name="cards")

console = Console()


@app.command("list-devices")
def list_devices() -> None:
    """List avfoundation capture devices (screens, cameras, capture cards)."""
    devices = recorder.list_avfoundation_devices()
    if not devices:
        console.print("No avfoundation devices found.")
        raise typer.Exit(code=1)
    table = Table("Index", "Name")
    for d in devices:
        table.add_row(str(d.index), d.name)
    console.print(table)


@app.command()
def record(
    device: int = typer.Option(..., "--device", help="Device index from list-devices"),
    out: Path = typer.Option(Path("recordings"), "--out", help="Output directory"),
    framerate: int = typer.Option(30, "--framerate"),
    max_width: int = typer.Option(
        1920, "--max-width", help="Downscale capture to this width (keeps aspect ratio)"
    ),
) -> None:
    """Record a Pokémon TCG Live session to video. Ctrl+C to stop."""
    console.print(f"Recording device {device} to {out}/ — press Ctrl+C to stop.")
    video_path = recorder.record(
        device_index=device, out_dir=out, framerate=framerate, max_width=max_width
    )
    console.print(f"Saved recording: {video_path}")


@matches_app.command("log")
def matches_log(
    deck: str = typer.Option(..., "--deck"),
    result: storage.Result = typer.Option(..., "--result"),
    opponent_deck: Optional[str] = typer.Option(None, "--opponent-deck"),
    notes: Optional[str] = typer.Option(None, "--notes"),
    video_file: Optional[str] = typer.Option(None, "--video-file"),
) -> None:
    """Log the result of a match you just played."""
    match_id = storage.log_match(
        deck=deck,
        result=result,
        opponent_deck=opponent_deck,
        notes=notes,
        video_file=video_file,
    )
    console.print(f"Logged match {match_id}: {deck} vs {opponent_deck or '?'} -> {result}")


@matches_app.command("list")
def matches_list() -> None:
    """List all logged matches, most recent first."""
    rows = storage.list_matches()
    if not rows:
        console.print("No matches logged yet.")
        return
    table = Table("ID", "Played (UTC)", "Deck", "Opponent", "Result", "Video")
    for m in rows:
        table.add_row(
            str(m.id),
            m.played_at_utc,
            m.deck,
            m.opponent_deck or "-",
            m.result,
            m.video_file or "-",
        )
    console.print(table)


@matches_app.command("stats")
def matches_stats() -> None:
    """Show win-rate stats overall and per deck."""
    stats = storage.compute_stats()
    if stats.total == 0:
        console.print("No matches logged yet.")
        return
    console.print(
        f"Overall: {stats.wins}W-{stats.losses}L-{stats.ties}T "
        f"over {stats.total} matches ({stats.win_rate:.0%} win rate)"
    )
    table = Table("Deck", "W", "L", "T", "Win rate")
    for deck, (w, l, t) in sorted(stats.by_deck.items()):
        total = w + l + t
        table.add_row(deck, str(w), str(l), str(t), f"{w / total:.0%}" if total else "-")
    console.print(table)


@matches_app.command("annotate")
def matches_annotate(
    match_id: int = typer.Option(..., "--match-id"),
    text: str = typer.Option(..., "--text"),
    offset: Optional[float] = typer.Option(
        None, "--offset", help="Position in the recording, in seconds"
    ),
    label: Optional[storage.Label] = typer.Option(None, "--label"),
) -> None:
    """Add a timestamped note to a match."""
    storage.add_note(match_id, text, offset_seconds=offset, label=label)
    where = f"at {offset:.1f}s" if offset is not None else "on the match overall"
    console.print(f"Noted {where} for match {match_id}.")


@matches_app.command("notes")
def matches_notes(match_id: int = typer.Argument(...)) -> None:
    """List a match's notes and turn markers in chronological order."""
    events = storage.list_events(match_id)
    if not events:
        console.print("No notes for this match yet.")
        return
    table = Table("Offset (s)", "Kind", "Detail")
    for e in events:
        offset = f"{e.video_offset_seconds:.2f}" if e.video_offset_seconds is not None else "-"
        kind = f"{e.kind} ({storage.LABELS[e.label]})" if e.label else e.kind
        table.add_row(offset, kind, e.detail or "")
    console.print(table)


@app.command()
def serve(
    port: int = typer.Option(8000, "--port", help="Port to serve the review app on"),
) -> None:
    """Run the local review web app: watch recordings and add notes in the browser."""
    import uvicorn

    console.print(f"Serving on http://127.0.0.1:{port} (Ctrl+C to stop)")
    uvicorn.run("pokemontcg_analyser.webapp:app", host="127.0.0.1", port=port)


@app.command("app")
def desktop_app(
    port: int = typer.Option(8000, "--port"),
    data_dir: Optional[Path] = typer.Option(
        None, "--data-dir", help="Folder holding data/ and recordings/ (default: current)"
    ),
) -> None:
    """Run as a desktop app: its own window, quitting it stops the server."""
    desktop.run(port=port, data_dir=data_dir)


@app.command("install-app")
def install_app(
    port: int = typer.Option(8000, "--port"),
    data_dir: Path = typer.Option(
        Path("."), "--data-dir", help="Folder holding data/ and recordings/"
    ),
) -> None:
    """Create a double-clickable macOS app in ~/Applications."""
    bundle = desktop.install_app(data_dir=data_dir, port=port)
    console.print(f"Installed {bundle}")


@cards_app.command("sync")
def cards_sync() -> None:
    """Download the full card database from pokemontcg.io into a local cache."""
    console.print("Fetching card database from pokemontcg.io (this may take a minute)...")
    count = cards.sync_cache()
    console.print(f"Cached {count} cards to {cards.DEFAULT_CACHE_PATH}")


if __name__ == "__main__":
    app()
