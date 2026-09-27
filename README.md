# pokemontcg_analyser

Record your own Pokémon TCG Live matches and analyse them afterwards: build a
turn-by-turn event timeline, recognise cards that appeared on screen, and
track win/loss stats per deck over time.

## Status

Early scaffold. Working today:

- `record` — screen-capture a Pokémon TCG Live session on macOS to an MP4,
  with a JSON sidecar of when recording started.
- `matches` — log match metadata (deck, opponent deck, result) to a local
  SQLite database, list/summarise your stats, and attach timestamped notes
  to a match for later review.
- `analyze` — extract frames from a recorded video and detect scene changes
  (candidate turn/board-state boundaries) as a first pass at an event
  timeline.
- `cards sync` — pull the card database from the public
  [pokemontcg.io](https://pokemontcg.io) API and cache it locally, as the
  foundation for card recognition.
- `serve` — a local web app for reviewing a recording: play the video and
  drop timestamped notes on it as you watch, without switching to a
  terminal.

Not built yet: actual card recognition (matching video frames to specific
cards) and automatic win/loss detection — both need real recorded footage to
calibrate against.

## Setup

Requires Python 3.11+ and `ffmpeg` (`brew install ffmpeg`).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Usage

```bash
# See available screen/capture devices (macOS avfoundation)
pokemontcg-analyser list-devices

# Record a session (Ctrl+C to stop)
pokemontcg-analyser record --device 1 --out recordings/

# Log a match you just played
pokemontcg-analyser matches log --deck "Charizard ex" --opponent-deck "Lost Box" --result win

# See your stats
pokemontcg-analyser matches stats

# Jot a note at a specific moment in the recording (or omit --offset for a
# whole-match note, e.g. a post-game reflection)
pokemontcg-analyser matches annotate --match-id 1 --text "misplayed retreat here" --offset 245.5

# Review a match's notes alongside its detected scene changes
pokemontcg-analyser matches notes 1

# Pull the card database (for future card recognition)
pokemontcg-analyser cards sync

# Analyze a recording for scene-change boundaries
pokemontcg-analyser analyze recordings/2026-09-27_190000.mp4

# Open the review app (watch a recording + take notes in the browser)
pokemontcg-analyser serve
# -> http://127.0.0.1:8000
```

## Project layout

```
src/pokemontcg_analyser/
  recorder.py   screen capture (ffmpeg/avfoundation)
  cards.py      pokemontcg.io client + local card cache
  storage.py    SQLite schema + match logging/stats
  analysis.py   frame extraction + scene-change detection
  webapp.py     local review app (FastAPI + Jinja2 templates)
  templates/    review app HTML
  cli.py        command-line entry point
tests/
```
