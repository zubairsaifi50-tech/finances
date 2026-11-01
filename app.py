import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google.oauth2.credentials import Credentials as UserOAuthCredentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from datetime import datetime, date, timedelta
import base64
import json
import uuid
import re
from bs4 import BeautifulSoup

# ==============================================================================
# 1. PAGE CONFIGURATION & MOBILE-FRIENDLY STYLING
# ==============================================================================
st.set_page_config(
    page_title="Finances & Groww AI Tracker",
    page_icon="💳",
    layout="wide",
    initial_sidebar_state="collapsed"
)

st.markdown("""
    <style>
    .block-container {
        padding-top: 1.5rem;
        padding-bottom: 2rem;
    }
    div[data-testid="stMetric"] {
        background-color: rgba(128, 128, 128, 0.08);
        border: 1px solid rgba(128, 128, 128, 0.2);
        padding: 15px;
        border-radius: 10px;
    }
    </style>
""", unsafe_allow_html=True)

# ==============================================================================
# 2. CONSTANTS, TOKEN CAPS & SMART TRANSFER RULE DEFINITIONS
# ==============================================================================
SHEET_NAME = "Finances"
TRACKER_TAB = "Tracker"
ACCOUNTS_TAB = "Accounts"

# Phase 4 & 5: Token Optimization & Cost Management Constants
MONTHLY_TOKEN_BUDGET = 1_000_000       # 10-Lakh Monthly Token Allowance
MAX_INPUT_CHARS = 1000                 # Strict 1,000-character truncation cap for SMS/Email
GEMINI_MAX_OUTPUT_TOKENS = 300         # Strict 300 output token cap for parsing
GEMINI_INSIGHTS_MAX_TOKENS = 450       # Strict 450 output token cap for AI Financial Insights
GEMINI_TEMPERATURE = 0.0               # Zero-temperature deterministic extraction

# Local Pre-filter Regex: Essential Financial Trigger Words
FINANCIAL_TRIGGER_REGEX = re.compile(
    r"\b(debited|credited|spent|upi|sip|paid|transferred|auto[\s\-]?debit|"
    r"withdrawn|withdrawal|allotted|redemption|settled|imps|neft|rtgs|nach|mandate)\b",
    re.IGNORECASE
)

# Local Pre-filter Regex: Currency / Numeric Amount Presence
AMOUNT_INDICATOR_REGEX = re.compile(
    r"(?:rs\.?|inr|₹|\bamt\b|\bamount\b)\s*[\d,]+(?:\.\d{1,2})?|\b\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?\b",
    re.IGNORECASE
)

# Local Pre-filter Regex: Non-Transaction Noise (OTPs, Promos, Newsletters)
NOISE_EXCLUSION_REGEX = re.compile(
    r"\b(one\s*time\s*password|otp\s*is|verification\s*code|login\s*otp|"
    r"pre[\s\-]?approved\s*loan|apply\s*now\s*for|upgrade\s*your\s*limit)\b",
    re.IGNORECASE
)

TRACKER_HEADERS = [
    "Transaction_ID", "Date", "Type", "Amount", "Category", 
    "Source_Account", "Destination_Account", "Notes", "Timestamp"
]

ACCOUNTS_HEADERS = [
    "Account_ID", "Account_Type", "Initial_Balance", "Credit_Limit"
]

DEFAULT_ACCOUNTS = [
    ["Cash Wallet *0000", "Cash", 5000.0, 0.0],
    ["HDFC Checking *1092", "Bank Account", 45000.0, 0.0],
    ["SBI Savings *4410", "Bank Account", 120000.0, 0.0],
    ["Amex Gold *8821", "Credit Card", 0.0, 150000.0],
    ["ICICI Sapphiro *3319", "Credit Card", 0.0, 300000.0],
    ["Groww Stocks Demat *9901", "Investment", 250000.0, 0.0],
    ["Groww Mutual Funds (Equity/ELSS) *9902", "Investment", 180000.0, 0.0],
    ["Home Loan *5512", "Loan", -1500000.0, 0.0]
]

EXPENSE_CATEGORIES = [
    "Groceries & Dining", "Housing & Rent", "Utilities & Bills", 
    "Transportation & Fuel", "Shopping & Entertainment", "Healthcare", 
    "Education", "EMIs & Debt", "Miscellaneous"
]

INCOME_CATEGORIES = [
    "Salary & Wages", "Freelance & Business", "Dividends & Realized Gains", 
    "Refunds & Cashbacks", "Gifts & Other"
]

TRANSFER_CATEGORIES = [
    "SIP / Mutual Fund Allocation (Groww)",
    "ELSS Tax-Saving Allocation (Groww)",
    "Stock / Demat Allocation (Groww)",
    "Investment Redemption / SWP (To Bank)",
    "Credit Card Bill Payment", 
    "Self Transfer (Bank to Bank)", 
    "Cash Withdrawal (ATM)", 
    "Loan Principal Prepayment"
]

ALL_CATEGORIES = EXPENSE_CATEGORIES + INCOME_CATEGORIES + TRANSFER_CATEGORIES

DEFAULT_GMAIL_QUERY = (
    'newer_than:7d ("debited" OR "credited" OR "spent" OR "UPI" OR "Groww" '
    'OR "SIP" OR "paid" OR "transferred" OR "auto-debit")'
)

SMART_TRANSFER_REGEX_RULES = [
    {
        "rule_name": "Groww ELSS Tax-Saving SIP/Allocation",
        "pattern": r"(elss|tax\s*saver\s*fund)",
        "forced_category": "ELSS Tax-Saving Allocation (Groww)",
        "target_account_hint": "Groww Mutual Funds"
    },
    {
        "rule_name": "Groww Mutual Fund / SIP Installment",
        "pattern": r"(sip\s*installment|sip\s*processed|systematic\s*investment|mutual\s*fund|iccl|indian\s*clearing\s*corp|bse\s*star\s*mf|folio\s*no|groww\s*mf|units\s*allotted)",
        "forced_category": "SIP / Mutual Fund Allocation (Groww)",
        "target_account_hint": "Groww Mutual Funds"
    },
    {
        "rule_name": "Groww Stocks / Demat Funding",
        "pattern": r"(groww\s*stocks|groww\s*demat|nextbillion\s*technology|nse\s*clearing|equity\s*settlement|demat\s*allocation)",
        "forced_category": "Stock / Demat Allocation (Groww)",
        "target_account_hint": "Groww Stocks Demat"
    },
    {
        "rule_name": "Credit Card Bill Settlement / Auto-Debit",
        "pattern": r"(credit\s*card\s*payment|cc\s*bill\s*payment|card\s*bill\s*auto[\s\-]*debit|billdesk\s*cc|cred\s*club|cc\s*settlement|towards\s*credit\s*card|payment\s*received\s*towards\s*your\s*card)",
        "forced_category": "Credit Card Bill Payment",
        "target_account_hint": "Credit Card"
    },
    {
        "rule_name": "Inter-Bank Self-Transfer (Own Account)",
        "pattern": r"(self[\s\-]*transfer|own\s*account|trf\s*to\s*self|to\s*self|internal\s*transfer|sweep\s*transfer)",
        "forced_category": "Self Transfer (Bank to Bank)",
        "target_account_hint": "Bank Account"
    },
    {
        "rule_name": "ATM Cash Withdrawal",
        "pattern": r"(atm\s*withdrawal|cash\s*withdrawal\s*at\s*atm|atm\s*cash\s*wdl|withdrawn\s*at\s*atm)",
        "forced_category": "Cash Withdrawal (ATM)",
        "target_account_hint": "Cash Wallet"
    },
    {
        "rule_name": "Loan Principal Prepayment",
        "pattern": r"(principal\s*prepayment|loan\s*part\s*payment|home\s*loan\s*prepayment)",
        "forced_category": "Loan Principal Prepayment",
        "target_account_hint": "Home Loan"
    }
]

# ==============================================================================
# 3. PYDANTIC SCHEMAS FOR GEMINI (PARSING & PHASE 5 AI INSIGHTS)
# ==============================================================================
class ExtractedTransaction(BaseModel):
    is_financial_transaction: bool = Field(description="True ONLY for completed financial transactions.")
    transaction_type: str = Field(description="'Expense', 'Income', or 'Transfer'.")
    is_self_transfer_or_allocation: bool = Field(description="True for self-transfers, CC bill payments, ATM, or Groww SIP/Demat.")
    transfer_rule_matched: str = Field(description="Short rule label (<8 words).")
    amount: float = Field(description="Numeric amount.")
    date: str = Field(description="YYYY-MM-DD.")
    category: str = Field(description="Exact category from master list.")
    source_account: str = Field(description="Exact source Account_ID from master list.")
    destination_account: str = Field(description="Exact destination Account_ID if Transfer, else ''.")
    notes: str = Field(description="Concise merchant/security note (<10 words).")
    confidence_note: str = Field(description="Brief match reason (<12 words).")

class AISpendingInsightReport(BaseModel):
    executive_headline: str = Field(description="1-sentence punchy financial health headline.")
    top_spending_analysis: str = Field(description="2-sentence analysis of top expense categories and burn rate concentration.")
    burn_spike_alert: str = Field(description="1-2 sentences flagging any unusual single-transaction spikes or category anomalies.")
    portfolio_and_credit_health: str = Field(description="1-2 sentences evaluating Groww SIP/Demat investment discipline and credit card utilization.")
    actionable_recommendations: list[str] = Field(description="Exactly 3 concise, high-impact financial action items for next week/month.")

# ==============================================================================
# 4. PHASE 4: LOCAL PRE-FILTERING & PAYLOAD TRUNCATION ENGINE
# ==============================================================================
def prepare_token_optimized_payload(raw_text: str, max_chars: int = MAX_INPUT_CHARS) -> str:
    """Normalizes whitespace and strictly truncates text to 1,000 characters."""
    if not raw_text:
        return ""
    cleaned = re.sub(r"[ \t]+", " ", raw_text)
    cleaned = re.sub(r"\n\s*\n+", "\n", cleaned).strip()
    return cleaned[:max_chars]

def passes_local_financial_prefilter(raw_text: str) -> tuple[bool, str]:
    """Executes zero-cost local regex checks BEFORE calling Gemini to preserve token budget."""
    if not raw_text or len(raw_text.strip()) < 12:
        return False, "Skipped: Text too short (<12 chars)."

    truncated = prepare_token_optimized_payload(raw_text, MAX_INPUT_CHARS)

    if NOISE_EXCLUSION_REGEX.search(truncated) and not re.search(r"\b(debited|credited|spent|paid)\b", truncated, re.IGNORECASE):
        return False, "Skipped by Local Pre-Filter: Detected OTP / Promotional noise."

    trigger_match = FINANCIAL_TRIGGER_REGEX.search(truncated)
    if not trigger_match:
        return False, "Skipped by Local Pre-Filter: Missing financial trigger words."

    has_amount = bool(AMOUNT_INDICATOR_REGEX.search(truncated)) or bool(re.search(r"\b\d+(?:\.\d{1,2})?\b", truncated))
    if not has_amount:
        return False, "Skipped by Local Pre-Filter: No numeric transaction amount detected."

    return True, f"Passed Pre-Filter (Trigger: '{trigger_match.group(0).lower()}')"

