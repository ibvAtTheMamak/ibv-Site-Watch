#!/usr/bin/env python3
"""Site Watch: checks web pages for NEW links that match keywords and alerts via Teams and email.

Each page to monitor is a "watcher" in watchers.yaml. No code changes are needed to add a site.
"""
import argparse
import html
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urldefrag, urljoin

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "watchers.yaml"
STATE_FILE = ROOT / "state" / "seen.json"
USER_AGENT = "Mozilla/5.0 (compatible; SiteWatch/1.0)"
MAX_SEEN = 3000          # links remembered per watcher
MAX_ITEMS_IN_ALERT = 15


# ---------- config and state ----------

def load_watchers(path):
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    defaults = cfg.get("defaults") or {}
    watchers = []
    for w in cfg.get("watchers") or []:
        merged = {**defaults, **w}
        if not merged.get("name") or not merged.get("url"):
            raise ValueError(f"Every watcher needs a name and url: {w}")
        if merged.get("enabled", True):
            watchers.append(merged)
    return watchers


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8") or "{}")
    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def secret(name):
    """Read a secret from env vars, or from SECRETS_JSON (all repo secrets, passed by the workflow)."""
    if not name:
        return None
    if os.environ.get(name):
        return os.environ[name]
    try:
        return json.loads(os.environ.get("SECRETS_JSON", "{}")).get(name)
    except json.JSONDecodeError:
        return None


# ---------- fetching and parsing ----------

def fetch(url, verify_ssl=True, retries=3):
    last_error = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30, verify=verify_ssl)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            last_error = e
            time.sleep(5 * (attempt + 1))
    raise last_error


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def extract_links(page_html, base_url, selector=None, href_pattern=None):
    soup = BeautifulSoup(page_html, "html.parser")
    scopes = soup.select(selector) if selector else [soup]
    pattern = re.compile(href_pattern, re.I) if href_pattern else None
    links = {}  # url -> best title
    for scope in scopes:
        for a in scope.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
                continue
            url = urldefrag(urljoin(base_url, href))[0]
            if pattern and not pattern.search(url):
                continue
            title = clean(a.get_text(" ")) or clean(a.get("title")) or url
            if len(title) > len(links.get(url, "")):
                links[url] = title
    return [{"url": u, "title": t} for u, t in links.items()]


def normalise(text):
    # "GEA-7", "GEA 7" and "gea_7" all become "gea 7"
    return re.sub(r"\s+", " ", re.sub(r"[-_\u2013\u2014/]+", " ", text.lower())).strip()


def matches(item, keywords, regex):
    if not keywords and not regex:
        return True
    haystack = normalise(f"{item['title']} {item['url']}")
    if regex and re.search(regex, haystack, re.I):
        return True
    return any(normalise(k) in haystack for k in keywords or [])


# ---------- notifications ----------

def send_teams(webhook, title, source_url, items, note=None):
    body = [
        {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True},
        {"type": "TextBlock", "text": f"Source: [{source_url}]({source_url})", "isSubtle": True, "wrap": True},
    ]
    if note:
        body.append({"type": "TextBlock", "text": note, "wrap": True})
    for it in items[:MAX_ITEMS_IN_ALERT]:
        body.append({"type": "TextBlock", "text": f"- [{it['title']}]({it['url']})", "wrap": True})
    if len(items) > MAX_ITEMS_IN_ALERT:
        body.append({"type": "TextBlock", "text": f"...and {len(items) - MAX_ITEMS_IN_ALERT} more", "wrap": True})
    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard", "version": "1.4", "body": body,
            },
        }],
    }
    r = requests.post(webhook, json=payload, timeout=30)
    r.raise_for_status()


def send_email(recipients, title, source_url, items, note=None):
    host = secret("SMTP_HOST")
    if not host:
        raise RuntimeError("SMTP_HOST secret not set")
    lines = [f"- {it['title']}\n  {it['url']}" for it in items]
    text = f"{title}\nSource: {source_url}\n\n" + (f"{note}\n\n" if note else "") + "\n".join(lines)
    rows = "".join(f'<li><a href="{html.escape(it["url"])}">{html.escape(it["title"])}</a></li>' for it in items)
    html_body = (f"<p><b>{html.escape(title)}</b><br>Source: <a href=\"{html.escape(source_url)}\">"
                 f"{html.escape(source_url)}</a></p>" + (f"<p>{html.escape(note)}</p>" if note else "")
                 + f"<ul>{rows}</ul>")

    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = secret("EMAIL_FROM") or secret("SMTP_USER")
    msg["To"] = ", ".join(recipients)
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(host, int(secret("SMTP_PORT") or 587), timeout=30) as s:
        s.starttls()
        if secret("SMTP_USER"):
            s.login(secret("SMTP_USER"), secret("SMTP_PASSWORD"))
        s.send_message(msg)


