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
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "RayFamilyFinanceBot")

# Validate critical env vars on startup
if not TELEGRAM_TOKEN or not GROQ_API_KEY or not SHEET_ID:
    raise ValueError("Missing required environment variables: TELEGRAM_TOKEN, GROQ_API_KEY, or GOOGLE_SHEET_ID")

# Parse allowed users
try:
    ALLOWED_USERS = json.loads(os.environ.get("ALLOWED_USERS", "[]"))
    if not ALLOWED_USERS:
        logger.warning("ALLOWED_USERS is empty - bot will reject all requests")
except json.JSONDecodeError:
    raise ValueError("ALLOWED_USERS must be valid JSON array")

# Categories for validation
ALLOWED_CATEGORIES = [
    'Groceries', 'Food Takeout', 'Travel', 'Subscription',
    'Investment', 'Household', 'Transport', 'Other'
]

# Limits
MAX_AMOUNT = 10000  # Sanity check for AI hallucinations

# --- SETUP CLIENTS ---
client = Groq(api_key=GROQ_API_KEY, timeout=15.0)

# Google Sheets client (with error handling)
gc = None

def get_sheets_client():
    """Lazy initialization of Google Sheets client with error handling"""
    global gc
    if gc is None:
        try:
            creds_dict = json.loads(os.environ.get("GOOGLE_JSON_KEY"))
            scope = [
                'https://spreadsheets.google.com/feeds',
                'https://www.googleapis.com/auth/drive'
            ]
            creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
            gc = gspread.authorize(creds)
            logger.info("Google Sheets client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Google Sheets client: {e}")
            raise
    return gc

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
4. Commas (,) are decimal separators: "6,55" = 6.55 EUR
5. Integers without separators are whole euros: "655" = 655.00 EUR
6. Numbers with commas: "655,00" = 655.00 EUR
7. TRUST the exact number given - do not scale down large amounts
8. Output ONLY valid JSON - no markdown, no explanations, no preamble

EXAMPLES:
Input: "45 Rewe"
Output: {{"amount": 45.0, "category": "Groceries", "merchant": "Rewe", "note": ""}}

Input: "5 DM"
Output: {{"amount": 5.0, "category": "Household", "merchant": "dm-drogerie markt", "note": ""}}

Input: "12,50 pizza"
Output: {{"amount": 12.5, "category": "Food Takeout", "merchant": "Unknown", "note": "pizza"}}

Input: "655 investment etf"
Output: {{"amount": 655.0, "category": "Investment", "merchant": "Unknown", "note": "etf"}}

Input: Receipt image showing: "EDEKA - Total: 34,89 EUR"
Output: {{"amount": 34.89, "category": "Groceries", "merchant": "Edeka", "note": ""}}
"""

HELP_TEXT = """
🤖 **Family Finance Bot**

Commands:
`/start` - Wake up bot
`/summary` - 📊 Interactive Dashboard
`/undo` - Delete your last expense
`/share` - Share bot with family

Just send an expense like "45 Rewe" or upload a receipt photo!
"""

SHARE_TEXT = f"""
🤝 **Share this Bot**

1. **Forward this message** to your family member
2. Tell them to click: https://t.me/{BOT_USERNAME}?start=family
3. **Important:** Get their Telegram ID from @userinfobot and add it to ALLOWED_USERS in Vercel settings

📊 **Google Sheet:**
https://docs.google.com/spreadsheets/d/{SHEET_ID}
(They need to request access)
"""

# --- TELEGRAM API HELPERS ---
def send_telegram(chat_id, text, reply_markup=None):
    """Send a message via Telegram Bot API"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            logger.error(f"Failed to send message: {resp.text}")
    except Exception as e:
        logger.error(f"Error sending telegram message: {e}")

def edit_telegram_message(chat_id, message_id, text, reply_markup=None):
    """Edit an existing message"""
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
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            logger.warning(f"Edit message failed: {resp.text}")
    except Exception as e:
        logger.error(f"Edit error: {e}")

