import os
import re
import json
import time
import smtplib
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Dict, List, Tuple
from urllib.parse import urljoin, quote_plus

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
        "customer facing analytics", "data access", "self-serve", "self serve",
        "kpi", "performance tracking", "report builder", "reporting layer",
    ],
    "hiring": [
        "data engineer", "analytics engineer", "data analyst", "business intelligence",
        "bi engineer", "analytics", "machine learning", "data platform", "data science",
        "revops", "operations analyst", "product analyst", "head of data",
        "data infrastructure", "data warehouse",
    ],
    "growth": [
        "launch", "launched", "new product", "expanded", "expanding", "grew", "growth",
        "announced", "rolled out", "general availability", "enterprise", "customers",
        "scale", "scaled", "new feature", "new module", "platform update",
    ],
    "org_change": [
        "appointed", "joined as", "named", "chief", "vp", "head of", "leader", "executive",
        "hired", "promoted", "new cto", "new cfo", "new coo", "new head of data",
    ],
    "funding": [
        "raised", "series a", "series b", "series c", "seed", "funding", "investment",
        "backed by", "capital", "venture", "growth equity",
    ],
}


def get_secret(name: str, default: str = "") -> str:
    try:
        value = st.secrets.get(name, default)
        return str(value).strip()
    except Exception:
        return os.getenv(name, default).strip()


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def truncate_text(text: str, max_chars: int = 7000) -> str:
    return clean_text(text)[:max_chars]


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
    url = (url or "").strip()
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
        "/resources",
        "/use-cases",
        "/case-studies",
        "/pricing",
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
    return truncate_text(soup.get_text(separator=" "), 10000)


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
    lowered = (text or "").lower()
    return sum(lowered.count(keyword.lower()) for keyword in keywords)


def infer_basic_signals(page_map: Dict[str, Dict[str, str]], search_results: List[Dict[str, Any]], company: str) -> List[Dict[str, str]]:
    signals: List[Dict[str, str]] = []

    combined_pages = " ".join([page.get("text", "") for page in page_map.values()])
    careers_like = " ".join([
        page_map.get("careers", {}).get("text", ""),
        page_map.get("jobs", {}).get("text", ""),
        combined_pages,
    ])
    growth_like = " ".join([
        page_map.get("blog", {}).get("text", ""),
        page_map.get("news", {}).get("text", ""),
        page_map.get("product", {}).get("text", ""),
        page_map.get("platform", {}).get("text", ""),
        page_map.get("customers", {}).get("text", ""),
        combined_pages,
    ])

    analytics_hits = count_keyword_hits(combined_pages, SIGNAL_KEYWORDS["analytics_relevance"])
    hiring_hits = count_keyword_hits(careers_like, SIGNAL_KEYWORDS["hiring"])
    growth_hits = count_keyword_hits(growth_like, SIGNAL_KEYWORDS["growth"])

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
            "description": f"{company}'s public web presence suggests analytics, reporting, dashboards, or data access are relevant.",
            "why_it_matters": "That increases the chance Metabase could be useful for self-serve BI, internal reporting, or embedded analytics."
        })

    if hiring_hits > 0:
        signals.append({
            "type": "Hiring",
            "description": f"{company} appears to be hiring for data, analytics, BI, or adjacent operational roles.",
            "why_it_matters": "Analytics hiring often points to increasing reporting complexity and more internal demand for dashboards and metrics access."
        })

    if funding_hits > 0:
        signals.append({
            "type": "Funding",
            "description": f"Search results suggest recent funding or investment-related activity for {company}.",
            "why_it_matters": "Funding can accelerate growth, increase metrics scrutiny, and create new demand for better reporting across teams."
        })

    if growth_hits > 0:
        signals.append({
            "type": "Product Growth",
            "description": f"{company}'s site suggests launches, product expansion, customer growth, or scaling activity.",
            "why_it_matters": "Growth can create reporting bottlenecks, more cross-functional dashboard demand, and possible embedded analytics opportunities."
        })

    if org_hits > 0:
        signals.append({
            "type": "Org Change",
            "description": f"Search results suggest relevant leadership or organizational changes.",
            "why_it_matters": "New leaders often reevaluate tooling, reporting workflows, and how teams access metrics."
        })

    return signals[:3]


def build_research_context(company: str, website: str, notes: str) -> Dict[str, Any]:
    base_url = normalize_url(website)
    pages = guess_pages(base_url) if base_url else []

    page_map: Dict[str, Dict[str, str]] = {}
    label_order = [
        "homepage", "careers", "jobs", "about", "blog", "news", "product", "platform",
        "customers", "solutions", "docs", "resources", "use_cases", "case_studies", "pricing"
    ]

    for idx, url in enumerate(pages[:len(label_order)]):
        final_url, html = fetch_url(url)
        meta = extract_title_and_meta(html)
        text = html_to_visible_text(html)
        page_map[label_order[idx]] = {
            "url": final_url,
            "title": meta["title"],
            "description": meta["description"],
            "text": text,
        }
        time.sleep(0.25)

    search_queries = [
        f"{company} funding",
        f"{company} analytics jobs",
        f"{company} data jobs",
        f"{company} new product launch",
        f"{company} leadership appointment",
        f"{company} embedded analytics",
        f"{company} dashboard reporting",
        f"{company} customer portal analytics",
    ]

    search_results = []
    for query in search_queries:
        search_results.append({"query": query, "results": search_google_html(query, max_results=5)})
        time.sleep(0.5)

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

