# Lead Research Bot

A Telegram bot for an agency sales team. Type a niche and a location, get back
businesses from Google Maps with phone, website, social links, emails, rating,
and a ready-to-send outreach message. Team members then send the message from
their own WhatsApp or Instagram. The bot never automates outreach, so there is
no account-ban risk.

## How it works

1. You send: `dental clinics in Andheri Mumbai`
   The first time, the bot asks how many businesses you want (1 to 60). Change it later with `/count`.
2. The bot calls Google Places API (New) Text Search, paging as needed:
   `POST https://places.googleapis.com/v1/places:searchText`
3. For every business with a website, the bot fetches the site and pulls out
   emails and Instagram / Facebook / LinkedIn / YouTube / TikTok links.
4. Results are cached in SQLite for 30 days, so repeat searches are free.
5. Tap **Draft messages** to get a short personalised WhatsApp message per lead,
   with a `wa.me` link that opens the chat on your phone with the text filled in.
   Drafts are written by Claude or Gemini. Each team member can add their own key
   in the chat with `/setkey gemini KEY` or `/setkey anthropic KEY`. With no key,
   built-in templates are used.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env          # then fill in the values
python -m bot.main
```

### Keys you need

| Variable | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram, message @BotFather, `/newbot` |
| `GOOGLE_PLACES_API_KEY` | Google Cloud Console. Enable **Places API (New)**, create an API key, restrict it to that API. Billing must be enabled even for free-tier use. |
| `ANTHROPIC_API_KEY` | Optional server-wide key from console.anthropic.com. |
| `GEMINI_API_KEY` | Optional server-wide key from aistudio.google.com. |
| `ALLOWED_USER_IDS` | Optional. Your team's Telegram user IDs, comma separated. Get yours from @userinfobot. |

## Google Places cost

The field mask in `bot/places.py` requests phone, website, and rating. On the
Text Search endpoint those fields bill under the **Enterprise** SKU.

| | |
|---|---|
| Free calls per month | 1,000 |
| Businesses per call | up to 20 |
| Free leads per month | around 20,000 |
| Price after free tier | about $35 per 1,000 calls |

Set a budget alert in Google Cloud. Repeat searches and "More results" pages
each count as one call, but cached businesses are never re-enriched.

## Commands

| Input | Result |
|---|---|
| any text | search Google Maps |
| a bare number | answers "how many results?" or sets the count |
| `/count 15` | businesses per search (1–60) |
| `/more` or button | next batch of the same search |
| `/drafts` or button | outreach messages for the current results |
| `/setkey gemini KEY` | store your own Gemini key (message is deleted after) |
| `/setkey anthropic KEY` | store your own Claude key |
| `/provider gemini` | choose anthropic, gemini, or template |
| `/settings` | show your count, provider, and masked keys |
| `save 3` | save lead number 3 |
| `/saved` | list saved leads |
| `/history` | recent searches |

## Project layout

```
bot/
  main.py       Telegram handlers and flow
  places.py     Places API (New) Text Search client
  enrich.py     website scraping for emails and social links
  ai.py         message drafting (Claude or Gemini, template fallback)
  formatter.py  Telegram HTML output and wa.me links
  db.py         SQLite cache, history, saved leads, sessions
  config.py     environment settings
```

## Deploy on Railway

The repo contains a `Dockerfile` and `railway.json`, so Railway builds it with no
extra configuration. The bot is a background worker with no HTTP port.

1. Push this repo to GitHub.
2. Railway: **New Project → Deploy from GitHub repo**, pick the repo.
3. **Variables** tab: add `TELEGRAM_BOT_TOKEN`, `GOOGLE_PLACES_API_KEY`,
   `GEMINI_API_KEY` (or `ANTHROPIC_API_KEY`), `ALLOWED_USER_IDS`, `AGENCY_NAME`,
   and `DB_PATH=/data/leads.db`.
4. **Settings → Volumes → Add Volume**, mount path `/data`. Without this the
   SQLite cache, saved leads and user keys are lost on every redeploy.
5. Deploy. The **Logs** tab should show `Run polling for bot @...`.

## Next steps

- Add SMTP email sending with a review step, once the domain has SPF, DKIM and DMARC.
- Export saved leads to CSV or Google Sheets.
- Add a "skip businesses I already contacted" filter.

## Contact

Built by Susanta Sahoo. Reach out for collaboration, custom bots, or agency work.

- Website: https://iamsusantasahoo.github.io/
- Email: sahoosusantaku2@gmail.com
- Phone / WhatsApp: +91 99376 81391