# ==============================================================================
# 5. PHASE 3 SMART TRANSFER RULE & DEDUPLICATION ENGINE
# ==============================================================================
def extract_masked_accounts_from_text(raw_text: str, account_list: list) -> list:
    """Scans text for 4-digit account/card numbers and returns matching Account_IDs."""
    matched_accounts = []
    for acc_id in account_list:
        digit_match = re.search(r"\*(\d{4})$", acc_id.strip())
        if digit_match:
            digits = digit_match.group(1)
            if digits == "0000":
                continue
            pattern = rf"(?:x|X|\*|ending|no\.?|a/c|card|\s|-){digits}\b"
            if re.search(pattern, raw_text, re.IGNORECASE):
                matched_accounts.append(acc_id)
    return matched_accounts

def find_fallback_account(df_accounts: pd.DataFrame, hint: str, exclude_account: str = "") -> str:
    """Finds the best matching destination account from the Master Accounts list."""
    if df_accounts.empty:
        return ""
    for _, row in df_accounts.iterrows():
        acc_id = row["Account_ID"]
        acc_type = row["Account_Type"]
        if acc_id == exclude_account:
            continue
        if hint.lower() in acc_id.lower() or hint.lower() == acc_type.lower():
            return acc_id
    for acc_id in df_accounts["Account_ID"].tolist():
        if acc_id != exclude_account:
            return acc_id
    return ""

def apply_smart_transfer_rules(parsed_tx: dict, raw_text: str, df_accounts: pd.DataFrame) -> dict:
    """Deterministic guardrail ensuring self-transfers, CC settlements, and Groww SIPs are always 'Transfer'."""
    tx = parsed_tx.copy()
    account_list = df_accounts["Account_ID"].tolist() if not df_accounts.empty else []
    combined_text = f"{raw_text} {tx.get('notes', '')} {tx.get('category', '')}".lower()

    matched_own_accounts = extract_masked_accounts_from_text(raw_text, account_list)
    if len(matched_own_accounts) >= 2 and matched_own_accounts[0] != matched_own_accounts[1]:
        tx["transaction_type"] = "Transfer"
        tx["is_self_transfer_or_allocation"] = True
        tx["source_account"] = matched_own_accounts[0]
        tx["destination_account"] = matched_own_accounts[1]
        if tx.get("category") not in TRANSFER_CATEGORIES:
            dest_type = df_accounts[df_accounts["Account_ID"] == matched_own_accounts[1]]["Account_Type"].values
            if len(dest_type) > 0 and dest_type[0] == "Credit Card":
                tx["category"] = "Credit Card Bill Payment"
            elif len(dest_type) > 0 and dest_type[0] == "Investment":
                tx["category"] = "SIP / Mutual Fund Allocation (Groww)"
            else:
                tx["category"] = "Self Transfer (Bank to Bank)"
        tx["transfer_rule_matched"] = f"Dual Masked Account Match ({matched_own_accounts[0]} → {matched_own_accounts[1]})"
        return tx

    for rule in SMART_TRANSFER_REGEX_RULES:
        if re.search(rule["pattern"], combined_text, re.IGNORECASE):
            tx["transaction_type"] = "Transfer"
            tx["is_self_transfer_or_allocation"] = True
            tx["category"] = rule["forced_category"]
            tx["transfer_rule_matched"] = rule["rule_name"]

            src = tx.get("source_account", "")
            dest = tx.get("destination_account", "")
            if not dest or dest == src:
                inferred_dest = find_fallback_account(df_accounts, rule["target_account_hint"], exclude_account=src)
                if inferred_dest:
                    tx["destination_account"] = inferred_dest

            if not df_accounts.empty and tx.get("source_account") == tx.get("destination_account"):
                tx["source_account"] = find_fallback_account(df_accounts, "Bank Account", exclude_account=tx["destination_account"])
            return tx

    if tx.get("category") in TRANSFER_CATEGORIES:
        tx["transaction_type"] = "Transfer"
        tx["is_self_transfer_or_allocation"] = True
        if not tx.get("transfer_rule_matched") or tx.get("transfer_rule_matched") == "Standard Expense/Income":
            tx["transfer_rule_matched"] = "Transfer Category Enforcement"
        if not tx.get("destination_account") or tx.get("destination_account") == tx.get("source_account"):
            tx["destination_account"] = find_fallback_account(df_accounts, "Bank Account", exclude_account=tx.get("source_account", ""))

    return tx

def detect_transfer_mirror_duplicates(candidate_tx: dict, df_tracker: pd.DataFrame, pending_queue: list) -> str:
    """Detects same-day, same-amount mirror transfer alerts in the ledger or review queue."""
    if candidate_tx.get("transaction_type") != "Transfer":
        return ""

    amt = float(candidate_tx.get("amount", 0.0))
    dt = candidate_tx.get("date", "")
    qid = candidate_tx.get("queue_id", "")

    if not df_tracker.empty:
        ledger_matches = df_tracker[
            (df_tracker["Type"] == "Transfer") &
            (df_tracker["Date"] == dt) &
            (df_tracker["Amount"].round(2) == round(amt, 2))
        ]
        if not ledger_matches.empty:
            first_match = ledger_matches.iloc[0]
            return (
                f"⚠️ Potential Mirror Duplicate: A Transfer of {amt:,.2f} on {dt} "
                f"({first_match['Source_Account']} → {first_match['Destination_Account']}) is already in your Ledger (`{first_match['Transaction_ID']}`)."
            )

    for other in pending_queue:
        if other.get("queue_id") != qid and other.get("transaction_type") == "Transfer":
            if other.get("date") == dt and round(float(other.get("amount", 0.0)), 2) == round(amt, 2):
                return (
                    f"⚠️ Mirror Alert in Queue: Another Transfer of {amt:,.2f} on {dt} "
                    f"(`{other.get('queue_id')}`) is also waiting in this review queue."
                )
    return ""

# ==============================================================================
# 6. GOOGLE SHEETS AUTHENTICATION & SYNCHRONIZATION ENGINE
# ==============================================================================
@st.cache_resource
def get_gspread_client():
    """Authenticates with Google Cloud using st.secrets without hardcoding credentials."""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    if "gcp_service_account" not in st.secrets:
        st.error("🚨 Missing `gcp_service_account` in `st.secrets`. Please configure your credentials.")
        st.stop()
        
    creds_dict = dict(st.secrets["gcp_service_account"])
    credentials = ServiceAccountCredentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(credentials)

def init_google_sheet():
    """Ensures the 'Finances' spreadsheet and required tabs ('Tracker', 'Accounts') exist."""
    client = get_gspread_client()
    try:
        sh = client.open(SHEET_NAME)
    except gspread.SpreadsheetNotFound:
        st.error(f"🚨 Spreadsheet **'{SHEET_NAME}'** not found! Please create a Google Sheet named '{SHEET_NAME}' and share it with your Service Account email.")
        st.stop()

    try:
        ws_tracker = sh.worksheet(TRACKER_TAB)
        if not ws_tracker.row_values(1):
            ws_tracker.append_row(TRACKER_HEADERS)
    except gspread.WorksheetNotFound:
        ws_tracker = sh.add_worksheet(title=TRACKER_TAB, rows="1000", cols="10")
        ws_tracker.append_row(TRACKER_HEADERS)

    try:
        ws_accounts = sh.worksheet(ACCOUNTS_TAB)
        if not ws_accounts.row_values(1):
            ws_accounts.append_row(ACCOUNTS_HEADERS)
            ws_accounts.append_rows(DEFAULT_ACCOUNTS)
    except gspread.WorksheetNotFound:
        ws_accounts = sh.add_worksheet(title=ACCOUNTS_TAB, rows="100", cols="5")
        ws_accounts.append_row(ACCOUNTS_HEADERS)
        ws_accounts.append_rows(DEFAULT_ACCOUNTS)

    return sh

@st.cache_data(ttl=60)
def load_data():
    """Fetches transaction logs and account configurations from Google Sheets."""
    sh = init_google_sheet()
    ws_tracker = sh.worksheet(TRACKER_TAB)
    ws_accounts = sh.worksheet(ACCOUNTS_TAB)

    tracker_records = ws_tracker.get_all_records()
    accounts_records = ws_accounts.get_all_records()

    df_tracker = pd.DataFrame(tracker_records) if tracker_records else pd.DataFrame(columns=TRACKER_HEADERS)
    df_accounts = pd.DataFrame(accounts_records) if accounts_records else pd.DataFrame(columns=ACCOUNTS_HEADERS)

    if not df_tracker.empty:
        df_tracker["Amount"] = pd.to_numeric(df_tracker["Amount"], errors="coerce").fillna(0.0)
        df_tracker["Date_Parsed"] = pd.to_datetime(df_tracker["Date"], errors="coerce")

    if not df_accounts.empty:
        df_accounts["Initial_Balance"] = pd.to_numeric(df_accounts["Initial_Balance"], errors="coerce").fillna(0.0)
        df_accounts["Credit_Limit"] = pd.to_numeric(df_accounts["Credit_Limit"], errors="coerce").fillna(0.0)

    return df_tracker, df_accounts

def append_transaction(row_data: list):
    """Appends a single transaction row into the 'Tracker' tab and clears cache."""
    sh = init_google_sheet()
    ws_tracker = sh.worksheet(TRACKER_TAB)
    ws_tracker.append_row(row_data, value_input_option="USER_ENTERED")
    st.cache_data.clear()

def append_account(row_data: list):
    """Appends a new masked financial account into the 'Accounts' tab and clears cache."""
    sh = init_google_sheet()
    ws_accounts = sh.worksheet(ACCOUNTS_TAB)
    ws_accounts.append_row(row_data, value_input_option="USER_ENTERED")
    st.cache_data.clear()

# ==============================================================================
# 7. GMAIL API MODULE & TOKEN-OPTIMIZED GEMINI PARSING ENGINE
# ==============================================================================
def get_gmail_service():
    """Authenticates with the Gmail API using OAuth2 user credentials stored in st.secrets['gmail_oauth']."""
    if "gmail_oauth" not in st.secrets:
        raise ValueError("Missing `[gmail_oauth]` section in `st.secrets`.")

    oauth_cfg = st.secrets["gmail_oauth"]
    creds = UserOAuthCredentials(
        token=None,
        refresh_token=oauth_cfg["refresh_token"],
        token_uri=oauth_cfg.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=oauth_cfg["client_id"],
        client_secret=oauth_cfg["client_secret"],
        scopes=["https://www.googleapis.com/auth/gmail.readonly"]
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)

