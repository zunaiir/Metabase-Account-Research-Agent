import os
import json
import time
import smtplib
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Dict, List, Optional

import requests
import streamlit as st

try:
    import anthropic
except Exception:
    anthropic = None

try:
    import gspread
    from google.oauth2.service_account import Credentials
except Exception:
    gspread = None
    Credentials = None


APP_TITLE = "Metabase Account Brief Agent"
SHEET_TAB_NAME = "researched_accounts"


def get_secret(name: str, default: str = "") -> str:
    try:
        value = st.secrets.get(name, default)
        return str(value).strip()
    except Exception:
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
            return json.loads(text[start:end + 1])
        raise


def brightdata_google_search(query: str, country: str = "us", num_results: int = 8) -> Dict[str, Any]:
    api_key = get_secret("BRIGHTDATA_API_TOKEN")
    serp_zone = get_secret("BRIGHTDATA_SERP_ZONE")
    if not api_key:
        raise RuntimeError("Missing BRIGHTDATA_API_TOKEN.")
    if not serp_zone:
        raise RuntimeError("Missing BRIGHTDATA_SERP_ZONE.")

    url = "https://api.brightdata.com/request"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "zone": serp_zone,
        "url": f"https://www.google.com/search?q={query.replace(' ', '+')}&num={num_results}&gl={country}",
        "format": "raw",
        "country": country,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=90)
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text}


def summarize_search_results(search_payload: Dict[str, Any]) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []

    # Support a few plausible response shapes.
    if isinstance(search_payload, dict):
        if isinstance(search_payload.get("organic"), list):
            for item in search_payload["organic"]:
                items.append(
                    {
                        "title": str(item.get("title", "")),
                        "url": str(item.get("link", item.get("url", ""))),
                        "snippet": str(item.get("description", item.get("snippet", ""))),
                    }
                )
        elif isinstance(search_payload.get("results"), list):
            for item in search_payload["results"]:
                items.append(
                    {
                        "title": str(item.get("title", "")),
                        "url": str(item.get("url", item.get("link", ""))),
                        "snippet": str(item.get("description", item.get("snippet", ""))),
                    }
                )
        elif isinstance(search_payload.get("raw"), str):
            items.append({"title": "Raw search response", "url": "", "snippet": search_payload["raw"][:6000]})

    return items[:10]


def build_research_context(company: str, website: str, notes: str) -> Dict[str, Any]:
    queries = [
        f'{company} company overview',
        f'{company} funding news',
        f'{company} hiring analytics data jobs',
        f'{company} product launch analytics reporting',
        f'site:{website.replace("https://","").replace("http://","").strip("/")} jobs data analytics' if website else f'{company} jobs data analytics',
    ]

    search_results = []
    for q in queries:
        try:
            payload = brightdata_google_search(q)
            search_results.append({"query": q, "results": summarize_search_results(payload)})
            time.sleep(1.0)
        except Exception as exc:
            search_results.append({"query": q, "results": [{"title": "Search failed", "url": "", "snippet": str(exc)}]})

    return {
        "company": company,
        "website": website,
        "ae_notes": notes,
        "search_results": search_results,
    }


SYSTEM_PROMPT = """You are a GTM engineer building outbound strategy for Metabase.

Your job is to analyze a target account using structured web research context and generate a Metabase-specific selling brief.

Metabase is strongest with:
- B2B SaaS, fintech, ecommerce, logistics, developer tools, and modern technology companies
- teams growing into analytics complexity
- companies that need self-serve analytics for non-technical users
- companies embedding analytics or customer-facing reporting into products
- teams dealing with dashboard sprawl, inconsistent metrics, or hard-to-access data

Tasks:
1. Identify up to 3 meaningful signals:
   - Hiring
   - Funding
   - Product Growth
   - Org Change
   - Analytics Relevance
2. For each signal:
   - describe it clearly
   - explain why it matters for Metabase
3. Score ICP fit from 1 to 4:
   - 1 = weak fit
   - 2 = possible fit
   - 3 = good fit
   - 4 = strong fit
4. Explain ICP reasoning briefly
5. Generate:
   - account_summary
   - why_metabase
   - target_persona
   - core_pain
   - messaging_angle
   - email_a
   - email_b

Return ONLY valid JSON with this exact schema:
{
  "company": "",
  "website": "",
  "account_summary": "",
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
  "why_metabase": "",
  "target_persona": "",
  "core_pain": "",
  "messaging_angle": "",
  "email_a": "",
  "email_b": "",
  "sources": [""]
}

Rules:
- be skeptical, not optimistic
- if evidence is weak, say so clearly
- keep output concise and practical
- email_a should lean efficiency / speed
- email_b should lean governance / consistency
"""


