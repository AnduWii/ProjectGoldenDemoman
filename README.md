# TDS Macro

A desktop app for **Windows and macOS** that records and replays strategies in
the Roblox game **Tower Defense Simulator**.

It works entirely from the outside: your mouse and keyboard are simulated through
the operating system's own input API (`SendInput` on Windows, Quartz events on
macOS), and the game is read from screenshots. It does **not** inject code into
Roblox, attach a debugger, read the game's memory, load a DLL, or use an
executor. It needs no administrator rights, installs nothing, edits no registry
keys, and writes only inside its own folder.

It works like TinyTask: press **Record**, play a match, press **Record** again,
then **Play**. Unlike TinyTask it understands what you did — a hotbar key and a
click become "place tower 2 here", an upgrade key becomes "upgrade T1" — so a
replay can wait for the right wave and enough cash instead of clicking blind on
a timer. You can also **give it a video of a match**, yours or another
player's, and it works out what was bought, when and where.

**On a Mac** there is one unavoidable exception: macOS will not let *any* app
click, type or read another app's window until you switch it on yourself in
**System Settings → Privacy & Security** — see [On a Mac](#on-a-mac).

---

## Read this first

**Automating Roblox is against its Terms of Service, and accounts have been
banned for it.** Being an external tool does not change that. Roblox can detect
automation from server-side behaviour — hundreds of matches with inputs landing
on identical relative coordinates is a recognisable pattern no matter how the
clicks are produced. This app does not try to hide what it is doing, and no tool
in this category can promise you won't be banned.

Use it on an account you are willing to lose.

**What has and has not been verified.** The coordinate maths, the strategy
engine, the recorder, the HUD reader, the loop controller and the video importer
are covered by 367 automated tests that run on every commit. On `windows-latest`, CI also checks
per-monitor DPI awareness, claims and releases a real global hotkey, captures
the screen, and **launches the built `TDSMacro.exe`**. On macOS (Apple Silicon
and Intel), CI checks that every macOS framework call the app makes exists,
builds real input events, and **launches the built `TDSMacro.app`**. What has
**never been run against Roblox itself** is input actually reaching the game on
either platform, and template matching against the real TDS HUD and lobby. On a
Mac that includes the permission prompts, which no CI machine can answer.
The video importer has only been run on synthetic match videos whose answers
are known exactly; how well it reads real TDS footage is unproven until you try
it. Tick **Practice** for your first replay, and see
[Troubleshooting](#troubleshooting) when something is off.

---

## Getting started

### Option A — run the built app (Windows)

1. Download `TDSMacro-<version>-windows.zip` from the Actions artifacts (or build
   it yourself, below) and unzip it anywhere — Desktop, a games folder, a USB
   stick. Keep the folder together; `TDSMacro.exe` needs the files beside it.
2. Start Roblox, join a TDS match, and leave it in a window you can see.
   Fullscreen-exclusive mode is not supported; borderless or windowed is fine.
3. Run `TDSMacro.exe`. It opens on the simple screen — see
   [Using it like TinyTask](#using-it-like-tinytask).

Windows may warn you about an unrecognised app. That is SmartScreen reacting to
an unsigned executable, which this is. Everything it does is in this repository
if you would rather read it than trust it.

### On a Mac

Which download you need depends on your Mac's chip: Apple menu → **About This
Mac**. "Chip: Apple M…" means **arm64**; "Processor: Intel" means **x86_64**.

1. From the Actions artifacts, download **TDSMacro-mac-arm64** (or
   **TDSMacro-mac-x86_64**). Double-click the downloaded zip, then double-click
   the `TDSMacro-…-mac-….zip` inside it. Move the resulting **TDSMacro** folder
   somewhere permanent, such as Documents — settings are saved beside the app.
2. **First launch.** The app is not signed by Apple, so macOS refuses to open it
   the first time. Double-click **TDSMacro**, press *Done* on the warning, then
   go to System Settings → Privacy & Security, scroll down and press **Open
   Anyway** beside the TDS Macro message. You only do this once. (If you would
   rather not, run `xattr -dr com.apple.quarantine` followed by a space and the
   TDSMacro.app dragged into Terminal — that removes the download flag from the
   file instead.)
3. In Roblox, turn full screen off from inside the game: **Esc → Settings →
   Fullscreen: Off**. On a MacBook, F11 is a volume key, not Roblox's
   full-screen toggle.
4. On first launch an **Allow TDS Macro on your Mac** window opens, with a
   button for each permission; each one opens the right pane in System
   Settings, where you switch **TDS Macro** on. (Reopen it later from the
   yellow banner on the main screen.)

   | Permission | Why |
   | --- | --- |
   | **Accessibility** | to click and press keys in Roblox — without it, macOS silently discards every input the app sends |
   | **Screen Recording** | to see the Roblox window — without it, screenshots show only your wallpaper |
   | **Input Monitoring** | for the hotkeys and for recording your moves |

   After switching on Screen Recording or Input Monitoring, **quit and reopen
   TDS Macro**; macOS only applies those to a fresh launch. You can switch any of
   them off again whenever you like. These are the only system changes the app
   needs, and it cannot make them itself.

**Mac hotkeys** are **Ctrl+Option+R** (record), **Ctrl+Option+P** (play) and
**Ctrl+Option+S** (stop), because F6–F8 are media keys on a Mac keyboard. You
can change them in Settings. Wherever this README says F6, F7 or F8, use these.

If you rebuild or update the app, macOS may ask for the permissions again: they
are tied to the exact build.

### Option B — run from source

```bash
git clone https://github.com/AnduWii/ProjectGoldenDemoman.git
```

```bash
cd ProjectGoldenDemoman && pip install -r requirements.txt
```

```bash
python -m tds_macro
```

Python 3.12 or newer. Useful flags:

```bash
python -m tds_macro --dry-run --verbose
```

| Flag | What it does |
| --- | --- |
| `--dry-run` | Log every action without sending any input |
| `--settings-dir DIR` | Keep `settings.json` somewhere other than the app folder |
| `--verbose` | Debug logging |
| `--no-wizard` | Don't open the macOS permissions window on start |
| `--self-test MS` | Build the UI, run for MS milliseconds, exit (used by CI) |

### Building the app

On Windows (or a Mac), with `pip install -r requirements-dev.txt`:

```bash
python build/build.py
```

On Windows that produces `build/dist/TDSMacro/` and a zip beside it. Add
`--onefile` for a single `.exe` as well — handier to send to a friend, but it
self-extracts on every launch (several seconds) and antivirus flags single-file
PyInstaller binaries much more readily. On a Mac it produces
`build/dist/TDSMacro.app` and a zip made with `ditto`, which keeps the bundle's
symlinks and permissions intact. PyInstaller does not cross-compile, so build on
the platform you are building for.

---

## Using it like TinyTask

The main screen has three big buttons and a few options:

| | |
| --- | --- |
| **Record** (F6) | Start recording. Play the match. Press it again to stop. The recording is saved straight away as `strategies/recording-<date>-<time>.json` — no save dialog. |
| **Play** (F7) | Replay the recording chosen in the **Recording** list. |
| **Stop** (F8) | Stops everything immediately and lets go of any held key or button. Works even when the app is in the background. |
| **Repeat matches** | Replay match after match, *forever* or a set number of times. Needs the lobby screens captured once (**Set up...**). |
| **Practice (send nothing)** | Replay without sending any input; the log shows what would have happened. Use it for every new recording. |
| **Import a video...** | Learn a match from a video — see [Learning from a video](#learning-from-a-video). |
| **Set up wave & cash reading** | Optional, and the thing that makes replays reliable — see below. |
| **Advanced editor** | The full step editor. **< Simple view** comes back. The app remembers which one you used last. |

**Without wave & cash reading** a replay works like TinyTask: the same inputs
with the same gaps. That breaks as soon as a match runs differently — a slower
wave, less cash — because the clicks keep coming on the old schedule.

**With wave & cash reading** set up (once, about five minutes), the app watches
the wave and cash numbers on screen. Recordings then say "wait for wave 3"
instead of "wait 41 seconds", purchases wait until you can afford them, and a
placement that did not take (cash did not drop) is retried. That is the main
reason to use this app rather than TinyTask.

Picking another recording from the list never loses edits: unsaved changes are
saved aside as `strategies/unsaved-backup-<date>-<time>.json` first.

---

## The setup wizard

Optional. Open it from **Set up wave & cash reading** on the main screen or
**Tools → Setup / calibration wizard**. It is needed for waiting on waves and
cash, for confirming placements, and for repeating matches. Six pages:

1. **Welcome** — the warning above.
2. **Find the Roblox window** — press *Find Roblox automatically*. If it picks
   the wrong window (which can happen after a Roblox update changes its window
   class), choose it yourself from the list below.
3. **Teach the app to read your HUD** — see the next section. This is the only
   fiddly part.
4. **Test a click** — pick a harmless spot and press *Send the click*. That
   starts a 3-second countdown: **click on the Roblox window before it reaches
   zero**, because input only goes to Roblox while it is the active window. The
   cursor should jump to your spot. If nothing happens, input is not reaching
   Roblox and nothing else will work.
5. **Loop mode screens** — optional, only for repeating matches automatically.
6. **Ready** — your hotkeys, listed.

You can rerun it any time.

---

## Calibrating the HUD

The app reads your wave number and cash by matching the shapes of digits against
examples you give it once. There is no OCR engine involved — see
[Why not OCR](#why-not-ocr).

**On the HUD page:**

1. Press **Take a screenshot of Roblox**. The app makes its own windows
   invisible for a moment while it captures, so it does not photograph itself —
   but anything *else* covering Roblox (a browser, an overlay) will be captured,
   so keep the game unobstructed.
2. Press **Mark the WAVE number** and drag a box around *just the digits*. Not
   the word "Wave", not the icon — only the number. The box is stored as a
   fraction of the window, so it keeps working if you resize or change
   resolution later.
3. Do the same for **Mark the CASH number**.
4. In the two text fields, **type exactly what each readout currently shows**,
   including the `$` and any commas: `12` and `$1,250`.
5. Press **Learn these characters**.

The app pairs each character you typed with each shape it found. If the counts
don't match it tells you rather than guessing — a wrong pairing would corrupt
every later reading, which is worse than no calibration at all. If that happens,
redraw the box more tightly or correct what you typed.

**Keep going until every digit is known.** The line under the buttons shows
which of 0–9 are still missing, for example `Wave: still needs 3 4 7`. Play on,
press *Take a screenshot* again, type the new values, and *Learn* again. Three
or four captures usually covers everything.

Press **Test the reading** to see what the app reads right now, with a confidence
score. The main window shows the live values at the top right — if those track
your real wave and cash, you are done.

Re-run this if Roblox restyles its HUD, or if you switch to a wildly different
screen resolution and the readings degrade.

### Why not OCR

Tesseract is built for document text and misreads stylised game fonts often
enough to break a `Wait for cash` gate — and bundling it would add tens of
megabytes plus a language-data folder to an app meant to be a portable drop-in.
TDS draws its HUD in one fixed font, so ten labelled glyph templates are both
smaller (about 50 KB) and more accurate. The trade-off is the labelling step
above, and needing to redo it if the HUD is restyled.

---

## Recording a strategy

Press **F6** (or the Record button), play a round the way you want it played,
then press **F6** again.

Your inputs are turned into *steps*, not raw coordinates:

- a hotbar key followed by a click in the game becomes **Place**;
- clicking a tower you already placed selects it, so the next upgrade, sell or
  ability input becomes a step that refers to that tower by name (`T1`, `T2`);
- clicking empty ground deselects, exactly as in game;
- idle gaps become **Wait** steps, rounded to a tenth of a second — or, with
  wave & cash reading on, **Wait for wave N** whenever a new wave has started.

Everything else is kept too, as in TinyTask:

- any other click becomes a **Click** (left, right or middle button);
- any other key becomes **Press key**, held for as long as you held it — so
  holding W for two seconds replays as two seconds of W, not thirty taps;
- a right-button drag (turning the camera) becomes a **Drag** that follows the
  same movement at the same speed. Roblox locks the cursor while the camera
  turns, so the movement is recorded from the mouse itself, not from where
  the cursor appears. **On a Mac** the turn replays the same way. **On
  Windows** it may not: replaying it faithfully would need relative mouse
  movement, which depends on your pointer-speed settings — something this app
  promises not to rely on. If a recording needs a camera angle on Windows, set
  it before pressing Play;
- the scroll wheel (zoom) becomes **Scroll**, with notches in one burst merged.

Not recorded: the app's own hotkeys, and keyboard shortcuts with Ctrl, Alt,
Cmd or Option held — replaying "Cmd+Q" would be a bad surprise. Mouse movement
with no button held is not recorded either; it does nothing in TDS.

Tower matching is by position, so it is a *guess*. The log says how many steps
were matched that way and warns about clicks it could not place at all. Check
which tower each step targets in the editor before trusting a recording — two
towers placed close together can resolve to the wrong one.

---

## Learning from a video

**Import a video...** reads a recording of a match — your own screen recording,
or another player's YouTube video once you have downloaded it — and turns it
into a recording you can play.

A video does not show keys or the mouse reliably, so the app does not try to
copy the cursor. It watches the **cash number** instead: every time cash drops,
something was bought. The size of the drop is the price; what appeared on the
map at that moment is where; a drop next to an existing tower is an upgrade.
Towers that cost the same are taken to be the same tower.

1. **Choose video...** MP4 and MOV work everywhere; MKV, WebM and AVI work on
   most computers. If a file will not open, convert it to MP4 (H.264) — for
   example with QuickTime's **File → Export** or VLC's **Convert/Save**.
2. If the video has black bars, a webcam or an overlay, **Mark the game
   area...** around just the game. If something inside the game view changes
   when the player buys things but is not a tower — the tower panel, a
   webcam, chat — **Ignore an area...** over it. In longer videos (six or more
   purchases) the app also finds the interface by itself: anything that
   changes with most purchases is left out, and the result says so.
3. The app needs to read this video's wave and cash numbers. If your own wave &
   cash reading already works on it (your own recording, same window size), it
   says so. Otherwise press **Teach wave & cash from this video...**: mark each
   number, type what it shows, press **Learn**, and repeat at a few other
   moments until every digit is known.
4. **Find the purchases.** A 20-minute video takes a few minutes.
5. **Check the result.** For each tower price, choose which of *your* hotbar
   slots holds that tower — another player's loadout is not yours. For each
   purchase, fix the type (placed / upgraded / ignore) and click the picture to
   move a tower if it landed in the wrong spot. Unclear ones are yellow and
   are left out unless you choose what they were.
6. **Create recording.** It opens like any other recording. Use **Practice**
   first.

Ticked by default when wave & cash reading is set up: **Wait for the right wave
and enough cash before each purchase**. That replays the build order at the
pace of *your* match. Unticked, it uses the video's own timing, TinyTask-style.

What it cannot learn from a video, so add these yourself in the editor if they
matter: selling, abilities, targeting changes, skipping waves, and anything the
player does that does not cost cash. If the player turns the camera while
buying, that purchase's spot cannot be found and is marked unclear. Two
purchases a fraction of a second apart are usually told apart; any closer and
they show up as one purchase at the combined price. A video at
a different shape from your screen (YouTube is 16:9, a MacBook is 16:10) is
handled: tower spots are moved to the same place on the map.

The best videos for this: 720p or better, the HUD not covered, the camera left
still, the same map and mode you will play.

---

## Editing a strategy

The step list runs top to bottom. Drag to reorder, double-click to edit,
**Duplicate** / **Delete** / **Up** / **Down** as you'd expect.
**Enable/disable** mutes a step without deleting it, which is how you bisect a
strategy that misbehaves.

| Step | What it does |
| --- | --- |
| **Place** | Select a hotbar slot, then click a position |
| **Upgrade** | Select a placed tower, then press upgrade N times |
| **Sell** | Select a placed tower, then sell it |
| **Ability** | Select a placed tower, then trigger its ability |
| **Skip wave** | Press the skip key N times |
| **Wait** | A fixed pause |
| **Wait for wave** | Block until the HUD reports wave N or higher. If the counter reads `12/40`, the 12 is used |
| **Wait for cash** | Block until the HUD reports at least $N |

Each wait has a **timeout**. *use default* follows **Settings → Timing →
Default wait timeout**, which is *wait forever* unless you change it — a wave
gate in TDS is routinely ten minutes or more away. If the match ends while a
wait is pending (your base is destroyed), the strategy stops cleanly instead of
waiting it out, provided the end-of-match screen has been captured in the wizard.

Prefer **Wait for wave** and **Wait for cash** over fixed **Wait** steps. They
adapt when a round runs faster or slower; a fixed wait does not.

For **Place** steps, press **Pick on screenshot…** to set the position. You get a
frozen capture of the game with a magnifier and a coordinate readout, and every
existing placement point marked. It is a still frame rather than the live game on
purpose: clicking into the live game to "mark" a spot would risk actually placing
a tower there. **Tools → Preview placement points** shows them all at once along
with your calibrated HUD regions.

Red steps have problems. **Tools → Validate strategy** lists them: a tower
referenced before it is placed, a tower used after being sold, a hotbar slot
outside 1–5, a position outside the window. Playback refuses to start with any of
these rather than skipping the broken step silently.

Strategies are plain JSON — **File → Save** / **Open**, and
**Export a copy to share** for sending to a friend. `strategies/` holds a worked
example using every step type. Its positions are placeholders: open it and set
your own.

---

## Playing back

Press **F7**, or **Play**. Before anything is sent, the app checks:

- Roblox is running and measurable;
- the strategy validates;
- the HUD is calibrated, if any step depends on it;
- the lobby screens are captured, if loop mode is on.

Anything missing is listed in the log and nothing is sent.

**Turn on Dry run for the first replay of a new strategy.** Nothing reaches
Roblox; the log shows every move, press and click that *would* have happened, in
order. It is the cheapest way to catch a strategy that would place a tower in the
wrong spot.

While running:

- **Input only goes to Roblox when Roblox is the focused window.** Alt-tab and
  playback pauses, releases any held key, and says so. Click back and it resumes
  where it left off — pending waits are extended by however long you were away,
  so a 10-second wait doesn't quietly expire while you're in a browser.
- **F8 stops everything, from anywhere**, and releases every key and mouse button
  the app was holding.
- The status line says what it is doing and what it can see:
  `Waiting for wave 12 — detected wave 11`.

**Failed placements.** A placement is confirmed by watching your cash drop — the
only signal available from outside the game. Because kill income keeps arriving,
the app watches for any drop from the highest value it has seen since the click,
not just a drop below the starting figure. If no drop shows within the
*placement check window*, it retries (twice by default, configurable). If cash
can't be read at all it says the placement is unverifiable rather than
pretending it worked.

One limit worth knowing: if a purchase and a larger burst of income both land
between two HUD readings (they are about 150 ms apart), the purchase is
invisible and a retry can place a second tower. It is rare, not impossible. Set
**Placement retries** to 0 if that would be costly in your strategy.

### Loop mode

Tick **Loop matches** to replay repeatedly. After the strategy finishes, the app
waits for the end-of-match screen — for up to an hour by default, since the map
usually runs long after your last step — then clicks *Play Again* if it's
offered, otherwise picks your map and presses Play, waits until the next match
has actually started, and runs again, for a set number of runs or until you stop
it. A match that ends early (base lost) re-queues the same way. Runs completed
are logged.

If you alt-tab in the middle of a lobby click, the app releases the mouse
button immediately and redoes that click once Roblox has focus again.

Each click is confirmed by waiting for the *next* expected screen rather than
assuming it landed, and every wait has a timeout with a specific message, so an
unexpected screen stops the loop instead of leaving it clicking into a menu for
hours.

This needs the four lobby screens captured in the wizard. Without them, loop mode
tells you which are missing and refuses to start.

---

## Settings

**Hotkeys** — defaults are F6 record, F7 play, F8 stop. All three are editable;
conflicts and typos are flagged as you type, and Save stays disabled until they
are fixed. If another program already owns a hotkey, Windows refuses it and the
log names the key — pick a different one.

**Game keybinds** — hotbar slots 1–5 and the four tower actions. **None of these
are assumed.** Check them against your own TDS keybind screen. Each tower action
is either a key press or a click on a menu button; if your TDS shows Upgrade and
Sell as on-screen buttons rather than keys, choose *Click a menu button* and point
at it.

**Timing** — delays between actions, key and click hold times, how long to wait
for the tower menu, and how long before checking whether a placement went
through. Raise these if the game drops inputs. Jitter adds a little randomness,
because identical back-to-back timings make a dropped input reproducible rather
than rare.

**Loop mode** — number of runs, whether to try *Play Again*, how long to wait for
each lobby screen, and the *longest match after the last step* (1 hour by
default; 0 means forever).

**Safety** — Dry run, *only send input while Roblox is focused* (leave this on;
with it off the macro keeps clicking into whatever window you switch to), and
hardware scan codes (on by default, since more games read scan codes than virtual
key codes).

Everything lives in `settings.json` beside the exe. If that folder isn't writable
— you dropped the app in `Program Files` — settings go to `%LOCALAPPDATA%\TDSMacro`
instead and the log says so. A corrupt settings file is moved aside as
`settings.json.broken` and defaults are used, rather than preventing startup.

---

## Troubleshooting

**Start the diagnostics first.** On Windows:

```bash
python tools/win_check.py
```

On a Mac (from source):

```bash
python3 tools/mac_check.py
```

These check the platform APIs, your monitor layout, window detection, hotkeys
and screen capture — and on a Mac, which of the three permissions are on. They
send no input and do not need Roblox running.

| Symptom | Cause and fix |
| --- | --- |
| **Mac:** the macro runs but nothing happens in Roblox | Accessibility is off. System Settings → Privacy & Security → Accessibility → switch on TDS Macro. The log says so explicitly. |
| **Mac:** the screenshot shows only the wallpaper | Screen Recording is off, or was switched on without restarting TDS Macro. |
| **Mac:** hotkeys do nothing | Input Monitoring is off, or TDS Macro needs reopening after you switched it on. |
| **Mac:** "Roblox window not found" | Roblox is minimized, or full screen on its own desktop. Turn full screen off inside the game (Esc → Settings → Fullscreen: Off). |
| **Mac:** permissions were on, and stopped working | You updated or rebuilt the app; macOS ties permissions to the exact build. Switch them off and on again. |
| "Roblox window not found" | Roblox isn't running, is minimized, or is in fullscreen-exclusive mode. Switch to borderless/windowed, then **Tools → Re-find Roblox window**. After a Roblox update changes its window class, pick the window yourself in the wizard. |
| Nothing happens when playback starts | Roblox must be the *focused* window. Click on Roblox, then trigger playback with the hotkey rather than the on-screen button. |
| Clicks land in the wrong place | Almost always DPI. Check **Help → About** says *per-monitor DPI aware v2*. If it says anything else, report it with your Windows version. Also re-check that the app found the right window — the client area, not a wrapper. |
| Clicks land on the wrong monitor | Run `tools/win_check.py` and check the *absolute round trip* line says *lossless* and that the corner checks pass on your layout. |
| Wave or cash reads wrong, or is blank | Re-run the calibration wizard. Check the missing-digits line: a digit the app has never seen cannot be read. Make the boxes tighter — they should contain only the number. |
| "Timed out waiting for wave N" | Only happens if you set a timeout. Usually the HUD isn't being read correctly, and the macro gave up rather than acting blind. Re-calibrate. The live readout at the top right tells you immediately whether reading works. |
| Calibration screenshot shows something other than the game | Something besides TDS Macro is covering Roblox — a browser, an overlay. The app hides its own windows while capturing, not other programs'. |
| The test click says Roblox was not the active window | Press *Send the click*, then click on Roblox during the 3-second countdown. |
| Placement keeps retrying and failing | Not enough cash, an invalid spot, or the hotbar slot holds a different tower than when you recorded. Watch a Dry run to see which. |
| Upgrade/sell does nothing | Your TDS probably uses on-screen buttons, not keys. **Settings → Game keybinds** → *Click a menu button* and pick it. |
| A hotkey doesn't work | Something else owns it. The log says which. Pick another in Settings. |
| Loop mode stops after one match | A lobby screen wasn't recognised. The log names which one; re-capture it in the wizard. These are the most fragile part of the app — the lobby changes more often than the in-match HUD. |
| Windows Defender flags the exe | An unsigned PyInstaller binary that synthesises input and captures the screen is exactly the shape heuristics dislike. Run from source instead if you'd rather not add an exclusion. |
| A key stays held down | It shouldn't — every stop path, including a crash, releases held keys. If you see it, press F8 and please report it with the log. |

Logs are written to `tds_macro.log` beside `settings.json`, and the in-app log
panel shows the same thing with timestamps.

---

## How it is put together

```
tds_macro/
  input/     coordinates and key tables; Windows (SendInput, RegisterHotKey,
             low-level hooks) and macOS (Quartz events, event taps, window
             list, permissions) behind one platform factory
  vision/    screen capture, HUD calibration, digit recognition, template
             matching, video analysis
  strategy/  step model and JSON format, playback engine, recorder, loop
             controller
  ui/        PySide6: simple screen, advanced editor, pickers, settings,
             wizard, video import
tools/       win_check.py and mac_check.py diagnostics
tests/       367 tests, no Windows or Mac required
```

A few decisions worth knowing if you intend to change it:

**Everything is stored relative to the game window.** Positions are fractions of
the Roblox client area (0.0–1.0), so a strategy recorded in a 1280×720 window
replays correctly at 2560×1440 fullscreen, on a different monitor, at a different
DPI. `norm_to_screen` and `screen_to_norm` are exact inverses on the same
rectangle.

**Mouse movement is absolute, flagged `MOUSEEVENTF_VIRTUALDESK`.** That makes
pointer acceleration, mouse speed and *Enhance pointer precision* irrelevant to
where a click lands, without reading or changing any of your system settings.
Screen-to-absolute conversion aims at the centre of each pixel band, because
Windows converts back with an integer `(abs * width) >> 16` — targeting the
leading edge lands clicks one pixel short, which only shows up at a monitor edge,
where it spills onto the next display. The virtual-desktop origin is handled as
signed, since it is negative whenever a monitor sits left of or above the primary.

**DPI awareness is set before Qt loads.** Qt otherwise claims its own, lower,
awareness first, after which `GetClientRect` returns scaled logical pixels while
`mss` captures physical ones — silently offsetting every click on any display
above 100% scaling.

**The engine never blocks.** Steps are generators yielding `Sleep`/`Do`/`Await`
commands, advanced by a short timer tick with the clock injected. That keeps the
UI responsive, makes a dry run a faithful trace of what would be sent, and makes
every timing path testable without real delays.

**Win32 structures use explicit fixed-width types**, not `ctypes.wintypes`, whose
`LONG` is 64-bit on Linux. One definition that is correct on Windows and
verifiable off it — `INPUT` is 40 bytes, `MSG` 48, `RECT` 16.

### Stack notes

Python 3.12 with PySide6, `ctypes` for Win32, `mss` for capture, OpenCV for
template matching, PyInstaller to package. Three deviations worth naming:

**No pywin32.** Everything Win32 goes through `ctypes` directly. pywin32 would
add roughly ten megabytes to the bundle and give up exact control of structure
layout, which is the one thing that has to be right — a wrong `INPUT` size means
`SendInput` rejects every event and the app appears to do nothing. Declaring the
widths explicitly also makes the ABI checkable from Linux, where pywin32 cannot
even be installed.

**No Tesseract.** Covered under [Why not OCR](#why-not-ocr).

**C#/.NET would have been the better engineering choice**, and it is worth being
honest about that: first-class Win32 interop with no ctypes marshalling, a ~3 MB
self-contained exe, per-monitor DPI v2 declared in the app manifest rather than
set at runtime, and far fewer antivirus false positives. Python wins on OpenCV
iteration speed and on being readable and modifiable by whoever owns this next,
which for a tool you will keep tweaking matters more.

### Development

```bash
pip install -r requirements-dev.txt
```

```bash
ruff check . && mypy tds_macro && pytest -q
```

Qt needs a platform plugin for the UI tests. On headless Linux:

```bash
QT_QPA_PLATFORM=offscreen pytest -q
```

CI runs lint, types and tests on Linux, then on `windows-latest` runs the suite
again (where the Win32 modules import for real), runs `tools/win_check.py`, boots
the GUI from source with `--self-test`, builds the bundle, **launches the built
exe** with `--self-test`, and only then uploads it. The macOS jobs (Apple Silicon
and Intel) do the same with `tools/mac_check.py` and the built `.app`, and also
confirm the bundle can load the macOS frameworks — without them it would start
but silently do nothing.

---

## What this app will not do

No memory reading, no DLL injection, no process attachment, no executor, no
bypassing Roblox's anti-cheat, and no attempt to disguise automation as human
input. If you need any of that, this is the wrong tool — and that boundary is not
negotiable, it is the entire design.
