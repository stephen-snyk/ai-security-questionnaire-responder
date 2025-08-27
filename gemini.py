
import gspread
import google.generativeai as genai
import time
import os
from google.oauth2.service_account import Credentials
from pathlib import Path
import random
import requests
from google.api_core import exceptions as gcloud_exceptions
from gspread.exceptions import APIError as GSpreadAPIError
from concurrent.futures import ThreadPoolExecutor, as_completed

# Configuration
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
# Allow overriding spreadsheet and worksheet via environment variables for flexibility
SPREADSHEET_ID = os.getenv('SPREADSHEET_ID', '1pm0m9UKfGNpXq-9df2n09Z8B9IT9J8TsE6Of6lbUGFI')
WORKSHEET_INDEX = int(os.getenv('WORKSHEET_INDEX', '0'))
SERVICE_ACCOUNT_FILE = os.getenv('GOOGLE_APPLICATION_CREDENTIALS', './snyk-cx-se-demo-0ce146967b8c.json')
DOCS_DIR = './docs'  # Folder containing documents (PDFs, spreadsheets) to provide as context
MAX_WORKERS = int(os.getenv('GEMINI_MAX_WORKERS', '8'))  # Concurrency for Gemini requests
VERIFY_WRITES = os.getenv('VERIFY_WRITES', 'false').lower() in {'1', 'true', 'yes'}

# Track auth mode for help messages
ACTIVE_AUTH = None  # "service_account" | "oauth"
SERVICE_ACCOUNT_EMAIL = None

# Configure Gemini
if not GEMINI_API_KEY:
    raise SystemExit("GEMINI_API_KEY is not set. Export it and re-run.")
genai.configure(api_key=GEMINI_API_KEY)

# Set up Google Sheets access
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

def _find_pdf_paths(docs_dir: str):
    try:
        base = Path(os.path.expanduser(docs_dir)).resolve()
        if not base.exists():
            return []
        allowed_suffixes = {
            '.pdf',                # Portable Document Format
            '.xlsx', '.xls',       # Excel workbooks
            '.csv', '.tsv',        # Delimited text
            '.ods',                # OpenDocument Spreadsheet
        }
        files = [p for p in base.iterdir() if p.is_file() and p.suffix.lower() in allowed_suffixes]
        return sorted(files)
    except Exception:
        return []

def _upload_pdfs(paths):
    uploaded = []
    ext_to_mime = {
        '.pdf': 'application/pdf',
        '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        '.xls': 'application/vnd.ms-excel',
        '.csv': 'text/csv',
        '.tsv': 'text/tab-separated-values',
        '.ods': 'application/vnd.oasis.opendocument.spreadsheet',
    }
    for p in paths:
        try:
            print(f"Uploading to Gemini: {p.name}")
            mime = ext_to_mime.get(p.suffix.lower())
            if mime:
                f = genai.upload_file(path=str(p), mime_type=mime)
            else:
                f = genai.upload_file(path=str(p))
            uploaded.append(f)
        except Exception as e:
            print(f"Failed to upload {p.name}: {e}")
    return uploaded

def _wait_for_files_active(files, timeout_seconds: int = 180, poll_seconds: int = 2):
    if not files:
        return []
    deadline = time.time() + timeout_seconds
    remaining = {f.name for f in files}
    last_states = {}
    while remaining and time.time() < deadline:
        next_remaining = set()
        for fid in list(remaining):
            try:
                f = genai.get_file(name=fid)
                state = getattr(getattr(f, 'state', None), 'name', None)
                last_states[fid] = state
                if state == 'ACTIVE':
                    continue
                elif state == 'FAILED':
                    print(f"File processing failed: {fid}")
                else:
                    next_remaining.add(fid)
            except Exception as e:
                print(f"Error checking file {fid}: {e}")
        remaining = next_remaining
        if remaining:
            time.sleep(poll_seconds)
    # Return only ACTIVE files
    ready = []
    for f in files:
        try:
            g = genai.get_file(name=f.name)
            if getattr(getattr(g, 'state', None), 'name', None) == 'ACTIVE':
                ready.append(g)
        except Exception:
            pass
    return ready

