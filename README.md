# Site Watch

Checks web pages every hour and alerts on Teams and email when a NEW link appears
that matches your keywords (e.g. a GEA-7 notice on the DOE site). Runs free on GitHub Actions.

## How it works
1. `monitor.py` reads each watcher in `watchers.yaml`, downloads the page and collects every link.
2. It compares them with the links it saw last time (`state/seen.json`).
3. New links whose title or URL contains a keyword are sent to Teams and/or email.
4. If a page fails 3 checks in a row (site down, or layout changed so no links are found),
   you get a "monitor broken" alert, then a "working again" alert when it recovers.

The first run of each watcher only records the existing links, so you don't get flooded.

## Setup (about 20 minutes)

1. **Create a private GitHub repo** and upload these files, keeping the folder structure
   (including `.github/workflows/site-watch.yml` and `state/seen.json`).

2. **Teams webhook.** In Teams, open the target channel > "..." > Workflows >
   "Send webhook alerts to a channel". Save, then copy the URL it gives you.

3. **Add repo secrets** (Settings > Secrets and variables > Actions > New repository secret):

   | Secret | Needed for | Example |
   |---|---|---|
   | `TEAMS_WEBHOOK_URL` | Teams | the Workflows URL from step 2 |
   | `EMAIL_TO` | Email | `you@company.com, colleague@company.com` |
   | `SMTP_HOST` | Email | `smtp.office365.com` |
   | `SMTP_PORT` | Email | `587` |
   | `SMTP_USER` | Email | sending mailbox address |
   | `SMTP_PASSWORD` | Email | that mailbox's password or app password |
   | `EMAIL_FROM` | Email (optional) | defaults to `SMTP_USER` |

   Skip the email secrets if you only want Teams.

4. **Check the target URL.** Open the URL in `watchers.yaml` in a browser and confirm it
   lists the posts you care about. Update it if needed.

5. **Test.** Actions tab > Site Watch > Run workflow > tick "Send a test alert". You should
   get a test message in Teams and email.

6. **Start it.** Run the workflow once normally to save the baseline. After that it runs every hour.

## Adding another site
Add a block to `watchers.yaml`:

```yaml
  - name: erc-news
    description: ERC news releases
    url: https://example.gov/news
    keywords: [green energy auction, GEAR price]
    selector: div.view-content          # optional: only read links in the list area
    email_to: [someone@company.com]     # optional: extra recipients
    teams_webhook_secret: TEAMS_WEBHOOK_ERC   # optional: different Teams channel
```

If you use a different Teams channel, create its webhook and add it as a repo secret with
the name you put in `teams_webhook_secret`. No workflow changes needed.

Tip: if a site sends noisy alerts, set `selector` to the CSS selector of the news/notice list
(right-click the list in Chrome > Inspect, and look for the class on the wrapping `div`).

## Running locally
```
pip install -r requirements.txt
TEAMS_WEBHOOK_URL=... python monitor.py --dry-run          # print what would alert, save nothing
python monitor.py --only doe-geap-gea7 --dry-run           # one watcher
python monitor.py --test-notify                            # send test alerts
```

## Limits and known issues
- Works on plain HTML pages. Pages that load their content with JavaScript will show
  "no links found"; those need a headless-browser version.
- It detects new links, not edits to text on an existing page.
- Some government sites block cloud servers. If a watcher keeps failing on GitHub but the
  page opens fine in your browser, that's the likely cause.
- Hourly runs use roughly 720 Actions minutes a month. Check this against your GitHub plan's
  allowance for private repos.
- Office 365 often has SMTP sign-in disabled. If email fails with an authentication error,
  either ask IT to enable SMTP AUTH for the sending mailbox, or skip SMTP and add a
  "Send an email (V2)" step to the Teams Workflow from step 2 so one webhook sends both.
