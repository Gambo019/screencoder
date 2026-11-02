# ScreenCoder

A desktop app (Python + Qt) that solves coding tasks from **screenshots of your IDE**, whether that's VS Code, JetBrains or a browser-based IDE. It uses the **Claude Code CLI**:

- **Coding agent:** Claude Opus 4.8 at `xhigh` (extra-high) effort reads your screenshots, files and text. It explains its understanding and plan, then writes the code.
- **Quick answers:** while the agent works, questions heard from your PC's audio are answered within seconds by a separate session (Claude Opus 4.8 at `medium` effort) without interrupting the agent. You can also steer the agent directly.

Both models can be changed in **Settings**. Sonnet 4.6 and Haiku 4.5 are lighter options for quick answers.

## Requirements

- Windows 10/11 (macOS/Linux work too, without global hotkeys)
- Python 3.10+
- Claude Code, logged in. The app finds `claude` on PATH, in `~/.local/bin`, or the copy bundled with the VS Code extension. You can also set the path in **Settings**.

## Run

Double-click `run.bat`. The first run creates `.venv` and installs PySide6. You can also start it by hand:

```bat
.venv\Scripts\python -m screencoder
```

ScreenCoder runs as a single window: launching it again (double-clicking `run.bat` once more) brings the window you already have to the front instead of opening a second copy.

## Using it

The window has the question-and-answer panel on the left and the code side on the right (plan, coding activity, files). Drag the divider between them to give either side more room; the position is remembered. The window can be as narrow as about 800 px, and the button rows wrap when a panel gets narrow.