def prepare_gemini_files(docs_dir: str):
    paths = _find_pdf_paths(docs_dir)
    if not paths:
        return []
    uploaded = _upload_pdfs(paths)
    ready = _wait_for_files_active(uploaded)
    if ready:
        names = ", ".join(getattr(f, 'display_name', getattr(f, 'name', 'file')) for f in ready)
        print(f"Files ready: {names}")
    else:
        print("No files became ACTIVE. Continuing without document context.")
    return ready

def _backoff_sleep(attempt: int, base: float = 2.0, factor: float = 2.0, jitter: float = 0.5):
    delay = base * (factor ** (attempt - 1))
    delay *= 1.0 + random.uniform(0.0, jitter)
    time.sleep(min(delay, 30.0))

def generate_with_retry(model_obj, inputs, max_attempts: int = 5):
    attempt = 1
    while True:
        try:
            return model_obj.generate_content(inputs)
        except (gcloud_exceptions.DeadlineExceeded,
                gcloud_exceptions.ServiceUnavailable,
                gcloud_exceptions.InternalServerError,
                requests.exceptions.ConnectionError) as e:
            if attempt >= max_attempts:
                raise e
            print(f"Gemini transient error (attempt {attempt}/{max_attempts}): {e}. Retrying...")
            _backoff_sleep(attempt)
            attempt += 1

def _normalize_compliance_statement(compliance_statement: str, allowed_doc_names):
    """Normalize model output to either a valid statement or 'not_found'."""
    if not compliance_statement:
        return 'not_found'

    normalized_reply = compliance_statement.strip().lower()
    not_found_indicators = [
        'not_found',
        'insufficient information',
        'insufficient evidence',
        'cannot be found',
        'not found in the provided documents',
    ]
    if (not normalized_reply) or any(ind in normalized_reply for ind in not_found_indicators):
        return 'not_found'

    # Enforce that cited document is among uploaded files; otherwise mark as not_found
    if allowed_doc_names:
        lower_stmt = compliance_statement.lower()
        if not any(name and name.lower() in lower_stmt for name in allowed_doc_names):
            return 'not_found'

    return compliance_statement

def update_cell_with_retry(sheet, row: int, col: int, value: str, max_attempts: int = 5):
    attempt = 1
    current_sheet = sheet
    while True:
        try:
            current_sheet.update_cell(row, col, value)
            return current_sheet
        except (GSpreadAPIError, requests.exceptions.ConnectionError) as e:
            if attempt >= max_attempts:
                raise e
            print(f"Sheets transient error (attempt {attempt}/{max_attempts}): {e}. Reconnecting and retrying...")
            _backoff_sleep(attempt)
            # Recreate client and worksheet
            client_re = setup_sheets_client()
            try:
                spreadsheet_re = client_re.open_by_key(SPREADSHEET_ID)
                worksheets_re = spreadsheet_re.worksheets()
                # Keep the same worksheet index if possible
                idx = WORKSHEET_INDEX if 0 <= WORKSHEET_INDEX < len(worksheets_re) else 0
                current_sheet = worksheets_re[idx]
            except Exception as open_err:
                print(f"Failed to reopen spreadsheet during retry: {open_err}")
            attempt += 1

def _find_header_column_index(sheet, possible_names):
    """Return 1-based column index by matching headers case-insensitively; None if not found."""
    try:
        headers = sheet.row_values(1)
    except Exception as _:
        return None
    normalized_headers = [h.strip().lower() for h in headers]
    for name in possible_names:
        lower = name.strip().lower()
        if lower in normalized_headers:
            return normalized_headers.index(lower) + 1
    return None

def _column_index_to_letter(index_one_based: int):
    """Convert 1-based column index to A1 column letter(s)."""
    result = ""
    n = int(index_one_based)
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result

