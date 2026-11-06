# Work Delegation Agent

An AI-assisted system for assigning work, following up on it, and seeing its status across an organization.

A leader describes work in plain language, by chat, by email, or by pasting meeting notes. The system finds the right people, emails them the task, sends reminders on a schedule, reads their replies, turns each reply into a structured status, escalates problems to the right managers, and shows everything on a live dashboard.

The AI is used only to read text. Scheduling, deciding who is told what, sending email and calculating reports are ordinary code, so those parts behave the same way every time.

---

## Contents

- [What it does](#what-it-does)
- [How it works](#how-it-works)
- [Tech stack](#tech-stack)
- [Project structure](#project-structure)
- [Getting started](#getting-started)
- [Configuration reference](#configuration-reference)
- [Using the system](#using-the-system)
- [Data model](#data-model)
- [API overview](#api-overview)
- [Scripts](#scripts)
- [Testing](#testing)
- [Switching the AI model](#switching-the-ai-model)
- [Limitations](#limitations)

---

## What it does

### Assigning work

| Way | How it works |
|---|---|
| **Chat** | A chat panel on the dashboard. A leader types, for example, "Ask Ravi to finish the report by Friday" and the agent creates the task. The paperclip button attaches files (see [Attachments](#attachments)). |
| **Email** | A leader emails the agent's mailbox with the instruction in the body. The agent creates the tasks and replies with a confirmation of what it created. Only active employees marked as leaders can assign work by email. |
| **Meeting notes** | Paste notes or a transcript. The agent proposes a list of tasks, each with its owner, deadline and the exact words in the notes it came from. The leader edits or unticks rows, then confirms. Nothing is created or emailed until then. |

How names are resolved:

- People are matched by name or email, and teams by team name. The AI never chooses an email address.
- If a name matches more than one person, the agent asks instead of guessing.
- A task for a team goes to the team leader, or to every member when the instruction says so ("each member of QA").
- Deadlines in the past are rejected. Relative dates such as "Friday" are resolved to a calendar date in the configured time zone.
- A deadline given as a number of working days ("within 4 working days") is counted by the system, skipping Saturdays and Sundays (public holidays are not known). The AI only reports the number, because it gets calendar arithmetic wrong.
- The same request is never carried out twice. A chat message repeated within `DUPLICATE_WINDOW_MINUTES` (same leader, same words), an email read twice (same Message-ID), and an identical task (same assigner, person, title and deadline) all create nothing new, and the leader is told it was already done.
- In meeting notes, an action item with no named owner is shown as "no owner" for the leader to fill in. Items whose supporting quote cannot be found in the notes are flagged as unverified and left unticked.

### Attachments

In the chat panel, the paperclip button attaches up to 5 files (10 MB each, 15 MB in total by default). When the message assigns work, **the files are attached to the assignment email** of every task it creates, including each member of a team when the task goes to everyone. The email also lists the file names, and the task history records which files were sent. If a send fails and is retried, the retry carries the files too.

- Only leaders can attach files, and only files they uploaded themselves.
- Programs and scripts (`.exe`, `.bat`, `.js`, `.ps1` and similar) are refused, because mail servers reject them and the whole email would fail. Put them in a `.zip`.
- Files are stored in the database. A file that is never used in a task is deleted after `ATTACHMENT_ORPHAN_HOURS` (24).
- If the message does not create a task (for example, the name was ambiguous), the files stay in the chat so the leader can correct the message and send again.
- Only the assignment email carries the files. Reminders and escalations do not.

### Delivery

- Assignment emails are sent in the background, so the screen responds immediately.
- Each task shows its delivery state: **sending**, **sent** or **failed**. In chat, a line under the reply updates itself as the email goes out.
- A failed send is retried after 1, 5 and 15 minutes, then marked failed. The task itself is never lost.
- Reminders and escalations start only for tasks whose assignment email was actually delivered.

### Following up

- Reminders are sent on a schedule that depends on how close the deadline is: every 72 hours when it is more than 7 days away, 48 hours at 2 to 7 days, 24 hours when within 2 days, and 24 hours once overdue. All gaps are configurable.
- Reminders are sent only during working hours and skip weekends (both configurable).
- Any reply from the assignee resets that task's unanswered-reminder count.
- A leader can also ask for a follow-up on a task in chat.

### Reading replies

Employees reply to the task email in their own words. The agent:

1. **Finds the task** the reply belongs to, using, in order: the email thread headers, the `[Task #N]` number in the subject, an optional plus-address, the sender's only open task, and finally the AI choosing between their open tasks. If it is still unclear, it asks which task they mean.
2. **Reads the reply** into one status: `on-track`, `blocked`, `delayed` or `done`, with a one-line summary, the blocker if any, a new deadline if the employee proposes one, and a confidence level.
3. **Updates the task**, writes it to the task history, and thanks the employee.

Safeguards on incoming mail:

- Only the assignee's reply can change a task's status. A comment from anyone else is recorded in the history only.
- Mail from addresses that are not active employees, automatic replies (out-of-office, bounces, newsletters) and duplicate messages are ignored.
- The AI's answer is restricted to fixed fields and values. The text of an email is treated as data and never as instructions.

### Checking when the AI is unsure

If the AI reads a reply with low confidence:

- The status is saved but shown with a **?** on the dashboard.
- The employee is emailed to confirm ("Reply yes, or tell me the correct status").
- **Alerts to managers are held back** until the status is confirmed, so a misread cannot raise a false alarm.
- A plain "yes" confirms it without using the AI. A different answer corrects the status. If nobody answers within the configured time, the status stays and the held alerts are sent.

A leader can also correct a status from the dashboard: open a task's history and use "Wrong status?". Only the person who assigned the task, or a leader above the assignee, can do this. The change is recorded and the assignee is emailed.

### Escalation

Managers are emailed when:

- an employee reports a **blocker** (once the status is confirmed),
- a task has had too many **unanswered reminders**,
- a task has been **overdue** longer than the configured time.

The recipients are the person who assigned the task, then the assignee's manager chain and team leader. Each task is escalated once. The assigner is also told when a task is completed.

### Dashboard

A single-page dashboard served by the application:

- **Status view** with drill-down from company to division to team to person: counts by status, completion, overdue and escalated tasks, and a "needs attention" list.
- **At risk** list: open tasks that are not late yet but are heading that way.
- **Live updates:** the page refreshes itself within about two seconds of a change, with a live indicator in the header.
- **Task history:** click any task to see its full timeline.
- **People view:** add, edit and deactivate divisions, teams and employees, with a change log, and import people from a CSV file with a preview before anything is saved.
- **Meeting notes**, **Morning brief** and **Impact** windows, described below.

### Morning brief

An email sent every working morning to the top of the organization (every active leader who has no manager), or to addresses set in `BRIEF_TO`. It is built from the database with fixed rules, with no AI.

- Headline for the last 24 hours (since Friday on Mondays): finished, new, newly blocked, escalated.
- Current counts by status.
- **Needs you today:** overdue, escalated, or blocked for more than 24 hours.
- **At risk:** due within 2 days with no reply; due within 2 days but delayed or blocked; or two or more unanswered reminders.
- Open tasks and issues by division.

The dashboard's **Morning brief** window shows exactly what would be sent and has a "Send now" button.

### Impact numbers

The **Impact** window summarizes a chosen period (7 days, 30 days, or all time), counted from the task history and email logs:

- Tasks assigned, completed, and the share completed on time.
- What the agent did itself: reminders sent, replies read, blocker alerts, escalations, emails sent.
- Typical time to the assignee's first reply, and the time to alert managers once a blocker reply is read.
- How often the agent was unsure, and how many of those statuses were confirmed or corrected.
- An **estimate** of manager time saved: the number of steps the agent handled multiplied by `MANUAL_STEP_MINUTES`. The assumption is shown next to the figure.

---

## How it works

```
 Browser (dashboard)                      Employees' mailboxes
        │  HTTP + API key                        ▲        │
        ▼                                        │ SMTP   │ IMAP
 ┌─────────────────────────────── FastAPI application ───────────────────────────┐
 │  routes ─► services (assign, inbox, follow-up, escalation, brief, reports)    │
 │                 │                                   ▲                         │
 │                 ▼                                   │                         │
 │            AI model (reads text only)      background jobs:                   │
 │                                              • read the mailbox               │
 │                                              • send assignment emails         │
 │                                              • reminders and escalation       │
 │                                              • morning brief                  │
 └───────────────────────────────────┬───────────────────────────────────────────┘
                                     ▼
                              PostgreSQL database
```

**The AI is used in four places, and nowhere else:**

| Where | Job |
|---|---|
| Understanding an assignment | Turns "Ask Ravi to finish the report by Friday" into a title, person and date |
| Reading meeting notes | Lists the action items in pasted notes |
| Reading a reply | Turns free text into a status, summary, blocker, new date and confidence |
| Choosing a task | Picks which open task an unlabeled reply refers to |

The chat panel also uses a small tool-calling agent that chooses between assigning work, reporting status and sending a follow-up.

**Background jobs** (started with the application):

| Job | Frequency |
|---|---|
| Read the agent mailbox | every `INBOX_POLL_SECONDS` (default 60) |
| Send waiting assignment emails | immediately when a task is created, plus a check every 60 seconds for retries |
| Reminders, escalation, expiry of unanswered status questions | every `FOLLOWUP_JOB_MINUTES` (default 15), working hours only |
| Morning brief | daily at `BRIEF_HOUR`, working days |

---

## Tech stack

| Part | Technology |
|---|---|
| Language | Python 3.10 or newer |
| Web framework | FastAPI, served by Uvicorn |
| Database | PostgreSQL through SQLAlchemy (async) and asyncpg. SQLite is supported for local trials and is used by the tests |
| Email | SMTP to send (aiosmtplib), IMAP to read (imapclient). Written for Gmail, works with any provider that offers SMTP and IMAP |
| Scheduling | APScheduler |
| AI | LangChain with Groq by default. Ollama and any OpenAI-compatible server are also supported |
| Validation | Pydantic |
| Dashboard | One HTML file with plain JavaScript. No build step |
| Tests | pytest, pytest-asyncio, httpx |

---

## Project structure

```
app/
  main.py              application start-up, health and live-update endpoints
  config.py            all settings, read from .env
  database.py          database connection
  models.py            database tables
  schema_upgrade.py    adds columns that an older database is missing
  schemas.py           request and response shapes
  security.py          API key check
  llm_client.py        the one place that chooses the AI model
  llm_tasks.py         the AI jobs: read an assignment, notes, a reply
  agent.py             the chat agent
  directory.py         finding people and teams, manager chains
  email_client.py      sending and reading email
  email_templates.py   the text and layout of every email
  scheduler.py         background jobs
  timeutil.py          time zones and working hours
  changes.py           the "something changed" counter for live updates
  metrics.py           timing and token logging for AI, database and email
  routes_agent.py      chat, notes, brief and manual-trigger endpoints
  routes_tasks.py      task endpoints
  routes_reports.py    status and impact reports
  routes_org.py        people, teams, divisions and CSV import
  services/
    assign.py          create tasks from an assignment
    tasks.py           task changes, history, email delivery
    delivery.py        background sending with retries
    attachments.py     chat attachments: checks, storage, linking to tasks
    inbox.py           reading and handling incoming email
    followup.py        reminders and escalation timing
    escalation.py      emails to managers
    notes.py           meeting notes into proposed tasks
    brief.py           the morning brief
    risk.py            the "at risk" rules
    reports.py         status roll-ups
    impact.py          impact numbers
    org_import.py      CSV import
dashboard/
  index.html           the dashboard
scripts/               setup and maintenance commands (see Scripts)
data/
  employees_sample.csv template for the people import
tests/                 automated tests
```

---

## Getting started

### 1. Requirements

- Python 3.10 or newer
- A PostgreSQL database (a hosted one such as Neon works well)
- A mailbox for the agent with SMTP and IMAP enabled. For Gmail: turn on 2-Step Verification, then create an **App Password**. Use that, not the account password
- An API key for the AI provider (Groq by default)

### 2. Install

```bash
python -m venv .venv
# Windows:      .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

Create a file named `.env` in the project folder. This is the minimum:

```ini
DATABASE_URL=postgresql://user:password@host/dbname
GROQ_API_KEY=your-groq-key
GMAIL_SMTP_USER=agent@example.com
GMAIL_SMTP_PASSWORD=your-app-password
API_KEY=choose-a-long-random-string
```

The `.env` file holds secrets. Never commit it (it is already in `.gitignore`).

To try the system without sending any real email, add `EMAIL_DRY_RUN=true`. Emails are then written to the log instead.

### 4. Create the tables

The tables are created automatically the first time the application starts. You can also run:

```bash
python -m scripts.init_db
```

### 5. Load your organization

Prepare a CSV with these columns (one row per person):

```csv
division,team,full_name,email,title,manager_email,is_leader
Executive,Leadership,Alex Morgan,ceo@example.com,CEO,,true
Engineering,Platform,Sam Lee,sam@example.com,Team Lead,ceo@example.com,true
Engineering,Platform,Priya Nair,priya@example.com,Engineer,sam@example.com,false
```

- `manager_email` is empty for the top person.
- `is_leader` marks people who may assign work.
- The top person (a leader with no manager) is also who receives the morning brief.

Then import it (safe to run again):

```bash
python -m scripts.import_employees path/to/your_file.csv
```

You can also manage people from the dashboard's People view, which supports importing a CSV with a preview.

### 6. Run

```bash
uvicorn app.main:app --reload
```

Run a single copy of the application (see [Limitations](#limitations)).

| Address | What it is |
|---|---|
| `http://127.0.0.1:8000/dashboard` | The dashboard |
| `http://127.0.0.1:8000/docs` | Interactive API documentation |
| `http://127.0.0.1:8000/health` | Health check |

The dashboard asks for the `API_KEY` the first time and keeps it for the browser session.

### 7. Try it

1. Open the dashboard, open the chat panel and choose a leader in the "chatting as" box.
2. Type: *Assign Priya to finish the quarterly report by Friday.*
3. Watch the "sending → sent" line appear under the reply.
4. Reply to that email from Priya's mailbox: *"Blocked, waiting for the data from finance."*
5. Within about a minute (sooner if `INBOX_POLL_SECONDS` is lower) the dashboard shows the task as blocked and the managers are emailed.
6. Open **Morning brief** and **Impact** from the header.

---

## Configuration reference

All settings are read from `.env`. Every setting except the four required ones has a default.

### Required

| Setting | Meaning |
|---|---|
| `DATABASE_URL` | Database address. `postgresql://...` is converted automatically for the async driver. `sqlite:///file.db` works for local trials |
| `GMAIL_SMTP_USER` | Mailbox address the agent sends from and reads |
| `GMAIL_SMTP_PASSWORD` | Password or app password for that mailbox |
| `GROQ_API_KEY` | AI key, when `LLM_PROVIDER=groq` |

### AI model

| Setting | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER` | `groq` | `groq`, `ollama`, `openai` (any OpenAI-compatible server) or `dummy` |
| `LLM_MODEL` | `openai/gpt-oss-120b` | Model name for the chosen provider |
| `LLM_BASE_URL` | empty | Server address for `ollama` and `openai` |
| `OPENAI_API_KEY` | `not-needed` | Key for an OpenAI-compatible server |

### Email

| Setting | Default | Meaning |
|---|---|---|
| `SMTP_HOST` / `SMTP_PORT` | `smtp.gmail.com` / `587` | Sending server |
| `GMAIL_IMAP_USER` / `GMAIL_IMAP_PASSWORD` | same as SMTP | Reading login, if different |
| `IMAP_HOST` / `IMAP_PORT` | `imap.gmail.com` / `993` | Reading server |
| `EMAIL_DRY_RUN` | `false` | Log emails instead of sending them |
| `SEND_ACKS` | `true` | Thank the employee after each reply |
| `ASYNC_DELIVERY` | `true` | Send assignment emails in the background |
| `USE_PLUS_ADDRESSING` | `false` | Set Reply-To to `agent+t<ID>@...` (needs provider support) |
| `REQUIRE_EMAIL_AUTH` | `false` | Require SPF/DKIM to pass before accepting an assignment by email |
| `MARK_IGNORED_AS_READ` | `false` | Mark ignored messages as read in the mailbox |
| `IMAP_FETCH_LIMIT` | `20` | Messages read per check |
| `IMAP_MAX_ATTEMPTS` | `3` | Tries before a message is given up on |

### Working time

| Setting | Default | Meaning |
|---|---|---|
| `TIMEZONE` | `Asia/Kolkata` | Company time zone |
| `WORK_START_HOUR` / `WORK_END_HOUR` | `9` / `18` | Working hours for reminders |
| `SKIP_WEEKENDS` | `true` | No reminders or briefs on weekends |

### Reminders and escalation

| Setting | Default | Meaning |
|---|---|---|
| `FOLLOWUP_GAP_FAR_HOURS` | `72` | Gap between reminders when the deadline is more than 7 days away |
| `FOLLOWUP_GAP_MID_HOURS` | `48` | Deadline 2 to 7 days away |
| `FOLLOWUP_GAP_NEAR_HOURS` | `24` | Deadline within 2 days |
| `FOLLOWUP_GAP_OVERDUE_HOURS` | `24` | Already overdue |
| `MAX_UNANSWERED_REMINDERS` | `3` | Unanswered reminders before escalating |
| `OVERDUE_ESCALATE_AFTER_HOURS` | `24` | Time overdue before escalating |

Do not set the reminder gaps to `0`: reminders would then be sent on every pass of the follow-up job.

### Background jobs

| Setting | Default | Meaning |
|---|---|---|
| `ENABLE_SCHEDULER` | `true` | Run the background jobs inside the application |
| `INBOX_POLL_SECONDS` | `60` | How often the mailbox is read |
| `FOLLOWUP_JOB_MINUTES` | `15` | How often reminders and escalation are checked |

### Morning brief, status checks and impact

| Setting | Default | Meaning |
|---|---|---|
| `BRIEF_ENABLED` | `true` | Send the daily brief |
| `BRIEF_HOUR` | `9` | Local hour to send it |
| `BRIEF_TO` | empty | Comma-separated addresses. Empty means the top leader(s) |
| `DASHBOARD_URL` | empty | If set, the brief includes a link to the dashboard |
| `CONFIRM_LOW_CONFIDENCE` | `true` | Ask the employee to confirm a status the AI was unsure about |
| `STATUS_CHECK_HOURS` | `24` | How long to wait for that answer |
| `MANUAL_STEP_MINUTES` | `3` | Assumed minutes for one manual step, used in the time-saved estimate |

### Application

| Setting | Default | Meaning |
|---|---|---|
| `API_KEY` | empty | If set, every data endpoint needs it in the `X-API-Key` header. Empty means no key is required |
| `AUTO_CREATE_TABLES` | `true` | Create missing tables and add missing columns at start-up |
| `AGENT_NAME` | built-in name | Name shown in email signatures |
| `MAX_ATTACHMENT_MB` | `10` | Largest single attached file |
| `MAX_ATTACHMENTS` | `5` | Most files on one chat message |
| `MAX_ATTACHMENTS_TOTAL_MB` | `15` | Total size of the files on one email (mail servers stop at about 25 MB, and encoding adds about a third) |
| `ATTACHMENT_ORPHAN_HOURS` | `24` | Uploaded files never used in a task are deleted after this many hours |
| `DUPLICATE_WINDOW_MINUTES` | `10` | The same assignment (same assigner, assignee, title and deadline) asked again within this time is treated as a repeat: no second task and no second email, and the leader is told. `0` turns this off |

---

## Using the system

### Chat

Type plain instructions in the chat panel:

- *Ask Ravi to finish the NCR review by Friday.*
- *Ask each member of the QA team to complete the safety training by the 30th.*
- *What is the status of the QA team?*
- *Follow up with Meena about the vendor drawings.*

The "chatting as" box sets which leader is acting.

### Assigning by email

Send an email **to the agent's mailbox** from a leader's address, with the instruction in the body:

> Ask the QA team to complete the root-cause review by Friday, and ask Meena to send the drawings to the vendor on Monday.

The agent creates the tasks, emails each assignee, and emails you a confirmation. Writing directly to an employee does not create a task; the agent only acts on mail sent to its own mailbox.

### Replying to a task

Employees reply to the task email in their own words, for example:

- *"Done, report attached."*
- *"Blocked, waiting for the vendor part."*
- *"Will need two more days."*

No special format is needed. Replies stay in the same email thread, so the agent can match them to the task.

### Meeting notes

Click **Meeting notes** in the header, paste the notes (up to 12,000 characters, up to 25 tasks per paste), and review the proposed tasks. Edit any title, owner or deadline, untick rows you do not want, and confirm. Rows that fail (for example an ambiguous name) stay editable so they can be fixed and sent again without redoing the others.

---

## Data model

| Table | Holds |
|---|---|
| `divisions`, `teams`, `employees` | The organization. Each employee has a team, a manager, a leader flag and an active flag |
| `tasks` | Each task: title, description, assignee, assigner, status, deadline, delivery state, reminder counts, escalation state, completion time |
| `task_events` | The history of every task: created, delivered, status changes, reminders, escalations, comments, confirmations and corrections |
| `attachments`, `task_attachments` | Files attached in the chat (the bytes are stored in the database) and which tasks they were sent with |
| `outbound_emails` | Every email the agent sent, with its type and the task it belongs to |
| `email_log` | Every email the agent read and what it did with it |
| `org_changes` | A log of changes made to people, teams and divisions |
| `request_log` | Memory of requests already acted on (what the leader typed, or an email's Message-ID), so a repeat is recognised. Rows older than 30 days are deleted |

Task statuses: `pending` (assigned, no reply yet), `on-track`, `blocked`, `delayed`, `done`.

---

## API overview

Every endpoint except `/health` and `/dashboard` requires the `X-API-Key` header when `API_KEY` is set. Full interactive documentation is at `/docs`.

| Area | Endpoints |
|---|---|
| App | `GET /health`, `GET /dashboard`, `GET /changes` (live-update counter) |
| Chat and agent | `POST /agent` (chat; accepts `attachment_ids`), `POST /agent/attachments` (upload one file, multipart with `as_email` and `file`), `POST /agent/process-inbox`, `POST /agent/run-followups` |
| Meeting notes | `POST /agent/notes/preview`, `POST /agent/notes/confirm` |
| Morning brief | `GET /agent/brief/preview`, `POST /agent/brief/send` |
| Tasks | `GET`/`POST /tasks`, `GET`/`PATCH /tasks/{id}`, `GET /tasks/{id}/events`, `GET /tasks/delivery?ids=...`, `POST /tasks/{id}/correct` |
| Reports | `GET /reports/company`, `/reports/division/{id}`, `/reports/team/{id}`, `/reports/person/{id}`, `/reports/impact?days=7` |
| Organization | `GET`/`POST /org/divisions`, `/org/teams`, `/org/employees`, `PATCH` on each by id, `GET /org/employees/{id}/impact`, `GET /org/overview`, `GET /org/changes`, `POST /org/import/preview`, `POST /org/import/apply` |

---

## Scripts

Run from the project folder, for example `python -m scripts.init_db`.

| Script | What it does |
|---|---|
| `init_db` | Creates missing tables. Never deletes data |
| `import_employees <file.csv>` | Loads divisions, teams and employees from a CSV. Safe to run again |
| `add_delivery_columns` | One-time upgrade for an older database: adds the delivery columns to `tasks` |
| `add_status_check_column` | One-time upgrade for an older database: adds the `status_check` column to `tasks` |
| `llm_report` | Summarizes timing and token use from `logs/llm_calls.jsonl` (AI calls, database statements, emails, per operation). Use `--clear` to start a new measurement |
| `db_ping` | Measures raw database speed, with no AI or email involved |
| `seed_demo` | Optional: adds a few sample tasks in different states. Set `EMAIL_DRY_RUN=true` first, because the running application emails the assignees of new tasks |
| `clear_org` | **Danger:** deletes all people, teams, divisions and tasks |
| `reset_db` | **Danger:** deletes all tables and recreates them empty |

With `AUTO_CREATE_TABLES=true`, missing columns are also added automatically at start-up, so the two `add_*` scripts are only needed if you turn that off.

---

## Testing

```bash
python -m pytest -q
```

More than 150 automated tests run against a throwaway SQLite database, with a fake mail sender and a fake AI model, so they need no internet, no real mailbox and no API key. They cover assignment, name matching, delivery and retries, reply handling, status confirmation and correction, reminders and escalation, the morning brief, impact numbers, the people import and the API.

To run them against PostgreSQL instead:

```bash
# PowerShell
$env:TEST_DATABASE_URL="postgresql://user:pass@localhost/testdb"; python -m pytest -q
```

Measuring speed: every AI call, database statement and email send is timed per operation and written to `logs/llm_calls.jsonl`. View the summary with `python -m scripts.llm_report`.

---

## Switching the AI model

The model is chosen only in `.env`, with no code changes:

```ini
# Groq (cloud, default)
LLM_PROVIDER=groq
GROQ_API_KEY=...

# A local or in-house server running Ollama
LLM_PROVIDER=ollama
LLM_BASE_URL=http://server:11434
LLM_MODEL=your-model

# Any OpenAI-compatible server (for example vLLM)
LLM_PROVIDER=openai
LLM_BASE_URL=http://server:8000/v1
OPENAI_API_KEY=anything
LLM_MODEL=your-model
```

Answer quality depends on the model. Smaller models should be tested on your own replies before being relied on.

---

## Limitations

- **Single user key, no per-user login.** One shared `API_KEY` protects the data, and the "chatting as" box selects who is acting. Anyone with the key can act as any leader. Real sign-in and roles are needed before wider use.
- **One application process.** The background jobs and the live-update counter live in the application's memory. Run exactly one copy.
- **The application must stay running.** Mail is read, reminders are sent and the brief is sent by jobs inside it. If it is stopped or put to sleep (as many free hosting plans do), none of that happens until it is running again.
- **Email is checked by polling.** A reply is seen up to `INBOX_POLL_SECONDS` after it arrives. Instant delivery would need a provider API with push notifications.
- **Attachments:** only the chat panel can attach files. Meeting-notes tasks and tasks assigned by email do not carry files, and the dashboard has no download link for a sent file (the assignee has it in their email).
- **Email only.** There is no WhatsApp or chat-app integration.
- **Sends real email.** With real addresses in the organization, the system emails real people. Use `EMAIL_DRY_RUN=true` or test addresses when experimenting.
- **The AI can misread a reply.** Low-confidence readings are confirmed with the employee, and leaders can correct statuses, but a confident wrong reading that nobody questions is not caught. The accuracy figure in the Impact window counts only statuses that were confirmed or corrected.
- **Task text is sent to the configured AI provider.** With the default (Groq), that is an outside service. Use the Ollama or OpenAI-compatible options to keep it on your own servers.
- **Not load-tested.** It has not been tried with hundreds of users or thousands of tasks at once.