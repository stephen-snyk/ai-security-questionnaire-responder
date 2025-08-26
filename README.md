README.md

Security Questionnaire Responder

This tool allows you to upload documents from https://trust.synk.io (or anywhere really) to Google Gemini, then read security requirements from a Google Sheet, then use the LLM to compare requirements to what is in the documents, and provide responses in the Google Sheet.


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