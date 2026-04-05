import os
import re
import json
import time
import smtplib
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, quote_plus

import requests
import streamlit as st
from bs4 import BeautifulSoup

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

COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

SIGNAL_KEYWORDS = {
    "analytics_relevance": [
        "analytics", "reporting", "dashboard", "dashboards", "insights", "metrics",
        "business intelligence", "embedded analytics", "customer-facing analytics",
        "data access", "self-serve", "self serve", "kpi", "performance tracking",
    ],
    "hiring": [
        "data engineer", "analytics engineer", "data analyst", "business intelligence",
        "bi engineer", "analytics", "machine learning", "data platform", "data science",
        "revops", "operations analyst", "product analyst",
    ],
    "growth": [
        "launch", "launched", "new product", "expanded", "expanding", "grew", "growth",
        "announced", "rolled out", "general availability", "enterprise", "customers",
        "scale", "scaled",
    ],
    "org_change": [
        "appointed", "joined as", "named", "chief", "vp", "head of", "leader", "executive",
        "hired", "promoted",
    ],
    "funding": [
        "raised", "series a", "series b", "series c", "seed", "funding", "investment",
        "backed by", "capital",
    ],
}


def get_secret(name: str, default: str = "") -> str:
    try:
        value = st.secrets.get(name, default)
        return str(value).strip()
    except Exception:
        return os.getenv(name, default).strip()


def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text


def truncate_text(text: str, max_chars: int = 7000) -> str:
    text = clean_text(text)
    return text[:max_chars]


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


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def guess_pages(base_url: str) -> List[str]:
    base_url = normalize_url(base_url)
    if not base_url:
        return []

    candidates = [
        "",
        "/careers",
        "/jobs",
        "/about",
        "/blog",
        "/news",
        "/product",
        "/platform",
        "/customers",
        "/solutions",
        "/docs",
    ]
    return [urljoin(base_url.rstrip("/") + "/", path.lstrip("/")) for path in candidates]