def extract_email_body(payload: dict) -> str:
    """Recursively extracts clean text from Gmail message MIME payloads."""
    if "body" in payload and payload["body"].get("data"):
        raw_data = payload["body"]["data"]
        decoded = base64.urlsafe_b64decode(raw_data).decode("utf-8", errors="ignore")
        if payload.get("mimeType") == "text/html":
            soup = BeautifulSoup(decoded, "html.parser")
            return soup.get_text(separator=" ", strip=True)
        return decoded

    if "parts" in payload:
        for part in payload["parts"]:
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                raw_data = part["body"]["data"]
                return base64.urlsafe_b64decode(raw_data).decode("utf-8", errors="ignore")
        for part in payload["parts"]:
            text = extract_email_body(part)
            if text:
                return text
    return ""

def fetch_recent_gmail_alerts(query: str, max_results: int = 10) -> list:
    """Fetches recent transaction emails from Gmail and truncates payload to 1,000 characters."""
    service = get_gmail_service()
    results = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    messages = results.get("messages", [])
    
    fetched_emails = []
    for msg_meta in messages:
        msg_id = msg_meta["id"]
        msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
        headers = msg.get("payload", {}).get("headers", [])
        
        subject = next((h["value"] for h in headers if h["name"].lower() == "subject"), "No Subject")
        date_hdr = next((h["value"] for h in headers if h["name"].lower() == "date"), "")
        snippet = msg.get("snippet", "")
        
        body_text = extract_email_body(msg.get("payload", {}))
        raw_combined = f"Subject: {subject} | Date: {date_hdr} | Alert: {body_text if body_text else snippet}"
        truncated_payload = prepare_token_optimized_payload(raw_combined, MAX_INPUT_CHARS)
        
        fetched_emails.append({
            "msg_id": msg_id,
            "subject": subject,
            "date_header": date_hdr,
            "raw_text": truncated_payload
        })
    return fetched_emails

def parse_message_with_gemini(raw_text: str, df_accounts: pd.DataFrame) -> dict:
    """Token-Optimized Gemini 2.5 Flash Parser with max_output_tokens=300 and temperature=0.0."""
    api_key = st.secrets.get("GEMINI_API_KEY") or st.secrets.get("gemini", {}).get("api_key")
    if not api_key:
        raise ValueError("Missing `GEMINI_API_KEY` in `st.secrets`.")

    truncated_alert = prepare_token_optimized_payload(raw_text, MAX_INPUT_CHARS)
    accounts_compact = [
        f"{r['Account_ID']} ({r['Account_Type']})" 
        for _, r in df_accounts.iterrows()
    ] if not df_accounts.empty else []

    client = genai.Client(api_key=api_key)
    today_str = date.today().strftime("%Y-%m-%d")

    prompt = (
        f"Extract financial transaction JSON. Today: {today_str}\n"
        f"Accounts: {json.dumps(accounts_compact)}\n"
        f"ExpenseCats: {json.dumps(EXPENSE_CATEGORIES)}\n"
        f"IncomeCats: {json.dumps(INCOME_CATEGORIES)}\n"
        f"TransferCats: {json.dumps(TRANSFER_CATEGORIES)}\n"
        "Rules:\n"
        "1. Match account by bank name or last 4 digits (*1092).\n"
        "2. Self-transfers, CC bill payments/auto-debits, ATM withdrawals, and Groww SIP/ELSS/Demat allocations MUST be transaction_type='Transfer' & is_self_transfer_or_allocation=true with source_account & destination_account.\n"
        "3. Merchant spends -> 'Expense', destination_account=''.\n"
        f"Alert (<=1000 chars):\n{truncated_alert}"
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ExtractedTransaction,
            temperature=GEMINI_TEMPERATURE,
            max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS
        )
    )

    usage = getattr(response, "usage_metadata", None)
    prompt_tokens = getattr(usage, "prompt_token_count", 0) or int(len(prompt) / 4)
    output_tokens = getattr(usage, "candidates_token_count", 0) or int(len(response.text or "") / 4)
    total_call_tokens = prompt_tokens + output_tokens

    if "token_telemetry" in st.session_state:
        st.session_state["token_telemetry"]["api_calls"] += 1
        st.session_state["token_telemetry"]["prompt_tokens"] += prompt_tokens
        st.session_state["token_telemetry"]["output_tokens"] += output_tokens
        st.session_state["token_telemetry"]["total_tokens"] += total_call_tokens

    raw_parsed = json.loads(response.text)
    raw_parsed["tokens_used"] = total_call_tokens
    return apply_smart_transfer_rules(raw_parsed, truncated_alert, df_accounts)

# ==============================================================================
# 8. SMART TRANSFER, RECONCILIATION & FINANCIAL ACCOUNTING ENGINE
# ==============================================================================
def compute_account_balances(df_accounts: pd.DataFrame, df_tracker: pd.DataFrame) -> pd.DataFrame:
    """Calculates dynamic balances, invested capital, and credit utilization for every account."""
    if df_accounts.empty:
        return df_accounts

    df_calc = df_accounts.copy()
    current_balances = []
    net_flows = []

    for _, acc in df_calc.iterrows():
        acc_id = acc["Account_ID"]
        acc_type = acc["Account_Type"]
        init_bal = float(acc["Initial_Balance"])

        if df_tracker.empty:
            current_balances.append(init_bal)
            net_flows.append(0.0)
            continue

        expenses = df_tracker[
            (df_tracker["Source_Account"] == acc_id) & (df_tracker["Type"] == "Expense")
        ]["Amount"].sum()
        
        transfers_out = df_tracker[
            (df_tracker["Source_Account"] == acc_id) & (df_tracker["Type"] == "Transfer")
        ]["Amount"].sum()

        incomes = df_tracker[
            (df_tracker["Source_Account"] == acc_id) & (df_tracker["Type"] == "Income")
        ]["Amount"].sum()
        
        transfers_in = df_tracker[
            (df_tracker["Destination_Account"] == acc_id) & (df_tracker["Type"] == "Transfer")
        ]["Amount"].sum()

        if acc_type == "Credit Card":
            delta = expenses + transfers_out - incomes - transfers_in
            bal = init_bal + delta
        else:
            delta = incomes + transfers_in - expenses - transfers_out
            bal = init_bal + delta

        current_balances.append(bal)
        net_flows.append(delta)

    df_calc["Net_Capital_Flow"] = net_flows
    df_calc["Current_Balance"] = current_balances
    return df_calc

def compute_dashboard_and_reconciliation_kpis(
    df_accounts_calc: pd.DataFrame, 
    df_tracker: pd.DataFrame, 
    selected_year: int, 
    selected_month: int
) -> dict:
    """Computes top-level KPIs and Phase 3 Transfer Reconciliation Breakdown."""
    liquid_mask = df_accounts_calc["Account_Type"].isin(["Bank Account", "Cash"])
    total_liquid_balance = df_accounts_calc[liquid_mask]["Current_Balance"].sum() if not df_accounts_calc.empty else 0.0

    inv_mask = df_accounts_calc["Account_Type"] == "Investment"
    df_inv = df_accounts_calc[inv_mask] if not df_accounts_calc.empty else pd.DataFrame()
    total_investments = df_inv["Current_Balance"].sum() if not df_inv.empty else 0.0
    investment_accounts_list = df_inv["Account_ID"].tolist() if not df_inv.empty else []

    cc_mask = df_accounts_calc["Account_Type"] == "Credit Card"
    df_cc = df_accounts_calc[cc_mask] if not df_accounts_calc.empty else pd.DataFrame()
    cc_accounts_list = df_cc["Account_ID"].tolist() if not df_cc.empty else []
    
    total_credit_limit = df_cc["Credit_Limit"].sum() if not df_cc.empty else 0.0
    total_credit_used = df_cc["Current_Balance"].sum() if not df_cc.empty else 0.0
    credit_utilization_pct = (total_credit_used / total_credit_limit * 100.0) if total_credit_limit > 0 else 0.0

    monthly_income = 0.0
    monthly_expense = 0.0
    monthly_transfers = 0.0
    monthly_invested_allocation = 0.0
    monthly_cc_settlements = 0.0
    monthly_self_and_atm_transfers = 0.0
    monthly_loan_prepayments = 0.0

    if not df_tracker.empty and "Date_Parsed" in df_tracker.columns:
        month_mask = (
            (df_tracker["Date_Parsed"].dt.year == selected_year) & 
            (df_tracker["Date_Parsed"].dt.month == selected_month)
        )
        df_month = df_tracker[month_mask]

        monthly_income = df_month[df_month["Type"] == "Income"]["Amount"].sum()
        monthly_expense = df_month[df_month["Type"] == "Expense"]["Amount"].sum()
        
        df_month_transfers = df_month[df_month["Type"] == "Transfer"]
        monthly_transfers = df_month_transfers["Amount"].sum()

        for _, trow in df_month_transfers.iterrows():
            amt = float(trow["Amount"])
            cat = str(trow["Category"])
            dest = str(trow["Destination_Account"])
            src = str(trow["Source_Account"])

            if dest in investment_accounts_list or "Groww" in cat:
                monthly_invested_allocation += amt
            elif src in investment_accounts_list or "Redemption" in cat:
                monthly_invested_allocation -= amt
            elif dest in cc_accounts_list or cat == "Credit Card Bill Payment":
                monthly_cc_settlements += amt
            elif "Loan" in cat or "Loan" in dest:
                monthly_loan_prepayments += amt
            else:
                monthly_self_and_atm_transfers += amt

    monthly_net_savings = monthly_income - monthly_expense
    savings_rate_pct = (monthly_net_savings / monthly_income * 100.0) if monthly_income > 0 else 0.0

    return {
        "liquid_balance": total_liquid_balance,
        "total_investments": total_investments,
        "credit_limit": total_credit_limit,
        "credit_used": total_credit_used,
        "credit_util_pct": credit_utilization_pct,
        "monthly_income": monthly_income,
        "monthly_expense": monthly_expense,
        "monthly_net_savings": monthly_net_savings,
        "savings_rate_pct": savings_rate_pct,
        "monthly_transfers": monthly_transfers,
        "monthly_invested_allocation": monthly_invested_allocation,
        "monthly_cc_settlements": monthly_cc_settlements,
        "monthly_self_and_atm_transfers": monthly_self_and_atm_transfers,
        "monthly_loan_prepayments": monthly_loan_prepayments
    }

