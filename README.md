# ScreenCoder

A desktop app (Python + Qt) that solves coding tasks from **screenshots of your IDE**, whether that's VS Code, JetBrains or a browser-based IDE. It uses the **Claude Code CLI**:

- **One agent** (Claude Opus 4.8) holds everything: the chat, your task, its understanding, its approach and its code. Side jobs are done by **copies** of it at a suitable effort, and what a copy says is merged back into it, so nothing it knows is split between sessions.
  - **Before a task**, the quick-answer agent is that agent: it answers in the chat from the moment the app starts.
  - **Start** continues that conversation as the task's agent (a fork of it, now with write access and its own working folder). It takes the task in at `medium` effort, without solving anything, and judges how much thinking solving it will take (`low` … `max`).
  - **💡 Get Understanding** and **🧭 Get How to solve** are each written by a copy of the agent at `medium` effort. They can be asked for at once, and while the agent is solving. The agent is told what they said with its next message, and so is every copy made of it until then.
  - **▶ Solve** is done by the agent itself, at the effort it judged.
- **Quick answers:** questions heard from your PC's audio are answered within seconds without interrupting the agent. You can also steer the agent directly.
  - During a task, each answer comes from a **copy of the agent**: it knows everything the agent has seen, read and decided so far, while the agent carries on untouched. It runs at the quick-answer effort (`medium`), the level the agent waits at between pieces of work, so it reuses the agent's cache: an answer costs a few cents. The copy can read files, but the CLI refuses any change it tries.
  - A request for real work is passed on to the agent, together with how much thinking the quick answer judged it needs (`[low]` … `[max]`); the agent works on it at that level. While it is busy the level only goes up, so work in progress is never cut short.
  - The agent is told what the panel answered on its behalf, with its next message, so the two stay consistent.
  - Nothing an answer can do without is left in its way:
    - One copy of the agent is kept **started and waiting** for the next question, so an answer does not first wait about a second for a CLI to start. A copy knows only what the agent had done when it was made, so it is renewed a second after the agent moves on, and at least every 10 s while it keeps working; it can read the files on disk for anything newer.
    - A question asked while another answer is still being written goes to **another copy at once**, instead of waiting for that answer.
    - When the app starts, the quick-answer session's fixed part (instructions and tools) is **cached in advance** by a one-word throwaway request, so the first question does not wait for it either. The CLI keeps that cache for an hour; it is warmed at most once an hour.
    - What is left is the model itself: about 1.5 s to the first words of a short answer, longer when screenshots come with the question or it reads a file first.
  - With an API provider, or when the work is done on a copy of your own folder, quick answers run in a session of their own (Claude Opus 4.8 at `medium` effort by default). Working on a copy of your folder, a copy of the session would carry the agent's whole work log, which the quick answers must not see there (see below).
