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
GROQ_API_KEY = os.environ.get("GROQ_API_KEY") # New Key
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
ALLOWED_USERS = json.loads(os.environ.get("ALLOWED_USERS", "[]"))

# --- SETUP CLIENTS ---
client = Groq(api_key=GROQ_API_KEY)

# Google Sheets Setup
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

def send_telegram(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    requests.post(url, json=payload)

def get_telegram_image_base64(file_id):
    # 1. Get File Path
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
    resp = requests.get(url).json()
    file_path = resp['result']['file_path']
    
    # 2. Download Image
    download_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    image_data = requests.get(download_url).content
    
    # 3. Encode to Base64 for Groq
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
        
        # Security Check
        if user_id not in ALLOWED_USERS:
            self.send_response(200); self.end_headers(); return

        # Help Command
        if 'text' in msg and msg['text'] == '/start':
            send_telegram(chat_id, "🤖 **Groq Bot Ready!**\nType `15 Lunch` or send a photo.")
            self.send_response(200); self.end_headers(); return

        # Prepare Content for AI
        try:
            prompt_text = SYSTEM_PROMPT.format(date=datetime.now().strftime("%Y-%m-%d"))
            messages = []

            if 'photo' in msg:
                send_telegram(chat_id, "👀 Scanning receipt (via Groq)...")
                base64_image = get_telegram_image_base64(msg['photo'][-1]['file_id'])
                
                messages = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt_text + "\nAnalyze this receipt image."},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                            }
                        ]
                    }
                ]
            elif 'text' in msg:
                messages = [
                    {
                        "role": "system",
                        "content": prompt_text
                    },
                    {
                        "role": "user",
                        "content": msg['text']
                    }
                ]
            else:
                self.send_response(200); self.end_headers(); return

            # Call Groq AI (Llama 3.2 Vision)
            chat_completion = client.chat.completions.create(
                messages=messages,
                model="meta-llama/llama-4-scout-17b-16e-instruct",
                temperature=0,
                response_format={"type": "json_object"}
            )
            
            response_content = chat_completion.choices[0].message.content
            parsed = json.loads(response_content)
            
            amount = float(parsed.get('amount', 0))
            if amount > 0:
                sh = gc.open_by_key(SHEET_ID).sheet1
                sh.append_row([
                    datetime.now().strftime("%Y-%m-%d %H:%M"),
                    amount,
                    parsed.get('category', 'Other'),
                    parsed.get('merchant', 'Unknown'),
                    parsed.get('note', ''),
                    msg.get('from', {}).get('first_name', 'User')
                ])
                send_telegram(chat_id, f"✅ Saved *€{amount}* to *{parsed.get('category')}*")
            else:
                send_telegram(chat_id, "⚠️ No amount found.")

        except Exception as e:
            print(f"Error: {e}")
            send_telegram(chat_id, "⚠️ Error. Try again.")

        self.send_response(200)
        self.end_headers()
