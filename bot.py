import base64
import io
import json
import os
import re
import threading
import time
from datetime import datetime, timezone

from PIL import Image, ImageOps

import anthropic
import gspread
import requests
from flask import Flask, request, jsonify
from google.oauth2.service_account import Credentials

app = Flask(__name__)

print("ENV KEYS:", [k for k in os.environ.keys() if not k.startswith("PATH")])

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
SHEET_NAME = "2026 HUAT"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

ACCOUNTING_FORMAT = '_($* #,##0.00_);_($* (#,##0.00);_($* "-"??_);_(@_)'


def get_gsheet():
    creds_json = os.environ.get("GOOGLE_CREDS_JSON")
    creds_file = os.environ.get("GOOGLE_CREDS_FILE")
    if creds_json:
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID).worksheet(SHEET_NAME)


RECEIPT_PROMPT = """Analyze this image carefully. It may contain one or more of the following: a paper receipt, a device/screen showing delivery figures, and/or a cash deposit slip. Extract ALL items present — do not skip any.

Also extract the date shown on each item (not today's date). Return it as "date" in DD/MM/YYYY format. The date may appear as "End of Day DD-MM-YYYY" on the receipt, or "DDJUN/YYYY" on the deposit slip.

Return a JSON array containing one object per item found. Each object should be one of:

Receipt object (look for "End of Day" receipt from Mei Heong Yuen Dessert):
{"type": "receipt", "date": "DD/MM/YYYY or null", "cash": <number or null>, "paynow": <number or null>, "paynow_qr": <number or null>, "mall_voucher": <number or null>}

Field mapping for this receipt format — use values from the FIRST "Payment Modes" block only. This block appears under the "*** Total Sales (Declaration) ***" / "Cashier Declared" header. IGNORE the second "Payment Modes (System Calcuated)" block entirely.

- "cash": the dollar amount on the same line as "CASH [xx]" in the first Payment Modes block. This will often be a large number like $770.10. Do NOT return 0 if you can see a non-zero amount.
- "paynow": the dollar amount on the same line as "Paynow [xx]" — this is a SEPARATE line from PayNow QR. If it genuinely shows $0.00, return 0.
- "paynow_qr": the dollar amount on the same line as "PayNow QR [xx]" — this is DIFFERENT from Paynow. Do NOT copy the paynow_qr value into paynow.
- "mall_voucher": the dollar amount on the same line as "Mall Voucher [xx]"

IMPORTANT: "Paynow" and "PayNow QR" are two completely different fields on two different lines. Read each line independently. Always read from the first Payment Modes block (Cashier Declared), never the System Calcuated block.

Delivery screen object (a photo showing two devices — Grab app on one device and Foodpanda app on another):
{"type": "delivery", "date": "DD/MM/YYYY or null", "grab_sales": <number or null>, "foodpanda_sales": <number or null>}
- "date": look for the date in the Grab app (e.g. "Today, 16 Jun 2026" → "16/06/2026"). Use this date for the whole object even if Foodpanda has no date.
- "grab_sales": the Net Sales (S$) figure shown in the Grab app
- "foodpanda_sales": the total SGD amount shown under Recent Orders in the Foodpanda app (e.g. "All 2 · SGD49.20" → 49.20)
Return this as a single object, not two separate objects.

Cash deposit slip object (look anywhere in the image for a UOB ATM TRANSACTION RECORD slip — it may be small, placed beside the receipt, partially overlapping, or in a corner):
{"type": "deposit", "date": "DD/MM/YYYY or null", "account_match": <true or false>, "account_found": "<account number on slip>", "tran_amount": <number or null>}
- Look for the UOB logo (three red bars) or text "ATM TRANSACTION RECORD" or "UOB" anywhere in the image
- "account_found": the ACCOUNT NO value on the slip
- Check if ACCOUNT NO is 3493354335 (UOB). Set account_match true if it matches.
- "tran_amount": the total deposited amount. On the slip there are two sections: a LEFT side showing a denomination breakdown table (e.g. rows of $50, $10 bills — IGNORE this entire table) and a RIGHT side showing summary fields. Read the "DEPOSIT" or "TRAN AMOUNT" field value from the RIGHT summary section only (e.g. $550.00 → 550.0). This will be the largest dollar figure on the slip and will match the TOTAL shown in the denomination table.
- "date": look for the DATE field on the slip for the day and month (e.g. "22JUN2026" → "22/06/2026"). The year on thermal slips is often misread by OCR — do not worry about it, the system will correct the year automatically.

Return ONLY the JSON array, nothing else. Example for two items: [{"type": "receipt", ...}, {"type": "deposit", ...}]"""


def set_webhook(webhook_url: str):
    resp = requests.post(f"{TELEGRAM_API}/setWebhook", json={"url": webhook_url})
    resp.raise_for_status()
    return resp.json()


def download_file(file_id: str) -> bytes:
    resp = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id})
    resp.raise_for_status()
    file_path = resp.json()["result"]["file_path"]
    download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    file_resp = requests.get(download_url)
    file_resp.raise_for_status()
    return file_resp.content


