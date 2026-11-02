# Anvil Fletch

A local agent that lives on your Windows laptop. Ask it for an outcome — tidy a folder,
gather files, rename a batch, look something up, download a song or a video, drive an app,
run a chore — and it plans, uses tools, shows its work compactly, then gives you the result.
A Win32 front end in C++ owns a Python agent runtime with 267 tools; the brain is any model
you point it at — a local Ollama model or an OpenAI-compatible API. Licensed under MIT.

## Features at a glance

- **267 tools** across files, downloads, docs, images, media, web, UI control, clipboard,
  system, processes, network and utility — plus deterministic fast routes that answer
  common commands in under a second without a model round trip.
- **Drives real apps** through UI Automation, OCR and raw input: opens the copy you already
  have, reads windows and chats, clicks what it sees, types where you point it.
- **Full downloader** — YouTube audio/video, playlists, bulk fetches, wallpapers, with live
  progress in the UI.
- **Bulk file chores** — convert, rename, gather, deduplicate, archive, organize whole
  folders in one request.
- **Self-testing and self-teaching** — 142 routing scenarios (984 checks) run offline, and
  the agent writes itself lessons from failures.
- **Multi-model** — Ollama or any OpenAI-compatible endpoint (OpenAI, OpenRouter, LM
  Studio, vLLM, ...), switchable from the model menu.

## Requirements

- Windows 10/11 (x64)
- Ollama running locally with `qwen2.5-coder:7b` pulled
- The Python launcher `py` on PATH (the front end starts `src\agent_backend.py` through it)
- Python packages for downloads, media, images and app control:
  ```
  py -m pip install --user yt-dlp imageio-ffmpeg pywinauto pillow
  ```
  `imageio-ffmpeg` provides the ffmpeg binary used to merge video+audio and convert audio;
  an `ffmpeg.exe` on PATH works too. `pywinauto` drives other apps through Windows UI
  Automation (see **Controlling apps** below). `pillow` powers batch image conversion.
- 7-Zip is optional but recommended: `archive` uses `7z.exe` for zip, 7z, rar, tar, gz,
  bz2, xz, cab, iso, jar, apk and wim (including passwords and multi-volume archives).
  Without it, plain zip/tar fall back to Python's standard library.
- Node.js is recommended: yt-dlp uses it to solve YouTube's JavaScript challenges. Without
  it some videos stay unavailable. On the first challenge yt-dlp fetches its official
  solver component (`ejs`) from GitHub; after that it is cached.

## Models and providers

The default is a local Ollama model, but any OpenAI-compatible endpoint works too —
OpenAI, OpenRouter, LM Studio, vLLM, llama.cpp servers, anything speaking
`/chat/completions`. Profiles live in `config/models.json`:

```json
{
  "temperature": 0.2,
  "num_ctx": 16384,
  "models": [
    { "name": "qwen2.5-coder:7b",
      "provider": "ollama",
      "host": "http://127.0.0.1:11434" },

    { "name": "gpt-4o-mini",
      "provider": "openai",
      "base_url": "https://api.openai.com/v1",
      "api_key_env": "OPENAI_API_KEY" },

    { "name": "qwen/qwen3-coder",
      "provider": "openai",
      "base_url": "https://openrouter.ai/api/v1",
      "api_key_env": "OPENROUTER_API_KEY" },

    { "name": "local-model",
      "provider": "openai",
      "base_url": "http://127.0.0.1:1234/v1" }
  ]
}
```

- `provider` is `ollama` (default) or `openai`. Ollama profiles may set `host`.
- `base_url` is the API root; `/chat/completions` and `/models` are appended.
- **Never paste keys into the file.** Set `api_key_env` to the name of an environment
  variable holding the key (preferred), or `api_key` for a throwaway local profile
  (keep it out of git). Extra request headers go in `headers`.
- Top-level `temperature` and `num_ctx` are the sampling defaults for all profiles.
- Profiles are matched to requests by `name` — that is also the label the model menu
  shows. The picked model travels with the chat request; vision attachments use the
  same provider.
