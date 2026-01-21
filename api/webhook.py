from http.server import BaseHTTPRequestHandler
import json
import os
import base64
import requests
from groq import Groq
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
ALLOWED_USERS = json.loads(os.environ.get("ALLOWED_USERS", "[]"))

# --- SETUP CLIENTS ---
client = Groq(api_key=GROQ_API_KEY)

try:
    creds_dict = json.loads(os.environ.get("GOOGLE_JSON_KEY"))
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    gc = gspread.authorize(creds)
except Exception as e:
    print(f"Auth Error: {e}")

# --- UPDATED BRAIN RULES ---
SYSTEM_PROMPT = """
Current Date: {date}
Categories: Groceries 🛒, Food Takeout 🍕, Travel ✈️, Subscription 📺, Investment 💰, Household 🏠, Transport 🚌, Other 🤷.
Task: Parse input (text or image) into JSON: {{"amount": float, "category": str, "merchant": str, "note": str}}.

CRITICAL RULES:
1. "DM" or "dm" means the shop "dm-drogerie markt". DO NOT treat it as Deutsche Mark currency.
2. Always output amount in EUR.
3. If no currency is specified, assume EUR.
4. If category is ambiguous, use "Other".
5. Output JSON only.
"""

def send_telegram(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    requests.post(url, json=payload)

def get_telegram_image_base64(file_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
    resp = requests.get(url).json()
    file_path = resp['result']['file_path']
    download_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    image_data = requests.get(download_url).content
    return base64.b64encode(image_data).decode('utf-8')

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        
        try:
            data = json.loads(post_data)
        except:
            self.send_response(200); self.end_headers(); return

        if 'message' not in data:
            self.send_response(200); self.end_headers(); return

        msg = data['message']
        chat_id = msg['chat']['id']
        user_id = msg.get('from', {}).get('id')
        
        # 1. ROBUST USER NAME EXTRACTION
        first_name = msg.get('from', {}).get('first_name', '')
        username = msg.get('from', {}).get('username', '')
        # Use first name, fallback to username, fallback to 'Unknown'
        user_name = first_name if first_name else (username if username else 'Unknown')
        
        # Security Check
        if user_id not in ALLOWED_USERS:
            self.send_response(200); self.end_headers(); return

        if 'text' in msg and msg['text'] == '/start':
            send_telegram(chat_id, "🤖 **Bot Ready!**\nType `5 DM` to test.")
            self.send_response(200); self.end_headers(); return

        try:
            prompt_text = SYSTEM_PROMPT.format(date=datetime.now().strftime("%Y-%m-%d"))
            messages = []

            if 'photo' in msg:
                send_telegram(chat_id, "👀 Scanning receipt...")
                base64_image = get_telegram_image_base64(msg['photo'][-1]['file_id'])
                messages = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt_text + "\nAnalyze this receipt."},
                            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                        ]
                    }
                ]
            elif 'text' in msg:
                messages = [
                    {"role": "system", "content": prompt_text},
                    {"role": "user", "content": msg['text']}
                ]
            else:
                self.send_response(200); self.end_headers(); return

            # Call AI
            chat_completion = client.chat.completions.create(
                messages=messages,
                model="meta-llama/llama-4-scout-17b-16e-instruct", # Using the working model
                temperature=0,
                response_format={"type": "json_object"}
            )
            
            response_content = chat_completion.choices[0].message.content
            parsed = json.loads(response_content)
            
            amount = float(parsed.get('amount', 0))
            if amount > 0:
                sh = gc.open_by_key(SHEET_ID).sheet1
                # 2. EXPLICIT APPEND WITH USER NAME
                sh.append_row([
                    datetime.now().strftime("%Y-%m-%d %H:%M"),
                    amount,
                    parsed.get('category', 'Other'),
                    parsed.get('merchant', 'Unknown'),
                    parsed.get('note', ''),
                    user_name # Explicit variable
                ])
                send_telegram(chat_id, f"✅ Saved *€{amount}* to *{parsed.get('category')}*")
            else:
                send_telegram(chat_id, "⚠️ No amount found.")

        except Exception as e:
            print(f"Error: {e}")
            send_telegram(chat_id, "⚠️ Error. Try again.")

        self.send_response(200)
        self.end_headers()