def setup_sheets_client():
    """Initialize Google Sheets client.

    Prefers Service Account (non-interactive, reliable). Falls back to user OAuth.
    """
    global ACTIVE_AUTH, SERVICE_ACCOUNT_EMAIL

    # 1) Try Service Account
    try:
        sa_path_env = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
        sa_path = os.path.abspath(os.path.expanduser(sa_path_env or SERVICE_ACCOUNT_FILE))
        if os.path.isfile(sa_path):
            creds = Credentials.from_service_account_file(sa_path, scopes=SCOPES)
            SERVICE_ACCOUNT_EMAIL = getattr(creds, 'service_account_email', None)
            client = gspread.authorize(creds)
            ACTIVE_AUTH = "service_account"
            return client
        else:
            print(f"Service Account JSON not found at: {sa_path}")
    except Exception as e:
        print(f"Service Account auth failed: {e}")

    # 2) Fallback to OAuth
    try:
        cred_path = os.path.expanduser('~/.config/gspread/credentials.json')
        auth_user_path = os.path.expanduser('~/.config/gspread/authorized_user.json')
        client = gspread.oauth(
            credentials_filename=cred_path,
            authorized_user_filename=auth_user_path
        )
        ACTIVE_AUTH = "oauth"
        return client
    except FileNotFoundError:
        print("OAuth credentials not found. Please set up Google Sheets API:")
        print("1. Go to https://console.cloud.google.com/")
        print("2. Enable Google Sheets API")
        print("3. Create OAuth credentials")
        print("4. Download and save as ~/.config/gspread/credentials.json")
        raise SystemExit(1)
    except Exception as e:
        print(f"Authentication failed: {e}")
        raise SystemExit(1)