- Everything else — streaming, tool decisions, grammar-constrained JSON, step budget,
  fast routes, lessons — behaves identically on both providers.

## Run

Double-click `Start-AnvilFletch.bat`, or:

```
make
bin\AnvilFletch.exe
```

The exe is statically linked; no MinGW DLLs needed at runtime.

## Using it

- **Workspace** — the folder relative paths resolve in. Click the row to pick another with
  the system folder dialog. Standard folders (Desktop, Documents, Downloads, ...) are
  recognised even when the workspace is somewhere else.
- **Model** — click the model row to switch between available models: everything
  `ollama` has pulled plus every model named in `config/models.json` (see below).
- **New task** (`Ctrl+N`) — starts a clean conversation; recents stay in the sidebar.
- **Copy earlier prompts** — every recent prompt in the sidebar has a copy button, and every
  message you sent in the conversation has one too (top-right of the bubble). Clicking copies
  the full text and shows a short "Prompt copied to clipboard" toast, so repeating a task is
  one click instead of retyping. Recent prompts are stored in `data/prompts.json`, separate
  from conversation history, so they survive New task and restarts.
- **Clear recents** — the small **Clear** button next to RECENT empties that list (and
  `data/prompts.json`) without touching the current conversation. It is disabled-looking
  when the list is already empty.
- **Attach pictures and files** — the paperclip in the composer opens a multi-select picker
  (pictures, PDF, DOCX, text, CSV, JSON); you can also drag files onto the window. Chips above
  the composer show what is queued, the paperclip carries a count badge, and clicking a chip's
  × removes it. Up to eight files per message. Pictures are read with Windows OCR plus image
  metadata, and a local vision model (llava, qwen2.5-vl, minicpm-v, moondream, gemma3) adds a
  real description when one is pulled. PDFs, DOCX and text files are read too. The attachment
  text is inserted into the prompt under ATTACHED FILES with exact paths, so the model answers
  from it instead of guessing a path and failing. "Look at those screenshots in <folder>"
  uses `see_images` on the folder without attaching anything.
- **System tray** — closing the window while the agent is working hides it to the tray
  instead of orphaning a running backend. Left-click the tray icon to bring it back;
  right-click for **Open Anvil Fletch**, **Stop the running agent** (enabled only while
  busy) and **Quit (kills the agent)**. When a hidden run finishes, a balloon says so.
  Closing while idle exits normally.
- **Composer** — one plain text box that grows with your text up to a limit, then scrolls.
  `Enter` sends, `Shift+Enter` inserts a newline, `Esc` (or the round stop button) stops the
  run, including an in-progress download.
- **Live progress** — downloads, bulk fetches, wallpaper grabs, GitHub tool installs and
  folder conversions show a progress card just above the composer: the job label, the item
  being fetched, a bar, and either bytes (`Downloading sound effects - 42% (210/500 MB)`) or
  a file count (`Converting 120 audio file(s) - 37/120 files`). Jobs without a known total
  animate an indeterminate bar. The card clears when the job ends and never gets stuck
  (it times out after a few idle seconds). Bulk downloads start at 0% and accumulate their
  total as each response reports Content-Length.
- **Exact destinations** — when you type "download X to `C:\...\file.png`", that path is
  handed to the downloader directly, even if the model forgets it: the file lands exactly
  where you pointed, and the reply repeats the path the tool actually wrote. Same for
  `fetch_file` (`dest`) and `screenshot` (`path`).