def correct_orientation(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)  # rotate based on EXIF orientation tag
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def analyze_image(image_bytes: bytes) -> list:
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    last_err = None
    for attempt in range(4):
        if attempt > 0:
            time.sleep(2 ** attempt)  # 2s, 4s, 8s
        try:
            response = claude.messages.create(
                model="claude-opus-4-8",
                max_tokens=1024,
                thinking={"type": "adaptive"},
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": image_b64,
                                },
                            },
                            {"type": "text", "text": RECEIPT_PROMPT},
                        ],
                    }
                ],
            )
            text = response.content[-1].text.strip()
            match = re.search(r'\[.*\]', text, re.DOTALL)
            result = json.loads(match.group()) if match else json.loads(text)
            return result if isinstance(result, list) else [result]
        except Exception as e:
            last_err = e
            if "overloaded" not in str(e).lower():
                raise
    raise last_err


def parse_any_date(s: str):
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%-d/%-m/%Y", "%-d/%-m/%y",
                "%d-%m-%Y", "%d-%m-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def get_sheet_row(ws, date_str: str) -> int | None:
    try:
        target = parse_any_date(date_str)
        if target is None:
            return None
        col_a = ws.col_values(1)
        for i, val in enumerate(col_a):
            if val:
                sheet_date = parse_any_date(val)
                if sheet_date and sheet_date == target:
                    return i + 1
        return None
    except (ValueError, TypeError):
        return None


def write_cell(ws, cell: str, value):
    ws.update_acell(cell, round(float(value), 2))


def update_sheet(ws, row: int, data: dict) -> str:
    img_type = data.get("type")
    updates = []

    if img_type == "receipt":
        if data.get("cash") is not None:
            write_cell(ws, f"D{row}", data["cash"])
            updates.append(f"Cash: {data['cash']}")
        if data.get("paynow") is not None:
            write_cell(ws, f"G{row}", data["paynow"])
            updates.append(f"PayNow: {data['paynow']}")
        if data.get("paynow_qr") is not None:
            write_cell(ws, f"K{row}", data["paynow_qr"])
            updates.append(f"PayNow QR: {data['paynow_qr']}")
        if data.get("mall_voucher") is not None:
            write_cell(ws, f"I{row}", data["mall_voucher"])
            updates.append(f"Mall Voucher: {data['mall_voucher']}")

    elif img_type == "delivery":
        if data.get("foodpanda_sales") is not None:
            write_cell(ws, f"N{row}", data["foodpanda_sales"])
            updates.append(f"Foodpanda Sales: {data['foodpanda_sales']}")
        if data.get("grab_sales") is not None:
            write_cell(ws, f"P{row}", data["grab_sales"])
            updates.append(f"Grab Sales: {data['grab_sales']}")

    elif img_type == "deposit":
        if data.get("tran_amount") is not None:
            write_cell(ws, f"Z{row}", data["tran_amount"])
            updates.append(f"Bank In: {data['tran_amount']}")

    return "\n".join(updates)


def format_reply(data: dict, sheet_row: int, date_str: str, update_summary: str) -> str:
    img_type = data.get("type")
    row_info = f" (row {sheet_row})" if sheet_row is not None else " (date not found in sheet)"
    lines = [f"Date: {date_str}{row_info}"]

    if img_type == "receipt":
        lines.append("Type: Receipt")
        lines.append(f"Cash: {data.get('cash', 'N/A')}")
        lines.append(f"PayNow: {data.get('paynow', 'N/A')}")
        lines.append(f"PayNow QR: {data.get('paynow_qr', 'N/A')}")
        lines.append(f"Mall Voucher: {data.get('mall_voucher', 'N/A')}")

    elif img_type == "delivery":
        lines.append("Type: Delivery Screen")
        lines.append(f"Grab Sales: {data.get('grab_sales', 'N/A')}")
        lines.append(f"Foodpanda Sales: {data.get('foodpanda_sales', 'N/A')}")

    elif img_type == "deposit":
        lines.append("Type: Cash Deposit Slip")
        if data.get("account_match"):
            lines.append("Account: CORRECT (UOB 3493354335)")
        else:
            lines.append(f"Account: MISMATCH - found {data.get('account_found', 'unknown')}")
        lines.append(f"Tran Amount: {data.get('tran_amount', 'N/A')}")

    if sheet_row is None:
        lines.append("\nCould not write to sheet: date not found.")
    elif update_summary:
        lines.append(f"\nUpdated Google Sheet:\n{update_summary}")
    else:
        lines.append("\nNo values written to sheet.")

    return "\n".join(lines)


def process_image(chat_id: str, file_id: str):
    try:
        image_bytes = correct_orientation(download_file(file_id))
        items = analyze_image(image_bytes)
        ws = get_gsheet()

        for data in items:
            image_date = data.get("date")
            date_str = image_date or "date not found on image"
            sheet_row = get_sheet_row(ws, image_date) if image_date else None

            if sheet_row is not None:
                update_summary = update_sheet(ws, sheet_row, data)
            else:
                update_summary = None

            reply = format_reply(data, sheet_row, date_str, update_summary)
            requests.post(f"{TELEGRAM_API}/sendMessage", json={
                "chat_id": chat_id,
                "text": reply,
            })

    except Exception as e:
        requests.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": chat_id,
            "text": f"Error processing image: {str(e)}",
        })


@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json()
    message = update.get("message", {})
    photos = message.get("photo")

    if photos:
        best = photos[-1]
        file_id = best["file_id"]
        chat_id = message["chat"]["id"]

        requests.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": chat_id,
            "text": "Reading your image...",
        })

        # Process in background so webhook returns 200 immediately
        threading.Thread(target=process_image, args=(chat_id, file_id), daemon=True).start()

    return jsonify({"ok": True})


if __name__ == "__main__":
    webhook_url = os.environ["WEBHOOK_URL"]
    result = set_webhook(webhook_url)
    print("Webhook set:", result)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
