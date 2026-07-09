#!/usr/bin/env python3
"""
TNP Watcher — logs into the BIT Mesra T&P portal and sends a Telegram message
when something new appears. It tracks two sources:
  1. New companies in "Recent Jobs" for the configured placement year.
  2. New notifications in "News & Events".

Usage:
    python watcher.py            # normal run: check both sources + notify on new
    python watcher.py --dump     # login and save dashboard + newsevents HTML
    python watcher.py --seed     # mark everything currently visible as "seen"
                                 #   without sending any messages (reset baseline)
    python watcher.py --chatid   # print Telegram chat ids the bot has seen

Config comes from environment variables (see .env.example). Locally these are
read from a .env file; on GitHub Actions they come from encrypted Secrets.
"""

import os
import re
import sys
import json
import time
import hashlib
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv is optional; on CI the env vars are already set

# Make emoji-bearing prints safe on Windows' legacy cp1252 console.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BASE_URL = "https://tp.bitmesra.co.in"
LOGIN_PAGE = f"{BASE_URL}/login.html"
LOGIN_POST = f"{BASE_URL}/auth/login.html"

# Two sources we track after login:
#   1) Recent Jobs table on the dashboard (companies for the placement year)
#   2) News/Events notifications page
DASHBOARD_URL = f"{BASE_URL}/dashboard"
NEWSEVENTS_URL = f"{BASE_URL}/newsevents"
CHANGE_YEAR_URL = f"{BASE_URL}/change_placeyr"

# Placement year to scope the "Recent Jobs" list to (matches the site's dropdown).
PLACEMENT_YEAR = os.getenv("TNP_PLACEMENT_YEAR", "2026-27")

STATE_FILE = Path(__file__).parent / "state.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

USERNAME = os.getenv("TNP_USERNAME", "")
PASSWORD = os.getenv("TNP_PASSWORD", "")

# Telegram delivery. Create a bot via @BotFather to get the token, and use the
# numeric chat id of yourself or the group (see README).
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")


# --------------------------------------------------------------------------- #
# Portal client
# --------------------------------------------------------------------------- #
# The portal issues its session cookie with an INVALID name ("/"), e.g.
#   Set-Cookie: /=<hexvalue>; path=/; HttpOnly
# Python's requests (and curl) refuse to send an RFC-invalid cookie name back
# via the normal cookie jar, so on a *successful* login Ion Auth's
# sess_regenerate() finds no session and the server 500s. Browsers tolerate the
# bad name and echo it back. So we manage this one cookie by hand: capture its
# value from every Set-Cookie and replay it as a raw `Cookie: /=<value>` header.
class Portal:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT})
        self.sid = None  # value of the "/"-named session cookie

    def _capture_cookie(self, resp: requests.Response) -> None:
        try:
            headers = resp.raw.headers.getlist("Set-Cookie")
        except Exception:
            raw = resp.headers.get("Set-Cookie")
            headers = [raw] if raw else []
        for line in headers:
            m = re.search(r"(?:^|[;,\s])/=([A-Za-z0-9]+)", line or "")
            if m:
                self.sid = m.group(1)
        # Never trust the jar for the "/" cookie — we replay it manually.
        self.s.cookies.clear()

    def _hdrs(self, extra=None) -> dict:
        h = dict(extra or {})
        if self.sid:
            h["Cookie"] = f"/={self.sid}"
        return h

    def get(self, url: str, follow=True, **kw) -> requests.Response:
        r = self.s.get(url, headers=self._hdrs(), allow_redirects=False,
                        timeout=30, **kw)
        self._capture_cookie(r)
        if follow and r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location", "")
            if loc:
                return self.get(urljoin(url, loc), follow=follow)
        return r

    def post(self, url: str, data: dict, **kw) -> requests.Response:
        r = self.s.post(url, data=data, headers=self._hdrs(), timeout=30,
                        allow_redirects=False, **kw)
        self._capture_cookie(r)
        return r


def _is_login_page(html: str) -> bool:
    low = html.lower()
    return 'name="identity"' in low and 'name="password"' in low