- **Study loop** — toggle in the sidebar. When on, the agent studies C++ and competitive
  programming on its own, with no further input: it picks something it is curious about next,
  searches the web, opens the search and up to six result pages in your default browser (so
  they land in your browsing history), reads them, and writes a markdown note to
  `Documents\AI_AgentLearns\` with the topic, what it learned and the sources. `_progress.md`
  keeps a running log. It then picks the next topic and keeps opening new pages indefinitely —
  a failed fetch or a slow Ollama reply is logged and skipped instead of ending the session.
  It stops only when you press `Esc` / Stop or turn the toggle off; the toggle follows the run
  (New task and Stop turn it off). This costs real CPU time — expect a few minutes per topic
  on a 7B.
- **Steps pill** — an expanded turn shows a live timeline (thinking lines, tool calls with
  timings). Click the pill to collapse/expand, click a tool row for its arguments and output.
  Stopped tools show amber, failed tools red, and the pill counts failures separately.
- **Plans** — multi-stage jobs get a plan card with per-item status.
- **Failures** are visible: red tool rows, and the final reply says what went wrong and what
  was tried instead. The prompt tells the model to read the error, change approach, and stop
  after two failures of the same tool instead of reinstalling things.
- **Memory across turns and sessions** — recent messages are kept in context, and the last
  dozen user requests from earlier sessions are copied into the prompt, so "download those",
  "do it again", "the same list" or "use the same folder" refer back to the list or path you
  gave earlier instead of starting from scratch.
- **Internet first** — "get me / find me / grab / download X" means the internet, not your
  workspace. The runtime no longer answers "I couldn't find it in the workspace" for a thing
  that was never there; it searches and fetches.
- **Voice is opt-in** — `speak` only runs when you actually asked to hear something ("read it
  out loud", "say it", "speak the answer"). It never narrates errors or talks over a normal
  task.

Simple questions and conversation skip the tool loop and stream an answer directly.
Anything touching files, apps, the web, media or system state goes through the agent loop.

## Downloads

`youtube_download` takes a URL, a video id, or a plain search phrase (`"lofi study mix"`),
plus `kind=audio|video`, an optional `folder`, and `quality`. Leave `quality` empty for the
best available; `video` merges to MP4 when ffmpeg is present. `download_audio`,
`download`, `download_page_media`, `convert_media`, `extract_audio`, `trim_media` and
`media_info` cover the rest.

Audio means audio: songs are fetched audio-only and converted, the saved file is checked for
an actual audio stream, and a video that turns out silent is retried and merged with audio.
Search results are scored — official audio/topic/lyric uploads win, while how-to videos,
ringtones and shorts are pushed down. Remix, sped up, slowed, cover, instrumental, live and
similar versions are avoided **unless you asked for that version**: say "sped up", "remix",
"nightcore", "instrumental" and the picker seeks that match instead of penalising it. The
note in the reply names the result it actually grabbed.

Every download streams progress to the UI. `download_many(urls, folder, label)` lets the
planner name the job (`"Downloading bulk sound effects"`), and the bar shows the shared byte
total across all four workers, so a batch reads like one transfer.

Media downloads run **inside** the backend process through yt-dlp's Python API (falling back to
the yt-dlp CLI when only that exists). Spawning a fresh `python.exe` for every download
proved unreliable on machines where third-party DLLs get injected into new processes, so
there is no child process to hang.

## Bulk work

Repetitive jobs are one request, not one request per file:

- `download_many` fetches several URLs at once (four at a time) and reports each result;
  `fetch_file` is the single-file version and names the file from the response headers.
- `youtube_download_many` queues songs or videos from searches or URLs, two at a time.
- `wallpapers("theme", count, folder, set_first)` downloads good 16:9 images from Wallhaven
  (1920x1080 minimum) with a Picsum fallback, four at a time, and can apply the first as
  your desktop. "Download wallpapers" is a direct path: the runtime answers it without
  waiting for the model to choose a tool.
- `convert_folder(path, fmt)` walks a folder and converts every matching image in place,
  replacing the original (webp, avif, jpeg, jpg, bmp, tiff -> png, and any other Pillow
  format). `overwrite=false` keeps the sources; `max_side` downsizes during conversion.
- `convert_folder(path, fmt="auto")` is the mixed-folder switch: every picture becomes PNG
  and every audio file becomes MP3 in one pass ("turn every audio file into mp3 and every
  picture into png"). `convert_audio_folder` does audio alone — wav, flac, ogg, m4a, aac,
  wma, opus, aiff, webm to mp3 at 192k by default via the bundled ffmpeg — and both report
  per-file progress. Originals are removed only after the converted file exists.
- `archive` is a real archive handler: list, extract, create or add, with passwords, using
  7-Zip for zip/7z/rar/tar/gz/bz2/xz/cab/iso/jar/apk/wim, falling back to zipfile/shutil.
- `resource_pack(version, template, replacements)` builds a Minecraft resource pack: it
  takes a template zip/folder if you have one, otherwise downloads that version's vanilla
  client jar from Mojang, extracts its assets, writes `pack.mcmeta` with the matching
  `pack_format`, overlays your replacement textures, zips the pack and tells you where it is.

## What it is best at

A 7B on CPU is not a brilliant planner, so the product leans on deterministic tools and
narrow fast paths for the jobs people repeat. The five highest-value uses:

1. **Building a media library** — "download these 20 songs as mp3", "grab the 4K version",
   "get 10 nature wallpapers", "rip the audio from these videos". Batch tools, audio-only
   extraction, explicit version handling (sped up / remix only when asked), and a direct
   wallpaper path keep it fast and predictable.
2. **Bulk file chores** — tidy or rename a folder, gather photos into one place, convert a
   folder of webp/avif/jpeg to png in place, deduplicate, archive or unpack, build a
   resource pack. These are deterministic tools with real paths, so the model does not have
   to guess.
3. **Driving apps and reading their contents** — open an app, read a chat, find a button by
   name, collect a long conversation, or OCR a custom-drawn window and click the exact text.
   The runtime auto-reads the target window before the model can claim it cannot see it.
4. **Research that ends in a file** — search, fetch pages, and write a clean brief or notes
   to disk, with internet-first behavior: "find me X" means the web, not your workspace.
5. **Repeating yesterday's chore** — copy a recent prompt with one click, and taught rules
   carry over, so the same download/tidy/research job is a paste away.

Optimised for those: direct wallpaper routing, four-way parallel downloads with a live
byte-count progress bar, 7-Zip batching, in-place image/audio conversion with per-file
progress, picture attachments read by OCR (plus a vision model when installed), reuse of
already-running apps instead of second copies, automatic window/tab context, DPI-correct
OCR coordinates, prompt history that survives New task, and a tray that keeps long jobs
running safely.

## Controlling apps

Opening an app reuses the copy you already have. `open_app("AyuGram")` enumerates every
titled top-level window — including minimized and tray-hidden ones — matches by executable
name or window title, then restores and focuses it instead of starting a second, unlogged
instance. `launch` does the same for plain app names with no arguments. A new window is only
started when no running copy exists, via the Start menu app id or PATH.

The agent drives any Windows app the way a person would, without a per-app integration:
`launch` it, `wait_window`, `window_text` it (all readable text in one call: document, chat
or editor contents, then labels, then OCR), read `ui_tree` (the accessibility tree: buttons,
menus, lists, text boxes), then act with `ui_click` (by name, control type or index, left/right/middle),
`ui_type`, `send_keys`/`hotkey`, `mouse_scroll` or raw `mouse_click`. `ui_wait_element`
waits for a control to appear, `ui_scroll` pages a view, and `ui_collect` focuses a list or
chat pane, wheels through up to N pages, and returns the de-duplicated text — that is how it
"digs through" a long chat or inbox. It inspects again after each step, and falls back to Tab
navigation, keys and screen coordinates when a control is not exposed to UI Automation.

The common two-step chore is a single deterministic route instead of two slow model turns:
"open notepad and type hello world" opens the existing copy (or starts it), focuses the real
window and pastes the text in about three seconds. "open chrome and search for cats" types
the query and presses Enter. Plain "open X" reuses a running, minimized or tray-hidden copy,
and a modal dialog from the same app can no longer be mistaken for its main window.

Windows that hide in the tray are handled first-class: `tray_icon_click(name)` clicks the
app's actual tray icon, `open_app` retries through the tray, and `app_restart(name)` relaunches
a wedged app with its data saved. A window whose title changed to its content (AyuGram shows
the open chat's name) is still found by its executable, so "dig through my chats" keeps
working after you switch chats.

When an app exposes no accessible controls at all (custom-drawn UI), `screen_ocr` captures
the screen (or a window PNG) with Windows' built-in OCR and returns the text with screen
coordinates plus each line's centre, so a follow-up `mouse_click` can hit what it read.
`find_on_screen("text", window)` does the OCR and returns the exact centre `(x, y)` and
bounding box for the matches, so the agent clicks what it actually sees instead of guessing.
The runtime is DPI-aware and coordinates are physical pixels: clicks land on the right text
on scaled displays (125%, 150%) too.

It is also aware of what is open, not only what is visible: the desktop is captured every
turn as a list of open window titles, and when a request mentions a browser, tabs, a page or
the screen, the live tab list is added (`browser_tabs` reads real tab titles through UI
Automation). That matters with an auto-hide taskbar — "the window is open" is known even when
nothing is on screen.

Ask it to open something, fill a form, read a chat, or reply to a message and it works the
real window instead of claiming it cannot see your apps. When a request is plainly about
reading a window ("read the Notepad window", "dig through my chats"), the runtime reads the
most likely window automatically before the model gets a chance to flounder, then hands it
the text — a small model no longer gets to guess a file path or deny access.

If a tool call arrives without its required arguments, the runtime re-asks for just that
tool's arguments under its own schema before running it, so a missed `text` or `path` does
not abort the task.

## Speed and self-tests

Fast routes answer short, unambiguous commands without a model round trip: open/focus/close
an app, click a visible label, type literal text, press a key or shortcut, scroll, read the
screen, wait, check the battery/disk/Wi-Fi/IP/uptime, take a screenshot, and the read-only
chat sweep. Each action is budgeted at a few seconds — opening or restoring an app is
0.2–0.7s, a tray-icon click 0.3s, a wedged-app restart about 3s, OCR about 1s, and a two-chat
dig about 4–5s — and the UI animates at 60fps even mid-run because repaints only redraw the
spinner.

When a request does reach the model, the prompt carries a ROUTE HINT naming the most likely
first tool, and a step budget stops a run from wandering: it warns three steps before the cap
and then answers with what it has. Two consecutive failures of the same tool generate a
lesson automatically (deduplicated, capped), and twelve core lessons cover the traps that
used to cause flailing: tray-hidden windows, renamed titles, clicking controls, reading app
text, typing into fields, empty UI Automation, read-only requests, and not undoing work you
just did.

`python scripts/selftest.py` runs the backend's own suite: 142 routing scenarios, schema
checks for every tool, lesson seeding, and the fast-route expectations — 984 checks in all.
`python scripts/selftest.py --timing` also prints the real latency of app launch, tray click,
focus, screen read and OCR.

## Hard or proprietary files

Some files are neither text nor pictures: Unity `.assets`, `.bundle`, `.unity3d`, game
archives and installer payloads. The agent takes the route a person would — find the right
tool online, download it, and drive the tool's UI.

- `github_release(owner/repo)` reads the GitHub releases API, downloads the latest asset
  (zip/7z/exe) into `tools/<repo>`, unpacks it safely and returns the path. `asset` picks a
  file by name fragment, `folder` sets the destination, `launch` starts the binary.
- `open_unity_asset(path)` fetches UABEA (Unity Asset Bundle Extractor Avalonia) when it is
  missing, opens the `.assets`/`.bundle`/`.unity3d` file and returns its window, so the edit
  happens in the real editor (inspect, export, change, import, save) instead of a fake parser.

Honest limit: this is GUI automation on third-party editors, not a one-shot API. The agent
can install and launch the tool, read the window, and click/type through the dialogs, then
verify the written file; a complicated edit can still take several steps and may need your
guidance. It should never claim a proprietary file cannot be edited — a person with this
laptop would install a tool and try.

## Teaching it

Explicit form:

```
teach: when gathering music from Downloads -> copy the mp3s to Music\Inbox and shorten the names
```

You can also correct it in plain language ("from now on put downloads in ..."); durable rules
are stored in `data/lessons.json` and included in the system prompt on later runs. It will not
store the current task itself as a lesson.

If a request says *read-only* / *do not move or change anything*, write tools are blocked for
that run.

## Build from source

```
C:\msys64\ucrt64\bin\g++.exe -O2 -std=c++17 -mwindows -municode src\main.cpp ^
    -o bin\AnvilFletch.exe -static -static-libgcc -static-libstdc++ ^
    -lcomctl32 -ldwmapi -luxtheme -lgdiplus -lole32 -luuid -lgdi32 -luser32 -lshell32