def answer_callback(callback_id, text=None):
    """Acknowledge a callback query"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Callback answer error: {e}")

def get_telegram_image_base64(file_id):
    """Download and encode a Telegram image"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
        resp = requests.get(url, timeout=10).json()
        
        if 'result' not in resp:
            raise ValueError("Invalid response from getFile")
        
        file_path = resp['result']['file_path']
        download_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        image_data = requests.get(download_url, timeout=15).content
        return base64.b64encode(image_data).decode('utf-8')
    except Exception as e:
        logger.error(f"Failed to download image: {e}")
        raise

# --- DASHBOARD ANALYTICS ENGINE ---
class DashboardEngine:
    """Handles all dashboard/analytics functionality"""
    
    def __init__(self):
        self.cache = {'data': None, 'timestamp': None}
        self.cache_ttl = 120  # 2 minutes cache
    
    def get_dataframe(self, force_refresh=False):
        """Get expense data as pandas DataFrame with caching"""
        now = datetime.now()
        
        # Return cached data if valid
        if not force_refresh and self.cache['data'] is not None:
            if self.cache['timestamp'] and (now - self.cache['timestamp']).seconds < self.cache_ttl:
                return self.cache['data']
        
        try:
            sheets_client = get_sheets_client()
            sh = sheets_client.open_by_key(SHEET_ID).sheet1
            raw_data = sh.get_all_values()
            
            if len(raw_data) < 2:
                return None
            
            # Use first row as headers
            df = pd.DataFrame(raw_data[1:], columns=raw_data[0])
            
            # Validate and find required columns
            date_col = self._find_column(df, ['date', 'timestamp', 'time'])
            amount_col = self._find_column(df, ['amount', 'price', 'cost', 'value'])
            
            if not date_col or not amount_col:
                logger.error(f"Missing required columns. Found: {df.columns.tolist()}")
                return None
            
            # Parse amounts
            df[amount_col] = pd.to_numeric(
                df[amount_col].astype(str).str.replace(',', '.'),
                errors='coerce'
            ).fillna(0)
            df = df.rename(columns={amount_col: 'Amount'})
            
            # Parse dates
            df = self._parse_dates(df, date_col)
            
            if 'Date' not in df.columns or df['Date'].isna().all():
                logger.error("All dates are invalid")
                return None
            
            # Cache the result
            self.cache['data'] = df
            self.cache['timestamp'] = now
            
            return df
            
        except Exception as e:
            logger.error(f"Dataframe Error: {e}", exc_info=True)
            return None
    
    def _find_column(self, df, possible_names):
        """Find a column by checking multiple possible names (case-insensitive)"""
        for col in df.columns:
            if col.lower() in possible_names:
                return col
        return None
    
    def _parse_dates(self, df, date_col):
        """Try multiple date formats"""
        date_formats = [
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%d/%m/%Y",
            "%d.%m.%Y",
            "%m/%d/%Y"
        ]
        
        for fmt in date_formats:
            try:
                parsed = pd.to_datetime(df[date_col], format=fmt, errors='coerce')
                if parsed.notna().sum() > 0:
                    df = df.rename(columns={date_col: 'Date'})
                    df['Date'] = parsed
                    return df
            except:
                continue
        
        return df
    
    def generate_summary(self, view_type="overview", drill_target=None, period="current_month"):
        """Generate dashboard summary with various views"""
        df = self.get_dataframe()
        
        if df is None:
            return "⚠️ No valid data found in sheet. Please check your Google Sheet structure.", []
        
        # Filter by period
        df_filtered = self._filter_by_period(df, period)
        
        if df_filtered.empty:
            period_name = self._get_period_name(period)
            return f"📊 No data found for {period_name}", []
        
        # Generate the appropriate view
        if view_type == "overview":
            return self._view_overview(df_filtered, period)
        elif view_type == "category":
            return self._view_category(df_filtered, period)
        elif view_type == "user":
            return self._view_users(df_filtered, period)
        elif view_type == "merchant":
            return self._view_merchants(df_filtered, period)
        elif view_type == "history":
            return self._view_history(df_filtered, period)
        elif view_type == "drill_user" and drill_target:
            return self._view_user_drill(df_filtered, drill_target, period)
        else:
            return self._view_overview(df_filtered, period)
    
    def _filter_by_period(self, df, period):
        """Filter dataframe by time period"""
        if period == "current_month":
            current_month = datetime.now().strftime("%Y-%m")
            return df[df['Date'].dt.strftime('%Y-%m') == current_month]
        elif period == "last_month":
            last_month = (datetime.now().replace(day=1) - pd.Timedelta(days=1)).strftime("%Y-%m")
            return df[df['Date'].dt.strftime('%Y-%m') == last_month]
        elif period == "year":
            current_year = datetime.now().strftime("%Y")
            return df[df['Date'].dt.strftime('%Y') == current_year]
        else:
            return df
    
    def _get_period_name(self, period):
        """Get human-readable period name"""
        if period == "current_month":
            return datetime.now().strftime('%B %Y')
        elif period == "last_month":
            return (datetime.now().replace(day=1) - pd.Timedelta(days=1)).strftime('%B %Y')
        elif period == "year":
            return datetime.now().strftime('%Y')
        return "All Time"
    
    def _view_overview(self, df, period):
        """Main overview dashboard"""
        total = df['Amount'].sum()
        count = len(df)
        avg = df['Amount'].mean()
        period_name = self._get_period_name(period)
        
        report = f"📊 **Dashboard: {period_name}**\n\n"
        report += f"💰 **Total:** €{total:,.2f}\n"
        report += f"📝 **Transactions:** {count}\n"
        report += f"📊 **Average:** €{avg:.2f}\n\n"
        
        # Top 3 categories
        report += "**🏆 Top Categories:**\n"
        top_cats = df.groupby('Category')['Amount'].sum().sort_values(ascending=False).head(3)
        for cat, amt in top_cats.items():
            pct = (amt / total * 100) if total > 0 else 0
            report += f"• {cat}: €{amt:,.2f} ({pct:.1f}%)\n"
        
        # No extra buttons for overview
        return report, []
    
    def _view_category(self, df, period):
        """Category breakdown view"""
        total = df['Amount'].sum()
        period_name = self._get_period_name(period)
        
        report = f"📊 **Dashboard: {period_name}**\n"
        report += f"💰 **Total: €{total:,.2f}**\n\n"
        report += "**📂 By Category:**\n"
        
        data = df.groupby('Category')['Amount'].sum().sort_values(ascending=False)
        for cat, amt in data.items():
            pct = (amt / total * 100) if total > 0 else 0
            report += f"• {cat}: €{amt:,.2f} ({pct:.1f}%)\n"
        
        return report, []
    
    def _view_users(self, df, period):
        """User breakdown view with drill-down buttons"""
        total = df['Amount'].sum()
        period_name = self._get_period_name(period)
        
        report = f"📊 **Dashboard: {period_name}**\n"
        report += f"💰 **Total: €{total:,.2f}**\n\n"
        report += "**👤 By User:**\n"
        
        extra_buttons = []
        data = df.groupby('User')['Amount'].sum().sort_values(ascending=False)
        
        for user, amt in data.items():
            pct = (amt / total * 100) if total > 0 else 0
            report += f"• {user}: €{amt:,.2f} ({pct:.1f}%)\n"
            
            # Create drill-down button
            short_user = str(user)[:20]
            extra_buttons.append({
                "text": f"🔎 {short_user}",
                "callback_data": f"u:{short_user}"
            })
        
        return report, extra_buttons
    
    def _view_merchants(self, df, period):
        """Top merchants view"""
        total = df['Amount'].sum()
        period_name = self._get_period_name(period)
        
        report = f"📊 **Dashboard: {period_name}**\n"
        report += f"💰 **Total: €{total:,.2f}**\n\n"
        report += "**🏆 Top 10 Merchants:**\n"
        
        data = df.groupby('Merchant')['Amount'].sum().sort_values(ascending=False).head(10)
        for rank, (merch, amt) in enumerate(data.items(), 1):
            report += f"{rank}. {merch}: €{amt:,.2f}\n"
        
        return report, []
    
    def _view_history(self, df, period):
        """Recent transactions view"""
        period_name = self._get_period_name(period)
        
        report = f"📊 **Dashboard: {period_name}**\n\n"
        report += "**📅 Last 10 Expenses:**\n"
        
        last_10 = df.sort_values('Date', ascending=False).head(10)
        for _, row in last_10.iterrows():
            date_str = row['Date'].strftime('%d %b') if pd.notnull(row['Date']) else "?"
            user = row.get('User', 'Unknown')
            report += f"• {date_str}: €{row['Amount']:.2f} - {row['Category']} ({user})\n"
        
        return report, []
    
    def _view_user_drill(self, df, drill_target, period):
        """Drill-down view for specific user"""
        period_name = self._get_period_name(period)
        
        report = f"👤 **Analysis: {drill_target}**\n"
        report += f"🗓️ {period_name}\n\n"
        
        # Filter for specific user
        df_user = df[df['User'].astype(str).str.startswith(drill_target)]
        
        if df_user.empty:
            report += f"No expenses found for {drill_target}."
            extra_buttons = [{
                "text": "⬅️ Back to Users",
                "callback_data": "user"
            }]
            return report, extra_buttons
        
        user_total = df_user['Amount'].sum()
        user_count = len(df_user)
        user_avg = df_user['Amount'].mean()
        
        report += f"💰 **Total:** €{user_total:,.2f}\n"
        report += f"📝 **Transactions:** {user_count}\n"
        report += f"📊 **Average:** €{user_avg:.2f}\n\n"
        
        report += "**📂 Category Breakdown:**\n"
        cat_data = df_user.groupby('Category')['Amount'].sum().sort_values(ascending=False)
        for cat, amt in cat_data.items():
            pct = (amt / user_total * 100) if user_total > 0 else 0
            report += f"• {cat}: €{amt:,.2f} ({pct:.1f}%)\n"
        
        # Add back button
        extra_buttons = [{
            "text": "⬅️ Back to Users",
            "callback_data": "user"
        }]
        
        return report, extra_buttons

