from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import analysis, cards, recorder, storage

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
) -> None:
    """Record a Pokémon TCG Live session to video. Ctrl+C to stop."""
    console.print(f"Recording device {device} to {out}/ — press Ctrl+C to stop.")
    video_path = recorder.record(device_index=device, out_dir=out, framerate=framerate)
    console.print(f"Saved recording: {video_path}")


@app.command()
def analyze(
    video: Path = typer.Argument(..., help="Path to a recorded video"),
    sample_fps: float = typer.Option(2.0, help="Frames per second to sample"),
    threshold: float = typer.Option(12.0, help="Scene-change sensitivity"),
    match_id: Optional[int] = typer.Option(
        None, help="Attach detected events to this match id"
    ),
) -> None:
    """Detect candidate scene changes (turn/board-state boundaries) in a video."""
    changes = analysis.detect_scene_changes(
        video, sample_fps=sample_fps, threshold=threshold
    )
    if not changes:
        console.print("No scene changes detected above threshold.")
        return

    table = Table("Offset (s)", "Score")
    for c in changes:
        table.add_row(f"{c.offset_seconds:.2f}", f"{c.score:.1f}")
    console.print(table)
    console.print(f"{len(changes)} candidate boundaries detected.")

    if match_id is not None:
        events = [
            (c.offset_seconds, "scene_change", f"score={c.score:.1f}") for c in changes
        ]
        storage.add_events(match_id, events)
        console.print(f"Attached {len(events)} events to match {match_id}.")


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
) -> None:
    """Add a timestamped note to a match."""
    storage.add_note(match_id, text, offset_seconds=offset)
    where = f"at {offset:.1f}s" if offset is not None else "on the match overall"
    console.print(f"Noted {where} for match {match_id}.")


@matches_app.command("notes")
def matches_notes(match_id: int = typer.Argument(...)) -> None:
    """List a match's notes and detected events in chronological order."""
    events = storage.list_events(match_id)
    if not events:
        console.print("No notes or events for this match yet.")
        return
    table = Table("Offset (s)", "Kind", "Detail")
    for e in events:
        offset = f"{e.video_offset_seconds:.2f}" if e.video_offset_seconds is not None else "-"
        table.add_row(offset, e.kind, e.detail or "")
    console.print(table)


@cards_app.command("sync")
def cards_sync() -> None:
    """Download the full card database from pokemontcg.io into a local cache."""
    console.print("Fetching card database from pokemontcg.io (this may take a minute)...")
    count = cards.sync_cache()
    console.print(f"Cached {count} cards to {cards.DEFAULT_CACHE_PATH}")


if __name__ == "__main__":
    app()