1. **Add the task** in the box at the top of the code side. Any mix of these works:
   - **Text:** a description of the problem, or "fix the failing test".
   - **Screenshots:**
     - **Ctrl+V** pastes from the clipboard (Win+Shift+S, PrintScreen, a copied image).
     - **✂ Snip** lets you drag a region on any monitor.
     - **🖥 Screen** grabs a whole monitor. With more than one monitor it asks which.
     - The global hotkeys **Ctrl+Alt+Shift+S** (region) and **Ctrl+Alt+Shift+F** (the monitor under the mouse) work while your IDE has focus, so you can scroll through a file and capture page after page.
   - **Files:** **📎 Files**, pasting files copied in Explorer, or drag & drop.

   Overlapping or repeated screenshots are fine; the agent merges them using line numbers and tabs. Identical screenshots are skipped automatically. Near-identical ones are flagged with `~#n` so you can remove them.

   Pick the **Language** for the solution. Python is the default.
   - **Other** follows the language of the problem.
   - **Vibe-Coding** means the developer writes no code himself: he directs your own coding agent. See [Directing an external coding agent](#directing-an-external-coding-agent-language-vibe-coding).
2. Press **Analyze & Solve** (Ctrl+Enter). The task box disappears; **＋ New task** in the toolbar starts over for the next problem.
   - **＋ New task** asks for confirmation first. On "Yes" it stops the current task if one is running and clears everything on screen: the plan, the prompts, the activity log, the files view, and the whole Q&A panel (transcript, attachments and chat).
   - It also empties the **Workspace** path, so set it again for the next task.
   - Nothing on disk is deleted: the files in the old workspace and the run folder under `~/ScreenCoder/runs/` stay.
   - **Understanding & plan:** the agent explains the problem and how it will solve it, briefly and in a spoken style.
     - It talks as if it is reading the code directly and never mentions screenshots.
     - An "Assumptions / unclear parts" section appears only when something really is unclear.
     - The view stays on this tab while the agent codes.
     - Tick *"Let me review the plan before coding starts"* to approve, add feedback, or ask for a revised plan first.
   - **Coding activity:** live progress, including files written, commands run and errors. Code the agent writes or changes is commented in a conversational first-person voice, like a developer thinking aloud step by step ("My plan here is simple…", "Careful: an empty input would…"). Code that already existed is left as it was.
   - **Verification:** before it finishes, the agent has to check its work.
     - It must syntax-check its files and run the problem's examples.
     - It must call every public function and method of the original code directly, not only through its own entry point.
     - If commands are disabled, it does these checks by reading and tracing, and says the code was not executed.
     - When it finishes, the app parses the Python and JSON files it wrote. If one fails to parse, it is sent back to the agent to fix, up to twice.
   - **Output Code:** a VS Code-style explorer and a code viewer. **◀ Explorer** folds the explorer away to give the code the full width.
     - The viewer has line numbers and syntax colours for Python, JavaScript, TypeScript, C#, Java, C, C++, Go and many more.
     - **Copy code** copies the open file for pasting back into your IDE.
     - File names are coloured by status. Plain means an original file from the problem that the agent didn't touch. Amber with **M** means an original file the agent edited. Green with **U** means a new file the agent created.
     - Inside an open file, lines the agent added or edited are at normal brightness with a green bar in the gutter. Original lines it didn't change are dimmed.
3. Use the **Ask while it codes** panel while the agent works. There is no typed question here: the question is what was heard, plus any screenshots or files you attach.
   - **PC audio transcript** (top of the panel): everything your PC plays is transcribed live, on the CPU and offline. That covers calls, videos, anything coming out of your speakers or headphones, but not your microphone.
     - White lines are new speech, grey lines were already asked about, and blue is the sentence being spoken right now.
     - Lines with nothing to answer, such as "hmm", "uh-huh", "okay", "got it" or "thank you", are skipped automatically. The status line shows how many were skipped. A line is skipped only if every word is filler, so "Um, how does an LRU cache work?" is kept.
     - Turn it on or off with **🎧 PC audio**.
     - The speech model downloads once on first use: about 70 MB from Hugging Face. The download uses parallel connections and resumes automatically after interruptions.
     - It uses roughly 4% of one CPU core while speech is playing, and nothing when the PC is silent.
   - **Attachments** (under the transcript): Snip, Screen, Files and Paste add screenshots and files that go with the heard question. Pasted text, such as copied code or an error message, is attached as a small text file.
   - **Chat** (bottom of the panel): what was heard and the replies appear as separate chat bubbles, newest at the top.
     - Every reply comes from one **AI developer** speaking in the first person ("I sorted first because…"), whichever button you used.
     - While an answer is being generated and none of it is there yet, its bubble shows a spinner and the line "Next answer is coming. Please check the current codebase while you wait." Both go the moment the first words arrive. This applies to quick answers and to replies from the coding or developer session.
     - Replies are conversational: short spoken sentences, with no headings, lists or "here is the breakdown".
     - The person he talks to only watches. He never tells them to run, open or check anything themselves ("you can run it yourself with…"), never hands them a command "to reproduce it", and never says where files are on disk. What is run or shown is run or shown by him: "I ran it with `python -m unittest` and all three pass", or "I'll run them for you now so you can see the output". This holds in every mode.
     - If what was heard is not a question (a cut-off sentence, small talk, a remark), there is no reply. The heard bubble is marked "not a question - no answer needed". If the rest of the sentence arrives with your next Ask, the two parts are answered together.
   - All three buttons send the same thing: the new speech plus the attachments. They differ in where it goes.
     - **⚡ Ask** is the one to use by default; it sorts out what it heard by itself.
       - A question gets an answer in the chat within seconds, from the quick model. The coding is not disturbed. The quick model has read-only access to the task, the plan, the progress and the code written so far.
       - A request to change the code ("can you also handle…", "rename…", "add a test…") is passed on to the coding session, as if you had clicked Send to agent. The heard bubble is marked "work request - passed to the coding session".
         - The coding session takes a while to reply, so the developer first says two short sentences straight away (about 20–30 words): one that puts what was asked in his own words, and one that buys the moment. For example: "Understood - you'd like mode to return None for an empty list rather than raise. Give me a moment to work out where that check belongs." It appears within a few seconds.
         - The full reply from the coding session then appears above that. The session is told what was already said, so it does not acknowledge twice.
       - Something that is not a question gets no reply.
     - **↪ Send to agent** goes to the coding session as a new instruction. It picks it up right after its current step, replies in the chat with what it will do, and adjusts the code. This needs a running task.
     - **✋ Interrupt & send** is the same as Send to agent, but stops the agent's current step first.
   - If someone is mid-sentence, that sentence is closed first so the whole question is included.
   - Select part of the transcript to send just that part.

The status bar shows the phase, elapsed time, the API-equivalent cost reported by Claude Code, and **Interrupt** / **Stop** buttons.

Every button is enabled only while what it does is possible:

| Button | Enabled when |
|---|---|
| **Analyze & Solve** | the task box holds text or an attachment |
| **⚡ Ask** | there is new speech, a selected part of the transcript, or an attachment in the left panel |
| **↪ Send to agent / developer** | the same, and a task's session is running |
| **✋ Interrupt & send** | the same, and that session is in the middle of a turn |
| **■ Stop answer** | a quick answer is being written |
| **Paste** | the clipboard holds an image, files or text |
| **Clear** | its box has text or attachments |
| **＋ New task** | there is a task or any history to clear |
| **Workspace** field and **Browse…** | no task has been started yet (the folder is fixed once it runs) |
| **Open folder** | a task has a workspace, or the typed folder exists |
| **↻ Revise plan** | the plan is waiting for approval and feedback has been typed |
| **↻ Refresh** / **📋 Copy code** (Output Code) | a task exists / a file is open |
| **✋ Interrupt** / **■ Stop** | the session is in the middle of a turn / is running |
| **✅ Agent finished** / **⏳ Agent still working** | a Vibe-Coding session is running and he has written a prompt |
| **✔ Sent to the agent** (on each prompt) | it is the newest prompt and has not been marked as sent yet |

## Directing an external coding agent (Language: Vibe-Coding)

With **Vibe-Coding** the developer in this app writes no code. A coding agent outside the app (Claude Code, Cursor, Copilot, anything) writes it, and the developer directs that agent. Three parties are involved:

- **The supervisor** is whoever is heard on the PC audio. They set the developer this task to assess how he works; he explains himself to them and answers their questions.
- **The external coding agent** only ever receives the prompts the developer writes.
- **You are the bridge.** You pass each prompt on to the coding agent, give the developer what came back, and run or edit things in the agent's workspace. The developer cannot write, edit or run anything; his session has read-only tools.

How the developer talks:

- He speaks to the supervisor directly as "you" ("You've asked me to build…") and never says "the supervisor".
- The task is his own assignment: he says "I", never "we".
- He calls the external coding agent "the agent".
- You do not exist in anything he says or writes. Passing a prompt on and running a command are his own actions to him: "I'm sending this to the agent", "I'll run the tests now".
- In every mode, coding or Vibe-Coding, he is briefed to sound like a professional engineer talking about his own work.

Setting up:

1. Put the folder your coding agent works in into **Workspace** (toolbar). The developer can read it at any time to see what the agent has made. Leave it empty if there is no folder to share; he then only knows what you show him.
2. Add the task, pick **Vibe-Coding**, press **Analyze & Solve**.

Where things appear:

- **Understanding & questions** (first tab): his first turn only, which is his understanding of the task and the questions he wants answered.
- **The chat on the left:** everything he says after that, newest at the top. Small bold labels mark the parts: *Reading the code*, *What I caught*, *Plan*, *Running next*, and - in a bubble of their own, once you have marked the prompt as sent - *As I prompt the agent* and *While it works*.
- **Prompts** (second tab): what he writes for the agent, and the commands he wants run. Nothing here is said out loud. The newest is at the top, and a prompt shows up while he is still writing it.
  - Parts of a prompt that already exist, character for character, in a file of the agent's workspace are highlighted in green, followed by the file and line (`⟨SPEC.md:12⟩`, `⟨src/reconciler.py:11-12⟩`). Copy those from the file in your agent's editor instead of typing them.
  - Only stretches worth the trip are marked: code-like text from about 16 characters, plain prose from about 28.
  - He is briefed to keep prompts short, to point at files instead of restating them, and to quote existing text exactly, so that as much as possible can be copied.
  - There is no copy button; the tab is for reading and typing off. Each prompt is still saved under `~/ScreenCoder/runs/<id>/prompts/`.

What happens, turn by turn:

1. **He clarifies first.** His first turn is his understanding and the questions he wants answered (inputs, edge cases, constraints, what "done" means). No plan and no prompt yet.
2. **The supervisor answers** on the PC audio; click **⚡ Ask**. Ask recognises that this moves the work and passes it to the developer. He says two short sentences straight away (what he took from it, and that he needs a moment), so there is no silence while his working session thinks. Then, in the chat, he gives the full reply, states his **plan** and says why he is asking the agent for the first step; the **first prompt** appears on the Prompts tab.
   - If no answer comes, click **▶ Go ahead** under the tabs: he says which assumptions he is going with and carries on.
3. **Type the prompt into your agent, then click its "✔ Sent to the agent" button** on the Prompts tab.
   - Until you click, the chat holds only what he says before sending (his reply, the plan, the code review).
   - On the click a new bubble appears with **As I prompt the agent** (why he sent it and what he expects back) and **While it works** (a trade-off, what he will check next, what he would do differently in production). That keeps what he says in the order things happen.
   - That text is already written, but it flows in like a reply being streamed: the first words are there at once, and the rest follows a word at a time, at roughly the pace of a model's stream.
   - Only the newest prompt can be marked as sent; the button then shows the time. If you click it while he is still writing the turn, those parts appear as he writes them.
   - If you skip the click and report on the agent with **Agent finished** or **Agent still working**, the prompt is marked as sent and the held-back parts are dropped, since their moment has passed.
4. **Show him how the agent is doing**, in the status field under the tabs.
   - Add screenshots of the agent's latest activity: its chat, its action history, a terminal. The field has its own **Snip**, **Screen**, **Files** and **Paste** buttons, and **Ctrl+V** pastes into it. While a Vibe-Coding task runs, the global capture hotkeys also put their screenshots here. He is told that these screenshots are the agent's latest activity, so nothing has to be explained. A line of text ("pytest: 3 passed, 1 failed") is optional.
   - Then say where the agent stands, with one of two buttons. Both work with only a screenshot, only text, or nothing added at all.
     - **✅ Agent finished:** the agent has completed its turn for his latest prompt. He reads what it wrote in the workspace, checks it against what its activity says it did, and reviews it in the left panel.
     - **⏳ Agent still working:** the agent has not finished. He takes the screenshots in and looks at the workspace, so that he knows how far it is. He speaks in the left panel only if that already gives him something worth saying; otherwise he stays quiet, and the status is marked "taken in - nothing to say yet" in the chat.
   - Before he has written his first prompt there is no agent at work yet. **Agent finished** is then disabled, and the second button reads **▶ Go ahead**: he proceeds on his own assumptions (see step 2).
   - He takes all of this as plain fact, not as something said to him, and he is also told which workspace files are new or changed since he last looked.
   - Screenshots attached in the left panel still go to him with **↪ Send to developer** (or **⚡ Ask** without speech), as before.
5. **He reads the code and pushes back.** In the chat his next turn can contain:
   - **Reading the code:** every new or changed line, as line number + code + his remark.
   - **What I caught:** over-engineering, a missed edge case, an invented API, an ignored constraint.
   - **As I prompt the agent:** why he is sending the next step or the fix. The prompt itself is on the Prompts tab.
   - **Running next:** what he says as he "runs" something. The exact command or manual edit is on the Prompts tab as a **Run** entry; carry it out in the agent's workspace and give him the output.
6. Repeat 3–5. If the agent fails twice on the same thing he stops re-prompting and debugs it himself: he reads the error and the code, and lists the one experiment that would confirm his hypothesis as a **Run** entry.

Questions from the supervisor at any time ("why does this work?", "what's the complexity?", "can you wrap up?"): click **⚡ Ask**. The quick model answers in seconds as the same developer. It knows his plan, his prompts and his reviews, and can read the workspace. A wrap-up covers what was built, what was verified and how, and what he would improve with more time. Answers given this way are passed to the developer's working session with his next message, so he stays consistent with them.

The **Output Code** tab shows the agent's workspace and refreshes by itself every few seconds: green **U** for files the agent created since the task started, amber **M** for files it changed, with the changed lines bright.

Each developer turn costs one round of the coding model. If the turns feel slow, lower the coding effort in **Settings** for this kind of task.

## Workspace and permissions

- **Workspace** (toolbar): leave it empty to get a fresh folder per task under `~/ScreenCoder/runs/<id>/workspace`. If the code is on your PC, point it at your project folder instead.
- **Your project folder is never changed** (coding languages). When you give a folder:
  - The developer gets his own copy of it at `~/ScreenCoder/runs/<id>/workspace` and edits and runs only there. Cache folders (`__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`) are not copied. For a very large folder (over 20,000 files or 400 MB) the app asks before copying.
  - His session is started in the copy and is not told where your folder is. It is also instructed to stay inside its working directory.
  - **You bring his changes over yourself.** The Output Code tab shows the copy: amber **M** for files he edited, green **U** for new ones, changed lines bright. **Open folder** opens the copy.
  - **He speaks as the owner of your folder and never about his own copy or its state**, because your folder is the only thing the person asking can see.
    - The quick-answer side is not told that a copy exists. For it there is one project. With every question the app compares the files and tells it which changes it "is making right now" (not in your folder yet, with the exact difference) and which it "has made" (in your folder).
    - While any change is missing from your folder, he answers as someone in the middle of the work: "Yes, I'm working on it right now. The bug is on line 7… I'm changing that line to…", "Not yet. Line 7 still reads…; once I have made that change I'll run the tests." He does not say "done", "fixed" or "tests pass", and he does not say things like "the fix isn't in the file yet" either, since that would point to another place. His work log, which reads as finished work, is kept from him until everything is in your folder.
    - Once everything he changed is in your folder, he speaks of it as done ("Yes, it's solved… pytest reports 1 passed").
    - He re-reads the file for every question, so he follows your folder as you bring changes over.
    - His coding session gets the same information when it replies in the chat to a request you pass on.
    - When his session finishes, his own tab says nothing about it. The status bar and the Coding activity tab tell you that the changes are waiting in his copy.
  - A folder that does not exist is refused instead of being created.
- With **Vibe-Coding** there is no copy: the folder belongs to your external agent and the developer's session only has read access to it.
- Each run stores its inputs, plan, event log and session ID under `~/ScreenCoder/runs/<id>/`. You can continue a session in a terminal with `claude --resume <session-id>`.
- **Coding permissions** (Settings). The agent runs headless, so nobody can approve prompts:
  - `acceptEdits` (default): it can write files, but shell commands are denied, so it verifies by tracing the examples by hand. Edits outside its working directory are denied too.
  - `bypassPermissions`: it can also run your code and tests. Nothing technical stops a shell command from reaching outside the copy in this mode; the protection is that the session does not know your folder's path and is told to stay inside its own.
  - `auto`: Claude Code's classifier approves safe commands, if your plan supports it. The same caveat applies to shell commands.

## Privacy

- The live transcript is produced locally and is never saved or uploaded on its own.
- Speech is sent to Claude only when you click **Ask**, **Send to agent** or **Interrupt & send**. Only the speech since your last send goes out. Ask also includes up to about 1,500 characters of earlier transcript as context.
- If you transcribe calls, make sure the other people are OK with it. In some places, recording or transcribing a conversation requires everyone's consent.

## Remote control: see and use the app from another machine

The app keeps running on this machine; a browser on another machine shows the same window, live, and can do everything in it.

**Switch it on**

1. Settings → tick **Remote control**, and under **Reach it** choose where the other machine is:
   - **from the internet**: works from anywhere. Nothing to set up on the router or firewall, and it works behind a VPN.
   - **from this network only**: for a machine on the same Wi-Fi/LAN. An address like `192.168.x.x` does not exist on the internet.
2. Save. A few seconds later the status line of the app shows **🔗 Remote … on**. Click it to copy the link (Settings shows it too), and open the link in a browser on the other machine (a current Chrome, Edge, Firefox or Safari). Nothing is installed there.

*From the internet.* The app uses Cloudflare's free tunnel program, `cloudflared`. The first time, Settings asks before downloading it (55 MB, from Cloudflare's releases on GitHub, into `~/ScreenCoder/bin`); it is run only if Windows confirms Cloudflare's signature on it. The app itself then listens on this machine only, the tunnel connects out to Cloudflare, and you get a link like `https://some-random-words.trycloudflare.com/#k=…`. No account is needed; in exchange the link is new every time the app starts, so copy it again after a restart. The tunnel stops when the app does.

