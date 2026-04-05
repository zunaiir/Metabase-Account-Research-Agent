import os
import json
import asyncio
import smtplib
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Dict, List

import streamlit as st

try:
    import gspread
    from google.oauth2.service_account import Credentials
except Exception:
    gspread = None
    Credentials = None

try:
    from claude_agent_sdk import query, ClaudeAgentOptions, AssistantMessage, TextBlock
except Exception:
    query = None
    ClaudeAgentOptions = None
    AssistantMessage = None
    TextBlock = None

APP_TITLE = "Metabase Account Research Agent"
SHEET_TAB_NAME = "researched_accounts"


def get_secret(name: str, default: str = "") -> str:
    if name in st.secrets:
        value = st.secrets[name]
        return str(value).strip() if value is not None else default
    return os.getenv(name, default).strip()


def parse_json_response(text: str) -> Dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


AGENT_PROMPT = """
You are a GTM engineer building outbound strategy for Metabase.

Your job is to research a company using MCP web tools and determine if there is a strong reason to reach out based on real signals.

Metabase is best for:
- B2B SaaS, fintech, ecommerce, developer tools, logistics, and modern technology companies
- teams growing into analytics complexity
- companies needing self-serve analytics
- companies embedding analytics into products
- teams struggling with dashboard sprawl or data access across functions

Tasks:
1. Identify up to 3 meaningful signals:
   - Hiring
   - Funding
   - Product Growth
   - Org Change
   - Analytics relevance
2. For each signal:
   - describe it clearly
   - explain why it matters for Metabase specifically
3. Score ICP fit from 1 to 4:
   - 1 = weak fit
   - 2 = possible fit
   - 3 = good fit
   - 4 = strong fit
4. Explain ICP reasoning briefly
5. Generate:
   - target persona
   - core pain
   - messaging angle
6. Write two outbound emails:
   - Email A: efficiency / speed angle
   - Email B: governance / consistency angle

Return ONLY valid JSON with this exact schema:
{
  "company": "",
  "website": "",
  "signals": [
    {
      "type": "",
      "description": "",
      "why_it_matters": ""
    }
  ],
  "signal_summary": "",
  "icp_score": 0,
  "icp_reasoning": "",
  "target_persona": "",
  "core_pain": "",
  "messaging_angle": "",
  "email_a": "",
  "email_b": "",
  "sources": [""]
}

Rules:
- Be skeptical, not optimistic
- If no strong signal exists, say so clearly
- Keep output concise and practical
- Think like a real AE preparing outreach
""".strip()


async def run_agent(company: str, website: str, notes: str) -> Dict[str, Any]:
    if query is None or ClaudeAgentOptions is None:
        raise RuntimeError(
            "claude-agent-sdk is not installed. Install requirements.txt first."
        )

    api_token = get_secret("BRIGHTDATA_API_TOKEN")
    web_unlocker_zone = get_secret("BRIGHTDATA_WEB_UNLOCKER_ZONE")
    browser_auth = get_secret("BRIGHTDATA_BROWSER_AUTH")

    missing = [
        name
        for name, value in {
            "BRIGHTDATA_API_TOKEN": api_token,
            "BRIGHTDATA_WEB_UNLOCKER_ZONE": web_unlocker_zone,
            "BRIGHTDATA_BROWSER_AUTH": browser_auth,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing required secrets: {', '.join(missing)}")

    prompt = f"""
{AGENT_PROMPT}

Research this account and return the required JSON.

Company: {company}
Website: {website}
AE Notes: {notes}
""".strip()

    # Local/stdIO MCP mode based on the Bright Data example repo the user shared.
    options = ClaudeAgentOptions(
        model=get_secret("CLAUDE_MODEL", "claude-sonnet-4-6"),
        max_turns=12,
        permission_mode="bypassPermissions",
        mcp_servers={
            "brightdata": {
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "@brightdata/mcp"],
                "env": {
                    "API_TOKEN": api_token,
                    "WEB_UNLOCKER_ZONE": web_unlocker_zone,
                    "BROWSER_AUTH": browser_auth,
                },
            }
        },
    )

    chunks: List[str] = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)

    if not chunks:
        raise RuntimeError("Claude returned no text output.")

    return parse_json_response("\n".join(chunks))


