# PTCG Live Analyser

Record your own Pokémon TCG Live matches and analyse them afterwards: watch
them back with notes and the battle log turn by turn, and track how your
decks do over time. A macOS desktop app; everything stays on your Mac.

Players: see the **[user guide](docs/user-guide.md)** for installing the app
and using it.

## What it does

- **Recording.** Start and stop a screen recording from the app, with your
  voice if you like, and flag moments while you play. A small
  ScreenCaptureKit recorder writes a smooth 30 fps video; the game can be
  full screen or in a window. A recording you don't want can be discarded.
- **Logging.** After a match, log it with one click on *Paste battle log*:
  the result, who went first and the opponent's deck name come from the
  log the game lets you copy.
- **Reviewing.** The video with labelled notes (misplay, good play, key
  moment, bad luck, lucky topdeck, note), spoken notes, turn markers and
  keyboard shortcuts. Turn changes are found in the video automatically,
  from the glow around the board. The battle log lines up with the video
  through those turns: click a line to jump there, the log follows the
  video, your own card plays are found in the frames, and any line can be
  commented on. Also on the review page: opening hand, Prize race chart, a
  one-line lesson, trimming the menus off the start, export as text.
- **Decks.** Decklists in versions with card pictures from
  [TCGdex](https://tcgdex.dev); results per version, which cards you really
  play, and opening-hand odds.
- **Opponents.** Your record and a note per type of opposing deck; deck
  names that are the same deck can be grouped.
- **Stats, Moments, Weeks.** Win rates by deck, matchup and turn order, game
  length, tempo, first Prize, opening hands, misplays per turn, records and
  comebacks; every labelled moment across matches; each week's record,
  sessions and lessons.
- **Housekeeping.** A daily backup of the database and spoken notes to iCloud
  Drive (14 days kept; set `POKEMONTCG_BACKUP_DIR` for another place),
  optional automatic deletion of old recordings, untrimmed originals
  removed after 14 days.
- **Spoken notes as text** (developer install only): written out on this
  machine with faster-whisper, using your card and deck names as vocabulary;
  `pip install -e ".[transcribe]"`.

Not built: recognising the opponent's cards in the video, and win/loss
detection from the video (the battle log covers both).

## Install the app

Download `PTCG Live Analyser.dmg` from the Releases page, drag the app to
Applications and allow it to record the screen. The
[user guide](docs/user-guide.md#installing) has the steps. It needs a Mac
with an Apple chip and macOS 14 or later.

Its data is kept in `~/Library/Application Support/PTCG Live Analyser`.

To build the `.dmg` yourself from a checkout: `pip install -e ".[package]"`
and `python packaging/build.py` (needs the Swift compiler, `xcode-select
--install`). The result is in `dist/`. The app is signed ad hoc, with a
designated requirement on its bundle id so that macOS keeps its Screen
Recording permission across updates.

## Developer setup

Requires Python 3.11+ and `ffmpeg` (`brew install ffmpeg`).

Recording the main display uses a small ScreenCaptureKit recorder
(`src/pokemontcg_analyser/capture/main.swift`), compiled on first start if
the Swift compiler is present (`xcode-select --install`). ffmpeg's screen
input only gets about 12 frames a second from macOS while a fullscreen game
is in front; this recorder does not have that limit. Without it, or for
another display, recording falls back to ffmpeg.

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
pokemontcg-analyser matches annotate --match-id 1 --text "misplayed retreat here" --offset 245.5 --label misplay

# Review a match's notes
pokemontcg-analyser matches notes 1

# Fetch card pictures for your saved decklists
pokemontcg-analyser cards sync

# Open the web app (record a match, watch it back + take notes in the browser)
pokemontcg-analyser serve
# -> http://127.0.0.1:8000

# Or as a desktop app: install once, then start "PTCG Live Analyser" from
# Spotlight/Launchpad. Quit with Cmd+Q; that also stops the server.
# Run this from the project folder — the app keeps its data here.
pokemontcg-analyser install-app
```

## Project layout

```
src/pokemontcg_analyser/
  recorder.py   starts/stops recordings (ScreenCaptureKit helper, ffmpeg fallback), trimming
  cards.py      TCGdex card data + pictures, cached locally
  storage.py    SQLite schema + match logging/stats
  analysis.py   frame extraction
  webapp.py     local review app (FastAPI + Jinja2 templates)
  insights.py   cross-match stats and labelled moments
  decks.py      decklist parsing and version diffs
  battlelog.py  parser for the game's exported battle log
  logtimes.py   finds the log's card plays in the recording
  turns.py      turn detection from the ring around the board
  transcribe.py spoken notes to text (faster-whisper, optional)
  desktop.py    desktop-app window + macOS .app launcher
  capture/      the ScreenCaptureKit screen recorder (Swift)
  templates/    review app HTML
  cli.py        command-line entry point
packaging/      recipe and script for the standalone app and .dmg
tests/
```

## License

Copyright (C) 2026 Laura van Rooijen

This program is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License, version 3, as published
by the Free Software Foundation. It is distributed in the hope that it will
be useful, but without any warranty. The full text is in [LICENSE](LICENSE).

The packaged app (`.dmg`) also contains:

- [FFmpeg](https://ffmpeg.org) 7.1, a build with GPL components, taken
  unchanged from the [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg)
  package. FFmpeg is licensed under the GPL; its source code is at
  <https://ffmpeg.org/download.html>.
- Python and the libraries listed in `pyproject.toml`, each under its own
  license.

Card data and pictures are fetched from [TCGdex](https://tcgdex.dev).

This is a fan-made tool. It is not affiliated with, endorsed by or
sponsored by Nintendo, The Pokémon Company or the makers of Pokémon TCG
Live. Pokémon and Pokémon TCG Live are trademarks of their owners.