*From this network only.* The link looks like `http://192.168.1.20:8765/#k=…`. The first time, Windows asks whether Python may accept network connections: allow it. If the machine has several addresses, pick the one the other machine can reach in the list next to the port. If the status line says the port is already in use, pick another one there. (The internet link needs no port: it takes any free one.)

Closing the app's window ends the app, and remote control and the tunnel with it: nothing keeps running in the background.

**What you get on the other machine**

- The whole window as it is: the conversation, the tabs, the prompts, the file list and code, the status line, with the same texts, colours and text size. It follows the app within a few milliseconds; a streaming answer grows on the page as it grows in the app.
- Every button, tick box, list and tab works, and is enabled or greyed out exactly as in the app. Text fields can be typed into; `Enter` and `Ctrl+Enter` do what they do in the app.
- Every other window of the app appears on top of the page: Settings, yes/no questions, menus, the attachment preview, and the folder and file dialogs (for these the app uses its own dialogs instead of Windows' ones, because only those can be shown remotely).
- **Snip** shows a frozen screenshot of the app machine's screen on the page; drag a region on it with the mouse. The rectangle is drawn by the page itself, so it follows the pointer without delay, and nothing is sent while you drag: the finished drag goes to the app as one small message, the app answers, and the picker closes. With several monitors there is a **Monitor 1 / Monitor 2** switch at the top. **Enter** takes the whole monitor on show; **Esc**, a right-click or **Cancel** ends the picking. If the app's answer does not arrive, the page says "Capturing…" for a few seconds, then asks the app where things stand and follows that. **Screen** captures that machine's whole screen.
- Paste (`Ctrl+V`) or drop a picture or file on the page and it is attached to the input it was pasted into, or the one under the pointer. **Browse** and **Files** show the folders of the machine the app runs on.
- Scrolling is shared: scroll the quick answers, Understanding & plan, Coding activity, Prompts or Output Code on either side and the other shows the same text at the top of the view. (The two sides wrap text at different widths, so they agree on the text, not on a pixel offset. If one view is taller it simply shows more below.) The file list scrolls on its own.
- The open tab is shared while **Sync tab**, the tick box beside the Output Code tab, is on. Untick it and each side picks its own tab, and a tab that is not open in the app scrolls on its own; tick it again and every browser goes to the app's tab.
- The **Window** button at the bottom of the page opens a map of the app machine's monitors, to scale, with the app's window on it and its exact position and size, following the window as it moves. Drag the window on the map to move the real one, or its edges and corners to resize it. It stops at the edges of the monitors, so it cannot get lost. A maximized window comes off at its normal size; a minimized one stays down and comes back where you put it.
- Typed text and everything else are shared too: it is one app with two views. Several browsers can be connected at once.

**Why it is quick**

It is not a video of the screen. The app describes its widgets once and afterwards sends only what changed, over one permanent connection: for a streaming answer that is about 50 bytes per update, not the whole text. Nothing is sent while nothing changes, and nothing is watched at all while nobody is connected. The small badge at the bottom of the page shows the measured round trip.

Pictures are the one heavy thing, so they are kept rare. The region picker's screenshot goes over once (WebP, about 95 KB for a 1920×1080 monitor; another monitor's only once the first is showing). Any other widget that paints itself is sent as a picture, and the next picture waits until the page has confirmed that the last one is on screen: a slow connection gets fewer pictures, each of them current, never a queue of old ones.