def process_requirements():
    """Main function to process requirements and update sheets"""
    # Connect to Google Sheets
    client = setup_sheets_client()
    
    print(f"Attempting to open spreadsheet with ID: {SPREADSHEET_ID}")
    
    try:
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        print(f"Successfully opened: {spreadsheet.title}")
        
        # List available sheets
        worksheets = spreadsheet.worksheets()
        print(f"Available sheets: {[ws.title for ws in worksheets]}")
        
        # Use selected worksheet index
        if WORKSHEET_INDEX < 0 or WORKSHEET_INDEX >= len(worksheets):
            print(f"WORKSHEET_INDEX {WORKSHEET_INDEX} out of range, defaulting to 0")
            selected_index = 0
        else:
            selected_index = WORKSHEET_INDEX
        sheet = worksheets[selected_index]
        print(f"Using sheet: {sheet.title} (index {selected_index})")
        
    except Exception as e:
        print(f"Error opening spreadsheet: {e}")
        print("Please check:")
        print("1. Spreadsheet ID is correct")
        print("2. Sheet is a regular Google Sheets document (not Excel import)")
        print("3. You have edit access to the sheet")
        if ACTIVE_AUTH == "service_account":
            hint_email = SERVICE_ACCOUNT_EMAIL or "your service account"
            print(f"4. Share the sheet with the service account email: {hint_email}")
        return
    
    # Prepare document context for Gemini
    provided_files = prepare_gemini_files(DOCS_DIR)
    if not provided_files:
        print("No documents found in ./docs. The model may ask for documents.")
    # Build allowed document names list for stricter citation control
    allowed_doc_names = []
    if provided_files:
        for _f in provided_files:
            display_name = getattr(_f, 'display_name', None)
            name_fallback = getattr(_f, 'name', '')
            base_name = os.path.basename(name_fallback) if name_fallback else None
            allowed_doc_names.append(display_name or base_name)
        # Filter out Nones just in case
        allowed_doc_names = [n for n in allowed_doc_names if n]
    allowed_doc_names_text = "\n".join(f"- {n}" for n in allowed_doc_names) if allowed_doc_names else ""
    
    # Resolve column indexes dynamically by header
    requirement_col_index = _find_header_column_index(sheet, [
        'Requirement',
        'requirement',
    ])
    compliance_col_index = _find_header_column_index(sheet, [
        'Compliance Statement',
        'Compliance_Statement',
        'compliance statement',
        'compliance_statement',
    ])

    if requirement_col_index is None:
        print("Could not find 'Requirement' column header. Please ensure row 1 has a 'Requirement' header.")
        return
    if compliance_col_index is None:
        print("Could not find 'Compliance Statement' column header. Please ensure row 1 has a 'Compliance Statement' header.")
        return
    print(f"Detected columns -> Requirement: {requirement_col_index}, Compliance: {compliance_col_index}")

    # Collect rows to process by reading raw columns to preserve physical row numbers
    rows_to_process = []  # list[(row_index, requirement_text)]
    try:
        requirement_col_values = sheet.col_values(requirement_col_index)
        compliance_col_values = sheet.col_values(compliance_col_index)
    except Exception as e:
        print(f"Failed to read column values: {e}")
        return

    # Ensure both lists cover the same number of rows for safe indexing
    max_len = max(len(requirement_col_values), len(compliance_col_values))
    # Pad lists to max_len
    requirement_col_values += [''] * (max_len - len(requirement_col_values))
    compliance_col_values += [''] * (max_len - len(compliance_col_values))

    for physical_row in range(2, max_len + 1):  # start from row 2 (after header)
        requirement_text = requirement_col_values[physical_row - 1]
        compliance_value = compliance_col_values[physical_row - 1]
        if (not requirement_text or not requirement_text.strip()) or (compliance_value and compliance_value.strip()):
            print(f"Skipping row {physical_row} - already processed or empty")
            continue
        rows_to_process.append((physical_row, requirement_text))

    if not rows_to_process:
        print("No new requirements to process.")
        print("Processing complete!")
        return

    print(f"Submitting {len(rows_to_process)} rows to Gemini (max_workers={MAX_WORKERS})...")

    def _build_prompt(req_text: str) -> str:
        return f"""
Using only the provided documents as sources, evaluate this requirement:

"{req_text}"

#PROMPTS

- Use only the provided documents; do not use external knowledge.
- Choose exactly one source. Prefer in this order when multiple match: SOC 2 report > ISO Statement of Applicability > policy/procedure > overview/FAQ.
- Ground the reasoning in a specific clause/section; be concrete, not generic.
- Keep the entire response on a single line (no newlines).
- Keep the reasoning concise (≤ 40 words).
- If the requirement is multi-part and only some parts are covered, answer Partially Compliant and name the covered parts succinctly.
- If you cannot confidently cite one Allowed document name verbatim with a page (and section when applicable), respond with exactly: not_found.

Provide a compliance statement in this exact format:
"[Compliant/Non-compliant/Partially Compliant] - [brief reasoning] (Reference: [Document name], Page [number], Section [if applicable])"

If any single document contains an exact textual match or a definitive section that directly addresses the requirement, STOP. Use only that one document for your answer and citation. Do not consult or reference other documents. Do not merge sources.

If and only if the provided documents do not contain sufficient evidence to assess the requirement, respond with exactly:
not_found

Allowed document names (you must cite exactly one of these when providing a reference):
{allowed_doc_names_text}
"""

    def _worker_generate(req_text: str) -> str:
        prompt = _build_prompt(req_text)
        inputs = [prompt] + provided_files if provided_files else [prompt]
        # Create a local model instance per thread for safety
        local_model = genai.GenerativeModel('gemini-1.5-pro')
        response = generate_with_retry(local_model, inputs)
        compliance_statement = getattr(response, 'text', '')
        compliance_statement = compliance_statement.strip() if compliance_statement else ''
        return _normalize_compliance_statement(compliance_statement, allowed_doc_names)

    # Run Gemini generations concurrently and write each result as it completes
    with ThreadPoolExecutor(max_workers=max(1, MAX_WORKERS)) as executor:
        future_to_row = {
            executor.submit(_worker_generate, req_text): row
            for (row, req_text) in rows_to_process
        }
        for future in as_completed(future_to_row):
            row = future_to_row[future]
            try:
                value = future.result()
            except Exception as e:
                value = f"ERROR: {e}"
            print(f"✓ Gemini completed for row {row}")
            try:
                sheet = update_cell_with_retry(sheet, row, compliance_col_index, value)
                # Optional verification and A1 fallback
                if VERIFY_WRITES:
                    try:
                        read_back = sheet.cell(row, compliance_col_index).value or ''
                        if not read_back.strip():
                            col_letter = _column_index_to_letter(compliance_col_index)
                            a1 = f"{col_letter}{row}"
                            print(f"Write verification failed for row {row}. Retrying with range update at {a1}...")
                            sheet.update(a1, [[value]])
                    except Exception as _verify_err:
                        print(f"Verification error for row {row}: {_verify_err}")
                print(f"✓ Updated row {row}")
                time.sleep(0.5)  # gentle pacing for Sheets API
            except Exception as e:
                print(f"✗ Error updating row {row}: {e}")
                time.sleep(0.5)

    print("Processing complete!")

if __name__ == "__main__":
    # Run the actual processing
    process_requirements()