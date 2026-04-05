# Metabase Account Research Agent

A Streamlit app that lets an AE submit one account at a time, researches the account with Claude + Bright Data MCP, generates a Metabase-specific outbound brief, emails the brief to the requester, and logs the result to Google Sheets.

## What it does

Input:
- requester name
- requester email
- company name
- website
- optional AE notes

Output:
- 1 to 3 GTM signals
- why each signal matters for Metabase
- ICP score and reasoning
- likely persona
- core pain
- messaging angle
- two outbound email drafts
- automatic email to the requester
- automatic logging to Google Sheets

## Files

- `streamlit_app.py` - main Streamlit app
- `requirements.txt` - Python dependencies
- `.streamlit/secrets.toml.example` - secrets template
- `.streamlit/config.toml` - small UI defaults

## Local setup

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2. Make sure Node.js and npx are installed

This app uses Bright Data MCP in local/stdIO mode via:

```bash
npx -y @brightdata/mcp
```

### 3. Create your local secrets file

Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and fill in your values.

### 4. Run the app

```bash
streamlit run streamlit_app.py
```

## Streamlit Community Cloud deployment

1. Push these files to GitHub.
2. Create a new Streamlit app and point it at `streamlit_app.py`.
3. In the app settings, add your secrets from `.streamlit/secrets.toml.example`.
4. Deploy.

## Required secrets

```toml
ANTHROPIC_API_KEY = ""
BRIGHTDATA_API_TOKEN = ""
BRIGHTDATA_WEB_UNLOCKER_ZONE = ""
BRIGHTDATA_BROWSER_AUTH = ""
GOOGLE_SERVICE_ACCOUNT_JSON = '{...}'
GOOGLE_SHEET_ID = ""
SMTP_USERNAME = ""
SMTP_PASSWORD = ""
SMTP_SENDER_EMAIL = ""
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = "587"
CLAUDE_MODEL = "claude-sonnet-4-6"
```

## Google Sheets setup

1. Create a Google Sheet.
2. Copy the sheet ID from the URL.
3. Share the sheet with your service account email as Editor.
4. Put the full service account JSON into `GOOGLE_SERVICE_ACCOUNT_JSON`.

The app will automatically create a tab called `researched_accounts` if it doesn't already exist.

## SMTP setup

If you use Gmail:
- use your Gmail address for `SMTP_USERNAME`
- create an app password for `SMTP_PASSWORD`
- use the same Gmail address for `SMTP_SENDER_EMAIL`

