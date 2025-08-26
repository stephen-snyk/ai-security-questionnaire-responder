
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

# Configuration
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
SPREADSHEET_ID = '1wtIZ2MAVp7CaL180l38xEHxtrhc9hZveuTdrNWpmW6g'  # From the URL
SERVICE_ACCOUNT_FILE = os.getenv('GOOGLE_APPLICATION_CREDENTIALS', './snyk-cx-se-demo-0ce146967b8c.json')
DOCS_DIR = './docs'  # Folder containing documents (PDFs, spreadsheets) to provide as context

# Track auth mode for help messages
ACTIVE_AUTH = None  # "service_account" | "oauth"
SERVICE_ACCOUNT_EMAIL = None

# Configure Gemini
if not GEMINI_API_KEY:
    raise SystemExit("GEMINI_API_KEY is not set. Export it and re-run.")
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-pro')

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
                current_sheet = worksheets_re[0]
            except Exception as open_err:
                print(f"Failed to reopen spreadsheet during retry: {open_err}")
            attempt += 1

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
        
        # Use first sheet
        sheet = worksheets[0]
        print(f"Using sheet: {sheet.title}")
        
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
    
    # Get all data
    all_records = sheet.get_all_records()
    
    print(f"Found {len(all_records)} requirements to process")
    
    for i, record in enumerate(all_records, start=2):  # Start at row 2 (skip header)
        requirement_text = record.get('Requirement', '')  # Adjust column name as needed
        
        # Skip if already processed or empty
        if record.get('Compliance Statement', '') or not requirement_text:
            print(f"Skipping row {i} - already processed or empty")
            continue
        
        print(f"Processing row {i}: {requirement_text[:50]}...")
        
        # Create prompt for Gemini
        prompt = f"""
Using only the provided documents as sources, evaluate this requirement:

"{requirement_text}"

Provide a compliance statement in this exact format:
"[Compliant/Non-compliant/Partially Compliant] - [brief reasoning] (Reference: [Document name], Page [number], Section [if applicable])"

If any single document contains an exact textual match or a definitive section that directly addresses the requirement, STOP. Use only that one document for your answer and citation. Do not consult or reference other documents. Do not merge sources.

If and only if the provided documents do not contain sufficient evidence to assess the requirement, respond with exactly:
not_found

Allowed document names (you must cite exactly one of these when providing a reference):
{allowed_doc_names_text}
"""
        
        try:
            # Send to Gemini with attached files context when available (with retries)
            inputs = [prompt] + provided_files if provided_files else [prompt]
            response = generate_with_retry(model, inputs)
            compliance_statement = response.text.strip()

            # Normalize to not_found when model indicates no evidence
            normalized_reply = compliance_statement.strip().lower()
            not_found_indicators = [
                'not_found',
                'insufficient information',
                'insufficient evidence',
                'cannot be found',
                'not found in the provided documents',
            ]
            if (not normalized_reply) or any(ind in normalized_reply for ind in not_found_indicators):
                compliance_statement = 'not_found'
            
            # Enforce that cited document is among uploaded files; otherwise mark as not_found
            if compliance_statement.lower() != 'not_found' and allowed_doc_names:
                lower_stmt = compliance_statement.lower()
                if not any(name and name.lower() in lower_stmt for name in allowed_doc_names):
                    compliance_statement = 'not_found'

            # Update the Google Sheet (with retries)
            # Assuming 'Compliance Statement' is in column B
            sheet = update_cell_with_retry(sheet, i, 2, compliance_statement)  # Column B = 2
            
            print(f"✓ Updated row {i}")
            
            # Rate limiting - be nice to the APIs
            time.sleep(2)
            
        except Exception as e:
            error_msg = f"ERROR: {str(e)}"
            try:
                sheet = update_cell_with_retry(sheet, i, 2, error_msg)
            except Exception as e2:
                print(f"Failed to write error to sheet on retry: {e2}")
            print(f"✗ Error on row {i}: {e}")
            time.sleep(1)
    
    print("Processing complete!")

def setup_instructions():
    """Print setup instructions"""
    print("""
    SETUP INSTRUCTIONS:
    
    1. GET GEMINI API KEY:
       - Go to https://aistudio.google.com/app/apikey
       - Create a new API key
       - Replace 'your-gemini-api-key-here' in the script
    
    2. SETUP GOOGLE SHEETS API:
       - Go to https://console.cloud.google.com/
       - Create/select a project
       - Enable Google Sheets API and Google Drive API
       - Create Service Account credentials
       - Download the JSON key file
       - Share your Google Sheet with the service account email
    
    3. GET SPREADSHEET ID:
       - From your Google Sheets URL: 
         https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit
       - Copy the SPREADSHEET_ID part
    
    4. UPDATE COLUMN NAMES:
       - Make sure your sheet has columns named:
         * 'Requirement' (or update line 35)
         * 'Compliance_Statement' (or update line 38 and 58)
    
    5. INSTALL REQUIRED PACKAGES:
       pip install gspread google-auth google-generativeai
    
    6. FIRST: Upload your PDFs to Gemini in the web interface
       Then run this script to process all requirements
    """)

if __name__ == "__main__":
    # Comment out setup_instructions to stop seeing them
    # setup_instructions()
    
    # Run the actual processing
    process_requirements()