def scan_ledger_for_misclassified_transfers(df_tracker: pd.DataFrame, df_accounts: pd.DataFrame) -> pd.DataFrame:
    """Scans existing 'Expense' or 'Income' rows in the Google Sheet for misclassified self-transfers."""
    if df_tracker.empty:
        return pd.DataFrame()

    suspect_rows = []
    non_transfer_df = df_tracker[df_tracker["Type"].isin(["Expense", "Income"])]

    for _, row in non_transfer_df.iterrows():
        test_text = f"{row.get('Notes', '')} {row.get('Category', '')}"
        for rule in SMART_TRANSFER_REGEX_RULES:
            if re.search(rule["pattern"], test_text, re.IGNORECASE):
                suspect_rows.append({
                    "Transaction_ID": row["Transaction_ID"],
                    "Date": row["Date"],
                    "Current_Type": row["Type"],
                    "Amount": row["Amount"],
                    "Source_Account": row["Source_Account"],
                    "Notes": row["Notes"],
                    "Suggested_Rule": rule["rule_name"],
                    "Suggested_Category": rule["forced_category"]
                })
                break
    return pd.DataFrame(suspect_rows)

# ==============================================================================
# 9. PHASE 5: AUTOMATED SUMMARY GENERATOR, AI SPENDING INSIGHTS & EXPORT ENGINE
# ==============================================================================
def compile_automated_summary_digest(
    df_tracker: pd.DataFrame,
    df_accounts_calc: pd.DataFrame,
    kpis: dict,
    horizon: str,
    ref_date: date,
    currency: str
) -> dict:
    """
    Phase 5 Automated Summary Generator:
    Compiles Daily, Weekly (Trailing 7 Days), or Monthly financial metrics,
    category burn rankings, and Groww allocations into a mobile-optimized executive digest.
    """
    if df_tracker.empty or "Date_Parsed" not in df_tracker.columns:
        df_period = pd.DataFrame(columns=TRACKER_HEADERS)
        period_label = ref_date.strftime("%d %b %Y")
    else:
        if horizon == "Daily Snapshot (Selected Date)":
            mask = df_tracker["Date_Parsed"].dt.date == ref_date
            period_label = f"Daily Snapshot ({ref_date.strftime('%d %b %Y')})"
        elif horizon == "Weekly Digest (Trailing 7 Days)":
            start_dt = ref_date - timedelta(days=6)
            mask = (df_tracker["Date_Parsed"].dt.date >= start_dt) & (df_tracker["Date_Parsed"].dt.date <= ref_date)
            period_label = f"Weekly Digest ({start_dt.strftime('%d %b')} - {ref_date.strftime('%d %b %Y')})"
        else:
            mask = (
                (df_tracker["Date_Parsed"].dt.year == ref_date.year) &
                (df_tracker["Date_Parsed"].dt.month == ref_date.month)
            )
            period_label = f"Monthly Executive Audit ({ref_date.strftime('%B %Y')})"
        df_period = df_tracker[mask].copy()

    df_exp = df_period[df_period["Type"] == "Expense"] if not df_period.empty else pd.DataFrame()
    df_inc = df_period[df_period["Type"] == "Income"] if not df_period.empty else pd.DataFrame()
    df_trf = df_period[df_period["Type"] == "Transfer"] if not df_period.empty else pd.DataFrame()

    period_expense = df_exp["Amount"].sum() if not df_exp.empty else 0.0
    period_income = df_inc["Amount"].sum() if not df_inc.empty else 0.0
    period_net = period_income - period_expense

    # Calculate Groww Allocations in this specific period
    inv_accounts = df_accounts_calc[df_accounts_calc["Account_Type"] == "Investment"]["Account_ID"].tolist() if not df_accounts_calc.empty else []
    period_groww_alloc = 0.0
    if not df_trf.empty and inv_accounts:
        period_groww_alloc = df_trf[df_trf["Destination_Account"].isin(inv_accounts)]["Amount"].sum()

    # Category Breakdown Table
    if not df_exp.empty and period_expense > 0:
        cat_df = (
            df_exp.groupby("Category", as_index=False)["Amount"]
            .sum()
            .sort_values(by="Amount", ascending=False)
        )
        cat_df["Share_%"] = (cat_df["Amount"] / period_expense * 100.0).round(1)
    else:
        cat_df = pd.DataFrame(columns=["Category", "Amount", "Share_%"])

    # Top 5 Largest Single Expenses (Spike Detection)
    if not df_exp.empty:
        top_spikes_df = df_exp.sort_values(by="Amount", ascending=False).head(5)[
            ["Date", "Amount", "Category", "Source_Account", "Notes"]
        ]
    else:
        top_spikes_df = pd.DataFrame(columns=["Date", "Amount", "Category", "Source_Account", "Notes"])

    # GrowwStocks vs MF split
    stocks_bal = 0.0
    mf_bal = 0.0
    if not df_accounts_calc.empty:
        df_inv = df_accounts_calc[df_accounts_calc["Account_Type"] == "Investment"]
        stocks_bal = df_inv[df_inv["Account_ID"].str.contains("Stocks|Demat", case=False, na=False)]["Current_Balance"].sum()
        mf_bal = df_inv[df_inv["Account_ID"].str.contains("Mutual Fund|ELSS|MF", case=False, na=False)]["Current_Balance"].sum()

    # Format concise Mobile Executive Text Card
    top_cat_str = (
        ", ".join([f"{r['Category']} ({currency}{r['Amount']:,.0f})" for _, r in cat_df.head(3).iterrows()])
        if not cat_df.empty else "No expenses logged"
    )

    mobile_digest_markdown = f"""📊 *{period_label}*
━━━━━━━━━━━━━━━━━━━━
💧 *Liquid Balance:* {currency}{kpis['liquid_balance']:,.2f}
🌱 *Groww Portfolio:* {currency}{kpis['total_investments']:,.2f} (Stocks: {currency}{stocks_bal:,.0f} | MF/ELSS: {currency}{mf_bal:,.0f})
💳 *Credit Utilization:* {kpis['credit_util_pct']:.1f}% ({currency}{kpis['credit_used']:,.0f} / {currency}{kpis['credit_limit']:,.0f})
━━━━━━━━━━━━━━━━━━━━
📥 *Period Income:* {currency}{period_income:,.2f}
🔥 *Operational Burn:* {currency}{period_expense:,.2f}
📈 *Period Net Savings:* {currency}{period_net:,.2f}
🚀 *Groww Capital Deployed:* {currency}{period_groww_alloc:,.2f}
🏆 *Top Spend Drivers:* {top_cat_str}"""

    return {
        "period_label": period_label,
        "df_period": df_period,
        "period_income": period_income,
        "period_expense": period_expense,
        "period_net": period_net,
        "period_groww_alloc": period_groww_alloc,
        "cat_df": cat_df,
        "top_spikes_df": top_spikes_df,
        "stocks_bal": stocks_bal,
        "mf_bal": mf_bal,
        "mobile_digest_markdown": mobile_digest_markdown
    }

def generate_ai_spending_insights(digest: dict, kpis: dict, currency: str) -> dict:
    """
    Phase 5 AI Spending Intelligence Engine:
    Sends a compact, pre-aggregated statistical summary (<800 chars) to Gemini 2.5 Flash
    to generate natural language insights on top spending categories, unusual spikes, and Groww health.
    """
    api_key = st.secrets.get("GEMINI_API_KEY") or st.secrets.get("gemini", {}).get("api_key")
    if not api_key:
        raise ValueError("Missing `GEMINI_API_KEY` in `st.secrets`.")

    cat_summary = [
        f"{r['Category']}:{currency}{r['Amount']:.0f}({r['Share_%']}%)"
        for _, r in digest["cat_df"].head(5).iterrows()
    ]
    spikes_summary = [
        f"{r['Date']}:{r['Notes'] or r['Category']}({currency}{r['Amount']:.0f})"
        for _, r in digest["top_spikes_df"].head(3).iterrows()
    ]

    compact_stats_payload = (
        f"Period: {digest['period_label']}\n"
        f"Income: {currency}{digest['period_income']:.0f} | OperatingBurn: {currency}{digest['period_expense']:.0f} | "
        f"NetSavings: {currency}{digest['period_net']:.0f} | GrowwAllocated: {currency}{digest['period_groww_alloc']:.0f}\n"
        f"LiquidCash: {currency}{kpis['liquid_balance']:.0f} | TotalGrowwPortfolio: {currency}{kpis['total_investments']:.0f} "
        f"(Stocks:{currency}{digest['stocks_bal']:.0f}, MF/ELSS:{currency}{digest['mf_bal']:.0f})\n"
        f"CreditCardUtil: {kpis['credit_util_pct']:.1f}% ({currency}{kpis['credit_used']:.0f}/{currency}{kpis['credit_limit']:.0f})\n"
        f"TopCategories: {json.dumps(cat_summary)}\n"
        f"LargestSpikes: {json.dumps(spikes_summary)}"
    )[:MAX_INPUT_CHARS]

    prompt = (
        "You are an elite personal wealth & spending advisor for an Indian investor using Groww.\n"
        "Analyze the pre-aggregated financial digest below and return structured JSON insights.\n"
        f"Digest:\n{compact_stats_payload}"
    )

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=AISpendingInsightReport,
            temperature=0.2,
            max_output_tokens=GEMINI_INSIGHTS_MAX_TOKENS
        )
    )

    usage = getattr(response, "usage_metadata", None)
    prompt_tokens = getattr(usage, "prompt_token_count", 0) or int(len(prompt) / 4)
    output_tokens = getattr(usage, "candidates_token_count", 0) or int(len(response.text or "") / 4)
    total_call_tokens = prompt_tokens + output_tokens

    if "token_telemetry" in st.session_state:
        st.session_state["token_telemetry"]["api_calls"] += 1
        st.session_state["token_telemetry"]["prompt_tokens"] += prompt_tokens
        st.session_state["token_telemetry"]["output_tokens"] += output_tokens
        st.session_state["token_telemetry"]["total_tokens"] += total_call_tokens

    parsed_insights = json.loads(response.text)
    parsed_insights["tokens_used"] = total_call_tokens
    return parsed_insights