async def research_account(company: str, website: str, notes: str) -> Dict[str, Any]:
    return await run_agent(company, website, notes)


def get_gspread_client() -> "gspread.Client":
    if gspread is None or Credentials is None:
        raise RuntimeError("gspread/google-auth is not installed.")

    raw_json = get_secret("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw_json:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON secret.")

    info = json.loads(raw_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def log_to_sheets(payload: Dict[str, Any]) -> None:
    sheet_id = get_secret("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise RuntimeError("Missing GOOGLE_SHEET_ID secret.")

    client = get_gspread_client()
    workbook = client.open_by_key(sheet_id)

    try:
        worksheet = workbook.worksheet(SHEET_TAB_NAME)
    except Exception:
        worksheet = workbook.add_worksheet(title=SHEET_TAB_NAME, rows=1000, cols=24)
        worksheet.append_row(
            [
                "timestamp",
                "requester_name",
                "requester_email",
                "company",
                "website",
                "ae_notes",
                "signals",
                "signal_summary",
                "icp_score",
                "icp_reasoning",
                "target_persona",
                "core_pain",
                "messaging_angle",
                "email_a",
                "email_b",
                "sources",
            ]
        )

    signal_text = " | ".join(
        [
            f"{signal.get('type', '')}: {signal.get('description', '')} — {signal.get('why_it_matters', '')}"
            for signal in payload.get("signals", [])
        ]
    )

    worksheet.append_row(
        [
            datetime.utcnow().isoformat(),
            payload.get("requester_name", ""),
            payload.get("requester_email", ""),
            payload.get("company", ""),
            payload.get("website", ""),
            payload.get("ae_notes", ""),
            signal_text,
            payload.get("signal_summary", ""),
            payload.get("icp_score", ""),
            payload.get("icp_reasoning", ""),
            payload.get("target_persona", ""),
            payload.get("core_pain", ""),
            payload.get("messaging_angle", ""),
            payload.get("email_a", ""),
            payload.get("email_b", ""),
            " | ".join(payload.get("sources", [])),
        ]
    )


def send_email_report(result: Dict[str, Any]) -> None:
    smtp_host = get_secret("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(get_secret("SMTP_PORT", "587"))
    smtp_username = get_secret("SMTP_USERNAME")
    smtp_password = get_secret("SMTP_PASSWORD")
    sender_email = get_secret("SMTP_SENDER_EMAIL", smtp_username)
    recipient = result.get("requester_email", "").strip()

    if not smtp_username or not smtp_password or not sender_email:
        raise RuntimeError(
            "Missing SMTP_USERNAME, SMTP_PASSWORD, or SMTP_SENDER_EMAIL secret."
        )
    if not recipient:
        raise RuntimeError("Requester email is required to send the brief.")

    signal_lines = []
    for signal in result.get("signals", []):
        signal_lines.append(
            f"- {signal.get('type', '')}: {signal.get('description', '')}\n  Why it matters: {signal.get('why_it_matters', '')}"
        )

    msg = EmailMessage()
    msg["Subject"] = f"Metabase Account Brief: {result.get('company', '')}"
    msg["From"] = sender_email
    msg["To"] = recipient
    msg.set_content(
        f"""
Requester: {result.get('requester_name', '')}
Company: {result.get('company', '')}
Website: {result.get('website', '')}

ICP Score: {result.get('icp_score', '')}
ICP Reasoning:
{result.get('icp_reasoning', '')}

Signal Summary:
{result.get('signal_summary', '')}

Signals:
{"\n".join(signal_lines)}

Target Persona:
{result.get('target_persona', '')}

Core Pain:
{result.get('core_pain', '')}

Messaging Angle:
{result.get('messaging_angle', '')}

Email A:
{result.get('email_a', '')}

Email B:
{result.get('email_b', '')}

Sources:
- """ + "\n- ".join(result.get("sources", []))
    )

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_username, smtp_password)
        server.send_message(msg)


def render_output(result: Dict[str, Any]) -> None:
    st.subheader("Research Output")

    top_left, top_right = st.columns([1, 2])
    with top_left:
        st.metric("ICP Score", result.get("icp_score", "-"))
        st.write("**Target Persona**")
        st.write(result.get("target_persona", ""))
        st.write("**Core Pain**")
        st.write(result.get("core_pain", ""))

    with top_right:
        st.write("**ICP Reasoning**")
        st.write(result.get("icp_reasoning", ""))
        st.write("**Messaging Angle**")
        st.write(result.get("messaging_angle", ""))
        st.write("**Signal Summary**")
        st.write(result.get("signal_summary", ""))

    st.write("**Signals**")
    for signal in result.get("signals", []):
        st.markdown(f"**{signal.get('type', '')}**")
        st.write(signal.get("description", ""))
        st.caption(signal.get("why_it_matters", ""))

    email_left, email_right = st.columns(2)
    with email_left:
        st.write("**Email A — Efficiency**")
        st.code(result.get("email_a", ""), language="text")
    with email_right:
        st.write("**Email B — Governance**")
        st.code(result.get("email_b", ""), language="text")

    st.write("**Sources**")
    for source in result.get("sources", []):
        st.write(f"- {source}")

    with st.expander("Raw JSON"):
        st.json(result)


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)
    st.caption(
        "Research one account with Claude + Bright Data MCP, generate a Metabase-specific outbound brief, email it to the requester, and auto-log results to Google Sheets."
    )

    with st.sidebar:
        st.subheader("Secrets needed")
        st.code(
            "\n".join(
                [
                    "ANTHROPIC_API_KEY",
                    "BRIGHTDATA_API_TOKEN",
                    "BRIGHTDATA_WEB_UNLOCKER_ZONE",
                    "BRIGHTDATA_BROWSER_AUTH",
                    "GOOGLE_SERVICE_ACCOUNT_JSON",
                    "GOOGLE_SHEET_ID",
                    "SMTP_USERNAME",
                    "SMTP_PASSWORD",
                    "SMTP_SENDER_EMAIL",
                    "SMTP_HOST  # optional",
                    "SMTP_PORT  # optional",
                    "CLAUDE_MODEL  # optional",
                ]
            ),
            language="bash",
        )
        st.info(
            "This version uses Bright Data's local/stdIO MCP pattern via npx. If your deployment environment does not have Node.js/npx available, use a platform that supports it or adapt to Bright Data's remote MCP mode."
        )

    requester_name = st.text_input("Requester name", placeholder="Zunair")
    requester_email = st.text_input("Requester email", placeholder="name@company.com")
    company = st.text_input("Company name", placeholder="Ramp")
    website = st.text_input("Website", placeholder="https://ramp.com")
    notes = st.text_area(
        "Optional AE notes",
        placeholder="Fintech, likely scaling reporting needs, possible embedded analytics relevance.",
        height=120,
    )

    if "last_result" not in st.session_state:
        st.session_state.last_result = None

    if st.button("Analyze account", type="primary"):
        if not requester_email.strip():
            st.error("Please enter the requester email.")
            st.stop()
        if not company.strip():
            st.error("Please enter a company name.")
            st.stop()

        with st.spinner("Researching account and generating Metabase brief..."):
            try:
                result = asyncio.run(research_account(company.strip(), website.strip(), notes.strip()))
                result["requester_name"] = requester_name.strip()
                result["requester_email"] = requester_email.strip()
                result["ae_notes"] = notes.strip()
                if not result.get("company"):
                    result["company"] = company.strip()
                if not result.get("website"):
                    result["website"] = website.strip()

                log_to_sheets(result)
                send_email_report(result)
                st.session_state.last_result = result
                st.success(
                    "Research complete, emailed to the requester, and logged to Google Sheets."
                )
            except Exception as exc:
                st.exception(exc)

    if st.session_state.last_result:
        render_output(st.session_state.last_result)


if __name__ == "__main__":
    main()
