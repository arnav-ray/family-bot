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
BOT_USERNAME = "FamilyFinanceBot"

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

YOUR TASK:
Parse user input (text or receipt image) into this exact JSON format:
{{"amount": float, "category": str, "merchant": str, "note": str}}

CATEGORIES (choose ONLY from these):
- Groceries 🛒 (Rewe, Aldi, Lidl, Edeka, Kaufland)
- Food Takeout 🍕 (Restaurants, delivery, fast food)
- Travel ✈️ (Flights, hotels, Deutsche Bahn, Uber, taxis)
- Subscription 📺 (Netflix, Spotify, Lingoda, gym memberships)
- Investment 💰 (Stocks, ETFs, savings deposits)
- Household 🏠 (dm-drogerie, cleaning supplies, furniture)
- Transport 🚌 (Public transit, fuel, parking)
- Other 🤷 (Use when nothing else fits)

CRITICAL PARSING RULES:
1. "DM" or "dm" = dm-drogerie markt drugstore, NOT Deutsche Mark currency
2. All amounts must be in EUR (Euros)
3. If no currency specified, assume EUR
4. Treat commas (,) as decimal separators: "6,55" = 6.55 EUR
5. Do NOT convert integers to cents: "655" = 655.00 EUR (not 6.55)
6. TRUST the exact number given - do not scale down large amounts
7. Output ONLY valid JSON - no markdown, no explanations, no preamble

EXAMPLES:
Input: "45 Rewe"
Output: {{"amount": 45.0, "category": "Groceries", "merchant": "Rewe", "note": ""}}

Input: "5 DM"
Output: {{"amount": 5.0, "category": "Household", "merchant": "dm-drogerie markt", "note": ""}}

Input: "12,50 pizza"
Output: {{"amount": 12.5, "category": "Food Takeout", "merchant": "Unknown", "note": "pizza"}}

Input: "655 investment etf"
Output: {{"amount": 655.0, "category": "Investment", "merchant": "Unknown", "note": "etf"}}

Input: "Taxi zum Flughafen 25"
Output: {{"amount": 25.0, "category": "Transport", "merchant": "Taxi", "note": "zum Flughafen"}}

Input: Receipt image showing: "EDEKA - Total: 34,89 EUR"
Output: {{"amount": 34.89, "category": "Groceries", "merchant": "Edeka", "note": ""}}
"""

# --- TEXT BLOCKS ---
HELP_TEXT = """
🤖 **Family Finance Bot**

**Commands:**
`/start` - Wake up the bot
`/undo` - Delete last expense
`/share` - Get links to share with family

**How to use:**
• Text: `15 Lunch`
• Photo: Send a receipt picture
"""

SHARE_TEXT = f"""
🤝 **Share this Bot**

1. **Forward this message** to your family member.
2. Tell them to click here: https://t.me/{BOT_USERNAME}?start=family
3. **Important:** Ask them for their Telegram ID (get it from @userinfobot) and add it to Vercel settings.

📊 **Google Sheet Link:**
https://docs.google.com/spreadsheets/d/{SHEET_ID}
(They will need to request access)
"""

def send_telegram(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}
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
        
        # --- FIX: ROBUST USER NAME EXTRACTION ---
        first_name = msg.get('from', {}).get('first_name', '')
        username = msg.get('from', {}).get('username', '')
        user_name = first_name if first_name else (username if username else 'Unknown')
        
        # Security Check
        if user_id not in ALLOWED_USERS:
            send_telegram(chat_id, f"⛔ **Unauthorized**\nYour ID is `{user_id}`.\nAsk the admin to add this ID to the allowed list.")
            self.send_response(200); self.end_headers(); return

        # --- COMMANDS ---
        if 'text' in msg and msg['text'].startswith('/'):
            text_lower = msg['text'].lower()
            
            if text_lower == '/start':
                send_telegram(chat_id, HELP_TEXT)
            elif text_lower == '/share':
                send_telegram(chat_id, SHARE_TEXT)
            elif text_lower == '/undo':
                try:
                    sh = gc.open_by_key(SHEET_ID).sheet1
                    rows = sh.get_all_values()
                    if len(rows) > 1:
                        last_row = rows[-1]
                        if len(last_row) > 5 and last_row[5] == user_name:
                            sh.delete_rows(len(rows))
                            send_telegram(chat_id, f"🗑️ *Deleted:* €{last_row[1]} ({last_row[3]})")
                        else:
                            send_telegram(chat_id, "⚠️ Can't delete: The last entry was not yours.")
                    else:
                        send_telegram(chat_id, "⚠️ Nothing to delete.")
                except Exception as e:
                    send_telegram(chat_id, "⚠️ Error deleting.")
            
            self.send_response(200); self.end_headers(); return

        # --- AI PROCESSING ---
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

            # Call Groq
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
                # --- FIX: EXPLICIT APPEND WITH USER NAME ---
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
