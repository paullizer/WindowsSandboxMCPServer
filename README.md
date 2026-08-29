# Windows Sandbox MCP Server

A local MCP server that stands up a **Windows Sandbox** and drives it, so a coding agent can run a
repo's install-and-deploy flow **from a pristine machine** while the
[Snagit MCP server](https://github.com/paullizer/LocalSnagitMCPServer) records the screen.

This repo is the **driver**; the Snagit server is the **camera**.

## Requirements

- Windows 11 Pro, Enterprise or Education (no Home), with hardware virtualization
- The **Windows Sandbox** optional feature enabled ([installation guide](https://learn.microsoft.com/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-install))
- Python 3.10+
- [Optional, for recording] - [Snagit MCP server](https://github.com/paullizer/LocalSnagitMCPServer)

## Setup

```powershell
git clone https://github.com/paullizer/WindowsSandboxMCPServer.git
Set-Location .\WindowsSandboxMCPServer
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m sandbox_mcp --selftest
```

`--selftest` prints `check_sandbox_setup` and should report `"ready": true` with an empty
`blockers` list.

The repo ships [.vscode/mcp.json](.vscode/mcp.json), so VS Code offers to start the server as soon
as you open the folder. To use it from any workspace, add the same block to your user-level
`mcp.json` with absolute paths instead of `${workspaceFolder}`.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `SANDBOX_MCP_ENV_FILE` | repo `.env`, then `.\.env` | Where server settings are read from before the environment |
| `SANDBOX_MCP_WORK_DIR` | `%LOCALAPPDATA%\sandbox-mcp` | Where generated `.wsb` configs and control folders live |
| `SANDBOX_MCP_PAYLOAD_DIR` | repo `payload/` if present | Folder mapped read-only to `C:\payload` |
| `SANDBOX_MCP_STORYBOARD_DIR` | repo `storyboards/` if present | Where `run_storyboard` looks up names |
| `SNAGIT_MCP_SRC` | `../LocalSnagitMCPServer/src` | Optional Snagit MCP server checkout, used only by `tests/smoke_recording.py` |
| `SANDBOX_MCP_MEMORY_MB` | `8192` | Guest RAM |
| `SANDBOX_MCP_NETWORKING` | `1` | `<Networking>` — see [Security](#security) |
| `SANDBOX_MCP_VGPU` | `1` | `<vGPU>` |
| `SANDBOX_MCP_WINDOW_SIZE` | `1600x900` | Client area the guest desktop is rendered into |
| `SANDBOX_MCP_BOOT_TIMEOUT_SECONDS` | `120` | How long `launch_sandbox` waits for a window |
| `SANDBOX_MCP_MAX_WAIT_SECONDS` | `600` | Upper bound for `beat` |
| `SANDBOX_MCP_LIVE_OVERLAY` | `0` | Draw the agenda in the guest. Off by default — it is composited in post |
| `AZURE_SPEECH_RESOURCE_ID` | — | Speech resource ARM id; selects Entra auth |
| `AZURE_SPEECH_REGION` | — | Speech region, e.g. `westus` |
| `AZURE_SPEECH_ENDPOINT` | — | Custom or private endpoint, used instead of the region |
| `AZURE_SPEECH_KEY` | — | Key auth fallback; prefer Entra so no secret is on disk |
| `AZURE_SPEECH_VOICE` | `en-US-ChristopherNeural` | Narrator. HD voices cannot be refitted — see below |
| `AZURE_SPEECH_STYLE` | — | Optional `mstts:express-as` style |
| `AZURE_SPEECH_RATE` | — | Optional baseline `<prosody rate>` |
| `AZURE_SPEECH_FORMAT` | `riff-24khz-16bit-mono-pcm` | Synthesis output format |
| `AZURE_SPEECH_CACHE_DIR` | `<work_dir>\narration\cache` | Content-addressed cue cache |

Copy [.env.example](.env.example) to `.env` rather than exporting these by hand. The real
environment always wins, so an exported variable still overrides the file. `.env` is gitignored.

Server settings go in `.env`; storyboard content goes in `storyboards/values.json`. Keep them
separate — anything in `values.json` is interpolated into storyboard text, so it can end up spoken
aloud or typed into the guest.

Narration needs two things that are easy to miss: the Speech resource must have a **custom
subdomain** (a plain regional endpoint only accepts keys), and the caller needs the **Cognitive
Services Speech User** role on it. Subscription Owner is not enough — Owner grants no data-plane
actions, so Entra calls fail with a 401 that looks like a configuration error.

## Tools

| Tool | What it does |
| --- | --- |
| `check_sandbox_setup` | Windows edition, feature state, virtualization, `wsb` CLI, running instances |
| `launch_sandbox` | Generate a `.wsb`, start the sandbox, pin the window to a fixed client size |
| `reattach_sandbox` | Adopt a sandbox that is still running, e.g. after restarting this server |
| `await_desktop` | Block until the guest agent and its console report ready |
| `sandbox_info` | Window handle, bounds, client origin and size, phase, mapped folders |
| `focus_sandbox` | Foreground the window, give the session view the keyboard, re-pin the size |
| `teardown_sandbox` | Close the sandbox and discard everything inside it |
| `send_keys` | One key chord, e.g. `win+r`, `ctrl+l`, `enter`, `ctrl+shift+escape` |
| `type_text` | Visibly typed, human-paced, for short strings on camera |
| `paste_text` | Clipboard + `ctrl+v` — use for anything long or exact |
| `click` / `move_mouse` / `scroll` | Guest-relative pointer input with visible cursor travel |
| `run_in_guest` | Run a command in the guest, on camera or silently, with a real exit code |
| `await_guest` | Block until a guest script publishes a marker |
| `guest_status` / `read_guest_log` | Guest phase, agent heartbeat, markers, console transcript |
| `read_guest_screen` | What the guest console shows **right now**, as text |
| `answer_guest` | Wait for a prompt to appear on screen, then answer it |
| `list_storyboards` / `run_storyboard` | Replay a declarative take, or validate one with `dry_run` |
| `show_agenda` | Put the run's agenda on the guest desktop, on camera, then hand over to the timeline bar |
| `beat` | Hold a beat so the last on-screen action is readable |
| `list_voices` | Browse the Azure Speech voice gallery |
| `synthesize_narration` | Speak a storyboard's `say:` lines and measure how long each really takes |
| `save_transcript` | Write `.vtt`, `.srt` and a readable voiceover script |
| `note_recording` / `export_timeline` | Record the camera's clock, then write `run.json` for the editor |

Every input tool re-verifies the foreground window before injecting, and raises rather than letting
keystrokes land on the host. `click` and `move_mouse` take **guest-relative** coordinates: `(0, 0)`
is the top left of the guest desktop.

`type_text` and `paste_text` refuse text matching a narrow set of credential patterns (assignments
to `password` / `client_secret` / `api_key`, JWTs, private key blocks, GitHub / OpenAI / Slack / AWS
token shapes). Pass `allow_secret: true` to override a false positive.

## The guest channel

`payload/` is mapped read-only to `C:\payload` and a per-session control folder is mapped
read-write to `C:\ctl`. `<LogonCommand>` runs `bootstrap.ps1`, which starts two things:

- a **hidden agent** (`lib/agent.ps1`) that polls `C:\ctl\inbox` and writes results to
  `C:\ctl\outbox` — this is what `run_in_guest(visible=false)` uses;
- a **visible console** (`lib/console.ps1`) which is the terminal seen on camera. Its `prompt`
  function publishes the exit code of every command, which is how `run_in_guest(visible=true)`
  returns a real result for something that was simply typed on screen.

Guest scripts call `mark <name>` to publish a sync point; the host blocks on it with `await_guest`.
Every file has exactly one writer and is published by rename, so the host never reads a half-written
document. Markers are separate files rather than a field in `status.json`, which keeps two writers
off the same file.

`payload/install-notepadpp.ps1` is a worked example: it resolves the latest release from the GitHub
API, downloads it with `curl`, installs it silently and marks `notepadpp_installed`.

A visible run also returns `input_clean`. If the guest transcript does not echo exactly the command
that was sent, something else typed into the session and the take is spoilt — worth checking before
keeping a recording.

### Seeing the screen

`read_guest_log` reads the transcript, which **only flushes when a command finishes**. That is no
help while a command is still running — a device code, a progress bar, an interactive prompt.
`read_guest_screen` reads the console's screen buffer instead, so it shows what is on screen right
now:

```
To sign in, use a web browser to open https://microsoft.com/devicelogin and enter the code ABCD-EFGH
Select a subscription and tenant (Type a number or Enter for no changes):
```

It works by attaching to the guest console from the hidden agent and calling
`ReadConsoleOutputCharacter`, reading up to the cursor so scrolled-off output is included. This is
also what makes keyboard driving safe: you can confirm what a prompt is asking before answering it,
rather than injecting a keystroke blind.

## Storyboards

A take belongs in a storyboard so it is repeatable and its pacing is tunable without touching code.
Each step is a single-key mapping, and **every step gets an implicit trailing beat** (0.6s by
default) because instant automation is unwatchable.

```yaml
name: notepadpp-from-clean-windows
defaults:
  beat: 0.6
steps:
  - await_desktop: 240
  - beat: { seconds: 2.5, reason: "let the clean desktop land" }
  - focus_sandbox:
  - run_in_guest:
      command: '& C:\payload\install-notepadpp.ps1'
      visible: true
      timeout: 420
  - await_guest: notepadpp_installed
  - type_text: { text: "Installed from a clean Windows Sandbox.", cps: 11 }
```

Scalar shorthand is expanded, so `send_keys: win` means `send_keys: {chord: win}`. Actions are
`await_desktop`, `await_guest`, `await_operator`, `answer_guest`, `beat`, `focus_sandbox`,
`include`, `show_agenda`, `send_keys`, `type_text`, `paste_text`, `click`, `move_mouse`, `scroll`
and `run_in_guest`.

### Before the camera rolls

Some of what a take assumes is not worth watching. A `setup:` list holds those steps, in the same
syntax, and runs as its own phase before the recording starts:

```yaml
setup:
  - run_in_guest:
      command: '& C:\payload\install-edge.ps1'
      visible: false
      timeout: 1200
steps:
  - focus_sandbox:
```

```
run_storyboard(name, phase: "setup")     # prepare the machine, off camera
start_recording(...)
run_storyboard(name)                     # the take itself
```

Setup is numbered separately from `steps`, publishes no agenda or timeline, and rejects `say:` /
`say_after:` — narration written there would never be heard. An included storyboard contributes its
setup to the parent's, ahead of the parent's own, so a composed run still prepares in dependency
order.

The SimpleChat deployment storyboards use it to install Microsoft Edge, because the closing shot
opens the deployed site and the guest image has no browser of its own.

### Agenda and timeline

A walkthrough that just starts doing things is hard to follow, so any step can carry `chapter:` and
`task:` labels. They build an agenda the guest shows **inside the recording** — a borderless card
with no title bar or taskbar, held for a few seconds by `show_agenda:`, and then a bar docked along
the bottom of the guest desktop for the rest of the run.

```yaml
agenda:
  title: Deploying to Azure from a clean Windows machine
  subtitle: Windows Sandbox - nothing installed, nothing left behind
steps:
  - await_desktop: { timeout: 300, chapter: Start from nothing }
  - focus_sandbox:
  - show_agenda: 11
  - run_in_guest: { command: '& C:\payload\check-tools.ps1', task: Prove it is empty }
  - run_in_guest:
      chapter: Install the toolchain
      task: Fetch and install
      command: '& C:\payload\install-prereqs.ps1'
```

The bar gives the chapter in flight most of its width and breaks it into its tasks; the rest
compress to the right so the whole plan stays on screen. Finished work goes grey and the chapter
running now fills green, so attention lands on what is happening rather than on what already did.
Nothing has to stop to tick a box: position is published as each step begins, and a step that has
no labels simply extends the one it runs inside.

Because `include:` is expanded before any of this is read, an included storyboard folds its
chapters into the parent's agenda instead of starting a second one — a run stitched together from
three storyboards still states one plan up front. If the result would be too long to read at a
glance the card drops the task lists and shows chapters only; the bar still carries the tasks for
the chapter that is actually running.

The console is framed above the bar whether or not a storyboard is driving it, so the terminal
never jumps when an agenda appears. To design the overlay without spending a sandbox cycle:

```powershell
.venv\Scripts\python.exe tests\check_overlay.py --shots recordings\overlay
```

### Storyboard values

Keep machine-, account- and deployment-specific inputs out of YAML by writing dotted placeholders
such as `{{azure.subscription_id}}`. Values come from `storyboards/values.json`:

```powershell
Copy-Item .\storyboards\values.example.json .\storyboards\values.json
```

Fill in the copy before replaying either SimpleChat deployment storyboard. The local file is ignored
by Git; [values.example.json](storyboards/values.example.json) is the public schema and set of safe
example choices. A fresh clone uses the example for `list_storyboards`, dry runs and teleprompter
generation, but an actual replay is refused until `values.json` exists.

Placeholders can appear anywhere in a storyboard value, including commands and narration. An exact
placeholder preserves its JSON type; a placeholder embedded in text is converted to text, with
booleans rendered as lowercase `true` or `false`. Included storyboards inherit the root storyboard's
values, so one companion file configures the whole composed run.

Treat the companion file as configuration, not a credential store. Resolved values can appear in a
dry-run result, command report, guest console or recording.

### Pausing for a human

Some scenarios cannot be fully automated — an interactive sign-in, a licence prompt, a hardware
key. `await_operator` handles those without giving up on repeatability:

```yaml
  - run_in_guest:
      command: 'az login --use-device-code'
      visible: true
      await_exit: false      # the command waits on a person, not on the guest
      timeout: 60
  - beat: { seconds: 10.0, reason: "let the device code render" }
  - await_operator:
      message: "Enter the device code at https://microsoft.com/devicelogin, then resume."
      log_tail: 30
```

Replay **suspends** rather than blocking, and returns everything needed to carry on:

```json
{ "suspended": true, "at_step": 14, "message": "Enter the device code ...",
  "guest_log_tail": ["...To sign in, use a web browser ... enter the code ABCD-EFGH..."],
  "resume_with": { "name": "simplechat-azd-deploy", "from_step": 15 } }
```

`guest_log_tail` carries the guest's screen buffer, so the device code comes back in the tool result
instead of having to be read off the screen. Do the out-of-band step, then call `run_storyboard`
again with that `from_step`. A failed step reports `resume_with` too, so a storyboard can be picked
up after a fix rather than restarted from scratch.

`to_step` stops a replay early on purpose, which is how you run a storyboard in segments — for
instance to pause a recording through a long silent install and resume it for the payoff:

```
run_storyboard(name, to_step: 5)                    # clean-machine reveal, on camera
pause_recording()
run_storyboard(name, from_step: 6, to_step: 8)      # 11 minutes of installers, off camera
resume_recording()
run_storyboard(name, from_step: 9)                  # carry on
```

### Composing storyboards

`include` splices another storyboard's steps in, so the parts worth reusing only get written once:

```yaml
steps:
  - include: prereqs-from-clean-windows
  - include: simplechat-clone-and-signin
  - run_in_guest: { command: 'azd up --no-prompt', visible: true, timeout: 4200 }
```

Includes are expanded when the storyboard is loaded, not while it runs, so `dry_run` shows one flat
list and `from_step` / `to_step` number straight through the seams. Circular includes are rejected
by name.

That makes the pieces independently useful: record the prerequisite install once, run a deployment
against an already-prepared sandbox, or stitch a whole take together — same files either way.

| Storyboard | What it does |
| --- | --- |
| [prereqs-from-clean-windows](storyboards/prereqs-from-clean-windows.yaml) | Part one: bare sandbox to a full toolchain, with a before/after check |
| [simplechat-clone-and-signin](storyboards/simplechat-clone-and-signin.yaml) | Part two: clone the repo and complete both device-code sign-ins |
| [simplechat-azd-deploy](storyboards/simplechat-azd-deploy.yaml) | Part three: the above two, then `azd up` with every choice seeded |
| [simplechat-azd-deploy-prompted](storyboards/simplechat-azd-deploy-prompted.yaml) | Part three, the talkative version: answers azd's prompts on camera and explains each one |
| [example-notepadpp](storyboards/example-notepadpp.yaml) | Clean Windows to a working app in ~95s, fully unattended |

### Answering interactive prompts

`answer_guest` waits for a prompt to appear on the live screen, then answers it:

```yaml
  - answer_guest:
      expect: "deployVideoIndexerService"
      send: "false"
      say: >-
        Video Indexer we're leaving off - it's the heaviest optional service here.
```

`expect` is matched against `read_guest_screen`, not the transcript, which is the only reason it can
see a prompt at all — a blocked command flushes nothing. Use `keys` instead of `send` for pickers
that need arrow keys. When a pattern never matches, the failure prints the last lines of the screen
so the real wording is visible.

### Reading the narration aloud

The `say:` fields are the only copy of the script. To record a voiceover without hunting through
YAML, build a teleprompter from them:

```
python -m sandbox_mcp.prompter --out prompter.html
python -m sandbox_mcp.prompter --serve        # localhost, needed for microphone capture
```

One self-contained HTML file with every storyboard embedded — no server needed to read it, no CDN,
opens by double-click. It shows what is happening on screen underneath each line, and what fills
the gap before the next one (`⏸ then: the command runs · hold 4.0s`).

**Auto-pace** is the default: rather than a constant scroll rate, each cue is anchored to the
moment it is due, from its own estimated reading time, and the scroll interpolates between
anchors. A cue's height and its spoken length are not proportional, so a fixed pixel rate drifts;
this does not. Untick it for manual speed.

**Paged mode** shows one cue at a time for recording take by take — <kbd>space</kbd> or
<kbd>→</kbd> for the next line, <kbd>←</kbd> to go back. Advancing marks the cue's time
automatically.

**Record** captures narration through the microphone. The count-in finishes, then the recorder,
the clock and the scroll all start on the same tick, so cue times are audio timestamps. Stopping
downloads the `.webm` and a matching `.vtt`. Microphone access needs a secure context, so use
`--serve` (bound to loopback) if the browser refuses on `file://`.

| Control | |
| --- | --- |
| <kbd>space</kbd> | play/pause, or next cue in paged mode |
| <kbd>→</kbd> / <kbd>←</kbd> | next / previous cue, paged mode |
| <kbd>↑</kbd> / <kbd>↓</kbd> | scroll speed, when auto-pace is off |
| <kbd>t</kbd> | tap as you start a line |
| <kbd>m</kbd> | mirror, for a beam-splitter rig |
| <kbd>r</kbd> | restart |

Exported cues come from your taps when there are any, and from the auto-pace schedule otherwise.
That is the opposite of `save_transcript`, which times cues against the sandbox run — right for a
screen recording, wrong for narration recorded separately.

### Writing a new scenario

Put the repeatable work in `payload/*.ps1` and have it publish `mark <name>` at each milestone, then
keep the storyboard to beats and synchronisation. That split is what makes a storyboard readable and
lets `await_guest` replace guesswork. Validate with `run_storyboard(dry_run: true)` before recording.

### Narration

Any step can carry `say` (spoken as the step begins) and `say_after` (once its result is on screen):

```yaml
  - run_in_guest:
      command: '& C:\payload\install-prereqs.ps1'
      visible: true
      say: >-
        The sandbox has no Store, no winget and no browser, so everything here is
        fetched directly and installed silently. This takes about ten minutes.
      say_after: >-
        There's the whole toolchain, on a machine that had none of it a moment ago.
```

Narration is authored beside the action it explains, but **timed from the run that actually
happened** — a step that takes eight minutes on one machine takes twelve on another, so estimating
up front would drift. `save_transcript` writes `.vtt` and `.srt` subtitles plus a readable `.md`
voiceover script, with cues shifted by `offset_seconds` if the recording started before the replay.
Segments accumulate, so a storyboard run in parts still produces one continuous transcript.

Splitting `say` from `say_after` is what keeps long steps from feeling narrated over: talk at the
start, stay quiet while the installer works, then talk about the result.

Two shipped examples:

| Storyboard | What it does |
| --- | --- |
| [example-notepadpp.yaml](storyboards/example-notepadpp.yaml) | Clean Windows to a working app in ~95s. Fully unattended. |
| [simplechat-azd-deploy-prompted.yaml](storyboards/simplechat-azd-deploy-prompted.yaml) | Deploys [microsoft/simplechat](https://github.com/microsoft/simplechat) to Azure with `azd up`, talking through every choice it asks for, then opens the running site. |

Validation is strict and names the offending step, so `run_storyboard(dry_run=true)` catches typos
before a take rather than halfway through one:

```
step 1 (type_text): unknown argument(s) ['speed']; allowed: ['text', 'cps', 'allow_secret']
```

Replay stops at the first failure and reports which step broke. The guest image has no browser, so
anything a take needs — including Edge for a closing shot — is fetched by its own `setup:` phase
first.

## Recording a demo

This repo is the driver; the [Snagit MCP server](https://github.com/paullizer/LocalSnagitMCPServer)
is the camera. An agent with both servers connected drives them together, and
[tests/smoke_recording.py](tests/smoke_recording.py) is the same thing as a runnable harness.

Order matters:

1. `launch_sandbox`, `await_desktop` and `focus_sandbox` all happen **before** the camera rolls.
   Boot is dead air, and `focus_sandbox` re-pins the window — doing that mid-take would move the
   shot.
2. `run_storyboard(phase: "setup")`, if the storyboard has a `setup:` list. Installs the take
   assumes already happened belong here, not in the recording.
3. `start_recording` with `target: "window"` and the handle from `sandbox_info`.
4. `run_storyboard`.
5. `stop_recording`.

**Record the window, not the display.** The sandbox client is pinned to 1600x900, so window capture
frames it exactly and keeps your host desktop, taskbar and notifications out of the video. A
fullscreen recording would put all of it on camera, and the whole point of the sandbox is that the
guest is the only thing worth showing.

The frame rate follows Snagit's own capture settings — neither MCP server sets it. If a recording
looks choppy, change it in Snagit rather than looking for a knob here.

Recordings land in `recordings/` (git-ignored).

## Generated configuration

```xml
<Configuration>
  <MemoryInMB>8192</MemoryInMB>
  <vGPU>Enable</vGPU>
  <Networking>Enable</Networking>
  <ClipboardRedirection>Enable</ClipboardRedirection>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>...\payload</HostFolder>
      <SandboxFolder>C:\payload</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
    <MappedFolder>
      <HostFolder>...\sessions\sbx-TIMESTAMP\control</HostFolder>
      <SandboxFolder>C:\ctl</SandboxFolder>
      <ReadOnly>false</ReadOnly>
    </MappedFolder>
  </MappedFolders>
</Configuration>
```

It is built with `ElementTree`, not string formatting, so host paths cannot inject markup. The
`<LogonCommand>` element is only emitted once `payload/bootstrap.ps1` exists (phase 3).

## Host-specific findings

Established by experiment on **Windows 11 Enterprise 25H2, build 26200.9106**:

- The sandbox window belongs to **`WindowsSandboxRemoteSession.exe`**, not the legacy
  `WindowsSandboxClient.exe`. Its class is `WinUIDesktopWin32WindowClass`, which is generic to every
  WinUI 3 app, so the **process name is the discriminator** — the class name is not.
  Both image names are searched so older builds still work.
- **Foreground is not enough for input.** After `SetForegroundWindow` the keyboard focus sits on the
  WinUI shell's `InputSiteWindowClass` and every injected keystroke is swallowed. Keys only reach
  the guest once the RDP view — class **`IHWindowClass`**, titled `Input Capture Window` — holds
  focus. The server sets that with `AttachThreadInput` + `SetFocus`, falling back to a click in the
  centre of the session view.
- **The window resizes itself once the guest session connects**, so the size pinned at launch does
  not stick. `focus_sandbox` re-applies it, which is why it should be called after the guest boots
  and before a take.
- **`<LogonCommand>` cannot be the visible terminal.** Its PowerShell has no interactive stdin, so
  it renders exactly one prompt, hits EOF and exits even with `-NoExit`. The symptom is a
  `prompt_index` stuck at 1 and a console PID that no longer exists. `bootstrap.ps1` therefore
  starts a separate console with `Start-Process` and treats itself as short-lived.
- **Starting child processes during logon is racy.** `Start-Process -WindowStyle Hidden` returned a
  PID whose process never ran the script, with no output at all. Launching through
  `ProcessStartInfo` with `UseShellExecute = false` and retrying until the heartbeat appears is
  reliable; bootstrap verifies its own agent rather than trusting the launch.
- `IsProcessorFeaturePresent` reports **`slat` and `virtualization_firmware_enabled` as false**
  whenever a hypervisor already owns those bits. They are only meaningful when
  `hypervisor_running` is false, and `check_sandbox_setup` only treats them as blockers then.
- `WM_CLOSE` tore the sandbox down cleanly with no confirmation dialog; `teardown_sandbox` still
  force-terminates whatever survives the grace period.
- With the pre-warmed VM (`vmmemCmZygote`) the window appeared in ~3s. Cold start is much slower —
  the 120s default timeout is for that case.

## What the guest image actually contains

The sandbox image is minimal, which matters when planning a storyboard:

| Present | Missing |
| --- | --- |
| `powershell.exe`, `cmd.exe`, `explorer.exe`, `curl.exe`, `regedit.exe`, `charmap.exe` | `notepad.exe`, `wordpad.exe`, `write.exe`, `mspaint.exe`, `msedge.exe`, `winget.exe` |

There is no Microsoft Store and no browser, so a demo has to fetch its own tooling with `curl` or an
install script. This is also why the phase 2 acceptance check opens PowerShell rather than the
Notepad the plan assumed. A take that genuinely needs a browser installs one:
[payload/install-edge.ps1](payload/install-edge.ps1) fetches the enterprise MSI, verifies its
published hash and signature, and turns off the first-run tour — as `setup:`, before recording.

The run box (`win+r`) is the most reliable way in — start-menu search depends on components the
image may not have. Quoted `-Command` strings survive the run box badly, so put real work in a
`.ps1` under the mapped folder and launch it with `-File`.

## Security

- **The read-write mapped folder is a real host-write path.** Each session gets its own empty
  `control/` folder under the work directory and nothing else is mapped writable.
- `payload_dir` is mapped **read-only**, and mapping a drive root is refused.
- `<Networking>Enable</Networking>` exposes the guest to your LAN, not just the internet. For
  untrusted repos, pre-stage dependencies and launch with `networking: false`.
- `focus_sandbox` **raises** instead of returning false when it cannot verify the foreground
  window. Injecting input while the host is focused is both a broken take and a real hazard.
- Screen recording captures whatever is on screen. Never put a secret in the guest while
  recording — use `az login --use-device-code`.

## Verifying

```powershell
.\.venv\Scripts\python.exe tests\check_config.py        # paths and first-run errors
.\.venv\Scripts\python.exe tests\check_storyboard.py    # parser only, no sandbox
.\.venv\Scripts\python.exe tests\check_prompter.py      # generated prompter, no sandbox
.\.venv\Scripts\python.exe tests\smoke_sandbox.py       # phase 1: lifecycle
.\.venv\Scripts\python.exe tests\smoke_input.py         # phase 2: input
.\.venv\Scripts\python.exe tests\smoke_guest.py         # phase 3: guest channel
.\.venv\Scripts\python.exe tests\smoke_storyboard.py    # phase 4: full replay
.\.venv\Scripts\python.exe tests\smoke_recording.py     # phase 5: recorded video
```

The three `check_*.py` commands above are host-only, fast, and run in CI on every supported Python
version. The smoke tests drive the MCP server in-process against a real sandbox: `smoke_sandbox`
launches, finds the window, verifies focus and tears down;
`smoke_input` opens PowerShell in the guest by keyboard alone and has the guest write back what
`type_text` and `paste_text` delivered so the host can compare byte for byte; `smoke_guest` checks a
non-zero exit code really comes from the guest, installs Notepad++ off camera and waits on the
resulting marker; `smoke_storyboard` replays the whole example take; `smoke_recording` records that
replay to an MP4 and checks Snagit reported no recorder error.

They put a sandbox window on screen and drive it with synthetic input. **Do not use the keyboard or
mouse while they run** — stray keystrokes land in the guest and the storyboard run will fail with
`input_clean: false`. For `smoke_recording`, turn on Do Not Disturb first: it records real pixels.

## Contributing, security and license

See [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a change. Report vulnerabilities privately
as described in [SECURITY.md](SECURITY.md). This project is licensed under the [MIT License](LICENSE).
