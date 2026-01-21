import json
import os
import requests
import google.generativeai as genai
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
from PIL import Image
from io import BytesIO

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
ALLOWED_USERS = json.loads(os.environ.get("ALLOWED_USERS", "[]"))

# --- SETUP CLIENTS ---
genai.configure(api_key=GOOGLE_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# Load Google Credentials
try:
    creds_dict = json.loads(os.environ.get("GOOGLE_JSON_KEY"))
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    gc = gspread.authorize(creds)
except Exception as e:
    print(f"Auth Error: {e}")

SYSTEM_PROMPT = """
Current Date: {date}
Categories: Groceries 🛒, Food Takeout 🍕, Travel ✈️, Subscription 📺, Investment 💰, Household 🏠, Transport 🚌.
Task: Parse input (text or image) into JSON: {{"amount": float, "category": str, "merchant": str, "note": str}}.
Rules: 
1. If no currency, assume EUR.
2. If category is ambiguous, use "Other".
3. Auto-fix merchant names.
4. Output JSON only.
"""

HELP_TEXT = f"""
🤖 **Family Finance Bot Instructions**

**1. Add an Expense**
• `45 Rewe`
• `12.50 Pizza`

**2. Scan a Receipt** 📸
Tap 📎 and send a photo.

**3. Delete Mistake** 🗑️
Type `/undo` to delete your last entry.

**4. View Data** 📊
[Open Google Sheets](https://docs.google.com/spreadsheets/d/{SHEET_ID})
"""

def send_telegram(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id, 
        "text": text, 
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    requests.post(url, json=payload)

def get_telegram_file(file_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
    resp = requests.get(url).json()
    file_path = resp['result']['file_path']
    download_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    file_data = requests.get(download_url).content
    image = Image.open(BytesIO(file_data))
    max_dim = 2048
    if image.width > max_dim or image.height > max_dim:
        image.thumbnail((max_dim, max_dim))
    return image

def handler(event, context):
    if event['httpMethod'] == 'GET':
        return {'statusCode': 200, 'body': 'Bot is Online 🟢'}
    if event['httpMethod'] != 'POST':
        return {'statusCode': 200, 'body': 'OK'}

    try:
        data = json.loads(event['body'])
        if 'message' not in data: return {'statusCode': 200, 'body': 'OK'}
        
        msg = data['message']
        chat_id = msg['chat']['id']
        user_id = msg.get('from', {}).get('id')
        user_name = msg.get('from', {}).get('first_name', 'Unknown')
        
        if user_id not in ALLOWED_USERS:
            return {'statusCode': 200, 'body': 'OK'}

        if 'text' in msg:
            text_lower = msg['text'].strip().lower()
            if text_lower in ['/start', '/help', 'help']:
                send_telegram(chat_id, HELP_TEXT)
                return {'statusCode': 200, 'body': 'OK'}
            if text_lower == '/undo':
                sh = gc.open_by_key(SHEET_ID).sheet1
                rows = sh.get_all_values()
                if len(rows) <= 1:
                    send_telegram(chat_id, "⚠️ Nothing to delete.")
                    return {'statusCode': 200, 'body': 'OK'}
                last_row = rows[-1]
                if len(last_row) > 5 and last_row[5] == user_name:
                    sh.delete_rows(len(rows))
                    send_telegram(chat_id, f"🗑️ *Deleted:* {last_row[1]}€ ({last_row[3]})")
                else:
                    send_telegram(chat_id, "⚠️ Can't delete: The last entry was not yours.")
                return {'statusCode': 200, 'body': 'OK'}

        input_content = []
        input_content.append(SYSTEM_PROMPT.format(date=datetime.now().strftime("%Y-%m-%d")))
        
        if 'photo' in msg:
            file_id = msg['photo'][-1]['file_id']
            send_telegram(chat_id, "👀 Scanning receipt...")
            image = get_telegram_file(file_id)
            input_content.append(image)
            input_content.append("Analyze this receipt image.")
        elif 'text' in msg:
            input_content.append(f"Input text: {msg['text']}")
        else:
            return {'statusCode': 200, 'body': 'OK'}

        response = model.generate_content(input_content)
        clean_json = response.text.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(clean_json)

        try:
            amount = float(parsed.get('amount', 0))
            if amount <= 0:
                send_telegram(chat_id, "⚠️ Amount must be greater than 0.")
                return {'statusCode': 200, 'body': 'OK'}
        except ValueError:
            send_telegram(chat_id, "⚠️ Couldn't understand the amount.")
            return {'statusCode': 200, 'body': 'OK'}

        sh = gc.open_by_key(SHEET_ID).sheet1
        sh.append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            amount,
            parsed.get('category'),
            parsed.get('merchant'),
            parsed.get('note'),
            user_name
        ])
        reply = f"✅ Saved *€{amount}* to *{parsed['category']}* ({parsed['merchant']})"
        send_telegram(chat_id, reply)

    except Exception as e:
        print(f"Error: {e}")
        send_telegram(chat_id, "⚠️ Error. Please try again.")
    return {'statusCode': 200, 'body': 'OK'}