```

`-static` matters: without it the exe binds to whichever `libstdc++-6.dll` is first on PATH.

## Layout

```
src/main.cpp          Win32 UI (GDI + GDI+), tray icon, owns the backend process and the event stream
src/agent_backend.py  agent loop, 267 tools, Ollama client, lessons, history, prompt log
src/ocr.ps1           screen OCR helper (Windows.Media.Ocr) used by screen_ocr
config/settings.json  workspace, model, study loop (user-specific, not in git)
config/models.json    provider profiles: local Ollama or OpenAI-compatible APIs
tools/registry.json   custom tools created at runtime
bin/anvil.ico         app icon (multi-size)
assets/               icon source art, used by scripts/make_icon.ps1
data/history.json     conversation history (also feeds the sidebar recents)
data/prompts.json     every user prompt, kept across New task and restarts
data/lessons.json     taught procedures
scripts/              dev harness: screenshots, clicks, tool smoke tests, icon builder,
                     selftest.py (routes + schemas + timings)
```

The UI starts the backend once and keeps it warm. They speak newline-delimited JSON events
over anonymous pipes (`status`, `thinking`, `tool`, `plan`, `token`, `answer`, `done`, ...).
If the backend process dies, the UI says so and respawns it on the next message.

## Tools (267)

The loop also has deterministic fast routes: short, unambiguous commands (Recycle Bin
contents, time, battery, Wi-Fi, disk space, process hogs, clipboard, screenshot, lock,
volume/media keys, opening a URL or a known app, listing a folder) are answered by the
matching tool directly, without a model round-trip. `do_actions` batches several tool
calls in one step and `ui_batch` runs many UI actions on one window, so multi-step chores
cost fewer planning rounds.

**Files (36)**: `read_file`, `write_file`, `edit_file`, `list_dir`, `tree`, `glob`, `grep`, `mkdir`, `copy`,
`move`, `rename`, `shorten_names`, `gather`, `delete`, `hash`, `archive`, `resource_pack`,
`file_info`, `folder_size`, `disk_usage`, `recent_files`, `find_duplicates`, `largest_files`,
`count_files`, `organize_folder`, `create_shortcut`, `path_exists`, `file_compare`,
`touch_file`, `file_version`, `hex_dump`, `file_attributes`, `drive_list`, `temp_clean`,
`open_with`, `file_permissions`.

**Download (14)**: `youtube_download`, `youtube_download_many`, `download_audio`, `download`, `fetch_file`,
`download_many`, `download_page_media`, `wallpapers`, `youtube_search`, `convert_media`,
`extract_audio`, `trim_media`, `media_info`, `github_release`.

**Docs (17)**: `pdf_info`, `pdf_text`, `pdf_merge`, `pdf_split`, `pdf_extract`, `pdf_rotate`, `pdf_to_images`,
`image_to_pdf`, `docx_create`, `docx_read`, `xlsx_write`, `xlsx_read`, `pptx_create`,
`csv_read`, `csv_write`, `read_json`, `write_json`.

**Images (16)**: `image_info`, `image_resize`, `image_convert`, `convert_folder`, `image_crop`, `image_rotate`,
`image_flip`, `image_blur`, `image_thumbnail`, `image_watermark`, `image_exif`, `image_merge`,
`image_ocr`, `screenshot`, `window_screenshot`, `see_images`.

**Media (10)**: `convert_audio_folder`, `video_frame`, `video_merge`, `video_scale`, `video_speed`,
`video_to_gif`, `gif_to_video`, `audio_play`, `audio_normalize`, `audio_merge`.

**Web (22)**: `open_url`, `web_search`, `web_fetch`, `wikipedia`, `weather`, `news`, `rss`, `define`,
`url_metadata`, `check_url`, `http_json`, `web_links`, `web_tables`, `web_archive`,
`web_headers`, `web_save_page`, `web_download_links`, `youtube_info`, `youtube_playlist`,
`web_image_search`, `webpage_screenshot`, `browser_tabs`.

**Control (52)**: `mouse_move`, `mouse_click`, `mouse_drag`, `mouse_scroll`, `mouse_position`, `type_text`,
`hotkey`, `send_keys`, `key_tap`, `page_scroll`, `window_list`, `window_focus`, `active_window`,
`close_window`, `minimize_window`, `maximize_window`, `restore_window`, `wait_window`,
`window_info`, `window_move`, `window_resize`, `window_set_rect`, `window_snap`,
`window_topmost`, `window_opacity`, `window_close_all`, `ui_tree`, `ui_click`, `ui_type`,
`ui_read`, `ui_scroll`, `ui_collect`, `ui_wait_element`, `ui_wait_text`, `ui_find`, `ui_invoke`,
`ui_set_value`, `ui_toggle`, `ui_keys`, `ui_menu`, `ui_batch`, `window_text`, `screen_ocr`,
`find_on_screen`, `open_app`, `launch`, `open_path`, `open_unity_asset`, `run_command`,
`process_list`, `kill_process`, `selected_text`.

**Clipboard (6)**: `clipboard_read`, `clipboard_write`, `clipboard_clear`, `clipboard_to_file`,
`clipboard_from_file`, `clipboard_image`.

**System (47)**: `sysinfo`, `battery`, `wifi_status`, `network_info`, `brightness`, `volume_up`, `volume_down`,
`mute`, `play_pause`, `media_next`, `media_prev`, `installed_apps`, `env`, `uptime`,
`lock_screen`, `speak`, `notify`, `empty_recycle_bin`, `recyclebin_list`, `recyclebin_restore`,
`set_wallpaper`, `wait`, `os_info`, `cpu_usage`, `memory_usage`, `disk_health`, `cpu_temp`,
`gpu_info`, `services_list`, `service_control`, `startup_list`, `startup_add`, `startup_remove`,
`scheduled_tasks`, `power_plan`, `shutdown_pc`, `event_log`, `windows_updates`, `printers_list`,
`bluetooth_devices`, `usb_devices`, `audio_devices`, `display_info`, `timezone_info`,
`font_list`, `drivers_list`, `system_report`.

**Processes (7)**: `process_info`, `process_top`, `process_tree`, `process_priority`, `process_wait`,
`process_suspend`, `process_resume`.

**Network (13)**: `ping_host`, `dns_lookup`, `port_check`, `net_connections`, `public_ip`, `wifi_networks`,
`wifi_profiles`, `wifi_connect`, `firewall_status`, `arp_table`, `route_table`, `proxy_info`,
`net_speed_test`.

**Flow (2)**: `do_actions`, `wait_for`.

**Utility (10)**: `now`, `calculate`, `remember`, `lessons`, `forget`, `create_tool`, `run_custom`,
`list_tools`, `voices_list`, `tts_save`.

## Known limits

- Speed is CPU inference: short replies land in a few seconds warm, file jobs take tens of
  seconds, long essays stream for minutes.
- The 7B can still pick a clumsy first tool; the loop recovers, but a bigger model is the
  real upgrade path (`ollama pull` a larger model, then switch in the model menu).
- Downloads depend on YouTube and yt-dlp staying compatible. When YouTube changes, run
  `py -m pip install --user -U yt-dlp`. The solver component (`ejs`) needs Node.js to be
  useful and is fetched from GitHub on demand.
- If a security tool injects DLLs into processes on your machine (e.g. Windhawk mods), the
  OS may delay or block newly spawned interpreter processes. Anvil Fletch avoids spawning
  interpreters for tools, but the `run_command` tool still spawns normal programs.
