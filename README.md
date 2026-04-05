# Metabase Account Brief Agent

A Streamlit app that:
- accepts one account at a time
- gathers first-party website and public search signals
- uses Claude to generate a Metabase-specific account brief
- emails the brief to the requester
- logs the request and output to Google Sheets
