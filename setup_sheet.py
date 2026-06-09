"""
Run this once to create and structure the Google Sheet.
Usage: python3 setup_sheet.py
"""
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, date, timedelta
import os

CREDS_FILE = os.environ["GOOGLE_CREDS_FILE"]
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
SHEET_NAME = "2026 HUAT"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
gc = gspread.authorize(creds)
sh = gc.open_by_key(SHEET_ID)

# Create or clear the sheet
try:
    ws = sh.worksheet(SHEET_NAME)
    ws.clear()
except gspread.WorksheetNotFound:
    ws = sh.add_worksheet(title=SHEET_NAME, rows=500, cols=30)

# Row 1: Title
ws.update("A1", [["MHY NOVENA", "", "", 2026]])

# Row 3: Section headers
ws.update("D3", [["WALK-IN"]])
ws.update("N3", [["DELIVERY"]])
ws.update("Z3", [["CASHIER"]])

# Row 4: Column headers
headers = [
    "Date", "Day", "",
    "CASH", "PAYWAVE", "NETS", "PAYNOW", "GRABPAY", "MALL V", "NETS F", "QR", "SUBTOTAL", "",
    "PANDA", "", "GRAB", "", "DELIVEROO", "", "SUBTOTAL", "REAL SUBTOTAL", "",
    "DAILY REVENUE", "REAL REVENUE", "",
    "BANK IN", "REMAIN", "+/-", "MONEYBAG BANK-IN"
]
ws.update("A4", [headers])

# Rows 5+: Dates for all of 2026
start_date = date(2026, 1, 1)
end_date = date(2026, 12, 31)
rows = []
current = start_date
while current <= end_date:
    row = [current.strftime("%d/%m/%Y"), current.strftime("%A")]
    rows.append(row)
    current += timedelta(days=1)

# Write dates and days in bulk
ws.update("A5", rows)

print(f"Sheet set up with {len(rows)} date rows.")
print("Done! Open your Google Sheet to verify.")