def login(portal: Portal) -> None:
    """Authenticate against the Ion Auth login form. Raises on failure.

    The portal returns an EMPTY 200 for both success and failure, so we can't
    judge from the POST response — we verify by fetching a protected page and
    checking we aren't bounced back to the login form.
    """
    if not USERNAME or not PASSWORD:
        sys.exit("ERROR: TNP_USERNAME / TNP_PASSWORD are not set.")

    payload = {"identity": USERNAME, "password": PASSWORD, "submit": "Login"}

    for attempt in range(1, 4):  # retry transient 500s from sess_regenerate
        portal.get(LOGIN_PAGE, follow=False)          # prime session cookie
        resp = portal.post(LOGIN_POST, payload)       # submit credentials
        if resp.status_code == 500:
            print(f"  login attempt {attempt}: server 500 (session regen), retrying...")
            time.sleep(2)
            continue
        break

    # Verify by probing a protected page (redirects to login if unauthenticated).
    probe = portal.get(DASHBOARD_URL)
    if not _is_login_page(probe.text):
        set_placement_year(portal)
        return  # authenticated

    Path(__file__).parent.joinpath("dump_login_response.html").write_text(
        probe.text, encoding="utf-8"
    )
    raise RuntimeError(
        "Login failed — the portal bounced us back to the login page after "
        "submitting credentials.\n"
        "  Most likely: wrong username/password, or the 'identity' field wants a\n"
        "  different value (roll number vs username vs email).\n"
        "  Double-check the exact values you type in the browser, and ensure\n"
        "  .env has no quotes/trailing spaces. Saved the page to\n"
        "  dump_login_response.html."
    )


def set_placement_year(portal: Portal) -> None:
    """Force the session's placement year so 'Recent Jobs' shows the right year."""
    try:
        portal.post(CHANGE_YEAR_URL, {"_placeyr": PLACEMENT_YEAR})
    except requests.RequestException as e:
        print(f"WARN: could not set placement year {PLACEMENT_YEAR}: {e}")


# --------------------------------------------------------------------------- #
# Fetch + parse
# --------------------------------------------------------------------------- #
def parse_recent_jobs(html: str) -> list[dict]:
    """
    Extract company postings from the "Recent Jobs" table on the dashboard.

    Table markup (id="job-listings"):
        Company | Dead Line | Posted On | Action
    The Action cell links to job/info/<hash> and job/notice/<hash>; the <hash>
    is a stable per-posting id we use for deduplication.

    Returns a list of dicts:
        {"id": <hash>, "title": <human text>, "url": <apply link>}
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find(id="job-listings")
    items: list[dict] = []
    seen: set[str] = set()

    rows = []
    if table:
        body = table.find("tbody") or table
        rows = body.find_all("tr")

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        company = cells[0].get_text(" ", strip=True)
        deadline = cells[1].get_text(" ", strip=True)
        posted = cells[2].get_text(" ", strip=True)

        # Pull the stable hash from any job/info or job/notice link.
        job_hash, apply_url = None, ""
        for a in cells[-1].find_all("a", href=True):
            m = re.search(r"job/(?:info|notice)/([0-9a-f]{16,})", a["href"])
            if m:
                job_hash = m.group(1)
            if "job/info" in a["href"]:
                apply_url = _abs(a["href"])
        if not company or not job_hash or job_hash in seen:
            continue
        seen.add(job_hash)

        title = company
        if deadline:
            title += f" - apply by {deadline}"
        if posted:
            title += f" (posted {posted})"

        items.append({
            "id": f"job:{job_hash}",
            "title": title,
            "url": apply_url or f"{BASE_URL}/job/info/{job_hash}",
            "source": "job",
        })

    return items


def parse_newsevents(html: str) -> list[dict]:
    """
    Extract notifications from the /newsevents table.

    Table markup (id="newsevents"):  Info | Post Date

    IMPORTANT: the Info cell links to job/notice/<hash>, but that hash is the
    *company job's* id, shared by ALL of that company's notifications (shortlist,
    interview, result, ...). It therefore CANNOT identify a single notification.
    We key each notification on its own content (info text + post date), which
    each carry a distinct timestamp — otherwise new updates for a company already
    seen would be silently deduped away.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find(id="newsevents")
    items: list[dict] = []
    seen: set[str] = set()
    if not table:
        return items

    body = table.find("tbody") or table
    for row in body.find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue  # header/separator row only
        # Be maximally inclusive: capture every row that has ANY text, whatever
        # its shape. First cell is the notification text, second (if any) is the
        # post date; fall back to the whole row's text so nothing is ever missed.
        info = cells[0].get_text(" ", strip=True)
        date = cells[1].get_text(" ", strip=True) if len(cells) > 1 else ""
        if not info:
            info = row.get_text(" ", strip=True)
        if not info:
            continue  # genuinely empty row

        link = row.find("a", href=True)
        href = link["href"] if link else ""
        uid = _text_hash(info + "|" + date)  # per-notification, not per-job
        if uid in seen:
            continue
        seen.add(uid)

        title = info if not date else f"{info}  ({date})"
        items.append({
            "id": f"news:{uid}",
            "title": title,
            "url": _abs(href),
            "source": "news",
        })

    return items


