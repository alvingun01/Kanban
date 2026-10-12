# How to install the Kanban board

This is a to-do board where the work is done by AI agents. You add a card, click
**Execute**, an agent does the job, and it comes back to you for approval.

These instructions assume you have never used a command line. Follow them in
order. It takes about ten minutes.

---

## Before you start: three things to check

### 1. Open PowerShell

You will type a few commands. The place you type them is called PowerShell.

1. Click the **Start** button.
2. Type `powershell`.
3. Click **Windows PowerShell**.

A blue or black window opens with a blinking cursor. That is PowerShell. Leave
it open — you will use it a few times.

> **Tip:** you copy text into PowerShell by right-clicking, not with Ctrl+V.

### 2. Check you have Python

Python is the language this tool is written in. Copy this line into PowerShell
and press Enter:

```
python --version
```

**If you see something like `Python 3.12.10`** — you are fine, skip to step 3.

**If you see an error, or a Microsoft Store window opens,** you need Python:

1. Go to <https://www.python.org/downloads/>
2. Click the big yellow **Download Python** button.
3. Run the file you downloaded.
4. **Important:** on the first screen, tick the box at the bottom that says
   **"Add python.exe to PATH"**, *then* click Install Now. If you miss that box
   nothing will work later, and you will have to install Python again.
5. Close PowerShell, open it again, and run `python --version` to check.

You need version 3.8 or higher. Anything starting `3.` is almost certainly fine.

### 3. Check you have Claude Code

You need Claude Code and a Claude subscription you are signed in to. If you are
already using Claude Code in VS Code, you have this and there is nothing to do —
the board finds it by itself.

You do **not** need to install anything extra, and you do **not** need an API key.
The board uses your normal subscription.

---

## Install it

### Step 1 — run the installer

In File Explorer, open the folder containing this file. You will see a file
called **`install.ps1`**.

**Right-click `install.ps1`** and choose **Run with PowerShell**.

A window appears, does its work, and shows something like:

```
  kanban is installed.

  tool     C:\Users\You\Documents\Kanban
  command  C:\Users\You\AppData\Local\kanban\bin\kanban.cmd
  python   C:\Users\You\AppData\Local\Programs\Python\Python312\python.exe
  path     Added to your PATH so you can type kanban anywhere.

  ---------------------------------------------------------
  WHAT TO DO NEXT
  ...
```

It then waits, showing **"Press Enter to close this window..."**. Read the
message, then press Enter.

That is it. Nothing was installed for other users of the computer, and no
administrator permission was needed.

> If it says **"Nothing was installed"** instead, the message underneath says
> why. See **If something goes wrong** at the bottom of this page.

### Step 2 — close and reopen PowerShell

This matters. The installer told Windows about a new command, and only *new*
PowerShell windows know about it.

Close the PowerShell window, open a fresh one, then type:

```
kanban doctor
```

If you see a short report with lines like `board`, `agents` and `limits`, or a
message saying *no kanban board here*, the install worked. Either answer is
success — the second one just means you have not made a board yet.

---

## Set up your first board

A "board" belongs to one project folder. Each project gets its own.

### Step 1 — pick a folder

Use a real project folder if you have one. If you just want to try it, make a
new one. In PowerShell:

```
cd $HOME\Documents
mkdir kanban-test
cd kanban-test
```

> **One thing to avoid:** don't put a board inside OneDrive, Dropbox, or Google
> Drive. Those services copy files while they are being written, which can
> corrupt them. `Documents` is fine as long as it is not the synced kind — if
> your Documents folder shows a little cloud icon, use `C:\work` instead.

### Step 2 — create the board

```
kanban init
```

You will see a list of things it created, ending with:

```
next: double-click Dashboard.cmd (everything happens there)
```

If it mentions *not a git repository*, that is only a note. The board works
without git; you just won't get a separate copy of the files per card. To get
that, run `git init` in the folder first.

### Step 3 — open the board

Open that folder in File Explorer and **double-click `Dashboard.cmd`**.

Two things happen:

- a small black window opens — **this is the board running. Leave it open.**
  Closing it switches the board off.
- your web browser opens showing five columns: **Inbox, Execute, Running,
  Review, Done**.

That browser page is the whole thing. You never need PowerShell again.

---

## Using it

### Adding a job

1. Click **New card**.
2. Give it a **Title** — a short name, like *Fix the login page*.
3. Fill in **Goal**. This is the important one. Write it as if for someone
   competent who has never seen your project and cannot ask you questions.
   *"Make the login form show an error message when the password is wrong"* is
   good. *"Fix login"* is not.
4. Click **Create in Inbox**.

### Getting it done

Your card is in **Inbox** with a button on it.

- **Plan it** — an agent looks at your project and writes a plan, then brings it
  back for you to approve before it changes anything. This is the default, and
  it is the safe way round.
- If you ticked *"skip planning"* when creating the card, the button says
  **Execute** instead and the agent starts work immediately.

