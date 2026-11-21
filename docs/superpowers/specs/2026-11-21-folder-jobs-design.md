# Folder jobs: every media folder has one job

Date: 21 Nov 2026 · Status: draft for the owner's review · Target: v2.20.0

## Why

Booth feedback since v2.19.0: the office keeps this week's screens in
two ProPresenter smart folders, **Announcements** and **Preach**, plus two
loop folders that hold one MP4 each (pre-service and end-service). Today
Runsheet Pilot treats every folder the same way: the AI picks the files
that seem to fit and drops the rest. That is wrong for these folders.
Every announcement must go in. Every preach screen goes under the preach,
and the loops always open and close the playlist.

Owner's words (20 Nov 2026):
- Announcements: "All of them, warn me about stale ones."
- Preach: "Usually … the preach slide and a preach title slide, or there may
  be a video … the preach screen and the preach title screen should always
  go in first."
- Loops: "they will always be" in the playlist, first and last.
- Tech Team notes "are just guidance. There is definitely more that the tech
  team should have."
- Static screens (Welcome, Ways to Give, Connect With Us) "don't really
  change". They stay in the service's ProPresenter template.

## The rule in one line

**Each screen lives in exactly one place.** Static screens live in the
template. This week's screens live in a folder, and every folder has one job.

## The five jobs

| Job | Shown to the operator as | What happens | Who decides where |
|---|---|---|---|
| `every` | **Every file: announcements** | Every file goes in. Each goes under the line it belongs to, e.g. Junior Youth Sunday under "Junior Youth Out". Files the AI can't place go under the announcements line. | AI picks the line; code makes sure none is dropped |
| `preach` | **Every file: under the preach** | Every file goes under the preach line. A title or announcer screen goes first, then the rest in name order. | Code only, no AI |
| `start` | **Plays first** | The folder's files open the playlist, at the top of the first line. | Code only |
| `end` | **Plays last** | The folder's files close the playlist, at the end of the last line. | Code only |
| `pick` | **Only what fits** | Today's behaviour: the AI chooses the files that fit and where they go, and leaves the rest out. | AI |

### How a folder gets its job

1. **Saved for this service** (the folder picker): the operator's choice is final.
2. **Not saved**: the folder AI call already chooses this service's folders
   (v2.19.0). It now also gives each chosen folder a job.
3. **The AI didn't answer**: code guesses from the folder name:
   - "announce", "notices", "news" → `every`
   - "preach", "sermon" → `preach`
   - "pre" with "loop" or "service", or "walk in" → `start`
   - "end", "post", "after", "exit" or "outro" with "loop" or "service" → `end`
   - anything else → `pick`

The same name guess pre-fills the job dropdown in the picker. A row saved
before v2.20.0 has folders but no jobs, so its folders get the name guess.
A Sunday row that ticks Announcements, Preach and the two loop folders
works with no further setup.

## What the AI is asked, and how often

A normal Sunday with saved folders makes **one** media call. A service with
no saved folders makes two, the same as v2.19.0:

1. **Folders** (only when none are saved): which folders are this service's, and
   each folder's job.
2. **Placement** (when there are `every` or `pick` files): one prompt lists
   the runsheet lines, numbered. Each line shows the screens it already has
   from the template, so the AI doesn't double one up. Then the files, with
   `every` files marked "must place". The AI answers:
   - `media`: which file goes under which line (as today);
   - `stale`: files that look out of date for the service's date, e.g.
     "Easter Services 2026" on a November service. A warning only.

`preach`, `start` and `end` files never go to the AI. Code places them.

**The office keeps the folders current; Runsheet Pilot doesn't judge
what's in them** (owner, 21 Nov 2026). An earlier draft also asked the AI
which screens a line's tech note wanted that nobody had made, and turned
that line's header red. Tried live on GPT-4.1 mini, it named the Junior
Youth screen as missing in both runs, on the line where it had just placed
"Junior Youth Sunday". A wrong red header on a Sunday is worse than none,
so it was dropped.

## What code guarantees, whatever the AI says

- Every `every` file is in the playlist exactly once. The AI may place it.
  Otherwise it goes under the announcements line: the first line the
  runsheet reader typed as an announcement.
- Every `preach` file is under the first preach line.
- `start` files sit at the top of the first line, before any other media
  there, the countdown included. `end` files sit at the end of the last line.
- Every number the AI returns is checked against the lists it was shown, as
  today. An unknown file or line is ignored.
- **Nothing from the template is removed.** Folder files come after the
  template's screens on the same line.
- **A screen the template already has is never added again from a
  folder**, matched by file or by name. The office can keep Welcome and
  Giving in the Announcements folder so they can see them; the template
  still owns them, so they show once (owner, 21 Nov 2026).
