# How Runsheet Pilot decides

One runsheet in, one ProPresenter playlist out. Along the way the app makes
ten choices. Each one is made by **you**, by a **rule** (plain code that
does the same thing every time), or by the **AI**. Wherever the AI chooses,
a rule checks the answer before anything reaches ProPresenter.

Who wins when they disagree: **you, then a rule, then the AI.** Anything
you pin or save is final. The AI only ever chooses among options a rule
has already allowed.

| # | Decision | Who decides | The rule, in one sentence | If nothing fits | You can change it |
|---|---|---|---|---|---|
| 1 | **Which template** (first guess) | Rule | Only playlists named "template" or "library" count; the one sharing the most words with the runsheet's file name and heading wins. | No template: every line gets a plain header. | Pick one from the dropdown; that pick is never second-guessed. |
| 2 | **What's in the service** | AI (call 1) | Every line, its time and length, its kind (song, sermon, announcement, MC, prayer, other), its tech note, and what kind of service it is. A rule puts back any timed line the AI dropped. | An error banner says why, e.g. no credit on the key. | Pick another model in Settings. |
| 3 | **Which template** (confirmed) | Rule | Decision 1 again, now with the kind of service the AI read. | Same as 1. | Your pinned template always wins. |
| 4 | **Template screens under each line** | AI, checked by a rule | The AI tags a line with a template section; otherwise the line's title is matched to a section or slide name, your aliases first. | A plain header. | Aliases in Settings; Re-match. |
| 5 | **Lighting** | The runsheet, then AI (call 2) | The runsheet's own levels stand; a saved lighting guide fills only the lines it left blank. | No level for that line. | Edit the guide. Lighting goes to Service Mate only. |
| 6 | **Which media folders** | You, else AI (call 3) | Folders saved for this service are final; otherwise the AI picks, and never sees folders named for another service. | Folders whose name names this service; else none, and the folder line says so. | Folder picker under Step 1, saved per service. |
| 7 | **Which file under which line** | AI (call 4) and rules, by folder job | See "Folder jobs" below. | Announcements still all go in; the folder line lists anything left over. | Folder jobs in the picker. |
| 8 | **Which song file** | Rule | The closest song name in your ProPresenter library, at least 55% alike. | Red ⚠ ACTION NEEDED header, and a Pick button. | Pick, the match slider, Re-match. |
| 9 | **Headers** | Rule | Every line gets a header, "Title — time (N min)", coloured by kind, with its song and screens underneath. | — | Edit titles in the table before Create. |
| 10 | **Timers** | Rule | A countdown timer for every line except songs and scripture, or only the key parts if you set that. | — | The timer checkboxes. |

Last check at Create: a screen that isn't in ProPresenter's Media area can't
be linked, so it is left out and the app names it.

## What goes under one line

Every line is filled from two places, always in this order:

1. **The template** (a rule). The line's title is matched to the template's
   section and slide names: "Welcome and Connection Cards" gets the Welcome
   slide, "Countdown" the countdown. Static screens live here and come out
   the same every week.
2. **The folders**, by each folder's job (below). A screen the template
   already uses is never added again from a folder, so the office can
   keep Welcome and Giving in the Announcements folder to see them.

On a Sunday with saved folders, the AI makes one media decision: which line
each announcement goes under. Loops, preach screens and template screens
are placed by rules.

## Folder jobs (decision 7, from v2.20)

Every media folder has one job. Static screens (Welcome, Ways to Give,
Connect With Us) stay in the template; this week's screens live in folders.

| Job | What happens | Who places it |
|---|---|---|
| **Every file** (announcements) | Every file goes in, under the line it belongs to; any the AI can't place go under the announcements line. Files that look out of date are flagged but still go in. | AI chooses the line; a rule makes sure none is dropped |
| **Under the preach** | Every file goes under the preach, title screen first, then name order. | Rule |
| **Plays first** | The pre-service loop opens the playlist. | Rule |
| **Plays last** | The end-service loop closes it. | Rule |
| **Only what fits** | The AI chooses the files that fit and leaves the rest out. | AI |

**Who keeps what current.** The template holds the static screens that
never change. The office keeps the Announcements, Preach and loop folders
current every week. Runsheet Pilot puts in everything those folders hold
and doesn't judge what's there; its only warning is a file whose name
looks past its date ("Easter Services 2026" in November), which still goes
in.

The Tech Team notes are hints, not the full list: a line with no note can
still get screens from its title and the template.

## AI calls in one runsheet

Up to four, all while the runsheet is read; none at Create:

1. Read the runsheet (always).
2. Lighting guide (only when one is saved).
3. Media folders (only when none are saved for this service).
4. Media placement (only when there are files to place).

Every call goes only to AI providers that keep nothing.