On the same network the round trip is a few milliseconds. Over the internet it is whatever the path between the two machines takes: the app and the tunnel add next to nothing. If this machine is behind a VPN, the tunnel goes through the VPN too, and a slow or uneven VPN shows directly in the badge. Most VPN apps can exclude one program ("split tunneling"); excluding `~/ScreenCoder/bin/cloudflared.exe` lets the tunnel use the direct connection.

**Who can connect**

- Anyone who has the link can use the app, including starting tasks and changing Settings, so treat it like a password. **New key** in Settings makes a new link and, once saved, shuts out whoever has the old one.
- From the internet, the connection is encrypted on the way (https) and passes through Cloudflare's servers.
- From this network only, the connection is not encrypted: use it on a network you trust. Do not forward its port to the internet; use the internet choice instead.
- It is off by default and stays off until you switch it on.

**Limits**

- Sound is not sent; the transcript is.
- The page needs a wide screen: it is the same layout as the app, and scrolls if the browser window is smaller than the app needs.
- The folder dialog does not accept a path that runs through a hidden folder (Qt's rule). Type such a path into the Workspace field instead.

## Settings

Settings let you change:
- the reading text size for the plan, coding activity, transcript and chat (150% by default);
- the speech model: Kroko (recommended: lightest, adds punctuation) or NVIDIA FastConformer, and whether PC-audio listening is on;
- the coding model and effort, and the quick-answer model and effort (Haiku is supported; effort isn't sent for it);
- a name for the AI developer (the coding agent and quick answers), so the interviewer can address it by name;
- the CLI path;
- the runs folder;
- whether thinking summaries are shown;
- the global hotkeys;
- remote control: on or off, from the internet or from this network only, and its link (see above);
- **AI providers** (see below): OpenAI and Anthropic API keys, the OpenAI model list, and which back end coding and quick answers use.

They are saved in `~/ScreenCoder/settings.json`. The API keys are stored there in plain text, like any other setting; keep that file private.

## AI providers

Two parts of the app each pick a back end — **Claude Code (the CLI)**, the **OpenAI API** or the **Anthropic API**:

- **Coding** — the analyze → plan → code agent.
- **Quick answers** — the ⚡ Ask button.

In Settings, under **AI providers**, enter an OpenAI key and/or an Anthropic key (hidden as you type; **Test** makes one tiny call to check it), edit the **OpenAI models** list to taste, then choose a provider and model for each part. No extra Python package is needed — the API calls go over the app's own networking.

With **OpenAI** or **Anthropic**, that part runs as an agent over the API with the same file tools the Claude Code agent uses — Read, Write, Edit, Glob, Grep and LS — working in your workspace (a copy of it, for a coding task). Running commands and tests (**Bash**) needs the **bypassPermissions** setting, exactly as with Claude Code; otherwise shell commands are refused. Quick answers get read-only tools. How well a run goes depends on the model you choose; Claude Code remains the default and the most capable for coding.

## Layout

| File | Purpose |
|---|---|
| `screencoder/main_window.py` | UI and the analyze → plan → code flow, the Vibe-Coding flow (directing an external agent), quick answers and steering |
| `screencoder/claude_cli.py` | `claude -p` stream-json session: streaming, interrupts, message queue tracking |
| `screencoder/prompts.py` | Prompts for the coding agent, for the developer who directs an external agent, and for the quick-answer model |
| `screencoder/attachments.py`, `images.py` | Screenshot encoding within API limits, duplicate detection, message content |
| `screencoder/capture.py`, `hotkeys.py` | Snipping overlay, full-screen capture, Windows global hotkeys |
| `screencoder/listener.py` | PC-audio capture (WASAPI loopback) or microphone, and live streaming speech-to-text (sherpa-onnx) |
| `screencoder/widgets.py` | Composer (paste/drop/attachments) and the live Markdown view |
| `screencoder/file_tree.py`, `code_view.py` | Explorer tree with status colours; code viewer with line numbers and syntax colours (Pygments) |
| `screencoder/chat_view.py` | Chat bubbles for the answers panel |
| `screencoder/llm.py` | Provider helpers (endpoints, keys, model menus) and a one-shot streaming client for the Settings key-test, over Qt networking (no extra package) |
| `screencoder/api_agent.py` | An agentic coding/quick-answer session over the OpenAI or Anthropic API, shaped as a drop-in for `ClaudeSession` (same signals and tool-block shapes), running the file/Bash tools locally |
| `screencoder/speech_filter.py` | Skips filler lines ("hmm", "okay"…) in the transcript |
| `screencoder/remote.py`, `remote_web/` | Remote control: mirrors the app's widgets to a browser page over one WebSocket and applies what is done there. It knows nothing about the app's features, so new ones show up remotely without changes here |
| `screencoder/tunnel.py` | The internet link for remote control: downloads (once, after asking), checks and runs Cloudflare's `cloudflared`, and reports the public address |