def build_markdown_audit_report(
    digest: dict,
    kpis: dict,
    df_accounts_calc: pd.DataFrame,
    ai_insights: dict | None,
    currency: str
) -> str:
    """Compiles a comprehensive, downloadable Markdown (.md) financial audit report for archival."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# 🏦 Personal Finance & Groww Portfolio Audit Report",
        f"**Period:** {digest['period_label']}  ",
        f"**Generated On:** {now_str}\n",
        "---",
        "## 1. Executive Summary & Portfolio KPIs",
        f"- **Total Liquid Balance (Bank + Cash):** {currency}{kpis['liquid_balance']:,.2f}",
        f"- **Total Groww Investment Portfolio:** {currency}{kpis['total_investments']:,.2f} (Stocks Demat: {currency}{digest['stocks_bal']:,.2f} | Mutual Funds/ELSS: {currency}{digest['mf_bal']:,.2f})",
        f"- **Credit Card Limit Utilization:** {kpis['credit_util_pct']:.1f}% ({currency}{kpis['credit_used']:,.2f} used of {currency}{kpis['credit_limit']:,.2f})",
        f"- **Period Operating Income:** {currency}{digest['period_income']:,.2f}",
        f"- **Period True Operational Burn (Expenses):** {currency}{digest['period_expense']:,.2f}",
        f"- **Period Net Savings:** {currency}{digest['period_net']:,.2f}",
        f"- **Period Groww Capital Deployed (SIPs/Stocks):** {currency}{digest['period_groww_alloc']:,.2f}\n",
        "---",
        "## 2. Top Expense Categories Breakdown"
    ]

    if not digest["cat_df"].empty:
        for _, r in digest["cat_df"].iterrows():
            lines.append(f"- **{r['Category']}:** {currency}{r['Amount']:,.2f} ({r['Share_%']:.1f}%)")
    else:
        lines.append("- *No operating expenses recorded for this period.*")

    lines.append("\n---")
    lines.append("## 3. Largest Individual Expense Spikes")
    if not digest["top_spikes_df"].empty:
        for _, r in digest["top_spikes_df"].iterrows():
            lines.append(f"- **{r['Date']}** | `{r['Category']}` | **{currency}{r['Amount']:,.2f}** via *{r['Source_Account']}* ({r['Notes']})")
    else:
        lines.append("- *No expense transactions recorded.*")

    if ai_insights:
        lines.extend([
            "\n---",
            "## 4. Gemini 2.5 Flash AI Spending & Wealth Advisory",
            f"**Headline:** {ai_insights.get('executive_headline', '')}\n",
            f"- **Spending & Burn Velocity:** {ai_insights.get('top_spending_analysis', '')}",
            f"- **Anomaly & Spike Alert:** {ai_insights.get('burn_spike_alert', '')}",
            f"- **Groww Portfolio & Credit Health:** {ai_insights.get('portfolio_and_credit_health', '')}\n",
            "### Actionable Recommendations:"
        ])
        for idx, rec in enumerate(ai_insights.get("actionable_recommendations", []), 1):
            lines.append(f"{idx}. {rec}")

    lines.extend(["\n---", "## 5. Master Account Balances Snapshot"])
    if not df_accounts_calc.empty:
        for _, acc in df_accounts_calc.iterrows():
            lines.append(f"- **{acc['Account_ID']}** ({acc['Account_Type']}): {currency}{acc['Current_Balance']:,.2f}")

    return "\n".join(lines)

# ==============================================================================
# 10. MAIN APPLICATION UI & SIDEBAR MODULE
# ==============================================================================
def main():
    if "pending_parsed_txns" not in st.session_state:
        st.session_state["pending_parsed_txns"] = []
    if "processed_msg_ids" not in st.session_state:
        st.session_state["processed_msg_ids"] = set()
    if "ai_insights_cache" not in st.session_state:
        st.session_state["ai_insights_cache"] = None
    if "token_telemetry" not in st.session_state:
        st.session_state["token_telemetry"] = {
            "api_calls": 0,
            "skipped_by_prefilter": 0,
            "prompt_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_tokens_saved": 0
        }

    df_tracker, df_accounts = load_data()
    df_accounts_calc = compute_account_balances(df_accounts, df_tracker)

    # Sidebar Configuration
    with st.sidebar:
        st.header("⚙️ Master Configuration")
        currency = st.selectbox("Currency Symbol", ["₹", "$", "€", "£"], index=0)
        
        if st.button("🔄 Force Sync Google Sheet", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

        if not df_accounts.empty:
            existing_ids = df_accounts["Account_ID"].tolist()
            missing_groww = [
                row for row in DEFAULT_ACCOUNTS 
                if "Groww" in row[0] and row[0] not in existing_ids
            ]
            if missing_groww:
                st.info("🌱 Groww default accounts detected as missing from your Google Sheet.")
                if st.button("➕ Provision Groww Accounts", use_container_width=True):
                    for g_acc in missing_groww:
                        append_account(g_acc)
                    st.success("Groww Demat & MF accounts provisioned!")
                    st.rerun()

        st.divider()
        st.subheader("🏦 Masked Accounts Master")
        st.caption("All accounts enforce masked identifiers (e.g., `*1092`) for security.")

        if not df_accounts_calc.empty:
            for acc_type, group in df_accounts_calc.groupby("Account_Type"):
                with st.expander(
                    f"{acc_type}s ({len(group)})", 
                    expanded=(acc_type in ["Bank Account", "Investment", "Credit Card"])
                ):
                    for _, row in group.iterrows():
                        if acc_type == "Credit Card":
                            limit = row["Credit_Limit"]
                            used = row["Current_Balance"]
                            util = (used / limit * 100) if limit > 0 else 0
                            st.markdown(f"**{row['Account_ID']}**  \nUsed: `{currency}{used:,.2f}` / `{currency}{limit:,.0f}` ({util:.1f}%)")
                            st.progress(min(max(util / 100.0, 0.0), 1.0))
                        else:
                            st.markdown(f"**{row['Account_ID']}**: `{currency}{row['Current_Balance']:,.2f}`")

        st.divider()
        with st.expander("➕ Add New Financial Account"):
            with st.form("add_account_form", clear_on_submit=True):
                acc_name = st.text_input("Institution / Label", placeholder="e.g., Groww ELSS Tax Saver")
                acc_digits = st.text_input("Last 4 Digits (Masked)", max_chars=4, placeholder="e.g., 9903")
                acc_type = st.selectbox("Account Category", ["Bank Account", "Investment", "Credit Card", "Cash", "Loan"])
                init_bal = st.number_input("Opening Valuation / Balance", value=0.0, step=500.0)
                cred_limit = st.number_input("Credit Limit (Cards Only)", value=0.0, min_value=0.0, step=5000.0)
                
                submit_acc = st.form_submit_button("Save Masked Account", use_container_width=True)
                if submit_acc:
                    if not acc_name.strip() or not re.match(r"^\d{4}$", acc_digits.strip()):
                        st.error("Please provide a valid name and exactly 4 numerical digits (e.g., 9901).")
                    else:
                        masked_id = f"{acc_name.strip()} *{acc_digits.strip()}"
                        if masked_id in df_accounts["Account_ID"].values:
                            st.error("Account ID already exists!")
                        else:
                            append_account([masked_id, acc_type, init_bal, cred_limit if acc_type == "Credit Card" else 0.0])
                            st.success(f"Added `{masked_id}`!")
                            st.rerun()

    # ==========================================================================
    # MAIN BODY: HEADER & DASHBOARD METRICS
    # ==========================================================================
    col_title, col_filter = st.columns([3, 1])
    with col_title:
        st.title("💸 Personal Finance & Groww AI Hub")
    with col_filter:
        today = date.today()
        selected_date_filter = st.date_input("Dashboard Reference Date", value=today)

    kpis = compute_dashboard_and_reconciliation_kpis(
        df_accounts_calc, 
        df_tracker, 
        selected_date_filter.year, 
        selected_date_filter.month
    )

    m1, m2, m3 = st.columns(3)
    with m1:
        st.metric(
            label="💧 Total Liquid Balance",
            value=f"{currency}{kpis['liquid_balance']:,.2f}",
            delta=f"Groww / Investments: {currency}{kpis['total_investments']:,.0f}",
            delta_color="off"
        )
    with m2:
        st.metric(
            label="💳 Credit Limit Utilization",
            value=f"{kpis['credit_util_pct']:.1f}%",
            delta=f"{currency}{kpis['credit_used']:,.0f} / {currency}{kpis['credit_limit']:,.0f} Limit",
            delta_color="inverse" if kpis['credit_util_pct'] > 30 else "normal"
        )
    with m3:
        month_label = selected_date_filter.strftime("%B %Y")
        st.metric(
            label=f"📈 Monthly Net Savings ({month_label})",
            value=f"{currency}{kpis['monthly_net_savings']:,.2f}",
            delta=f"Burn: {currency}{kpis['monthly_expense']:,.0f} | Invested: {currency}{kpis['monthly_invested_allocation']:,.0f}",
            delta_color="normal" if kpis['monthly_net_savings'] >= 0 else "inverse"
        )

    st.divider()

    # ==========================================================================
    # CORE MODULE TABS (INCLUDING PHASE 5 AI INSIGHTS & REPORTS TAB)
    # ==========================================================================
    pending_count = len(st.session_state["pending_parsed_txns"])
    inbox_tab_label = f"📥 Smart Inbox Parse ({pending_count})" if pending_count > 0 else "📥 Smart Inbox Parse"

    tab_logger, tab_inbox, tab_history, tab_accounts, tab_insights = st.tabs([
        "📝 Log Transaction", 
        inbox_tab_label,
        "📊 Ledger & Reconciliation", 
        "🏦 Account, Groww & Credit Health",
        "🧠 AI Insights & Reports"
    ])

    account_list = df_accounts_calc["Account_ID"].tolist() if not df_accounts_calc.empty else []

    # --------------------------------------------------------------------------
    # TAB 1: MOBILE-OPTIMIZED MANUAL TRANSACTION LOGGER
    # --------------------------------------------------------------------------
    with tab_logger:
        st.subheader("Record New Transaction")
        tx_type = st.radio(
            "Select Transaction Type",
            options=["Expense", "Income", "Transfer"],
            horizontal=True
        )

        if tx_type == "Transfer":
            st.caption("💡 **Smart Transfer Mode:** Moving money between your Bank, Credit Card, or Groww accounts updates account balances without inflating your Monthly Expense Burn Rate.")

        with st.form("transaction_logger_form", clear_on_submit=True):
            c1, c2 = st.columns(2)
            with c1:
                tx_date = st.date_input("Date", value=date.today())
                tx_amount = st.number_input(f"Amount ({currency})", min_value=0.01, step=500.0, format="%.2f")
            
            with c2:
                if tx_type == "Expense":
                    tx_category = st.selectbox("Category", EXPENSE_CATEGORIES)
                    tx_source = st.selectbox("Paid From (Account / Credit Card)", account_list)
                    tx_dest = ""
                elif tx_type == "Income":
                    tx_category = st.selectbox("Category", INCOME_CATEGORIES)
                    tx_source = st.selectbox("Deposited Into (Bank / Groww Account)", account_list)
                    tx_dest = ""
                else:
                    tx_category = st.selectbox("Transfer / Asset Allocation Type", TRANSFER_CATEGORIES)
                    tx_source = st.selectbox("From Source Account (Outflow)", account_list, index=0)
                    
                    default_dest_idx = min(1, len(account_list) - 1) if len(account_list) > 1 else 0
                    for idx, acc_name in enumerate(account_list):
                        if "Groww" in acc_name:
                            default_dest_idx = idx
                            break
                            
                    tx_dest = st.selectbox(
                        "To Destination Account (Inflow)", 
                        account_list, 
                        index=default_dest_idx
                    )

            tx_notes = st.text_input(
                "Notes / Merchant / Security Name", 
                placeholder="e.g., Parag Parikh Flexi Cap SIP, Amex Bill Auto-Debit, Self Transfer, Swiggy..."
            )

            auto_rule_guard = st.checkbox(
                "🛡️ Enable Phase 3 Smart Transfer Auto-Correction (Automatically converts accidental self-transfer/SIP notes into Transfers)",
                value=True
            )

            submitted = st.form_submit_button("🚀 Save to Google Sheets", use_container_width=True, type="primary")

            if submitted:
                if not account_list:
                    st.error("Please configure at least one account in the sidebar first.")
                else:
                    candidate = {
                        "transaction_type": tx_type,
                        "amount": float(tx_amount),
                        "date": tx_date.strftime("%Y-%m-%d"),
                        "category": tx_category,
                        "source_account": tx_source,
                        "destination_account": tx_dest if tx_type == "Transfer" else "",
                        "notes": tx_notes.strip()
                    }
                    if auto_rule_guard:
                        candidate = apply_smart_transfer_rules(candidate, tx_notes, df_accounts)

                    if candidate["transaction_type"] == "Transfer" and candidate["source_account"] == candidate["destination_account"]:
                        st.error("⚠️ Source and Destination accounts cannot be identical for an internal transfer.")
                    else:
                        tx_id = f"TXN-{uuid.uuid4().hex[:8].upper()}"
                        timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        row_payload = [
                            tx_id,
                            candidate["date"],
                            candidate["transaction_type"],
                            candidate["amount"],
                            candidate["category"],
                            candidate["source_account"],
                            candidate["destination_account"],
                            candidate["notes"],
                            timestamp_str
                        ]
                        with st.spinner("Syncing with Google Sheets 'Tracker' tab..."):
                            append_transaction(row_payload)
                        if candidate.get("transfer_rule_matched") and tx_type != "Transfer":
                            st.info(f"🛡️ Smart Transfer Guardrail auto-classified this entry as a **Transfer** (`{candidate['transfer_rule_matched']}`)!")
                        st.success(f"✅ {candidate['transaction_type']} of {currency}{tx_amount:,.2f} logged! (ID: `{tx_id}`)")
                        st.rerun()

    # --------------------------------------------------------------------------
    # TAB 2: SMART INBOX PARSE (PHASE 4 TOKEN-OPTIMIZED PIPELINE)
    # --------------------------------------------------------------------------
    with tab_inbox:
        st.subheader("🤖 Automated Email & SMS Ingestion (Token-Optimized Firewall)")
        st.caption(
            f"⚡ **Phase 4 Token Firewall Active:** Pre-filters irrelevant OTPs/promos locally, truncates payloads to `{MAX_INPUT_CHARS}` chars, "
            f"and enforces `max_output_tokens={GEMINI_MAX_OUTPUT_TOKENS}` & `temperature={GEMINI_TEMPERATURE}` to preserve your 10-Lakh monthly token budget."
        )

        telem = st.session_state["token_telemetry"]
        t_col1, t_col2, t_col3, t_col4 = st.columns(4)
        with t_col1:
            pct_used = (telem["total_tokens"] / MONTHLY_TOKEN_BUDGET) * 100.0
            st.metric("🪙 Session Tokens Used", f"{telem['total_tokens']:,}", delta=f"{pct_used:.4f}% of 10L Budget", delta_color="off")
        with t_col2:
            st.metric("🛡️ Alerts Blocked Locally", f"{telem['skipped_by_prefilter']}", delta=f"~{telem['estimated_tokens_saved']:,} Tokens Saved")
        with t_col3:
            st.metric("✂️ Input Payload Cap", f"{MAX_INPUT_CHARS} chars", delta="Strict Truncation Active", delta_color="off")
        with t_col4:
            st.metric("🎯 Output Cap & Temp", f"{GEMINI_MAX_OUTPUT_TOKENS} tok | T={GEMINI_TEMPERATURE}", delta=f"{telem['api_calls']} Gemini Calls", delta_color="off")

        col_gmail, col_sms = st.columns([3, 2])

        with col_gmail:
            with st.container(border=True):
                st.markdown("#### 📧 1. Fetch from Connected Gmail")
                gmail_query = st.text_input("Gmail Search Filter", value=DEFAULT_GMAIL_QUERY)
                max_emails = st.slider("Max Recent Emails to Scan", min_value=1, max_value=25, value=5)

                if st.button("🔄 Fetch, Pre-Filter & Parse Gmail Alerts", use_container_width=True, type="primary"):
                    try:
                        with st.spinner("Connecting to Gmail API & truncating payloads to 1,000 chars..."):
                            emails = fetch_recent_gmail_alerts(query=gmail_query, max_results=max_emails)

                        if not emails:
                            st.info("No matching emails found for the query.")
                        else:
                            new_parsed_count = 0
                            skipped_local_count = 0
                            progress_bar = st.progress(0, text="Running Local Pre-Filter & Gemini 2.5 Flash...")
                            for idx, mail in enumerate(emails):
                                if mail["msg_id"] in st.session_state["processed_msg_ids"]:
                                    progress_bar.progress((idx + 1) / len(emails))
                                    continue

                                st.session_state["processed_msg_ids"].add(mail["msg_id"])

                                passed_filter, filter_reason = passes_local_financial_prefilter(mail["raw_text"])
                                if not passed_filter:
                                    skipped_local_count += 1
                                    st.session_state["token_telemetry"]["skipped_by_prefilter"] += 1
                                    st.session_state["token_telemetry"]["estimated_tokens_saved"] += 450
                                    progress_bar.progress((idx + 1) / len(emails))
                                    continue

                                parsed = parse_message_with_gemini(mail["raw_text"], df_accounts)

                                if parsed.get("is_financial_transaction") and float(parsed.get("amount", 0)) > 0:
                                    parsed["queue_id"] = f"Q-{uuid.uuid4().hex[:6].upper()}"
                                    parsed["source_channel"] = f"Gmail: {mail['subject'][:45]}"
                                    parsed["raw_snippet"] = mail["raw_text"]
                                    parsed["prefilter_status"] = filter_reason
                                    st.session_state["pending_parsed_txns"].append(parsed)
                                    new_parsed_count += 1

                                progress_bar.progress((idx + 1) / len(emails))

                            st.success(
                                f"✨ Scanned {len(emails)} email(s): **{new_parsed_count}** extracted via Gemini, "
                                f"**{skipped_local_count}** irrelevant notifications blocked locally (0 tokens used)!"
                            )
                            st.rerun()
                    except Exception as e:
                        st.error(f"⚠️ Gmail / Gemini Integration Error: {e}")

        with col_sms:
            with st.container(border=True):
                st.markdown("#### 💬 2. Paste SMS / Alert Text")
                raw_sms_input = st.text_area(
                    "Paste Bank SMS, UPI Alert, CC Auto-Debit, or Groww SIP Confirmation",
                    height=115,
                    placeholder="e.g., Rs. 15,000.00 debited from HDFC Bank A/c *1092 towards SIP installment processed for Parag Parikh Flexi Cap Fund via ICCL."
                )
                if st.button("✨ Pre-Filter & Parse SMS Text", use_container_width=True):
                    if not raw_sms_input.strip():
                        st.warning("Please paste an SMS or email alert first.")
                    else:
                        truncated_sms = prepare_token_optimized_payload(raw_sms_input.strip(), MAX_INPUT_CHARS)
                        passed_filter, filter_reason = passes_local_financial_prefilter(truncated_sms)

                        if not passed_filter:
                            st.session_state["token_telemetry"]["skipped_by_prefilter"] += 1
                            st.session_state["token_telemetry"]["estimated_tokens_saved"] += 450
                            st.warning(f"🛡️ **Blocked by Local Token Pre-Filter (0 Tokens Used):** {filter_reason}")
                        else:
                            try:
                                with st.spinner("Pre-filter passed! Extracting with Gemini (max_output_tokens=300, temp=0.0)..."):
                                    parsed = parse_message_with_gemini(truncated_sms, df_accounts)
                                    if parsed.get("is_financial_transaction") and float(parsed.get("amount", 0)) > 0:
                                        parsed["queue_id"] = f"Q-{uuid.uuid4().hex[:6].upper()}"
                                        parsed["source_channel"] = "Manual SMS / Text Paste"
                                        parsed["raw_snippet"] = truncated_sms
                                        parsed["prefilter_status"] = filter_reason
                                        st.session_state["pending_parsed_txns"].append(parsed)
                                        st.success(f"✅ Extracted ({parsed.get('tokens_used', 0)} tokens) & added to Review Queue!")
                                        st.rerun()
                                    else:
                                        st.warning("Gemini determined this text is not a completed financial transaction.")
                            except Exception as e:
                                st.error(f"⚠️ Gemini Parsing Error: {e}")

        st.divider()

        q_header_col1, q_header_col2 = st.columns([4, 1])
        with q_header_col1:
            st.markdown(f"### 📋 Pending Review & Approval Queue (`{len(st.session_state['pending_parsed_txns'])}`)")
        with q_header_col2:
            if st.session_state["pending_parsed_txns"]:
                if st.button("🗑️ Clear All Pending", use_container_width=True):
                    st.session_state["pending_parsed_txns"] = []
                    st.rerun()

        if not st.session_state["pending_parsed_txns"]:
            st.info("No pending parsed transactions. Fetch your Gmail alerts or paste an SMS above to test the Token-Optimized Engine!")
        else:
            for idx, item in enumerate(list(st.session_state["pending_parsed_txns"])):
                qid = item["queue_id"]
                with st.container(border=True):
                    top_c1, top_c2 = st.columns([3, 2])
                    with top_c1:
                        rule_badge = item.get("transfer_rule_matched", "Standard Expense/Income")
                        tok_badge = f"🪙 `{item.get('tokens_used', 0)} tok`"
                        if item.get("transaction_type") == "Transfer":
                            st.markdown(f"**{item.get('source_channel', 'Alert')}** • `{qid}` • {tok_badge} • 🔄 **Auto-Rule: `{rule_badge}`**")
                        else:
                            st.markdown(f"**{item.get('source_channel', 'Alert')}** • `{qid}` • {tok_badge}")
                    with top_c2:
                        st.caption(f"🧠 *AI Match:* {item.get('confidence_note', '')}")

                    dup_warning = detect_transfer_mirror_duplicates(
                        item, df_tracker, st.session_state["pending_parsed_txns"]
                    )
                    if dup_warning:
                        st.warning(dup_warning)

                    e1, e2, e3, e4 = st.columns(4)
                    with e1:
                        type_options = ["Expense", "Income", "Transfer"]
                        default_t_idx = type_options.index(item["transaction_type"]) if item.get("transaction_type") in type_options else 0
                        rev_type = st.selectbox("Type", type_options, index=default_t_idx, key=f"type_{qid}")
                    with e2:
                        rev_amount = st.number_input(
                            f"Amount ({currency})", 
                            value=float(item.get("amount", 0.0)), 
                            step=50.0, 
                            format="%.2f", 
                            key=f"amt_{qid}"
                        )
                    with e3:
                        try:
                            parsed_dt = datetime.strptime(item.get("date", ""), "%Y-%m-%d").date()
                        except ValueError:
                            parsed_dt = date.today()
                        rev_date = st.date_input("Date", value=parsed_dt, key=f"dt_{qid}")
                    with e4:
                        cat_idx = ALL_CATEGORIES.index(item["category"]) if item.get("category") in ALL_CATEGORIES else 0
                        rev_cat = st.selectbox("Category", ALL_CATEGORIES, index=cat_idx, key=f"cat_{qid}")

                    e5, e6, e7 = st.columns([2, 2, 3])
                    with e5:
                        src_idx = account_list.index(item["source_account"]) if item.get("source_account") in account_list else 0
                        rev_src = st.selectbox("Source Account", account_list, index=src_idx, key=f"src_{qid}")
                    with e6:
                        dest_options = [""] + account_list
                        dest_idx = dest_options.index(item["destination_account"]) if item.get("destination_account") in dest_options else 0
                        rev_dest = st.selectbox(
                            "Destination Account (Transfers Only)", 
                            dest_options, 
                            index=dest_idx, 
                            disabled=(rev_type != "Transfer"),
                            key=f"dest_{qid}"
                        )
                    with e7:
                        rev_notes = st.text_input("Merchant / Notes", value=item.get("notes", ""), key=f"notes_{qid}")

                    with st.expander(f"🔍 View Truncated Payload (<= {MAX_INPUT_CHARS} chars) & Pre-Filter Log"):
                        st.caption(f"**Pre-Filter Status:** {item.get('prefilter_status', 'Passed')} | **Payload Length:** `{len(item.get('raw_snippet', ''))}` chars")
                        st.code(item.get("raw_snippet", ""), language="text")

                    btn_col1, btn_col2 = st.columns([3, 1])
                    with btn_col1:
                        if st.button("✅ Approve & Push to Google Sheet", key=f"approve_{qid}", use_container_width=True, type="primary"):
                            if rev_type == "Transfer" and (not rev_dest or rev_src == rev_dest):
                                st.error("⚠️ Please select a valid, distinct Destination Account for this Transfer.")
                            else:
                                tx_id = f"TXN-{uuid.uuid4().hex[:8].upper()}"
                                timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                row_payload = [
                                    tx_id,
                                    rev_date.strftime("%Y-%m-%d"),
                                    rev_type,
                                    float(rev_amount),
                                    rev_cat,
                                    rev_src,
                                    rev_dest if rev_type == "Transfer" else "",
                                    rev_notes.strip(),
                                    timestamp_str
                                ]
                                with st.spinner("Appending to Google Sheets 'Tracker'..."):
                                    append_transaction(row_payload)
                                st.session_state["pending_parsed_txns"].pop(idx)
                                st.toast(f"✅ Logged {currency}{rev_amount:,.2f} ({rev_notes}) to Google Sheets!")
                                st.rerun()
                    with btn_col2:
                        if st.button("🗑️ Dismiss", key=f"dismiss_{qid}", use_container_width=True):
                            st.session_state["pending_parsed_txns"].pop(idx)
                            st.rerun()

    # --------------------------------------------------------------------------
    # TAB 3: PHASE 3 TRANSFER RECONCILIATION UI & TRANSACTION LEDGER
    # --------------------------------------------------------------------------
    with tab_history:
        st.subheader(f"🛡️ Transfer Reconciliation & Burn-Rate Quick-Audit ({month_label})")
        st.caption(
            "Audits and separates your non-operating capital movements (Groww SIPs, Credit Card settlements, and Bank Self-Transfers) "
            "from your true operational living expenses."
        )

        rec1, rec2, rec3, rec4 = st.columns(4)
        with rec1:
            st.metric(
                label="🔥 True Operational Burn Rate",
                value=f"{currency}{kpis['monthly_expense']:,.2f}",
                delta=f"Savings Rate: {kpis['savings_rate_pct']:.1f}%",
                delta_color="normal" if kpis["savings_rate_pct"] >= 20 else "off"
            )
        with rec2:
            st.metric(
                label="🌱 Groww Asset Allocations",
                value=f"{currency}{kpis['monthly_invested_allocation']:,.2f}",
                delta="SIPs, ELSS & Demat Stocks",
                delta_color="off"
            )
        with rec3:
            st.metric(
                label="💳 Credit Card Settlements",
                value=f"{currency}{kpis['monthly_cc_settlements']:,.2f}",
                delta="Excluded from Expense Burn",
                delta_color="off"
            )
        with rec4:
            other_internal = kpis["monthly_self_and_atm_transfers"] + kpis["monthly_loan_prepayments"]
            st.metric(
                label="🔄 Self-Transfers & ATM / Loans",
                value=f"{currency}{other_internal:,.2f}",
                delta=f"Total Internal: {currency}{kpis['monthly_transfers']:,.0f}",
                delta_color="off"
            )

        with st.expander("📋 View Detailed Monthly Flow Reconciliation & Historical Misclassification Audit", expanded=False):
            reconcil_col1, reconcil_col2 = st.columns([3, 2])
            with reconcil_col1:
                st.markdown("##### Cash-Flow & Non-Operating Transfer Matrix")
                df_reconcil = pd.DataFrame([
                    {"Flow Bucket": "1. External Operating Income", "Classification": "Income", "Monthly Total": kpis["monthly_income"], "Impact on Burn Rate": "Increases Net Savings"},
                    {"Flow Bucket": "2. True Living & Operating Expenses", "Classification": "Expense", "Monthly Total": kpis["monthly_expense"], "Impact on Burn Rate": "Included in Burn Rate"},
                    {"Flow Bucket": "3. Groww SIPs / ELSS / Demat Allocations", "Classification": "Transfer (Asset Allocation)", "Monthly Total": kpis["monthly_invested_allocation"], "Impact on Burn Rate": "0% (Excluded from Burn)"},
                    {"Flow Bucket": "4. Credit Card Bill Settlements", "Classification": "Transfer (Liability Paydown)", "Monthly Total": kpis["monthly_cc_settlements"], "Impact on Burn Rate": "0% (Excluded from Burn)"},
                    {"Flow Bucket": "5. Bank-to-Bank Self-Transfers & ATM Cash", "Classification": "Transfer (Internal Liquidity)", "Monthly Total": kpis["monthly_self_and_atm_transfers"], "Impact on Burn Rate": "0% (Excluded from Burn)"},
                    {"Flow Bucket": "6. Loan Principal Prepayments", "Classification": "Transfer (Debt Reduction)", "Monthly Total": kpis["monthly_loan_prepayments"], "Impact on Burn Rate": "0% (Excluded from Burn)"}
                ])
                st.dataframe(
                    df_reconcil,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Monthly Total": st.column_config.NumberColumn(f"Amount ({currency})", format="%.2f")
                    }
                )
            with reconcil_col2:
                st.markdown("##### 🔍 Historical Ledger Audit Scanner")
                df_suspects = scan_ledger_for_misclassified_transfers(df_tracker, df_accounts)
                if df_suspects.empty:
                    st.success("✅ **100% Clean Ledger!** Zero self-transfers, credit card payments, or Groww SIPs are misclassified as Expenses or Incomes.")
                else:
                    st.warning(f"⚠️ Found **{len(df_suspects)}** historical row(s) that match self-transfer patterns:")
                    st.dataframe(df_suspects, use_container_width=True, hide_index=True)

        st.divider()
        st.subheader("Synchronized Transaction Ledger")

        if df_tracker.empty:
            st.write("No transactions logged yet.")
        else:
            f_col1, f_col2, f_col3 = st.columns(3)
            with f_col1:
                filter_types = st.multiselect(
                    "Filter by Type", 
                    options=["Expense", "Income", "Transfer"], 
                    default=["Expense", "Income", "Transfer"]
                )
            with f_col2:
                filter_accounts = st.multiselect("Filter by Account", options=account_list)
            with f_col3:
                search_query = st.text_input("Search Notes / Category", placeholder="Search SIP, ELSS, CC payment, merchant...")

            df_view = df_tracker.copy()
            if filter_types:
                df_view = df_view[df_view["Type"].isin(filter_types)]
            if filter_accounts:
                df_view = df_view[
                    df_view["Source_Account"].isin(filter_accounts) | 
                    df_view["Destination_Account"].isin(filter_accounts)
                ]
            if search_query:
                q = search_query.lower()
                df_view = df_view[
                    df_view["Notes"].astype(str).str.lower().str.contains(q) |
                    df_view["Category"].astype(str).str.lower().str.contains(q)
                ]

            df_view = df_view.sort_values(by=["Date", "Timestamp"], ascending=[False, False])
            display_cols = [
                "Date", "Type", "Amount", "Category", 
                "Source_Account", "Destination_Account", "Notes", "Transaction_ID"
            ]
            st.dataframe(
                df_view[display_cols],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Amount": st.column_config.NumberColumn(f"Amount ({currency})", format="%.2f")
                }
            )

    # --------------------------------------------------------------------------
    # TAB 4: GROWW INVESTMENT PORTFOLIO, LIQUID BALANCES & CREDIT HEALTH
    # --------------------------------------------------------------------------
    with tab_accounts:
        st.subheader("Master Portfolio, Liquid & Credit Health")
        if df_accounts_calc.empty:
            st.warning("No accounts configured.")
        else:
            st.markdown("### 📈 Groww Investment Portfolio (Stocks Demat & Mutual Funds)")
            df_inv_view = df_accounts_calc[df_accounts_calc["Account_Type"] == "Investment"].copy()

            if not df_inv_view.empty:
                total_inv_capital = df_inv_view["Current_Balance"].sum()
                initial_inv_capital = df_inv_view["Initial_Balance"].sum()
                net_new_allocated = df_inv_view["Net_Capital_Flow"].sum()

                stocks_val = df_inv_view[
                    df_inv_view["Account_ID"].str.contains("Stocks|Demat", case=False, na=False)
                ]["Current_Balance"].sum()
                mf_val = df_inv_view[
                    df_inv_view["Account_ID"].str.contains("Mutual Fund|ELSS|MF", case=False, na=False)
                ]["Current_Balance"].sum()

                inv_c1, inv_c2, inv_c3, inv_c4 = st.columns(4)
                with inv_c1:
                    st.metric("Total Invested Capital", f"{currency}{total_inv_capital:,.2f}")
                with inv_c2:
                    st.metric("Groww Stocks Demat", f"{currency}{stocks_val:,.2f}")
                with inv_c3:
                    st.metric("Groww Mutual Funds / ELSS", f"{currency}{mf_val:,.2f}")
                with inv_c4:
                    st.metric(
                        "Net New Capital Deployed", 
                        f"{currency}{net_new_allocated:,.2f}",
                        delta=f"Base: {currency}{initial_inv_capital:,.0f}",
                        delta_color="normal" if net_new_allocated >= 0 else "inverse"
                    )

                df_inv_view["Allocation_%"] = (
                    (df_inv_view["Current_Balance"] / total_inv_capital * 100.0).round(1)
                    if total_inv_capital > 0 else 0.0
                )

                st.dataframe(
                    df_inv_view[[
                        "Account_ID", "Initial_Balance", "Net_Capital_Flow", 
                        "Current_Balance", "Allocation_%"
                    ]],
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Account_ID": st.column_config.TextColumn("Investment Portfolio Account"),
                        "Initial_Balance": st.column_config.NumberColumn(f"Starting Valuation ({currency})", format="%.2f"),
                        "Net_Capital_Flow": st.column_config.NumberColumn(f"Net SIP / Capital Added ({currency})", format="%.2f"),
                        "Current_Balance": st.column_config.NumberColumn(f"Total Invested Balance ({currency})", format="%.2f"),
                        "Allocation_%": st.column_config.ProgressColumn("Portfolio Share (%)", format="%.1f%%", min_value=0, max_value=100),
                    }
                )
            else:
                st.info("No Investment accounts found. Use the Sidebar to provision your Groww accounts.")

            st.divider()

            col_a, col_b = st.columns(2)
            with col_a:
                st.markdown("#### 🏦 Liquid (Bank & Cash) & Loan Accounts")
                df_liquid_loans = df_accounts_calc[
                    df_accounts_calc["Account_Type"].isin(["Bank Account", "Cash", "Loan"])
                ][["Account_ID", "Account_Type", "Initial_Balance", "Current_Balance"]]
                
                st.dataframe(
                    df_liquid_loans,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Initial_Balance": st.column_config.NumberColumn(f"Opening Balance ({currency})", format="%.2f"),
                        "Current_Balance": st.column_config.NumberColumn(f"Current Balance ({currency})", format="%.2f"),
                    }
                )

            with col_b:
                st.markdown("#### 💳 Credit Cards & Dynamic Utilization")
                df_cc_view = df_accounts_calc[df_accounts_calc["Account_Type"] == "Credit Card"].copy()
                if not df_cc_view.empty:
                    df_cc_view["Available_Credit"] = df_cc_view["Credit_Limit"] - df_cc_view["Current_Balance"]
                    df_cc_view["Utilization_%"] = (
                        df_cc_view["Current_Balance"] / df_cc_view["Credit_Limit"] * 100.0
                    ).round(1)
                    st.dataframe(
                        df_cc_view[["Account_ID", "Current_Balance", "Available_Credit", "Credit_Limit", "Utilization_%"]],
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "Current_Balance": st.column_config.NumberColumn("Current Used", format="%.2f"),
                            "Available_Credit": st.column_config.NumberColumn("Available Limit", format="%.2f"),
                            "Credit_Limit": st.column_config.NumberColumn("Total Limit", format="%.2f"),
                            "Utilization_%": st.column_config.NumberColumn("Utilization (%)", format="%.1f%%")
                        }
                    )
                else:
                    st.info("No Credit Cards configured yet.")

    # --------------------------------------------------------------------------
    # TAB 5: PHASE 5 - AUTOMATED SUMMARY REPORTS, AI INSIGHTS & EXPORT MODULE
    # --------------------------------------------------------------------------
    with tab_insights:
        st.subheader("🧠 Automated Financial Summary Reports & AI Spending Intelligence")
        st.caption(
            "Compile instant Daily, Weekly, or Monthly executive summaries, generate token-optimized Gemini 2.5 Flash "
            "spending intelligence, and export archival CSV/Markdown audit files."
        )

        horizon_choice = st.radio(
            "Select Reporting Horizon",
            options=[
                "Monthly Executive Audit (Selected Month)",
                "Weekly Digest (Trailing 7 Days)",
                "Daily Snapshot (Selected Date)"
            ],
            horizontal=True
        )

        digest = compile_automated_summary_digest(
            df_tracker=df_tracker,
            df_accounts_calc=df_accounts_calc,
            kpis=kpis,
            horizon=horizon_choice,
            ref_date=selected_date_filter,
            currency=currency
        )

        # --- SECTION 1: AUTOMATED EXECUTIVE SUMMARY GENERATOR ---
        col_exec_left, col_exec_right = st.columns([3, 2])
        with col_exec_left:
            with st.container(border=True):
                st.markdown(f"#### 📊 {digest['period_label']} — Category Burn & Spike Breakdown")
                s1, s2, s3 = st.columns(3)
                with s1:
                    st.metric("Period Operating Burn", f"{currency}{digest['period_expense']:,.2f}")
                with s2:
                    st.metric("Period Net Savings", f"{currency}{digest['period_net']:,.2f}")
                with s3:
                    st.metric("Groww Capital Deployed", f"{currency}{digest['period_groww_alloc']:,.2f}")

                if not digest["cat_df"].empty:
                    st.dataframe(
                        digest["cat_df"],
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "Category": st.column_config.TextColumn("Expense Category"),
                            "Amount": st.column_config.NumberColumn(f"Total Spent ({currency})", format="%.2f"),
                            "Share_%": st.column_config.ProgressColumn("Burn Share (%)", format="%.1f%%", min_value=0, max_value=100)
                        }
                    )
                else:
                    st.info("No operating expenses logged in this time horizon.")

        with col_exec_right:
            with st.container(border=True):
                st.markdown("#### 📱 Mobile Executive Summary Card")
                st.caption("One-tap copyable summary for WhatsApp, Telegram, or Apple/Google Notes:")
                st.code(digest["mobile_digest_markdown"], language="markdown")

                if not digest["top_spikes_df"].empty:
                    st.markdown("##### 🚨 Top Individual Expense Spikes")
                    st.dataframe(
                        digest["top_spikes_df"][["Date", "Amount", "Category", "Notes"]],
                        use_container_width=True,
                        hide_index=True,
                        column_config={
                            "Amount": st.column_config.NumberColumn(f"Amt ({currency})", format="%.0f")
                        }
                    )

        st.divider()

        # --- SECTION 2: GEMINI 2.5 FLASH AI SPENDING INTELLIGENCE ENGINE ---
        st.markdown("### 🤖 Gemini 2.5 Flash Spending Intelligence & Portfolio Advisory")
        st.caption(
            f"Uses pre-aggregated statistical compression (`<={MAX_INPUT_CHARS}` chars input, `max_output_tokens={GEMINI_INSIGHTS_MAX_TOKENS}`) "
            "to analyze your burn rate, unusual spikes, and Groww asset allocation with minimal token consumption."
        )

        if st.button("✨ Generate AI Spending & Portfolio Insights", type="primary", use_container_width=True):
            try:
                with st.spinner("Analyzing statistical digest with Gemini 2.5 Flash..."):
                    st.session_state["ai_insights_cache"] = generate_ai_spending_insights(digest, kpis, currency)
            except Exception as e:
                st.error(f"⚠️ Gemini Insights Error: {e}")

        ai_report = st.session_state.get("ai_insights_cache")
        if ai_report:
            with st.container(border=True):
                st.markdown(f"#### 💡 {ai_report.get('executive_headline', 'Financial Health Diagnostic')}")
                st.caption(f"🪙 *Generated using `{ai_report.get('tokens_used', 0)}` tokens from pre-aggregated ledger metrics.*")

                ins_c1, ins_c2, ins_c3 = st.columns(3)
                with ins_c1:
                    st.info(f"**📊 Top Spending Analysis**\n\n{ai_report.get('top_spending_analysis', '')}")
                with ins_c2:
                    st.warning(f"**⚡ Burn Spike & Anomaly Alert**\n\n{ai_report.get('burn_spike_alert', '')}")
                with ins_c3:
                    st.success(f"**🌱 Groww & Credit Health**\n\n{ai_report.get('portfolio_and_credit_health', '')}")

                st.markdown("##### 🎯 Top 3 Actionable Recommendations")
                for idx, rec in enumerate(ai_report.get("actionable_recommendations", []), 1):
                    st.markdown(f"**{idx}.** {rec}")

        st.divider()

        # --- SECTION 3: EXPORT & ARCHIVAL REPORTING MODULE ---
        st.markdown("### 📥 Export & Monthly Audit Archival Module")
        st.caption("Download your synchronized ledger, Groww & bank account balances, or a formatted Markdown executive audit report.")

        exp_col1, exp_col2, exp_col3 = st.columns(3)
        file_date_tag = selected_date_filter.strftime("%Y_%m")

        with exp_col1:
            export_ledger_df = digest["df_period"].drop(columns=["Date_Parsed"], errors="ignore")
            csv_ledger_bytes = export_ledger_df.to_csv(index=False).encode("utf-8")
            st.download_button(
                label=f"📄 Download Period Ledger CSV ({len(export_ledger_df)} rows)",
                data=csv_ledger_bytes,
                file_name=f"Finances_Ledger_{file_date_tag}.csv",
                mime="text/csv",
                use_container_width=True
            )

        with exp_col2:
            csv_accounts_bytes = df_accounts_calc.to_csv(index=False).encode("utf-8")
            st.download_button(
                label="🏦 Download Master Portfolio & Balances CSV",
                data=csv_accounts_bytes,
                file_name=f"Finances_Portfolio_Balances_{file_date_tag}.csv",
                mime="text/csv",
                use_container_width=True
            )

        with exp_col3:
            md_audit_report = build_markdown_audit_report(
                digest=digest,
                kpis=kpis,
                df_accounts_calc=df_accounts_calc,
                ai_insights=ai_report,
                currency=currency
            )
            st.download_button(
                label="📑 Download Formatted Executive Audit (.md)",
                data=md_audit_report.encode("utf-8"),
                file_name=f"Finances_Executive_Audit_{file_date_tag}.md",
                mime="text/markdown",
                use_container_width=True
            )

if __name__ == "__main__":
    main()