Use all available context, including:
- company website pages
- careers/jobs pages
- blog/news content
- product, docs, pricing, and customer-facing pages
- public search results

Do not just summarize the company. Infer whether there is a real reason Metabase could be relevant now.

---

Metabase is strongest with companies that show one or more of these traits:
- B2B SaaS, developer tools, fintech, ecommerce, logistics, and modern technology businesses
- teams moving from lightweight reporting into real analytics complexity
- organizations where multiple functions need access to metrics, dashboards, or reporting
- companies trying to give non-technical users easier access to data
- businesses that may need embedded analytics or internal reporting at scale
- teams experiencing dashboard sprawl, inconsistent metrics, or bottlenecks around data access
- companies launching new products or expanding into enterprise
- companies increasing operational or product complexity

---

Metabase is generally a weaker fit for:
- very small companies with little analytics maturity
- businesses with no clear reporting needs
- companies with minimal internal data usage
- large enterprises with highly mature BI stacks and no clear gap

---

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

4. Determine ICP fit score from 1 to 4:

- 1 = weak fit  
- 2 = possible fit  
- 3 = good fit (strong ICP but unclear Metabase-specific problem)  
- 4 = strong fit (strong ICP + clear or emerging Metabase-relevant problem)

5. Explain ICP reasoning.

6. Determine "why now":
   - Is there a real reason this company is dealing with analytics or reporting challenges now?

7. Generate:
   - account_summary
   - signal_summary
   - why_metabase
   - why_now
   - target_persona
   - core_pain
   - messaging_angle
   - email_a
   - email_b

---

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
  "why_now": "",
  "target_persona": "",
  "core_pain": "",
  "messaging_angle": "",
  "email_a": "",
  "email_b": "",
  "sources": [""]
}

---

Rules:

- be skeptical, not optimistic
- separate ICP fit from urgency

---

CRITICAL THINKING RULES:

- Do NOT require explicit mention of BI tools to assign a 3 or 4
- You may infer fit from company type, product, and likely data needs

- BUT distinguish between:
  (a) general data complexity
  (b) a problem Metabase actually solves

- Do NOT assume growth automatically means a strong fit
- Large companies may already have internal or mature BI solutions

- If reasoning depends on assumptions without evidence, prefer a 3

---

WHY NOW RULES:

- "Why now" does NOT need to be a single event

Valid triggers include:
- product expansion
- enterprise features (RBAC, audit logs, permissions, SSO)
- increasing operational complexity
- multi-team usage
- scaling GTM or product motion

- Structural complexity is a valid trigger

- If a company is clearly growing into complexity, this can justify a 4

---

SCORING BEHAVIOR:

- 3 = strong company, unclear Metabase-specific need  
- 4 = strong company + clear or emerging Metabase-relevant problem  

- Do NOT require urgency for a 4
- Do NOT downgrade simply because the trigger is gradual

---

FINAL SCORING CHECK:

- Your score must match your reasoning

- If you describe:
  - increasing complexity
  - enterprise expansion
  - multi-team reporting needs
  - growing internal data demands

  then this indicates a real problem

→ In these cases, strongly consider assigning a 4

- Do NOT describe a clear problem and assign a 3

---

EMAIL RULES:

Each email must:

- be under 90 words
- use short paragraphs (1 to 2 sentences)
- be easy to scan
- use simple language (high school level)
- sound natural and human
- avoid clichés and buzzwords
- avoid overly polished phrasing

Structure:
- 2 to 4 short paragraphs

Strict constraints:
- ask ONLY one question
- include at most ONE link (optional)
- no attachments
- do NOT use dashes

Tone:
- curious, not pushy
- observational, not assumptive

CTA examples:
- “happy to share more if helpful”
- “curious if this is relevant”
- “can send an example if useful”

Email focus:
- email_a → speed and ease of getting answers from data  
- email_b → consistency and clarity in reporting  

---

Final instruction:

Think like an experienced AE deciding who to prioritize.

Do not just explain.

Make a decision.
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
        model=get_secret("CLAUDE_MODEL", "claude-sonnet-4-6"),
        max_tokens=3000,
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
        worksheet = workbook.add_worksheet(title=SHEET_TAB_NAME, rows=1000, cols=30)
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
                "why_now",
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
            payload.get("why_now", ""),
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

Why Now:
{result.get('why_now', '')}

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
        st.write("**Why Now**")
        st.write(result.get("why_now", ""))
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
                context = build_research_context(company.strip(), website.strip(), notes.strip())
                result = run_claude_brief(context)
                result["requester_name"] = requester_name.strip()
                result["requester_email"] = requester_email.strip()
                result["ae_notes"] = notes.strip()

                log_to_sheets(result)
                send_email_report(result)
                st.session_state.last_result = result
                st.success("Research complete, emailed to the requester, and logged to Google Sheets.")
            except Exception as exc:
                st.exception(exc)

    if st.session_state.last_result:
        render_output(st.session_state.last_result)


if __name__ == "__main__":
    main()