def _text_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", "ignore")).hexdigest()[:16]


def _abs(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return f"{BASE_URL}/{href.lstrip('/')}"


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"seen": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Telegram delivery
# --------------------------------------------------------------------------- #
def notify(text: str) -> bool:
    """Send one Telegram message. Returns True only if it was actually delivered.

    Handles the group→supergroup migration transparently: when a basic group is
    upgraded, Telegram rejects the old chat id with a 400 that carries the NEW id
    in `parameters.migrate_to_chat_id`. We follow it once and update the id for
    the rest of this run so later messages go straight through. (Persisting the
    new id is still worth doing — see the printed NOTE — so future runs skip the
    retry.)
    """
    global TELEGRAM_CHAT
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT):
        print("WARN: Telegram not configured; printing instead:\n", text)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for attempt in range(2):
        payload = {
            "chat_id": TELEGRAM_CHAT,
            "text": text,
            "disable_web_page_preview": True,
        }
        try:
            r = requests.post(url, json=payload, timeout=30)
        except requests.RequestException as e:
            print(f"WARN: Telegram request failed: {e}")
            return False
        if r.status_code == 200:
            time.sleep(1)  # stay under Telegram's ~30 msg/sec limit with margin
            return True
        try:
            body = r.json()
        except ValueError:
            body = {}
        migrate = (body.get("parameters") or {}).get("migrate_to_chat_id")
        if migrate and attempt == 0:
            print(f"NOTE: chat migrated to supergroup {migrate}; retrying. "
                  f"Update TELEGRAM_CHAT_ID to {migrate} to skip this next time.")
            TELEGRAM_CHAT = str(migrate)
            continue
        print(f"WARN: Telegram returned {r.status_code}: {r.text[:200]}")
        return False
    return False


def show_chat_id() -> None:
    """Print chat IDs the bot has seen (via getUpdates), so you can find your own
    or a group's numeric chat id for TELEGRAM_CHAT_ID."""
    if not TELEGRAM_TOKEN:
        sys.exit("ERROR: set TELEGRAM_BOT_TOKEN first.")
    data = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates", timeout=30
    ).json()
    if not data.get("ok"):
        sys.exit(f"Telegram error: {data}")
    results = data.get("result", [])

    # A chat can surface via many update kinds — scan them all.
    seen = {}
    for u in results:
        for key in ("message", "edited_message", "channel_post",
                    "my_chat_member", "chat_member"):
            chat = (u.get(key) or {}).get("chat", {})
            if chat.get("id") is not None:
                name = (chat.get("title") or chat.get("username")
                        or chat.get("first_name") or "")
                seen[chat["id"]] = f"{name} ({chat.get('type')})"

    if not seen:
        print(
            "No chats seen yet. Do ONE of these, then run --chatid again:\n"
            "  • DM: open your bot, TYPE and send it a message (e.g. 'hi').\n"
            "  • Group: mention the bot or send  /start@<yourbotusername>  in it\n"
            "    (bots don't see ordinary group messages unless you disable\n"
            "    privacy mode in @BotFather → /setprivacy → Disable, then re-add\n"
            "    the bot to the group)."
        )
        return
    print("Chats the bot has seen:\n")
    for cid, label in seen.items():
        print(f"  {label}\n     TELEGRAM_CHAT_ID={cid}\n")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def collect_items(portal: Portal) -> list[dict]:
    """Gather items from both tracked sources: Recent Jobs + News/Events."""
    items: list[dict] = []

    jobs = parse_recent_jobs(portal.get(DASHBOARD_URL).text)
    items.extend(jobs)
    print(f"  recent jobs ({PLACEMENT_YEAR}): {len(jobs)}")

    news = parse_newsevents(portal.get(NEWSEVENTS_URL).text)
    items.extend(news)
    print(f"  news/events: {len(news)}")

    return items