def fetch_url(url: str, timeout: int = 25) -> Tuple[str, str]:
    try:
        resp = requests.get(url, headers=COMMON_HEADERS, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        return resp.url, resp.text
    except Exception:
        return url, ""


def html_to_visible_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg", "img", "iframe"]):
        tag.decompose()

    text = soup.get_text(separator=" ")
    return truncate_text(text, 9000)


def extract_title_and_meta(html: str) -> Dict[str, str]:
    if not html:
        return {"title": "", "description": ""}
    soup = BeautifulSoup(html, "html.parser")
    title = clean_text(soup.title.get_text()) if soup.title else ""
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    description = clean_text(meta.get("content", "")) if meta else ""
    return {"title": title, "description": description}


def search_google_html(query: str, max_results: int = 6) -> List[Dict[str, str]]:
    search_url = f"https://www.google.com/search?q={quote_plus(query)}&num={max_results}"
    try:
        resp = requests.get(search_url, headers=COMMON_HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        return [{"title": "Search failed", "url": "", "snippet": str(exc)}]

    soup = BeautifulSoup(resp.text, "html.parser")
    results: List[Dict[str, str]] = []

    for g in soup.select("div.g"):
        a = g.find("a", href=True)
        title_tag = g.find(["h3", "h2"])
        snippet_tag = g.select_one("div.VwiC3b") or g.select_one("span.aCOpRe")
        if a and title_tag:
            url = a["href"]
            title = clean_text(title_tag.get_text())
            snippet = clean_text(snippet_tag.get_text()) if snippet_tag else ""
            if title and url:
                results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max_results:
            break

    if not results:
        text = truncate_text(html_to_visible_text(resp.text), 2000)
        results.append({"title": "Raw Google response", "url": search_url, "snippet": text})
    return results


def count_keyword_hits(text: str, keywords: List[str]) -> int:
    lowered = text.lower()
    return sum(lowered.count(keyword.lower()) for keyword in keywords)


def infer_basic_signals(page_map: Dict[str, Dict[str, str]], search_results: List[Dict[str, Any]], company: str) -> List[Dict[str, str]]:
    signals: List[Dict[str, str]] = []

    homepage_text = page_map.get("homepage", {}).get("text", "")
    careers_text = page_map.get("careers", {}).get("text", "")
    blog_text = page_map.get("blog", {}).get("text", "")
    docs_text = page_map.get("docs", {}).get("text", "")
    combined = " ".join([homepage_text, careers_text, blog_text, docs_text])

    analytics_hits = count_keyword_hits(combined, SIGNAL_KEYWORDS["analytics_relevance"])
    hiring_hits = count_keyword_hits(careers_text or combined, SIGNAL_KEYWORDS["hiring"])
    growth_hits = count_keyword_hits(blog_text or combined, SIGNAL_KEYWORDS["growth"])

    search_blob = " ".join(
        " ".join([item.get("title", ""), item.get("snippet", "")])
        for block in search_results
        for item in block.get("results", [])
    ).lower()

    funding_hits = count_keyword_hits(search_blob, SIGNAL_KEYWORDS["funding"])
    org_hits = count_keyword_hits(search_blob, SIGNAL_KEYWORDS["org_change"])

    if analytics_hits > 0:
        signals.append({
            "type": "Analytics Relevance",
            "description": f"{company}'s website mentions analytics/reporting-related concepts.",
            "why_it_matters": "This suggests a plausible need for self-serve BI, dashboarding, or embedded analytics."
        })

    if hiring_hits > 0:
        signals.append({
            "type": "Hiring",
            "description": f"{company} appears to be hiring for data, analytics, or BI-adjacent roles.",
            "why_it_matters": "Growing analytics hiring can indicate rising data complexity and stronger internal reporting needs."
        })

    if funding_hits > 0:
        signals.append({
            "type": "Funding",
            "description": f"Public search results suggest recent funding or investment-related activity for {company}.",
            "why_it_matters": "New funding often leads to team growth, more metrics scrutiny, and stronger demand for analytics visibility."
        })

    if growth_hits > 0:
        signals.append({
            "type": "Product Growth",
            "description": f"{company}'s site or blog suggests product launches, expansion, or scaling activity.",
            "why_it_matters": "Growth events often increase dashboard usage, cross-functional reporting needs, and data access pressure."
        })

    if org_hits > 0:
        signals.append({
            "type": "Org Change",
            "description": f"Search results suggest leadership or organizational changes relevant to data, ops, or growth.",
            "why_it_matters": "New leaders often reassess reporting workflows, tooling, and how teams access metrics."
        })

    return signals[:3]


def build_research_context(company: str, website: str, notes: str) -> Dict[str, Any]:
    base_url = normalize_url(website)
    pages = guess_pages(base_url) if base_url else []

    page_map: Dict[str, Dict[str, str]] = {}

    label_map = {
        0: "homepage",
        1: "careers",
        2: "jobs",
        3: "about",
        4: "blog",
        5: "news",
        6: "product",
        7: "platform",
        8: "customers",
        9: "solutions",
        10: "docs",
    }

    for idx, url in enumerate(pages[:11]):
        final_url, html = fetch_url(url)
        meta = extract_title_and_meta(html)
        text = html_to_visible_text(html)
        key = label_map.get(idx, f"page_{idx}")
        page_map[key] = {
            "url": final_url,
            "title": meta["title"],
            "description": meta["description"],
            "text": text,
        }
        time.sleep(0.4)

    search_queries = [
        f"{company} funding",
        f"{company} analytics jobs",
        f"{company} new product launch",
        f"{company} leadership appointment",
    ]
    search_results = []
    for query in search_queries:
        search_results.append({"query": query, "results": search_google_html(query, max_results=5)})
        time.sleep(0.8)

    inferred_signals = infer_basic_signals(page_map, search_results, company)

    return {
        "company": company,
        "website": base_url,
        "ae_notes": notes,
        "page_map": page_map,
        "search_results": search_results,
        "pre_inferred_signals": inferred_signals,
    }


SYSTEM_PROMPT = """You are a GTM engineer building outbound strategy for Metabase.

Your job is to analyze one target account using structured public-web research context and generate a Metabase-specific account brief.

Metabase is strongest with:
- B2B SaaS, fintech, ecommerce, logistics, developer tools, and modern technology companies
- teams growing into analytics complexity
- companies that need self-serve analytics for non-technical users
- companies embedding analytics or customer-facing reporting into products
- teams dealing with dashboard sprawl, inconsistent metrics, or hard-to-access data

Tasks:
1. Review the research context carefully.
2. Identify up to 3 meaningful signals:
   - Hiring
   - Funding
   - Product Growth
   - Org Change
   - Analytics Relevance
3. For each signal:
   - describe it clearly
   - explain why it matters for Metabase
4. Score ICP fit from 1 to 4:
   - 1 = weak fit
   - 2 = possible fit
   - 3 = good fit
   - 4 = strong fit
5. Explain ICP reasoning briefly
6. Generate:
   - account_summary
   - signal_summary
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

    prompt = f"""Here is the research context:

{json.dumps(context, indent=2)}

Return the required JSON only.
"""
    response = client.messages.create(
        model=get_secret("CLAUDE_MODEL", "claude-3-5-sonnet-latest"),
        max_tokens=2800,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    parts = []
    for block in response.content:
        if getattr(block, "type", "") == "text":
            parts.append(block.text)
    if not parts:
        raise RuntimeError("Claude returned no text output.")

    result = parse_json_response("\n".join(parts))
    if not result.get("company"):
        result["company"] = context.get("company", "")
    if not result.get("website"):
        result["website"] = context.get("website", "")

    return result


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

def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)
    st.caption(
        "Enter one account, gather first-party website and public search signals, generate a Metabase-specific brief with Claude, email it to the requester, and log it to Google Sheets."
    )

    with st.sidebar:
        st.subheader("Required secrets")
        st.code(
            "\n".join(
                [
                    'ANTHROPIC_API_KEY="..."',
                    "GOOGLE_SERVICE_ACCOUNT_JSON='{\"type\":\"service_account\",...}'",
                    'GOOGLE_SHEET_ID="..."',
                    'SMTP_USERNAME="you@gmail.com"',
                    'SMTP_PASSWORD="your_app_password"',
                    'SMTP_SENDER_EMAIL="you@gmail.com"',
                    'SMTP_HOST="smtp.gmail.com"',
                    'SMTP_PORT="587"',
                    'CLAUDE_MODEL="claude-3-5-sonnet-latest"',
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