def notify(watcher, title, items, note=None, dry_run=False):
    """Send to every configured channel. Returns True if at least one channel worked (or none is set)."""
    names = watcher.get("teams_webhook_secrets") or [watcher.get("teams_webhook_secret") or "TEAMS_WEBHOOK_URL"]
    webhooks = [u for u in (secret(n) for n in names) if u]
    recipients = list(watcher.get("email_to") or [])
    recipients += [e.strip() for e in (secret("EMAIL_TO") or "").split(",") if e.strip()]
    recipients = sorted(set(recipients))

    print(f"  ALERT: {title}")
    for it in items:
        print(f"    - {it['title']} | {it['url']}")
    if dry_run:
        return True

    attempted, delivered = 0, 0
    for hook in webhooks:
        attempted += 1
        try:
            send_teams(hook, title, watcher["url"], items, note)
            delivered += 1
        except Exception as e:
            print(f"  Teams send failed: {e}")
    if recipients:
        attempted += 1
        try:
            send_email(recipients, title, watcher["url"], items, note)
            delivered += 1
        except Exception as e:
            print(f"  Email send failed: {e}")
    if attempted == 0:
        print("  No Teams webhook or email recipients configured; logged only.")
        return True
    return delivered > 0


# ---------- main loop ----------

def run_watcher(w, state, dry_run=False):
    name = w["name"]
    s = state.setdefault(name, {})
    threshold = int(w.get("alert_after_failures", 3))
    print(f"[{name}] {w['url']}")

    try:
        page = fetch(w["url"], verify_ssl=w.get("verify_ssl", True))
        links = extract_links(page, w["url"], w.get("selector"), w.get("href_pattern"))
        if not links:
            raise RuntimeError("page loaded but no links were found (layout or selector may have changed)")
    except Exception as e:
        s["failures"] = s.get("failures", 0) + 1
        print(f"  Check failed ({s['failures']} in a row): {e}")
        if s["failures"] == threshold:
            notify(w, f"Site Watch: '{name}' has failed {threshold} checks in a row", [],
                   note=f"Last error: {e}. Alerts for this site are paused until it recovers.", dry_run=dry_run)
        return

    if s.get("failures", 0) >= threshold:
        notify(w, f"Site Watch: '{name}' is working again", [], dry_run=dry_run)
    s["failures"] = 0
    s["last_ok"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    seen = s.get("seen")
    current_urls = [l["url"] for l in links]
    if seen is None:
        s["seen"] = current_urls[-MAX_SEEN:]
        print(f"  First run: saved {len(links)} existing links as the baseline. No alert sent.")
        return

    seen_set = set(seen)
    new_links = [l for l in links if l["url"] not in seen_set]
    hits = [l for l in new_links if matches(l, w.get("keywords"), w.get("regex"))]
    print(f"  {len(links)} links, {len(new_links)} new, {len(hits)} matching")

    if hits:
        label = w.get("description") or name
        delivered = notify(w, f"New on {label}: {len(hits)} matching item(s)", hits, dry_run=dry_run)
        if not delivered:
            # keep the matching links unseen so the next run retries the alert
            new_links = [l for l in new_links if l not in hits]
    s["seen"] = (seen + [l["url"] for l in new_links])[-MAX_SEEN:]


def main():
    ap = argparse.ArgumentParser(description="Alert on new links matching keywords.")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--only", help="run a single watcher by name")
    ap.add_argument("--dry-run", action="store_true", help="print alerts, send nothing, save nothing")
    ap.add_argument("--test-notify", action="store_true", help="send a test alert for each watcher and exit")
    args = ap.parse_args()

    watchers = [w for w in load_watchers(args.config) if not args.only or w["name"] == args.only]
    if not watchers:
        print("No matching enabled watchers.")
        return 1

    if args.test_notify:
        ok = all(notify(w, f"Site Watch test for '{w['name']}'",
                        [{"title": "Test link", "url": w["url"]}], note="If you can see this, alerts work.")
                 for w in watchers)
        return 0 if ok else 1

    state = load_state()
    errors = 0
    for w in watchers:
        try:
            run_watcher(w, state, args.dry_run)
        except Exception as e:  # unexpected bug: keep going, fail the run at the end
            errors += 1
            print(f"  Unexpected error in '{w['name']}': {e}")
    if not args.dry_run:
        save_state(state)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