- Why the levels differ: most of an agent's time is hidden thinking (75–90% of what it wrote in real runs). In a test on a hard problem, a plan's first words came after 12 s at high and about 3 s at medium, and code written at high was no more correct. A copy only shares the agent's cache at the same level; at another level it reads the whole conversation again (about 40k tokens).
- **Thinking off for straightforward work:** work judged `low` is done with thinking switched off altogether (Opus 4.x; the 5.x and Fable models cannot switch it off), and it is switched back on for anything heavier. At low and medium effort Opus already skips thinking on easy edits; off makes sure of it, so a simple change never waits for thinking. The switch takes effect for the next request and reads the conversation into the cache once more. Quick answers and the copies keep their default: measured, they answered just as fast either way.

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

   Overlapping or repeated screenshots are fine; the agent merges them using line numbers and tabs. Near-identical ones are flagged with `~#n` so you can remove them.

   The model reads every pixel it is sent, and with many full screenshots that is what makes the first words of the plan slow. So of each message's screenshots only what it has not seen yet is sent; the whole screenshots are still saved, and it can read them from disk when it needs to:
   - a whole-monitor capture goes without the Windows taskbar;
   - a screenshot identical to an earlier one is not sent again (a note says so). A re-capture where only the clock changed counts as identical;
   - a screenshot of the same size as an earlier one is cut down to the rectangle where it differs from the first of them, with a note on where that is - unless that is most of it;
   - with **New part only** ticked (next to **Screen**, in both places), a screenshot that is the previous one scrolled up or down is cut down to the lines that came into view, plus a few lines for context. Leave it off when something else on screen changes between your screenshots: anything else that changed in the overlap would not be sent.

   While the agent takes the task in, the first tab shows what it is doing and for how long ("is thinking about the task (12 screenshots) · 45s"), then how long it took and the effort it judged solving needs.

   Pick the **Mode**, the kind of session. There is no language to pick: the code is always written in the language the problem or the code at hand is in (for a database query, the SQL dialect the problem names, MySQL if none).
   - **Auto** (the default): he judges which of the next four the task is - a problem to solve in code, a problem that wants an answer, pair programming, a code review, a system to design or a design to discuss - going by what was said first (the task's text, the chat) and by what is shown only where nothing says it. Then that flow runs, exactly as if it had been picked. The box beside the work buttons shows what he judged (**DSA - code**, **Code Review**, **System Design - he leads**...); pick another there and he works on the task as that instead (what he was doing is stopped first, and he is told). See [Several tasks in one session](#several-tasks-in-one-session).
   - **DSA**: a self-contained problem, as on HackerRank, LeetCode or a test platform. The flow below. When it takes the task in, the agent also judges what the task wants:
     - **Code**: an algorithm, a program reading STDIN and printing to STDOUT, a function to complete or fix, a SQL query, a regex, a shell script. **▶ Solve** writes it and checks it on the examples (a query against the example tables in SQLite).
     - **An answer**: a multiple-choice question with one or more right options, or a short written or numeric answer (such as what some code prints). The button reads **▶ Answer**: it names the option(s) exactly as labelled, or gives the answer, in bold, with a sentence or two on why - each question in turn when there are several. It writes no files. Where the answer depends on what code does, it first runs all such code in one go and goes by what it prints, and it settles every answer before writing it. Thinking stays on for answers (it takes only what a question needs): in a test without it, the model put a wrong option in bold and corrected itself a sentence later - code has its test run to catch that, an answer does not. With thinking on, the same three-question quiz was answered right in 11 s (12 s without).
     - For a single quick question, ⚡ **Ask** with a screenshot is faster still: the quick model answers in a few seconds, without a task.
   - **Pair Programming** and **Code Review**: see [Pair Programming](#pair-programming) and [Code Review](#code-review).
   - **System Design**: no code - he designs a system with you, or discusses a design you show him, with Mermaid diagrams. See [System Design](#system-design).
   - **Vibe-Coding** means the developer writes no code himself: he directs your own coding agent. See [Directing an external coding agent](#directing-an-external-coding-agent-mode-vibe-coding).

   Next to it, **Code in** says where the code really is for the person he talks to, so that he speaks of it that way. It is part of the agent's instructions for the whole task (and of every copy's):
   - **Local IDE**: a project in an editor on their computer - its files and lines ("I'll change line 21 of orders.py").
   - **Online IDE**: a browser editor you both see (CoderPad, HackerRank, CodeSignal...) - the code, its lines, running it; never a project folder, files being set up, or his own machine.
   - **GitHub**: a repository or a pull request - its files, line numbers, and the change he would make or suggest on a line.

   The code itself still comes as screenshots, files or the Workspace folder (nothing is fetched from GitHub). Whatever he does to get it into his own working folder is silent in every mode: for the person he talks to, the code has been in front of him all along.
2. Press **▶ Start** (Ctrl+Enter). The task box disappears; **＋ Next task** in the toolbar goes on to another task with the same agent, and **⟲ New session** starts over from scratch.
   - The agent goes on from the chat so far: everything asked, answered and attached there before Start is part of what it knows.
   - It takes the task in and does nothing else: no plan, no answer, no file. Then it waits, and the first tab says how much effort it judged solving needs.
   - **⟲ New session** asks for confirmation first. On "Yes" it stops the agent and clears every task of the session and everything on screen: the plan, the prompts, the activity log, the files view, and the whole Q&A panel (transcript, attachments and chat).
   - It also empties the **Workspace** path, so set it again for the next task.
   - Nothing on disk is deleted: the files in the old workspace and the run folder under `~/ScreenCoder/runs/` stay.
   - **Understanding & plan:** three buttons under it, once the agent has taken the task in.
     - **💡 Get Understanding**: what the problem is and what it is looking at, in two to four spoken sentences.
     - **🧭 Get How to solve**: its approach, the steps, and assumptions only when something really is unclear.
     - Both come from copies of the agent, so they can be asked for together, again later, and while it solves. They appear one after another, in the order asked. Text only: a copy cannot change any file.
     - **▶ Solve**: the agent writes the solution, at the effort it judged. Work requests kept from the chat go with it.
     - It talks as if it is reading the code directly and never mentions screenshots. The view stays on this tab while the agent codes.
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
   - **Attachments** (under the transcript): Snip, Screen, Files and Paste add screenshots and files that go with the heard question. Pasted text, such as copied code or an error message, is attached as a small text file. **Clear** there removes the transcript as well as the attachments, so the next Ask only sends what is said from then on.
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
       - **Before Solve** - before the task starts, and while the agent has only taken it in - a work request is kept instead of passed on. This applies to Ask and to a remote message sent to the AI.
         - The developer says his two sentences straight away ("…I'll build that in when I get to the solution."). The heard bubble is marked "work request - goes to the agent with the task (Start)" or "… with Solve".
         - Before the task, the task box shows "📌 N requests from the chat go to the coding agent with this task". Hover over it to list them, or click **Clear** to drop them. **Start** puts them in the task's first message: what was asked, who asked (said out loud or typed by a teammate) and what the developer answered. Start also works with an empty task box when there are kept requests; they are then the task.
         - After Start, the line beside Solve counts them, and **Solve** puts them in its message, with the screenshots and files that came with them.
         - **⟲ New session** drops them, along with the rest of the chat; **＋ Next task** drops them too (they were for the task left behind).
       - Something that is not a question gets no reply.
     - **↪ Send to agent** goes to the coding session as a new instruction. It picks it up right after its current step, replies in the chat with what it will do, and adjusts the code. This needs a running task.
     - **✋ Interrupt & send** is the same as Send to agent, but stops the agent's current step first.
   - If someone is mid-sentence, that sentence is closed first so the whole question is included.
   - Select part of the transcript to send just that part.

The status bar shows the phase, elapsed time, the API-equivalent cost reported by Claude Code, and **Interrupt** / **Stop** buttons.

Every button is enabled only while what it does is possible:

| Button | Enabled when |
|---|---|
| **▶ Start** | the task box holds text or an attachment, or work requests from the chat are kept for the task |
| **💡 Get Understanding** / **🧭 Get How to solve** | the agent has taken the task in, and that copy is not already writing (with an API provider, which has no copies: the agent is free) |
| **▶ Solve** | the agent has taken the task in, is not solving yet, and is not replying to a message |
| **💡 Explain the code** / **🧭 How I'd do it** (Pair Programming) | as Get Understanding |
| **🧪 Run the tests** (Pair Programming) | the agent has taken the project in and is free |
| **🔍 Get Review** (Code Review) | the review has been written once and the agent is free |
| **🛠 Fix…** (Code Review) | the review listed findings and the agent is free |
| **▶ Next** / **🗺 Draw it** (System Design) | he has taken the task in and is free - Next for a system to design, Draw it for a design to discuss |
| **▶ Go on** (System Design) | he has drawn one part of several and waits for you (the Next button reads this then) |
| **📋 Copy Mermaid** (Diagram tab) | a diagram is shown |
| **⚡ Ask** | there is new speech, a selected part of the transcript, or an attachment in the left panel |
| **↪ Send to agent / developer** | the same, and a task's session is running |
| **✋ Interrupt & send** | the same, and that session is in the middle of a turn |
| **■ Stop answer** | a quick answer is being written |
| **Paste** | the clipboard holds an image, files or text |
| **Clear** | its box has text or attachments; under the transcript, also when the transcript has speech |
| **⟲ New session** | there is a task or any history to clear |
| **＋ Next task** | a task has been read in and its agent runs (not in Vibe-Coding, which is a session of its own) |
| the kind box beside the work buttons | the task has been read in |
| **Workspace** field and **Browse…** | no task has been started yet (the folder is fixed once it runs) |
| **Open folder** | a task has a workspace, or the typed folder exists |
| **↻ Refresh** / **📋 Copy code** (Output Code) | a task exists / a file is open |
| **✋ Interrupt** / **■ Stop** | the session is in the middle of a turn / is running |
| **✅ Agent finished** / **⏳ Agent still working** | a Vibe-Coding session is running and he has written a prompt |
| **✔ Sent to the agent** (on each prompt) | it is the newest prompt and has not been marked as sent yet |

## Several tasks in one session

One agent can do one task after another - a design, then building part of it; two DSA problems; a review, then a feature - and it keeps everything from the earlier tasks.

- **＋ Next task** (toolbar) opens the task box again: add the next task (text, screenshots, files), pick its Mode (Auto by default) and Code in, and press **▶ Start task N** (or **Cancel**). If he is in the middle of something, you are asked first, and it is stopped.
- Or, with **Start a task by itself** on (Settings - off by default): when the person moves on ("next question", "now let's implement it"), the quick answer says so in two sentences and the session moves on to that task by itself, with what was said and attached as the task. The same goes for the first task: a brief heard before Start ("Today you'll design one system...") starts the session by itself, as if Start had been pressed.
- With the setting off, a task set in the chat before Start (a brief, "review this", a problem) still gets an instant two-sentence reaction - measured 1.1-1.7 s, where answering the whole brief took 10-15 s before the first word - and is kept for **Start**. A quick question is answered as before.
- He reads the next task in like any task, and also judges whether it **continues** an earlier one (it builds on what he made there - the design he drew, the code he wrote). Such a task is worked on in that task's folder, so the design or code is right there; any other task gets a new folder of its own. With a project folder in Workspace, every task works on that project. Either way he goes on as a copy of himself in the task's folder, with that task's instructions (the whiteboard of a design, or where the code is): all he knew stays.
- **Going back and forth.** A work request about another task of the session ("in your diagram for the first task, rename the load balancer", "back to the DSA problem - solve it") takes the work back to that task: its folder, its instructions, its buttons and first tab, with a "↩ Back to task N" line in the chat. The task it leaves keeps where it stood - waiting for Solve, its kept requests - for when it comes back. If he is in the middle of a step, the switch waits for it. Questions about any task are answered as usual, and nothing switches.
- **Screenshots or files with nothing said**, while a task is on, are part of that task ("📎 More of the task"): the problem that was only named when the session moved on, more of the code, the design. They go to the agent, not to the quick answer.
- **Task** (above the tabs, once there are two): pick any task of the session to see its first tab, Diagram, Design notes and Output Code again - an earlier one as it ended. When a later task went on working in its folder, a copy of that folder as the task ended is kept (`task-N/as-it-ended` in the run folder), and that is what is shown. The work buttons, the chat and the activity log are always for the latest task; the chat and the log mark where each task begins.
- On disk: the first task is in the run folder as before; task N has `task-N/` beside it, with its inputs and, unless it continues another, its `workspace/`.

## Pair Programming

For an existing codebase that the person you talk to changes with the developer, one request at a time.

1. Add the code as screenshots or files, as for DSA - or put the project folder in **Workspace** (optional; the developer works on a copy of it, and your folder is never changed), or both. Pick **Pair Programming** and press **▶ Start**.
2. He reads it in - its structure, the main files, how it is run and tested - and then waits: the phase reads *Ready for the next request*.
   - Code that came only as screenshots or files is put in his folder, exactly as it is, just before his first change - so Output Code shows those files as originals, and what he changed in them (M).
3. Every work request from the chat (Ask, or a message from the remote page) goes to him **at once** - nothing is kept for later. He does it there and then, small and focused, in the code's own style:
   - He talks it through **in the chat** as he goes, in one bubble: what he is about to do, then what he changed and how he checked it. Between his sentences the bubble shows what he is doing ("editing inventory.py…", "running a command…").
   - It runs at the effort the quick answer judged the request needs; straightforward ones at low, without thinking.
   - The change is in the developer's copy (Output Code shows M/U as usual); then he waits for the next request.
4. Questions are answered by the quick model as usual, without disturbing him.
5. Buttons on the **Notes** tab:
   - **💡 Explain the code**: how the codebase works - what it does, its main files, how a request flows through it, how it is tested. From a copy of the agent, merged back.
   - **🧭 How I'd do it**: how he would do the latest request in the chat (or, if it is done, how he went about it). From a copy, merged back.
   - **🧪 Run the tests**: he runs the tests (or the code) and says in the chat what came out - at low effort, without thinking.

## Code Review

For code the developer reviews: a PR, a diff, files or a folder.

1. Add the code, PR or diff as screenshots or files, as for DSA - or put the folder in **Workspace** (optional; he works on a copy), or both. Pick **Code Review** and press **▶ Start**. As in Pair Programming, code that came as screenshots or files is set up in his folder as it is before his first fix.
2. He reads it in, judges how much a careful review needs, and then **writes his findings by himself** on the **Review** tab, at that effort:
   - *What it does*, in a sentence or two.
   - *Findings*, most serious first, at most eight: each tagged [bug], [risk], [performance], [design], [readability] or [style], then exactly where it is - the file's path and line(s), like `src/orders.py:21-23`, and the function - then why it matters and what he would change, in spoken sentences. Line numbers are the ones the code shows (an editor's own numbering in a screenshot). No nitpick padding.
   - *Overall*: how ready it is, and the one thing to fix first.
   - He may read files, or run the code or its tests, to confirm a suspicion before he reports it. He changes nothing.
3. After that, what the person you talk to says decides what happens:
   - A question about a finding or the code is answered (by the quick model, or by him), and nothing is changed.
   - "Fix that" - asked out loud (⚡ Ask), in a remote message, or with ↪ Send to agent - goes to him at once: he fixes it in his copy and talks it through in the chat, as in Pair Programming.
   - **🛠 Fix…** lists his findings ("1. [bug] src/orders.py:16-18 - …") and *All of them*; pick one and he fixes it, at the effort he judged the review needed. This is his own next step, not a reply: he says it as his plan ("I'll fix … first, since …"), never "okay, sure" - that is for a fix the person he talks to asked for.
   - However a finding gets fixed - from the Fix menu or asked for in the chat ("fix the first bug") - he notes which numbers it dealt with on a line the app reads and never shows. That finding then stays in the Fix menu greyed out with a ✓ (the numbers stay those of the review), *All of them* becomes *All the rest (n)*, and the Review tab gets "✓ Finding 2 fixed.". Once all are fixed, the button is off; 🔍 Get Review starts a fresh list.
   - While he is still reviewing, the quick answers speak of it as going through the code ("I'm still going through the rest of it"), and pass on what they have found so far.
4. **🔍 Get Review** reviews again (for example after fixes; the Fix menu then lists the new findings); **💡 Get Understanding** explains what the code does, from a copy.

## System Design

For a system design round: no code, a whiteboard. Pick **System Design** (there is no Code in box, and Workspace is not used) and add the question - "Design a URL shortener" - or the diagram or architecture to discuss, as text, screenshots or files. Press **▶ Start**; he reads it in and judges which of two kinds it is:

- **A system to design - he leads.** Straight after the read-in he takes his first step by himself (the requirements), and each step ends with the one he will take next - the **▶ Next** button carries its name ("▶ Next: Estimates"). His usual order is requirements → estimates → API and data model → the high-level design → one to three deep dives → wrap-up, but he chooses, and goes where the person he talks to wants to go.
  - Each step is his own move, so he says it that way ("Let me put some numbers on this next, since…"), never "okay, sure". He opens with one sentence, then **talks and draws together, one part at a time**, as at a whiteboard: a sentence or two about the part, then that part appears on the diagram - never the whole diagram first and one long explanation after. Then he **waits for you**: the button reads **▶ Go on: <the next part>**, and only your click has him say and draw the next one, so there is time to say it and draw it on the real whiteboard. After the last part he hands over with a question when there is something to confirm, writes up his notes, and the button is **▶ Next: <the next step>** again. A change you ask for that takes several parts goes the same way.
  - **The time sets the detail.** A design is not scored by how much is on the board, so at the read-in he takes in how long the design has - an interview length, "you have 20 minutes", a timer on a screenshot - or assumes 45 minutes, and every step is told how much of it is gone. A short session gets fewer and shorter steps and a diagram of a few boxes; a long one more boxes, a flow or data model where it helps, and more deep dives; near the end he wraps up. (Measured: 9 boxes in three parts for 20 minutes, 10 in five parts for 60.)
  - What the person says goes through ⚡ Ask as usual. A question ("why Cassandra?") gets a quick answer, and so does an answer to his questions ("assume 100M links a month") - he takes it in, and his next step builds on it. A request ("skip the estimates", "add a cache for redirects", "go deeper into the ID generation") goes to him: he does it on the board and talks it through.
  - Steps that are mostly said (requirements, estimates, wrap-up) run at medium effort at most; the architecture and deep dives at the effort he judged the design needs.
- **A design to discuss - he only answers.** Nothing happens by itself. Questions are answered; a change he is asked for ("how would you remove that single point of failure? put it on the diagram") is made on the board and talked through. **🗺 Draw it** has him draw the design that was shown, as it is, without a word - to look at, and as the base for changes.

The board is two tabs:
- **Diagram**: his diagrams in Mermaid - the architecture (`flowchart`), and a flow (`sequenceDiagram`) or the data model (`erDiagram`) when they are worth drawing; the box above picks one when there are several. What his latest drawing added or relabelled is outlined in **green** - the app works it out by comparing with the version before, so while he draws part by part the green is always the part he is talking about. The wheel zooms, dragging moves it, a double-click fits it again. **Mermaid** shows the text under it; **📋 Copy Mermaid** copies it (Excalidraw, Miro, draw.io and mermaid.live import it). A diagram that cannot be drawn shows the last version that could, with Mermaid's error - and goes back to him to fix, up to twice.
- **Design notes**: his notes, whiteboard-style - requirements, estimates, API, data model, high-level design, deep dives, trade-offs - kept up to date as the design changes.

The first new diagram brings the Diagram tab up; after that, a dot on a tab (**Diagram ●**) says it changed while you were elsewhere. The work buttons sit under the tabs in this mode, so ▶ Next is there whichever tab is open. Diagrams are drawn by the Mermaid library bundled with the app (`screencoder/assets/mermaid`, MIT licence) in an offscreen page of Qt WebEngine: nothing is fetched and no diagram leaves the PC, and what is shown is a picture the app paints itself - so it is mirrored to the remote page, and stealth mode hides it like the rest of the window.

## Directing an external coding agent (Mode: Vibe-Coding)

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
2. Add the task, pick **Vibe-Coding**, press **Start**. In Vibe-Coding he starts on it at once (understanding and questions), as before, at the **Vibe-Coding effort** set in Settings.

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

Each developer turn costs one round of the coding model. If the turns feel slow, lower the Vibe-Coding effort in **Settings**.

## Workspace and permissions

- **Workspace** (toolbar): leave it empty to get a fresh folder per task under `~/ScreenCoder/runs/<id>/workspace`. If the code is on your PC, point it at your project folder instead.
- **Your project folder is never changed** (DSA, Pair Programming, Code Review). When you give a folder:
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
- Scrolling is shared: scroll the quick answers, Understanding & plan, Coding activity, Prompts or Output Code on either side and the other shows the same text at the top of the view. (The two sides wrap text at different widths, so they agree on the text, not on a pixel offset. If one view is taller it simply shows more below.) A view on the page has a scroll bar whenever the app's has one, even if all the text would fit on the page: with the app in a small window, the page gets empty room under the text so it can scroll as far as the app does. The file list scrolls on its own.
- Two blinking red lines on the page wrap the part of quick answers, Understanding & plan and Output Code that is on screen in the app, so the person at the page can see what the person at the app is looking at. They lie in the text and follow as either side scrolls. A tab that is not open in the app has none.
- The open tab is shared while **Sync tab** is on: a tick box at the end of the tab row in the app, right after the Output Code tab on the page. It is one box, so either side can switch it. Untick it and each side picks its own tab, and a tab that is not open in the app scrolls on its own; tick it again and every browser goes to the app's tab.
- The **Window** button at the bottom of the page opens a map of the app machine's monitors, to scale, with the app's window on it and its exact position and size, following the window as it moves. Drag the window on the map to move the real one, or its edges and corners to resize it. As in Windows it can go past the edges of the monitors, to a negative position or beyond their full width and height; the map makes room to keep it in sight, so it can always be dragged back. A maximized window comes off at its normal size; a minimized one stays down and comes back where you put it. Inside the window's rectangle a yellow line shows the divider between the app's two sides, quick answers and Understanding & plan, each with its name, and the top line gives its position; it follows when the divider or the window moves. **Screenshot** on the map takes a picture of each monitor and shows it there, to see the app among the other windows: the picture is that moment's, with the time on it, while the window's rectangle stays live. A window in stealth mode is not in the picture, which then shows what lies under it.
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

### Controlling the PC itself

With **…and control this PC from there** ticked in Settings (under Remote control; **off by default** - with it on, whoever has the link can use the whole PC, not only the app), the page's **PC** button (bottom, beside Window) opens a picture of one of the PC's monitors, and what you do on the picture is done on the PC:

- **Click, right-click, middle-click, double-click** - a click waits 0.3 s for a possible second click, so a double-click arrives as one.
- **Press, hold, move and let go** (drag a window, select text, draw): the whole gesture is one message - where it was pressed, the path, where it was let go, with its timing - and the PC replays it at the same speed (at most 4 s). Held without moving for 0.6 s or more, it is a long press.
- **The wheel** (Shift for sideways), and **▲ / ▼** in the bar for a phone.
- **Keys**: while the picture is open every key goes to the PC - text as typed, Enter, Backspace, arrows, F-keys, and combinations with the modifiers held around the key: Ctrl+Z, Ctrl+Shift+Z, Ctrl+C, Shift+arrows, Ctrl+Alt+T (AltGr still types its character). A key on its own gets its own picture; keys pressed within 0.25 s of each other go together and get one picture at the end. On a phone, the **Type text to send** box (⏎ adds Enter).
- **Combinations your own browser or computer keeps for itself** (Ctrl+W would close your tab; Alt+Tab, Alt+F4 and the Windows key are your computer's): the **Ctrl / Alt / Shift / Win** buttons in the bar stay down for your next key or click, then let go - Ctrl, then W, is Ctrl+W on the PC. The **Keys…** list sends the common ones in one go: Alt+Tab, Alt+F4, Win, Win+D, Win+E, Win+Tab, Ctrl+W, Ctrl+T, Ctrl+N, Ctrl+Tab, Ctrl+Shift+Esc, Esc, F5, F11, F12, PrtScn. (Ctrl+Alt+Del cannot be sent by any program.)
- **Monitor buttons** (1 ★, 2) switch monitors; **↻** gets a new picture without doing anything. A red ring shows where the mouse pointer is.

Nothing is streamed: each action gets one new picture of the monitor once the screen has settled (0.35 s), and only the parts of it that changed since your last picture are sent - a click on a button is a few hundred bytes, a whole monitor 45-110 KB at the width your browser shows it (measured on a 1920 × 1080 monitor). Actions sent quicker than they are answered are replayed one after another, and only the last gets a picture.

Limits: windows of programs running as administrator (and the UAC prompt) do not take input from a program that is not, and the lock screen cannot be seen or used. In stealth mode the app itself is not in the pictures, but clicks where it is still reach it.

## Settings

Settings let you change:
- the reading text size for the plan, coding activity, transcript and chat (150% by default);
- the speech model: Kroko (recommended: lightest, adds punctuation) or NVIDIA FastConformer, and whether PC-audio listening is on;
- the coding model and the **Vibe-Coding effort**, and the quick-answer model and effort (Haiku is supported; effort isn't sent for it). A coding task has no fixed effort: the agent judges each piece of work (see the top of this page);
- a name for the AI developer (the coding agent and quick answers), so the interviewer can address it by name;
- the CLI path;
- the runs folder;
- whether thinking summaries are shown;
- the global hotkeys;
- remote control: on or off, from the internet or from this network only, and its link (see above);
- **See-through** (also the tick box at the bottom right of the window; off by default, remembered): the backgrounds of the window's panels, text views, tabs, bars and chat bubbles become transparent, so the desktop shows behind it and only the text, borders, buttons, scroll bars and pictures stay. Over them lies one even dark tint, the **Backdrop** (the slider beside the tick box, 0-90%, remembered): lower shows more of the desktop, higher is easier to read over a busy or light one. All text gets a crisp dark outline (and group titles a small dark chip), so it reads over a white, grey or black desktop alike. The window keeps the mouse everywhere on it, even at 0% (the tint never quite reaches 0), so menus such as the toolbar's » stay open on the way to their buttons, and the chat scrolls anywhere over it. Windows gives this kind of transparency only to a window without its own frame, so in this mode the window has a title bar of its own: drag it to move the window, double-click it or press □ to maximise (to the monitor's work area, so the taskbar stays in sight), – and ×; drag the thin rim along an edge or corner to resize. The opacity slider still applies on top. Dialogs and menus are not see-through;
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
| `screencoder/pc_control.py`, `remote_web/pc.js` | Controlling the PC itself from the remote page (off by default): input replayed with SendInput, a picture of the monitor back - only the changed pieces |
| `screencoder/see_through.py` | See-through: a frameless, per-pixel translucent window with its own title bar and resize rim; backgrounds cleared over an even Backdrop tint, a dark outline around the text |
| `screencoder/diagram_view.py` | Mermaid diagrams (System Design), drawn by the bundled library in an offscreen page and shown as a picture |