# Initialize dashboard engine
dashboard = DashboardEngine()

# --- EXPENSE PROCESSING ---
def validate_parsed_expense(parsed):
    """Validate AI-parsed expense data"""
    errors = []
    
    # Check amount
    amount = parsed.get('amount', 0)
    try:
        amount = float(amount)
    except (ValueError, TypeError):
        errors.append("Invalid amount format")
        return False, errors
    
    if amount <= 0:
        errors.append("Amount must be greater than 0")
    
    if amount > MAX_AMOUNT:
        errors.append(f"Amount €{amount:,.2f} exceeds maximum of €{MAX_AMOUNT:,.2f}")
    
    # Check category
    category = parsed.get('category', 'Other')
    if category not in ALLOWED_CATEGORIES:
        logger.warning(f"Unknown category '{category}', defaulting to 'Other'")
        parsed['category'] = 'Other'
    
    return len(errors) == 0, errors

def save_expense(parsed, user_name):
    """Save expense to Google Sheets"""
    try:
        sheets_client = get_sheets_client()
        sh = sheets_client.open_by_key(SHEET_ID).sheet1
        
        sh.append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            float(parsed.get('amount', 0)),
            parsed.get('category', 'Other'),
            parsed.get('merchant', 'Unknown'),
            parsed.get('note', ''),
            user_name
        ])
        
        # Invalidate cache
        dashboard.cache['data'] = None
        
        return True
    except Exception as e:
        logger.error(f"Failed to save expense: {e}", exc_info=True)
        return False

