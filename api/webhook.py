from http.server import BaseHTTPRequestHandler
import json
import os
import requests
from google import genai
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
# New Google GenAI Client
client = genai.Client(api_key=GOOGLE_API_KEY)

# Load Google Sheets Credentials
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

HELP_TEXT = """
🤖 **Family Finance Bot Instructions**

**1. Add an Expense**
• `45 Rewe`
• `12.50 Pizza`

**2. Scan a Receipt** 📸
Tap 📎 and send a photo.

**3. Delete Mistake** 🗑️
Type `/undo` to delete your last entry.

**4. View Data** 📊
Check your Google Sheet!
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
    return image

# Vercel Serverless Handler
class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        
        try:
            data = json.loads(post_data)
        except:
            self.send_response(200)
            self.end_headers()
            return

        if 'message' not in data:
            self.send_response(200)
            self.end_headers()
            return

        msg = data['message']
        chat_id = msg['chat']['id']
        user_id = msg.get('from', {}).get('id')
        user_name = msg.get('from', {}).get('first_name', 'Unknown')

        # Security Check
        if user_id not in ALLOWED_USERS:
            self.send_response(200)
            self.end_headers()
            return

        # Handle Commands
        if 'text' in msg:
            text_lower = msg['text'].strip().lower()
            if text_lower in ['/start', '/help']:
                send_telegram(chat_id, HELP_TEXT)
                self.send_response(200)
                self.end_headers()
                return

            if text_lower == '/undo':
                try:
                    sh = gc.open_by_key(SHEET_ID).sheet1
                    rows = sh.get_all_values()
                    if len(rows) > 1:
                        last_row = rows[-1]
                        if len(last_row) > 5 and last_row[5] == user_name:
                            sh.delete_rows(len(rows))
                            send_telegram(chat_id, f"🗑️ *Deleted:* €{last_row[1]} ({last_row[3]})")
                        else:
                            send_telegram(chat_id, "⚠️ The last entry was not yours.")
                    else:
                        send_telegram(chat_id, "⚠️ Nothing to delete.")
                except Exception as e:
                    send_telegram(chat_id, "⚠️ Error deleting.")
                self.send_response(200)
                self.end_headers()
                return

        # Prepare AI Input
        input_content = []
        
        # Add System Prompt
        prompt_text = SYSTEM_PROMPT.format(date=datetime.now().strftime("%Y-%m-%d"))
        
        if 'photo' in msg:
            file_id = msg['photo'][-1]['file_id']
            send_telegram(chat_id, "👀 Scanning receipt...")
            image = get_telegram_file(file_id)
            # The new SDK handles images cleanly
            response = client.models.generate_content(
                model='gemini-1.5-flash',
                contents=[prompt_text, image, "Analyze this receipt."]
            )
        elif 'text' in msg:
            response = client.models.generate_content(
                model='gemini-1.5-flash',
                contents=[prompt_text, f"Input text: {msg['text']}"]
            )
        else:
            self.send_response(200)
            self.end_headers()
            return

        # Parse AI Response
        try:
            clean_json = response.text.replace("```json", "").replace("```", "").strip()
            parsed = json.loads(clean_json)
            
            amount = float(parsed.get('amount', 0))
            if amount > 0:
                sh = gc.open_by_key(SHEET_ID).sheet1
                sh.append_row([
                    datetime.now().strftime("%Y-%m-%d %H:%M"),
                    amount,
                    parsed.get('category', 'Other'),
                    parsed.get('merchant', 'Unknown'),
                    parsed.get('note', ''),
                    user_name
                ])
                reply = f"✅ Saved *€{amount}* to *{parsed.get('category')}* ({parsed.get('merchant')})"
                send_telegram(chat_id, reply)
            else:
                send_telegram(chat_id, "⚠️ Amount must be greater than 0.")
        except Exception as e:
            print(f"Error: {e}")
            send_telegram(chat_id, "⚠️ I couldn't understand that. Try '45 Groceries'")

        self.send_response(200)
        self.end_headers()
