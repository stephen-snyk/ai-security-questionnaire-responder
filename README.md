README.md

Security Questionnaire Responder

This tool allows you to upload documents from https://trust.synk.io (or anywhere really) to Google Gemini, read security requirements from a Google Sheet, use the LLM to compare requirements in the Sheet to what is in the documents, and provide responses in the Google Sheet.


    SETUP INSTRUCTIONS:
    
    1. GET GEMINI API KEY:
       - Go to https://aistudio.google.com/app/apikey
       - Create a new API key
       - From your CLI run 'export GEMINI_API_KEY=<the_key>'. You can also update your ~/.zshrc file with this to make it more permanent.
    
    2. SETUP GOOGLE SHEETS API:
       - Go to https://console.cloud.google.com/
       - Create/select a project
       - Enable Google Sheets API and Google Drive API
       - Create Service Account credentials
       - Download the JSON key file
       - Share your Google Sheet with the service account email
       - Update the python script SERVICE_ACCOUNT_FILE to point to the json key file
    
    3. GET SPREADSHEET ID:
       - From your Google Sheets URL: 
         https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit
       - Copy the SPREADSHEET_ID part
    
    4. UPDATE COLUMN NAMES:
       - Make sure your sheet has columns named:
         * 'Requirement'
         * 'Compliance_Statement'
    
    5. INSTALL REQUIRED PACKAGES:
       - Create a virtual environment, 'python3 -m venv ai-security-questionnaire-responder'
       - Activate it, 'source ai-security-questionnaire-responder-env/bin/activate'
       - Install requirements 'pip install gspread google-auth google-generativeai'
    
    6. DOWNLOAD RELEVANT DOCUMENTS:
       - Download relevant documents from https://trust.snyk.io. It's best to use the ISO27001, SIG Lite, and SOC2 report. Most of the other ones may generate undesireable responses.
       - Move them into a docs/ folder in this project.

    7. RUN THE SCRIPT:
       - Run the script with 'python ./gemini.py' from the project folder.
       - You should see it uploading the documents to Gemini, and then starting to populate the spreadsheet.

    8. TUNING:
      - Modify prompts for Gemini; Search for PROMPTS in the python script. Here you can modify how Gemini will be used to generate the responses.
      - The tool is multi-threaded with a default of 4 workers. You can either edit the line with 'GEMINI_MAX_WORKERS' in the script, or 'export GEMINI_MAX_WORKERS=8'