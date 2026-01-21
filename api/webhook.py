from http.server import BaseHTTPRequestHandler
import json
import os
import base64
import requests
from groq import Groq
import gspread
import pandas as pd
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
ALLOWED_USERS = json.loads(os.environ.get("ALLOWED_USERS", "[]"))
BOT_USERNAME = os.environ.get("BOT_USERNAME", "RayFamilyFinanceBot") # Updated default

# --- SETUP CLIENTS ---
client = Groq(api_key=GROQ_API_KEY)

try:
    creds_dict = json.loads(os.environ.get("GOOGLE_JSON_KEY"))
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    gc = gspread.authorize(creds)
except Exception as e:
    print(f"Auth Error: {e}")

# --- PROMPTS ---
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

HELP_TEXT = """
🤖 **Family Finance Bot**
Commands:
`/start` - Wake up
`/summary` - 📊 Interactive Dashboard
`/undo` - Delete last expense
`/share` - Share bot with family
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

# --- TELEGRAM API HELPERS ---
def send_telegram(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id, 
        "text": text, 
        "parse_mode": "Markdown", 
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(url, json=payload)

def edit_telegram_message(chat_id, message_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    
    try:
        resp = requests.post(url, json=payload)
        if resp.status_code != 200:
            print(f"Edit message failed: {resp.text}")
    except Exception as e:
        print(f"Edit error: {e}")

def answer_callback(callback_id, text=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
    payload = {"callback_query_id": callback_id}
    if text: payload["text"] = text
    requests.post(url, json=payload)

def get_telegram_image_base64(file_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
    resp = requests.get(url).json()
    file_path = resp['result']['file_path']
    download_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    image_data = requests.get(download_url).content
    return base64.b64encode(image_data).decode('utf-8')

# --- ANALYTICS ENGINE (ROBUST FIX) ---
def get_dataframe():
    try:
        sh = gc.open_by_key(SHEET_ID).sheet1
        raw_data = sh.get_all_values()
        if len(raw_data) < 2: return None
        
        # Use first row as headers
        df = pd.DataFrame(raw_data[1:], columns=raw_data[0])
        
        # Find the date column (flexible naming)
        date_col = None
        for col in df.columns:
            if col.lower() in ['date', 'timestamp', 'time']:
                date_col = col
                break
        
        if not date_col:
            print("No date column found!")
            return None
        
        # Parse amounts (Find column resembling 'Amount')
        amount_col = None
        for col in df.columns:
            if col.lower() in ['amount', 'price', 'cost', 'value']:
                amount_col = col
                break
        
        if amount_col:
            df[amount_col] = pd.to_numeric(
                df[amount_col].astype(str).str.replace(',', '.'), 
                errors='coerce'
            ).fillna(0)
            # Rename for consistency
            df = df.rename(columns={amount_col: 'Amount'})
        
        # Parse dates
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                df[date_col] = pd.to_datetime(df[date_col], format=fmt, errors='coerce')
                if df[date_col].notna().sum() > 0:
                    df = df.rename(columns={date_col: 'Date'})
                    break
            except:
                continue
        
        if 'Date' not in df.columns or df['Date'].isna().all():
            print("All dates invalid")
            return None
            
        return df
    except Exception as e:
        print(f"Dataframe Error: {e}")
        return None

def generate_pivot(view_type="month", drill_target=None):
    df = get_dataframe()
    if df is None: return "⚠️ No valid data found in sheet.", []
    
    current_month = datetime.now().strftime("%Y-%m")
    df_month = df[df['Date'].dt.strftime('%Y-%m') == current_month]
    
    if df_month.empty: return f"📊 No data found for {current_month}", []

    total = df_month['Amount'].sum()
    report = f"📊 **Dashboard: {datetime.now().strftime('%B %Y')}**\n💰 **Total: €{total:.2f}**\n\n"
    
    extra_buttons = [] 

    if view_type == "category":
        report += "**📂 By Category:**\n"
        data = df_month.groupby('Category')['Amount'].sum().sort_values(ascending=False)
        for cat, amt in data.items():
            report += f"• {cat}: €{amt:.2f}\n"
            
    elif view_type == "user":
        report += "**👤 Select User to Drill-down:**\n"
        data = df_month.groupby('User')['Amount'].sum().sort_values(ascending=False)
        for user, amt in data.items():
            report += f"• {user}: €{amt:.2f}\n"
            # Create a button for every user found (Truncate long names)
            short_user = str(user)[:20]
            extra_buttons.append({"text": f"🔎 {short_user}", "callback_data": f"u:{short_user}"})

    elif view_type == "merchant":
        report += "**🏆 Top 5 Merchants:**\n"
        data = df_month.groupby('Merchant')['Amount'].sum().sort_values(ascending=False).head(5)
        for merch, amt in data.items():
            report += f"• {merch}: €{amt:.2f}\n"
            
    elif view_type == "history":
        report += "**📅 Last 5 Expenses:**\n"
        last_5 = df_month.sort_values('Date', ascending=False).head(5)
        for _, row in last_5.iterrows():
            date_str = row['Date'].strftime('%d %b') if pd.notnull(row['Date']) else "?"
            report += f"• {date_str}: €{row['Amount']} ({row['Category']})\n"
            
    elif view_type == "drill_user" and drill_target:
        # DRILL DOWN LOGIC
        report = f"👤 **Analysis for: {drill_target}**\n🗓️ {datetime.now().strftime('%B %Y')}\n\n"
        
        # Filter for specific user
        df_user = df_month[df_month['User'].astype(str).str.startswith(drill_target)]
        
        if df_user.empty:
            report += f"No expenses found for {drill_target}."
        else:
            user_total = df_user['Amount'].sum()
            report += f"💰 **User Total: €{user_total:.2f}**\n\n**📂 Breakdown:**\n"
            cat_data = df_user.groupby('Category')['Amount'].sum().sort_values(ascending=False)
            for cat, amt in cat_data.items():
                report += f"• {cat}: €{amt:.2f}\n"
        
        # Add a "Back" button
        extra_buttons.append({"text": "⬅️ Back to Users", "callback_data": "user"})

    return report, extra_buttons

# --- MAIN HANDLER ---
class handler(BaseHTTPRequestHandler):
    # Health Check Endpoint (Fix Issue 4)
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Online")

    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        
        try:
            data = json.loads(post_data)
        except:
            self.send_response(200); self.end_headers(); return

        # 1. HANDLE CALLBACK QUERIES (BUTTON CLICKS)
        if 'callback_query' in data:
            cb = data['callback_query']
            callback_id = cb['id']
            chat_id = cb['message']['chat']['id']
            message_id = cb['message']['message_id']
            data_val = cb['data']
            
            drill_target = None
            view_mode = data_val
            
            # Check for drill-down prefix "u:" (User)
            if data_val.startswith("u:"):
                view_mode = "drill_user"
                drill_target = data_val[2:] # Remove "u:" to get username
            
            # Generate new report
            new_text, extra_buttons = generate_pivot(view_mode, drill_target)
            
            # Build Keyboard (Fix Issue 5 - Better Layout)
            nav_buttons = [
                [{"text": "📂 Category", "callback_data": "category"}, {"text": "👤 User", "callback_data": "user"}],
                [{"text": "🏆 Merchants", "callback_data": "merchant"}, {"text": "📅 Recent", "callback_data": "history"}],
                [{"text": "🔄 Refresh", "callback_data": "month"}]
            ]
            
            final_keyboard = []
            
            if view_mode == "drill_user":
                # In drill-down, only show back button and refresh
                if extra_buttons:
                    final_keyboard.append(extra_buttons) # Already a list of dicts
                final_keyboard.append([{"text": "🔄 Refresh", "callback_data": "category"}])
            else:
                # In normal mode, add user selection buttons
                if extra_buttons:
                    # Group user buttons in pairs
                    for i in range(0, len(extra_buttons), 2):
                        final_keyboard.append(extra_buttons[i:i+2])
                # Add standard navigation
                final_keyboard.extend(nav_buttons)
            
            keyboard = {"inline_keyboard": final_keyboard}
            
            edit_telegram_message(chat_id, message_id, new_text, keyboard)
            answer_callback(callback_id)
            self.send_response(200); self.end_headers(); return

        # 2. HANDLE MESSAGES
        if 'message' not in data:
            self.send_response(200); self.end_headers(); return

        msg = data['message']
        chat_id = msg['chat']['id']
        user_id = msg.get('from', {}).get('id')
        
        # User Name Extraction
        first_name = msg.get('from', {}).get('first_name', '')
        username = msg.get('from', {}).get('username', '')
        user_name = first_name if first_name else (username if username else 'Unknown')

        # Security Check
        if user_id not in ALLOWED_USERS:
            self.send_response(200); self.end_headers(); return

        # Commands
        if 'text' in msg and msg['text'].startswith('/'):
            text_lower = msg['text'].lower()
            
            if text_lower == '/start':
                send_telegram(chat_id, HELP_TEXT)
            elif text_lower == '/share':
                send_telegram(chat_id, SHARE_TEXT)
            elif text_lower == '/summary':
                send_telegram(chat_id, "⏳ Loading Dashboard...")
                report, _ = generate_pivot("category") # Default view
                keyboard = {
                    "inline_keyboard": [
                        [{"text": "📂 Category", "callback_data": "category"}, {"text": "👤 User", "callback_data": "user"}],
                        [{"text": "🏆 Merchants", "callback_data": "merchant"}, {"text": "📅 Recent", "callback_data": "history"}],
                        [{"text": "🔄 Refresh", "callback_data": "month"}]
                    ]
                }
                send_telegram(chat_id, report, keyboard)
            elif text_lower == '/undo':
                try:
                    sh = gc.open_by_key(SHEET_ID).sheet1
                    rows = sh.get_all_values()
                    if len(rows) > 1:
                        last_row = rows[-1]
                        # Fix Issue 5: Race Condition Check
                        if len(last_row) > 5 and last_row[5] == user_name:
                            sh.delete_rows(len(rows))
                            send_telegram(chat_id, f"🗑️ *Deleted:* {last_row[1]} ({last_row[2]})")
                        else:
                            send_telegram(chat_id, "⚠️ Can't delete: The last entry was not yours.")
                    else:
                        send_telegram(chat_id, "⚠️ Nothing to delete.")
                except:
                    send_telegram(chat_id, "⚠️ Error deleting.")
            
            self.send_response(200); self.end_headers(); return

        # AI Processing
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
                    user_name
                ])
                send_telegram(chat_id, f"✅ Saved *€{amount}* to *{parsed.get('category')}*")
            else:
                send_telegram(chat_id, "⚠️ No amount found.")

        except Exception as e:
            print(f"Error: {e}")
            send_telegram(chat_id, "⚠️ Error. Try again.")

        self.send_response(200)
        self.end_headers()