def run_claude_brief(context: Dict[str, Any]) -> Dict[str, Any]:
    api_key = get_secret("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("Missing ANTHROPIC_API_KEY.")
    if anthropic is None:
        raise RuntimeError("anthropic package is not installed.")

    client = anthropic.Anthropic(api_key=api_key)

    prompt = f"""Research context for account analysis:

{json.dumps(context, indent=2)}

Return the required JSON only.
"""

    resp = client.messages.create(
        model=get_secret("CLAUDE_MODEL", "claude-3-5-sonnet-latest"),
        max_tokens=2500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    parts = []
    for block in resp.content:
        if getattr(block, "type", "") == "text":
            parts.append(block.text)
    if not parts:
        raise RuntimeError("Claude returned no text output.")

    return parse_json_response("\n".join(parts))


def get_gspread_client() -> "gspread.Client":
    if gspread is None or Credentials is None:
        raise RuntimeError("gspread/google-auth not installed.")

    raw_json = get_secret("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw_json:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON.")

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
        raise RuntimeError("Missing GOOGLE_SHEET_ID.")

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
                "account_summary",
                "signals",
                "signal_summary",
                "icp_score",
                "icp_reasoning",
                "why_metabase",
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
            payload.get("account_summary", ""),
            signal_text,
            payload.get("signal_summary", ""),
            payload.get("icp_score", ""),
            payload.get("icp_reasoning", ""),
            payload.get("why_metabase", ""),
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
        raise RuntimeError("Missing SMTP credentials.")
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

Account Summary:
{result.get('account_summary', '')}

ICP Score: {result.get('icp_score', '')}
ICP Reasoning:
{result.get('icp_reasoning', '')}

Why Metabase:
{result.get('why_metabase', '')}

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


def render_output(result: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> None:
    st.subheader("Account Brief")

    top_left, top_right = st.columns([1, 2])
    with top_left:
        st.metric("ICP Score", result.get("icp_score", "-"))
        st.write("**Target Persona**")
        st.write(result.get("target_persona", ""))
        st.write("**Core Pain**")
        st.write(result.get("core_pain", ""))

    with top_right:
        st.write("**Account Summary**")
        st.write(result.get("account_summary", ""))
        st.write("**Why Metabase**")
        st.write(result.get("why_metabase", ""))
        st.write("**Messaging Angle**")
        st.write(result.get("messaging_angle", ""))

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

    if context:
        with st.expander("Research Context"):
            st.json(context)
    with st.expander("Raw JSON"):
        st.json(result)


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)
    st.caption("Enter one account, pull Bright Data search signals, generate a Metabase-specific brief with Claude, email it to the requester, and log it to Google Sheets.")

    with st.sidebar:
        st.subheader("Required secrets")
        st.code(
            "\n".join(
                [
                    "ANTHROPIC_API_KEY",
                    "BRIGHTDATA_API_TOKEN",
                    "BRIGHTDATA_SERP_ZONE",
                    "GOOGLE_SERVICE_ACCOUNT_JSON",
                    "GOOGLE_SHEET_ID",
                    "SMTP_USERNAME",
                    "SMTP_PASSWORD",
                    "SMTP_SENDER_EMAIL",
                ]
            ),
            language="toml",
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
    if "last_context" not in st.session_state:
        st.session_state.last_context = None

    if st.button("Analyze account", type="primary"):
        if not requester_email.strip():
            st.error("Please enter the requester email.")
            st.stop()
        if not company.strip():
            st.error("Please enter a company name.")
            st.stop()

        with st.spinner("Researching account and generating Metabase brief..."):
            try:
                context = build_research_context(company.strip(), website.strip(), notes.strip())
                result = run_claude_brief(context)
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
                st.session_state.last_context = context
                st.success("Research complete, emailed to the requester, and logged to Google Sheets.")
            except Exception as exc:
                st.exception(exc)

    if st.session_state.last_result:
        render_output(st.session_state.last_result, st.session_state.last_context)


if __name__ == "__main__":
    main()