Click the button. The card moves to **Execute**, then **Running** when an agent
picks it up. You can watch it move. Nothing needs you until it reaches
**Review**.

**Changed your mind?** A card in **Running** has **Move to Inbox**: it stops the
agent and puts the card back. While a card is in **Inbox**, click **Open** to add
detail to the goal or change anything else, then click **Save changes**.

### Reviewing it

Everything an agent finishes waits in **Review** for you. The card says in one
line what it is waiting for — it will be one of these:

| What you see | What to do |
|---|---|
| A plan | **Approve plan** to let it go ahead, or **Reject** with a note saying what to change |
| Finished work that changed files | **Approve** to accept it, or **Reject** with a note |
| Finished work that changed nothing (a lookup or question) | Read its report in **Open**, then **Accept answer** to close the card, or **Reject** with a follow-up |
| A question | Type your answer and click **Send answer** |

Each card only shows what it needs from you. Click **Open** on a card to see
everything else: the agent's report, the goal, the plan, its notes, which
files it changed, and the reply boxes.

**Nothing is ever finished without you clicking Approve or Accept answer.** That is the point of
the board — the agents cannot approve their own work, and there is no setting
that lets them.

### Stopping and starting

- **To stop the board:** close the small black window.
- **To start it again:** double-click `Dashboard.cmd`.

Your cards are files in the `cards` folder, so nothing is lost when the board is
off. Agents that were mid-job will have their cards handed back automatically
after about half an hour.

---

## If something goes wrong

### The window flashed up and closed instantly

This is normal Windows behaviour, not a crash: "Run with PowerShell" closes the
window the moment a script ends. **It very often means the install worked** —
you just weren't given time to read it.

Check whether it worked. Open a **new** PowerShell window and type:

```
kanban doctor
```

- If you get a short report, **or** the message *"no kanban board here"* — the
  install worked. The second message just means you aren't in a project folder
  yet. Carry on to *Set up your first board*.
- If you get *"kanban is not recognized"*, it did not work. See below.

To see the output next time, open the folder in File Explorer, click the
**address bar**, type `powershell`, press **Enter**, then type:

```
.\install.ps1
```

Run that way the window is yours and stays open.

### "install.ps1 is not digitally signed" or "running scripts is disabled"

Windows is being cautious about a file it thinks came from the internet.

First, a trick that saves you typing any long folder paths:

1. In File Explorer, open the folder containing `install.ps1`.
2. Click the **address bar** at the top (where the folder path is shown).
3. Type `powershell` over it and press **Enter**.

A PowerShell window opens *already pointing at that folder*, so you can refer to
files with just `.\` in front of the name.

Now type:

```
Unblock-File .\install.ps1
```

Then right-click `install.ps1` → **Run with PowerShell** again.

If it still refuses, run it this way instead:

```
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

### "kanban is not recognized"

You are in a PowerShell window that was open before you installed. Close it,
open a new one, and try again. (If a new window still does not work, restart the
computer — Windows sometimes needs that to notice a new command.)

### "Need Python 3.8 or newer on PATH"

Python is either missing, or was installed without ticking **"Add python.exe to
PATH"**. Reinstall it from <https://www.python.org/downloads/> and make sure
that box is ticked.

### The board says "Claude Code CLI not found"

The board cannot find Claude Code, so it cannot start agents. Everything else
still works. Open Claude Code once, sign in, then restart the board.

### A card is stuck in Execute and no agent starts

Run `kanban doctor` in the project folder. It shows what the board can and
cannot find. The usual cause is that Claude Code is not signed in.

### An agent stopped and said it could not run a command

By design, agents may only run a short list of commands. If your card needs
something else — running tests, for example — that must be added deliberately.
Open `.kanban\config.json` in Notepad and add it to `extra_allowed_tools`:

```json
"extra_allowed_tools": ["Bash(pytest *)"]
```

### The page says "dashboard stopped — this page is now stale"

The small black window was closed. Double-click `Dashboard.cmd` again.

### Nothing here matches

Open the project folder in PowerShell and run:

```
kanban doctor
kanban check
```

Between them, those two report almost everything that can be wrong.

---

## Removing it

Open the folder containing `install.ps1`, click the address bar, type
`powershell` and press Enter. Then:

```
powershell -File .\install.ps1 -Uninstall
```

**Your boards and cards are not touched** — they live in your project folders,
not with the tool. To remove a board as well, delete that project's `cards`,
`runs`, `wt` and `.kanban` folders.

---

## A note on cost

Agents run on your Claude subscription, not a separate paid account. There is no
API key and no per-card bill.

Two things are worth knowing:

- The board runs **two agents at a time** on purpose. Running more does not make
  them faster; they share the same limits and just use them up quicker. You can
  change this in `.kanban\config.json` if you want.
- Every agent costs something from your usage allowance even for small jobs, so
  one clear card beats five tiny ones.

The dollar figures on the dashboard are an *estimate of size*, not a bill you
will receive.
