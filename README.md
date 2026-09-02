# visa

A Playwright script that watches the French consulate (Rome) appointment
booking site and alerts you the moment a slot opens up.

It automates the exact manual flow a human would follow — click through to
the service page, set applicant/service counts, accept the terms, check the
calendar — and polls with randomized delays and a bit of human-like scrolling
so it doesn't hammer the site like a bot. When Cloudflare's human check shows
up, it pauses and asks you to solve it by hand; it does not try to bypass it.

## Setup

```bash
pip install playwright requests
playwright install chromium
```

## Usage

```bash
python monitor_slots_conservative.py \
  --telegram-bot-token YOUR_BOT_TOKEN \
  --telegram-chat-id YOUR_CHAT_ID
```

Both Telegram arguments are optional — without them you just get console
output, a terminal bell, and a Windows popup when a slot appears. Run
`--help` for the rest of the flags (headless mode, poll delay, browser
profile directory, etc).

The browser window stays open when a slot is found so you can complete the
booking yourself — the script does not attempt to submit the final
reservation.
