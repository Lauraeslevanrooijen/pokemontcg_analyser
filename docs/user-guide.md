# PTCG Live Analyser — user guide

PTCG Live Analyser records your Pokémon TCG Live matches and helps you look
back on them: what happened each turn, where you misplayed, and how your
decks do over time. Everything stays on your own Mac.

- [Installing](#installing)
- [Playing a match](#playing-a-match)
- [Reviewing a match](#reviewing-a-match)
- [The other pages](#the-other-pages)
- [Your data](#your-data)
- [When something goes wrong](#when-something-goes-wrong)

## Installing

You need a Mac with an Apple chip (M1 or newer) and macOS 14 or later.

1. Open `PTCG Live Analyser.dmg` and drag **PTCG Live Analyser** onto the
   **Applications** folder.
2. Open the app from Applications. The first time, macOS refuses because it
   cannot check who made the app. Go to **System Settings → Privacy &
   Security**, scroll down to the message about PTCG Live Analyser and click
   **Open Anyway**. You only do this once.
3. Give the app permission to record the screen. See
   [Recording permission](#recording-permission) below.

**Updating:** open the new `.dmg` and drag the app onto Applications
again, replacing the old one. Your matches stay.

### Recording permission

macOS only lets an app record the screen once you have switched it on
yourself:

1. Click **Start recording** in the app once. The app then shows a panel
   with a button that opens the right settings page. You can also go there
   yourself: **System Settings → Privacy & Security → Screen & System
   Audio Recording**.
2. If **PTCG Live Analyser** is in the list, switch it on. If it is not in
   the list, click **+** under the list, choose **Applications → PTCG Live
   Analyser**, and switch it on.
3. Quit the app with **Cmd+Q** and open it again. The permission only
   counts after a restart.

## Playing a match

### 1. Start recording

On the start page, click **Start recording**, then switch to Pokémon TCG
Live and play. The game can be full screen or in a window.

- Tick **Record my voice** to record your microphone too, for talking
  through your plays. The app remembers this choice.
- While recording, press **M** with the app window in front (or click **Mark moment**) to
  flag a moment you want to look at later.

### 2. Copy the battle log

When the game is over, **don't leave the summary screen yet**. Open
**Battle Log** there and copy it (**Cmd+A**, then **Cmd+C**). The game does
not keep it afterwards.

### 3. Stop and log the match

Back in the app, click **Stop recording**. The form to log the match
appears right below.

1. Fill in your deck and, if you like, the opponent's deck. The fields
   suggest names you have used before.
2. Click **Paste battle log**. The app reads the log from the clipboard
   and says how many turns it found. If it says the clipboard does not look
   like a battle log, copy the log in the game again.
3. Click **Log match**.

With a battle log you don't need to fill in the result or who went first;
both come from the log. If you leave the opponent's deck empty, the app
names it from the Pokémon they played.

**Don't want to keep a recording?** Choose it in the recording menu and
click **Discard recording**. It is deleted for good.

## Reviewing a match

Click a match in the list to open its review page. It has the video, a
timeline of notes and turns, and the battle log turn by turn.

### Notes and labels

Pause anywhere and add a note. Every note can carry a label:

| Key | Label |
|-----|-------|
| 1 | Misplay |
| 2 | Good play |
| 3 | Key moment |
| 4 | Bad luck |
| 5 | Lucky topdeck |
| 6 | Note |

You can also record a spoken note (**V**). Notes can be edited and deleted.

### Keyboard shortcuts

| Key | What it does |
|-----|--------------|
| Space | Play / pause |
| ← → | Back / forward 5 seconds |
| , . | One frame back / forward |
| < > | Slower / faster |
| ↑ ↓ | Previous / next note or turn |
| N | Pause and write a note (Enter saves, Esc cancels) |
| 1–6 | Label this moment |
| V | Voice note (press again to save) |
| T | New turn starts here |
| G | The game starts here |

**G** marks where the game itself starts. The app can then trim off the
menus before it. The untrimmed video is kept for 14 days in case you want
it back.

### Turns and the battle log

The app finds the turn changes in the video by itself, from the glow
around the board (top half on the opponent's turn, bottom half on yours).
This runs in the background after you log a match. If it got something
wrong, mark turns yourself with **T**, or click **Detect turns** to try
again.

The battle log lines up with the video through those turns:

- Click a line in the log to jump to that moment in the video.
- While the video plays, the log follows along.
- Your own card plays are looked up in the video, so they jump to the exact
  moment the card was played.
- Any log line can get a comment and a label, just like a note on the video.
  A comment on the wrong line can be moved up or down with the arrows.

From the log, the review page also shows your opening hand (including
extra cards after a mulligan) and a chart of Prize cards taken per turn.

### More on the review page

- **Lesson:** one line on what you take from this game. It shows on the
  start page before your next game.
- **Edit match:** correct the deck, result or anything else, or delete the
  match (its notes and recording go with it).
- **Download this match as text:** the result, lesson, battle log and your
  notes in one file.
- **Delete recording:** removes the video to save disk space. Your notes
  and the log stay.

## The other pages

- **Matches** (start page): record and log, your last results, the current
  session, a lesson from earlier, and every match with search and filters.
- **Decks:** your decklists with card pictures, kept in versions. Matches
  count towards the version you were playing at the time, so you can see
  whether a change helped. It also shows which cards you really play, cards
  you never use, and the odds of your opening hand.
- **Opponents:** your record against each kind of deck, with a note per
  deck that shows on every match against it. Deck names that are really the
  same deck can be grouped into one type.
- **Stats:** win rates by deck, matchup and going first or second, game
  length, tempo, opening hands, misplays, and records such as your fastest
  win and biggest comeback.
- **Moments:** every labelled moment across all matches, with a link to
  that point in the video, and the words that keep coming back in your
  misplays.
- **Weeks:** each week's record, sessions, lessons and misplays.

## Your data

- Everything lives on your Mac, in the `Library/Application Support/PTCG
  Live Analyser` folder in your home folder. Nothing is sent anywhere,
  except requests for card pictures to [TCGdex](https://tcgdex.dev).
- Once a day, your matches, notes and spoken notes are copied to iCloud
  Drive, in the `PTCG Live Analyser backups` folder (or to Documents without
  iCloud). The last 14 days are kept. Videos are not backed up.
- Videos take room, about 1.3 GB per hour of play. At the bottom of the start page
  you can have recordings deleted automatically after a number of weeks.
  Your notes and logs stay.
- Spoken notes are saved but not written out as text in the app.

## When something goes wrong

**Recording does not start, or the app says it has no permission.**
Follow [Recording permission](#recording-permission). If the app is already
switched on in the list but recording still fails, select it, click
**–** to remove it, add it again with **+**, and restart the app.

**Paste battle log says the clipboard is not a battle log.**
Copy the log again on the game's summary screen (Cmd+A, Cmd+C), then click
the button again. You can also open **Battle log (optional)** under the
form and paste it in with Cmd+V.

**The turns are wrong.**
Mark them yourself with **T** on the review page, or click
**Detect turns**. Turn detection needs the board to be visible; if the game
was hidden behind another window, it can't see the turns.

**Found a problem or have an idea?** Let Laura know — feedback is always
welcome.