- **A song line keeps its song.** Media placed under a song line now comes
  after the song. Until now the media replaced it: a bin file placed on a
  song line silently dropped the song from the playlist.
- A failed AI call never removes media already placed (v2.19.0 rule, kept).
  With no answer, `every` files still all go in, under the announcements
  line, and `pick` files are left out.

## What the operator sees

The folder line under Step 1 names each folder with its job:

> Media from 4 folders, saved for Sunday: Announcements (every file) ·
> Preach (under the preach) · Pre-service loop (plays first) · End loop
> (plays last).
> ⚠ May be out of date: Easter Services 2026. Still in the playlist; remove
> it in ProPresenter if it's finished.

The results table shows ♻ for the template's screens on a line and 📁 for
the folders', so "why is that slide here" has an answer. The folder picker
gets a job dropdown next to each ticked folder.

### When a job has nowhere to go

| Situation | Result |
|---|---|
| Announcement files, no announcement line | Placed by the AI only; any left over are listed: "3 announcements had no announcements line to go under." |
| Preach files, no preach line | Listed: "2 preach screens weren't placed: no preach line in this runsheet." |
| Loop folder empty | Nothing placed, nothing said |

## One-time setup for the booth

Once the folders have jobs, remove the weekly announcement and preach screens
from the Sunday template, so they don't show up twice. Keep the template's
static screens (Welcome, Ways to Give, Connect With Us).

## Fixes found while mapping the decisions

Tracing every decision from upload to playlist
([how-runsheet-pilot-decides.md](../../how-runsheet-pilot-decides.md))
turned up four places where one decision follows two different rules.
Each one can put the wrong thing on screen on a Sunday, so they ship in
this release:

1. **A song can lose its song file.** Title matching and Re-match never give
   a song a template slide, but the AI's section tag and Media-bin
   placement can. A song with a tag then skips the song match, and the
   playlist gets slides instead of the song. Fix: the AI's section tag no
   longer applies to songs (the rule title matching already had), and a
   song line always gets its song match, with any folder media after it.
2. **Re-match wipes Media-bin picks.** Re-match clears links that don't
   match the template, the bin's included, and never places them again.
   The folder line still claims the media is there. Fix: Re-match keeps
   entries marked `from_bin`.
3. **An AI failure on placement reads as "nothing fit".** On a first parse,
   a placement call that failed shows the same words as a real empty answer.
   Fix: the folder line says the AI didn't answer, and saving in the folder
   picker places the media again.
4. **Create's last-minute template rescue skips aliases.** Parse and
   Re-match match headers with the operator's aliases; Create's rescue
   doesn't. Fix: Create uses the same matcher, aliases included.

## Out of scope

- A third "service slides" folder: the static screens stay in the template.
- Deciding when an announcement is finished from its file date. The bin
  API gives names only, so "stale" is the AI reading the name against the
  service date.
- Changing how songs, templates, timers or lighting are chosen.

## Files

- `propresenter/media_match.py`: jobs (`folder_job` name guess), job-aware
  `scope`, and `place`, which guarantees the every/preach/start/end
  placements. Entries are marked `from_bin` one by one rather than per
  section, so placing again removes only the bin's own files from a line
  the template also fills (`with_bin`, `without_bin`).
- `parsing/folder_plan.py`: the answer carries a job per folder.
- `parsing/media_plan.py`: the "must place" marker, the template screens
  per line, and the `stale` answer.
- `routes/parse.py` `place_bin_media`: passes jobs through and returns
  `jobs`, `stale`, `left` and `ai_failed` for the folder line; Re-match
  keeps the bin's files; the section tag skips songs.
- `routes/settings.py` `POST /api/media_folders`: saves `jobs` and checks
  every value against the five jobs.
- `routes/playlist.py`: Create's template rescue calls the shared engine.
- `propresenter/playlist.py`: song plus section on one line; `start`
  entries before the song.
- `static/app.js`, `static/app.css`: job dropdown, folder line wording,
  warnings, ♻/📁 in the table.

## Testing

- Unit (`tests/test_folder_jobs.py` and the updated media tests): name
  guess table; an `every` file the AI didn't place lands on the
  announcements line; `preach` order puts the title first; `start` comes
  before the countdown on line 1 and before a first song; a song line keeps
  its song; placing again removes only bin entries; a template's screen is
  never added twice; a saved row without jobs gets the name guess;
  `/api/media_folders` drops an unknown job; `stale` indexes are held to
  what was shown; Re-match keeps the bin's files.
- Live, done 21 Nov 2026: the placement prompt against GPT-4.1 mini with a
  sample Sunday — every announcement on the right line, the past event
  flagged.
- Live, before release: this Sunday's runsheet against the booth's real
  folders.
