# TNP Watcher 🔔

Watches the **BIT Mesra Training & Placement portal** (`tp.bitmesra.co.in`) and
sends a **Telegram** message whenever something new appears. It tracks two things:

1. **New companies** added to *Recent Jobs* for placement year **2026-27**.
2. **New notifications** posted in *News & Events*.

Runs in the cloud on **GitHub Actions**, triggered every 30 minutes by **cron-jobs.org**.

```
cron-jobs.org ──POST──▶ GitHub Actions ──▶ watcher.py ──▶ Telegram Bot ──▶ you / your group
   (every 30 min)         (repository_dispatch)   login + diff
```

---

## 1. Test it locally first

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1

pip install -r requirements.txt

cp .env.example .env      # then edit .env
```

Fill in `.env`:
- `TNP_USERNAME`, `TNP_PASSWORD` — your portal login.
- `TNP_PLACEMENT_YEAR` — `2026-27` (already the default).
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — from the Telegram step below.

Then:

```bash
python watcher.py           # first run = silent baseline (records what exists now)
python watcher.py           # later runs alert only on NEW companies / notifications
```

---

## 2. Set up Telegram — 3 minutes

1. In Telegram, open **@BotFather**, send `/newbot`, follow the prompts, and copy
   the **bot token** it gives you → `TELEGRAM_BOT_TOKEN`.
2. Decide where alerts go:
   - **To yourself:** open your new bot and send it any message (e.g. `hi`).
   - **To a group:** create the group, **add your bot** to it, and post any message.
3. Find the chat id:
   ```bash
   python watcher.py --chatid
   ```
   It prints each chat the bot has seen with a ready-to-copy
   `TELEGRAM_CHAT_ID=...` line. Group ids are negative numbers (e.g. `-1001234567890`).
   Put it in `.env`.
4. Test:
   ```bash
   python -c "import watcher; watcher.notify('TNP Watcher test ✅')"
   ```
   You should get that message in Telegram.

> Telegram bots have their **own identity** — no personal phone number is linked,
> and it's the official, free, reliable way to broadcast to a group.

---

## 3. Deploy to GitHub Actions

1. Create a **private** GitHub repo and push this project:
   ```bash
   git init
   git add .
   git commit -m "Initial TNP watcher"
   git branch -M main
   git remote add origin https://github.com/<you>/tnp-watcher.git
   git push -u origin main
   ```
   (Commit the current `state.json` so the cloud starts from your baseline and
   doesn't re-announce the 800+ existing items.)
2. **Settings ▸ Secrets and variables ▸ Actions ▸ New repository secret**. Add:
   - `TNP_USERNAME`
   - `TNP_PASSWORD`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. **Actions** tab ▸ enable workflows ▸ **Run workflow** once to confirm it runs.

> Keep the repo **private** — `state.json` is committed there. Secrets stay
> encrypted regardless.

---

## 4. Trigger every 30 min with cron-jobs.org

GitHub's built-in `schedule:` is unreliable (often 15–45 min late), so we drive
it externally.

1. Create a **fine-grained GitHub Personal Access Token**: access limited to this
   repo, **Contents: read/write**. Copy it.
2. On **cron-jobs.org**, create a cron job:
   - **URL:** `https://api.github.com/repos/<you>/tnp-watcher/dispatches`
   - **Schedule:** every 30 minutes
   - **Method:** `POST`
   - **Body:** `{"event_type":"tnp-check"}`
   - **Headers:**
     ```
     Accept: application/vnd.github+json
     Authorization: Bearer <YOUR_GITHUB_TOKEN>
     User-Agent: tnp-watcher
     Content-Type: application/json
     ```
3. Save and run once to confirm the workflow fires (check the Actions tab).

Done — you'll get a Telegram ping within ~30 min of any new company or notification.

---

## Commands reference

| Command | What it does |
|---|---|
| `python watcher.py` | Check both sources and alert on anything new. First run = silent baseline. |
| `python watcher.py --seed` | Re-baseline to "everything current is already seen" (no messages). |
| `python watcher.py --dump` | Log in and save the dashboard + newsevents HTML, and show what the parsers extract. |
| `python watcher.py --chatid` | Print Telegram chat ids the bot has seen (to fill `TELEGRAM_CHAT_ID`). |

## What a message looks like

```
🏢 New company (2026-27):
Cisco - apply by 08/07/2026 (posted 07/07/2026)
https://tp.bitmesra.co.in/job/info/caab3fb1512cc6ce84d565eda2a58b29
```
```
📰 New notification:
Tata Consultancy Services Ltd : INTERVIEWS - Technical Result ...  (07/07/2026 02:49 IST)
https://tp.bitmesra.co.in/job/notice/e1450483becf45ce334c11a8091e3094
```

## Troubleshooting

- **"Login failed"** — re-check credentials; the portal's `identity` field may want
  a roll number vs username. Ensure `.env` has no quotes/trailing spaces.
- **No Telegram messages** — run `python watcher.py --chatid` to confirm the chat id;
  make sure you messaged the bot (or posted in the group) at least once first.
- **Getting re-spammed** — run `python watcher.py --seed` once to re-baseline.
- **Wrong placement year** — set `TNP_PLACEMENT_YEAR` in `.env` / secrets.