# --- MESSAGE HANDLERS ---
def handle_callback_query(callback_query):
    """Handle dashboard button clicks"""
    callback_id = callback_query['id']
    chat_id = callback_query['message']['chat']['id']
    message_id = callback_query['message']['message_id']
    data_val = callback_query['data']
    
    drill_target = None
    view_mode = data_val
    
    # Check for drill-down prefix
    if data_val.startswith("u:"):
        view_mode = "drill_user"
        drill_target = data_val[2:]
    
    # Generate new report
    new_text, extra_buttons = dashboard.generate_summary(view_mode, drill_target)
    
    # Build keyboard
    keyboard = build_dashboard_keyboard(view_mode, extra_buttons)
    
    edit_telegram_message(chat_id, message_id, new_text, keyboard)
    answer_callback(callback_id)

def build_dashboard_keyboard(view_mode, extra_buttons=None):
    """Build the dashboard navigation keyboard"""
    nav_buttons = [
        [
            {"text": "📊 Overview", "callback_data": "overview"},
            {"text": "📂 Category", "callback_data": "category"}
        ],
        [
            {"text": "👤 Users", "callback_data": "user"},
            {"text": "🏆 Merchants", "callback_data": "merchant"}
        ],
        [
            {"text": "📅 Recent", "callback_data": "history"},
            {"text": "🔄 Refresh", "callback_data": "overview"}
        ]
    ]
    
    final_keyboard = []
    
    if view_mode == "drill_user":
        # In drill-down, show extra buttons (back button) first
        if extra_buttons:
            final_keyboard.append(extra_buttons)
        final_keyboard.append([{"text": "🔄 Refresh", "callback_data": "user"}])
    else:
        # In normal mode, show user drill-down buttons if any
        if extra_buttons:
            # Group buttons in pairs
            for i in range(0, len(extra_buttons), 2):
                final_keyboard.append(extra_buttons[i:i+2])
        # Add navigation
        final_keyboard.extend(nav_buttons)
    
    return {"inline_keyboard": final_keyboard}