def _format(it: dict) -> str:
    if it.get("source") == "job":
        head = f"🏢 New company ({PLACEMENT_YEAR})"
    else:
        head = "📰 New notification"
    body = f"{head}:\n{it['title']}"
    if it.get("url"):
        body += f"\n{it['url']}"
    return body


def run(seed_only: bool = False) -> None:
    portal = Portal()
    login(portal)
    items = collect_items(portal)

    if not items:
        print("No items parsed. Run with --dump to inspect the page structure.")
        return

    state = load_state()
    seen = set(state.get("seen", []))
    current_ids = [it["id"] for it in items]
    new_items = [it for it in items if it["id"] not in seen]

    first_run = not seen
    if first_run or seed_only:
        # Baseline: record everything, don't spam existing items.
        save_state({"seen": current_ids})
        msg = (
            f"✅ TNP Watcher is active. Tracking {len(items)} existing items "
            f"(companies for {PLACEMENT_YEAR} + notifications). "
            f"You'll be pinged here whenever a NEW one appears."
        )
        print(msg)
        if not seed_only:
            notify(msg)
        return

    if not new_items:
        print(f"No new items ({len(items)} tracked).")
        return

    print(f"{len(new_items)} new item(s) found.")
    # Notify oldest→newest so messages arrive in chronological order.
    delivered: list[str] = []
    failed = 0
    for it in reversed(new_items):
        print(" ->", it["title"][:80])
        if notify(_format(it)):
            delivered.append(it["id"])
        else:
            failed += 1

    # Persist ONLY what we actually delivered. If Telegram delivery is broken
    # (bad chat id, bot lacks post rights, network), we must NOT mark undelivered
    # items as "seen" — otherwise they're lost forever once delivery is restored.
    # We also keep every current job/news id we DID see AND could deliver, plus
    # the prior baseline, so we never re-alert on things already sent.
    save_state({"seen": list(seen.union(delivered))})

    if failed:
        # Exit non-zero so a delivery outage shows up as a red run in CI instead
        # of silently passing — and so the items are retried on the next run.
        sys.exit(f"ERROR: {failed} of {len(new_items)} message(s) failed to send. "
                 "Nothing was marked seen for those; they will retry next run.")


def dump() -> None:
    portal = Portal()
    login(portal)
    print("Login OK ✔")
    for name, url in [("dashboard", DASHBOARD_URL), ("newsevents", NEWSEVENTS_URL)]:
        resp = portal.get(url)
        out = Path(__file__).parent / f"dump_{name}.html"
        out.write_text(resp.text, encoding="utf-8")
        print(f"  {name}: status {resp.status_code}, saved {len(resp.text)} bytes -> {out.name}")
    # Show what the parsers currently extract.
    jobs = parse_recent_jobs((Path(__file__).parent / "dump_dashboard.html").read_text(encoding="utf-8"))
    news = parse_newsevents((Path(__file__).parent / "dump_newsevents.html").read_text(encoding="utf-8"))
    print(f"\nParsed: {len(jobs)} recent jobs, {len(news)} notifications")
    for it in (jobs[:3] + news[:3]):
        print(f"  [{it['source']}] {it['title'][:80]}")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--dump":
        dump()
    elif arg == "--seed":
        run(seed_only=True)
    elif arg == "--chatid":
        show_chat_id()
    else:
        run()
