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
- `cards sync` — fetch card data and pictures from [TCGdex](https://tcgdex.dev)
  for your saved decklists. The Decks page also fetches them as needed.
- `serve` — a local web app: start and stop a recording with a button (and
  flag moments while it runs), log the match against it, then play the video
  back and annotate it: labelled notes (misplay, good play, key moment, bad
  luck, lucky topdeck, note), turn markers, who went first, and keyboard shortcuts so you never
  leave the video. Spoken notes are recorded with the microphone and
  stored in `recordings/voice/`. Mark where the game itself starts and trim
  the menus before it off the recording (the untrimmed file is kept in
  `recordings/originals/` for 14 days, then deleted).
- Recording can include a microphone, for talking through your plays; the
  match list shows each recording's size, can be searched and filtered, and
  a recording can be deleted while its notes are kept.
- A one-line lesson per match ("what do I take from this game"), shown on
  the start page before the next game.
- Stats and Moments pages in the web app: win rates per deck, per matchup
  and by going first or second, misplays per reviewed game and per turn, and
  every labelled moment across all matches with a link to that point in the
  video.
- Turn changes are found automatically when a match with a recording is
  logged (and again on request with "Detect turns" on the review page),
  from the ring around the playing field (its upper half lights up on the
  opponent's turn, its lower half on yours), and fills in who went first.
- Spoken notes are written out as text on this machine, using the card and
  deck names you have saved as vocabulary. This needs the optional speech
  model: `pip install -e ".[transcribe]"` (a few hundred MB on first use).
- Battle log: paste the log the game lets you copy after a match, when
  logging the match or on its review page. The timeline then shows what
  happened in each turn, and the result and who went first come from the
  log. The log has no timestamps; it lines up with the video through the
  turn markers, and your own card plays are then looked for in the video
  (the game shows a played card enlarged), so those lines jump to their
  moment, and while the video plays the timeline follows along: the
  current turn and the last play that has happened are marked. Any line of the log can be commented on and labelled, like
  a note on the video. From the log the review page also shows the opening hand
  and a chart of Prize cards taken per turn, the opponent's deck gets a
  name when you gave none, and the Stats page compares opening hands across
  games.
- Matches can be corrected or deleted from their review page, and each
  opponent deck can carry a note that shows on every match against it.
- The Decks page shows what you actually play, from the battle logs: how
  often each card is used, which listed cards never are, and which cards
  the logs show that the saved list lacks. The Stats page shows turn tempo
  and how many turns games take.
- The database and spoken notes are copied once a day to iCloud Drive
  (`Pokemon TCG Analyser backups`, or Documents without iCloud; set
  `POKEMONTCG_BACKUP_DIR` for another place), keeping 14 days. Recordings
  are not backed up, and can be set to be deleted after a number of weeks.
- From the battle logs the Stats page also shows who takes the first Prize
  card and when, in which of your turns your evolutions come into play, and
  how many of your turns pass without an Energy or an attack; the Decks
  page compares those per version of a list and gives the opening-hand odds
  of the current list.
- Opponents page: per deck played against, your record, your note, and
  what they played. Weeks page: each week's record, lessons and misplays.
- A match can be downloaded as a text file with its log, notes and lesson.
- Records on the Stats page (fastest win, biggest comeback, longest
  streak, most played card); games won from two or more Prize cards behind
  or lost from as far ahead are marked. The start page shows your last ten
  results, the current session and a lesson from further back; the Weeks
  page groups matches into sessions; the Opponents page starts with the
  decks met in the last four weeks; the Moments page lists words that keep
  coming back in your misplays.
- Decks page: save a decklist per deck with versions; matches count towards
  the version that was current when they were logged, so a change to the
  list shows up as a change in results.
- `app` / `install-app` — run the same thing as a desktop app in its own
  window instead of a browser tab, and install a double-clickable launcher
  in `~/Applications`.

Not built yet: card recognition (matching video frames to specific cards)
and automatic win/loss detection.

## Setup

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

# Or as a desktop app: install once, then start "Pokemon TCG Analyser" from
# Spotlight/Launchpad. Quit with Cmd+Q; that also stops the server.
# Run this from the project folder — the app keeps its data here.
pokemontcg-analyser install-app
```

## Project layout

```
src/pokemontcg_analyser/
  recorder.py   screen capture (ffmpeg/avfoundation)
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
  templates/    review app HTML
  cli.py        command-line entry point
tests/
```