def handle_command(msg):
    """Handle bot commands"""
    chat_id = msg['chat']['id']
    text = msg['text'].lower()
    user_name = msg.get('from', {}).get('first_name', 'Unknown')
    
    if text == '/start':
        send_telegram(chat_id, HELP_TEXT)
        
    elif text == '/share':
        send_telegram(chat_id, SHARE_TEXT)
        
    elif text == '/summary':
        send_telegram(chat_id, "⏳ Loading Dashboard...")
        report, _ = dashboard.generate_summary("overview")
        keyboard = build_dashboard_keyboard("overview")
        send_telegram(chat_id, report, keyboard)
        
    elif text == '/undo':
        handle_undo(chat_id, user_name)

def handle_undo(chat_id, user_name):
    """Handle /undo command with race condition protection"""
    try:
        sheets_client = get_sheets_client()
        sh = sheets_client.open_by_key(SHEET_ID).sheet1
        rows = sh.get_all_values()
        
        if len(rows) <= 1:
            send_telegram(chat_id, "⚠️ Nothing to delete.")
            return
        
        last_row = rows[-1]
        last_row_index = len(rows)
        
        # Verify ownership (assume User column is index 5)
        if len(last_row) > 5 and last_row[5] == user_name:
            # Store timestamp for verification
            timestamp = last_row[0] if len(last_row) > 0 else None
            
            # Re-fetch to check for race conditions
            current_rows = sh.get_all_values()
            
            # Verify nothing changed
            if len(current_rows) == last_row_index and \
               (not timestamp or current_rows[-1][0] == timestamp):
                sh.delete_rows(last_row_index)
                
                # Invalidate cache
                dashboard.cache['data'] = None
                
                amount = last_row[1] if len(last_row) > 1 else "?"
                category = last_row[2] if len(last_row) > 2 else "?"
                send_telegram(chat_id, f"🗑️ *Deleted:* €{amount} ({category})")
            else:
                send_telegram(chat_id, "⚠️ Cannot delete: New entries were added. Please try again.")
        else:
            send_telegram(chat_id, "⚠️ Cannot delete: The last entry is not yours.")
            
    except Exception as e:
        logger.error(f"Undo failed: {e}", exc_info=True)
        send_telegram(chat_id, "⚠️ Error deleting expense. Please try again.")

