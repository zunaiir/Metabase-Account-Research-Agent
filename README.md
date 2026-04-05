# Metabase Account Brief Agent

A Streamlit app that:
- accepts one account at a time
- uses Bright Data Google SERP to gather account signals
- uses Claude to generate a Metabase-specific account brief
- emails the brief to the requester
- logs the request and output to Google Sheets

## Required Streamlit secrets

```toml
ANTHROPIC_API_KEY="..."
BRIGHTDATA_API_TOKEN="..."
BRIGHTDATA_SERP_ZONE="..."
GOOGLE_SERVICE_ACCOUNT_JSON='{"type":"service_account",...}'
GOOGLE_SHEET_ID="..."
SMTP_USERNAME="you@gmail.com"
SMTP_PASSWORD="your_app_password"
SMTP_SENDER_EMAIL="you@gmail.com"
SMTP_HOST="smtp.gmail.com"
SMTP_PORT="587"
CLAUDE_MODEL="claude-3-5-sonnet-latest"
```

## Local run

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

## Google Sheet setup

Share your Google Sheet with the service account email from your Google service account JSON.