def handle_expense_message(msg):
    """Handle expense input (text or image)"""
    chat_id = msg['chat']['id']
    user_name = msg.get('from', {}).get('first_name', 
                        msg.get('from', {}).get('username', 'Unknown'))
    
    try:
        prompt_text = SYSTEM_PROMPT.format(date=datetime.now().strftime("%Y-%m-%d"))
        messages = []
        
        # Handle image
        if 'photo' in msg:
            send_telegram(chat_id, "👀 Scanning receipt...")
            try:
                base64_image = get_telegram_image_base64(msg['photo'][-1]['file_id'])
                messages = [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text + "\nAnalyze this receipt."},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                        }
                    ]
                }]
            except Exception as e:
                logger.error(f"Image processing failed: {e}")
                send_telegram(chat_id, "⚠️ Failed to process image. Please try again.")
                return
                
        # Handle text
        elif 'text' in msg:
            messages = [
                {"role": "system", "content": prompt_text},
                {"role": "user", "content": msg['text']}
            ]
        else:
            return
        
        # Call AI
        try:
            chat_completion = client.chat.completions.create(
                messages=messages,
                model="llama-3.2-90b-vision-preview",  # Updated to actual Groq model
                temperature=0,
                response_format={"type": "json_object"}
            )
            
            response_content = chat_completion.choices[0].message.content
            parsed = json.loads(response_content)
            
        except Exception as e:
            logger.error(f"AI processing failed: {e}", exc_info=True)
            send_telegram(chat_id, "⚠️ AI processing error. Please try again.")
            return
        
        # Validate
        is_valid, errors = validate_parsed_expense(parsed)
        
        if not is_valid:
            error_msg = "⚠️ " + "; ".join(errors)
            send_telegram(chat_id, error_msg)
            return
        
        # Save
        if save_expense(parsed, user_name):
            amount = float(parsed.get('amount', 0))
            category = parsed.get('category', 'Other')
            merchant = parsed.get('merchant', 'Unknown')
            
            confirm_msg = f"✅ Saved *€{amount:.2f}* to *{category}*"
            if merchant != 'Unknown':
                confirm_msg += f" ({merchant})"
            
            send_telegram(chat_id, confirm_msg)
        else:
            send_telegram(chat_id, "⚠️ Failed to save expense. Please try again.")
            
    except Exception as e:
        logger.error(f"Expense processing error: {e}", exc_info=True)
        send_telegram(chat_id, "⚠️ Error processing expense. Please try again.")

# --- MAIN HANDLER ---
class handler(BaseHTTPRequestHandler):
    """Vercel serverless function handler"""
    
    def do_GET(self):
        """Health check endpoint"""
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is Online")
    
    def do_POST(self):
        """Handle Telegram webhook"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            
            # Parse JSON
            try:
                data = json.loads(post_data)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON received")
                self.send_response(400)
                self.end_headers()
                return
            
            # Handle callback queries (dashboard interactions)
            if 'callback_query' in data:
                handle_callback_query(data['callback_query'])
                self.send_response(200)
                self.end_headers()
                return
            
            # Handle messages
            if 'message' not in data:
                self.send_response(200)
                self.end_headers()
                return
            
            msg = data['message']
            chat_id = msg.get('chat', {}).get('id')
            user_id = msg.get('from', {}).get('id')
            
            # Security check
            if user_id not in ALLOWED_USERS:
                logger.warning(f"Unauthorized access attempt from user {user_id}")
                self.send_response(200)
                self.end_headers()
                return
            
            # Route to appropriate handler
            if 'text' in msg and msg['text'].startswith('/'):
                handle_command(msg)
            else:
                handle_expense_message(msg)
            
            self.send_response(200)
            self.end_headers()
            
        except Exception as e:
            logger.error(f"Webhook handler error: {e}", exc_info=True)
            self.send_response(500)
            self.end_headers()
