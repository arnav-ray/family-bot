from http.server import BaseHTTPRequestHandler
import json
import os
import base64
import requests
from groq import Groq
import gspread
import pandas as pd
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta
import logging
import uuid

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

# Goal types for validation
ALLOWED_GOAL_TYPES = [
    'Financial', 'Vacation', 'Item', 'Activity', 'Skill', 'Task', 'Other'
]

# Limits
MAX_AMOUNT = 10000  # Sanity check for expenses
MAX_GOAL_AMOUNT = 100000  # Sanity check for goals

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
EXPENSE_SYSTEM_PROMPT = """
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

GOAL_SYSTEM_PROMPT = """
Current Date: {current_date}

YOUR TASK:
Parse user input into this exact JSON format for a goal/task:
{{"type": str, "goal": str, "target_amount": float, "target_date": str}}

GOAL TYPES (choose ONE):
- Financial: Savings targets (emergency fund, house down payment)
- Vacation: Travel plans (trips, holidays)
- Item: Tangible purchases (furniture, electronics, car)
- Activity: Events or experiences (concert, class)
- Skill: Learning objectives (language, instrument)
- Task: Administrative to-dos (renew insurance, file taxes)
- Other: Anything that doesn't fit above

PARSING RULES:
1. **type**: Classify based on keywords and context
   - Money + "save"/"fund" → Financial
   - Travel words → Vacation
   - "Buy X" → Item
   - "Learn" → Skill
   - Action verbs (renew, file, call) → Task

2. **goal**: Extract the core objective (3-100 characters)
   - Clean format: "Trip to Japan" not "i want to go to japan"

3. **target_amount**: Extract numeric value
   - Default to 0 if not mentioned
   - Remove currency symbols (€, EUR, dollars)
   - Must be >= 0 and <= 100000
   - If unrealistic (e.g., 999999), return 0

4. **target_date**: Parse deadline into YYYY-MM-DD format
   - "by June" → If current month < June, use current year; else next year
   - "in 3 months" → Add 3 months to current date
   - "by 2027" → Use "2027-12-31"
   - "next summer" → Use approximate date (e.g., "2026-07-15")
   - If no date mentioned or ambiguous → return null

VALIDATION:
- target_date must be in the future (after {current_date})
- If target_date is in the past, return null
- If amount has decimals, keep 2 decimal places
- Output ONLY valid JSON - no markdown, no explanations

EXAMPLES:
Input: "Trip to Japan 5000 by December"
Output: {{"type": "Vacation", "goal": "Trip to Japan", "target_amount": 5000.0, "target_date": "2026-12-31"}}

Input: "Emergency fund 10000"
Output: {{"type": "Financial", "goal": "Emergency fund", "target_amount": 10000.0, "target_date": null}}

Input: "Learn Spanish by summer"
Output: {{"type": "Skill", "goal": "Learn Spanish", "target_amount": 0.0, "target_date": "2026-07-15"}}

Input: "Buy new sofa €1500 by March 2027"
Output: {{"type": "Item", "goal": "Buy new sofa", "target_amount": 1500.0, "target_date": "2027-03-31"}}

Input: "Renew car insurance next month"
Output: {{"type": "Task", "goal": "Renew car insurance", "target_amount": 0.0, "target_date": "2026-02-22"}}

Input: "Get concert tickets 80"
Output: {{"type": "Activity", "goal": "Get concert tickets", "target_amount": 80.0, "target_date": null}}
"""

HELP_TEXT = """
🤖 **Family Finance Bot**

**💰 Expense Tracking:**
Just send: `45 Rewe` or upload a receipt photo

**🎯 Goal Tracking:**
• `/goal` - Add a new goal
• `/goals` - View all goals
• Click ✅ to mark complete

**📊 Dashboards:**
• `/summary` - Expense analytics
• `/goals` - Goal overview

**Other Commands:**
• `/undo` - Delete last expense
• `/share` - Invite family members

**Examples:**
📝 Expense: `15 lunch` or `12,50 pizza`
🎯 Goals:
  • `/goal Trip to Italy 2000 by June`
  • `/goal Emergency fund 10000`
  • `/goal Learn Spanish by summer`
  • `/goal Renew passport next month`

**Goal Format:**
`/goal [description] [amount] [deadline]`
Amount and deadline are optional!
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

# --- EXPENSE DASHBOARD ENGINE ---
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
            sh = sheets_client.open_by_key(SHEET_ID)
            expenses_ws = sh.worksheet("Expenses")  # Explicit worksheet name
            raw_data = expenses_ws.get_all_values()
            
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
            return "⚠️ No valid data found in Expenses sheet. Please check your Google Sheet structure.", []
        
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

# --- GOALS MANAGEMENT ---
class GoalsManager:
    """Handles all goal-related operations"""
    
    def __init__(self):
        self.goals_cache = {'data': None, 'timestamp': None}
        self.cache_ttl = 60  # 1 minute cache for goals
    
    def get_goals(self, force_refresh=False, status_filter='Pending'):
        """Fetch goals from Google Sheets with caching"""
        now = datetime.now()
        
        # Return cached data if valid
        if not force_refresh and self.goals_cache['data'] is not None:
            if self.goals_cache['timestamp'] and (now - self.goals_cache['timestamp']).seconds < self.cache_ttl:
                cached_goals = self.goals_cache['data']
                if status_filter:
                    return [g for g in cached_goals if g.get('Status') == status_filter]
                return cached_goals
        
        try:
            sheets_client = get_sheets_client()
            sheet = sheets_client.open_by_key(SHEET_ID)
            goals_ws = sheet.worksheet("Goals")  # Explicit worksheet name
            
            all_rows = goals_ws.get_all_values()
            
            if len(all_rows) < 2:
                return []
            
            # Parse into list of dicts
            headers = all_rows[0]
            goals = []
            for row in all_rows[1:]:
                if len(row) >= len(headers):
                    goal_dict = dict(zip(headers, row))
                    goals.append(goal_dict)
            
            # Cache the result
            self.goals_cache['data'] = goals
            self.goals_cache['timestamp'] = now
            
            # Filter by status
            if status_filter:
                return [g for g in goals if g.get('Status') == status_filter]
            
            return goals
            
        except gspread.exceptions.WorksheetNotFound:
            logger.error("Goals worksheet not found! Please create 'Goals' tab in your Google Sheet.")
            raise ValueError("Goals worksheet not found. Please create it manually.")
        except Exception as e:
            logger.error(f"Failed to fetch goals: {e}", exc_info=True)
            return []
    
    def add_goal(self, goal_data, user_name):
        """Add a new goal to the sheet"""
        try:
            sheets_client = get_sheets_client()
            sheet = sheets_client.open_by_key(SHEET_ID)
            goals_ws = sheet.worksheet("Goals")
            
            # Generate unique ID
            goal_id = str(uuid.uuid4())[:8]
            
            # Prepare row data (match schema: 10 columns)
            row_data = [
                datetime.now().strftime("%Y-%m-%d"),        # Created_Date
                goal_data.get('type', 'Other'),              # Type
                goal_data.get('goal', 'Unnamed'),            # Goal_Name
                float(goal_data.get('target_amount', 0)),    # Target_Amount
                goal_data.get('target_date', ''),            # Target_Date
                'Pending',                                    # Status
                user_name,                                    # Created_By
                goal_id,                                      # Goal_ID
                '',                                           # Completed_Date
                ''                                            # Notes
            ]
            
            goals_ws.append_row(row_data)
            
            # Invalidate cache
            self.goals_cache['data'] = None
            
            logger.info(f"Goal created: {goal_id} by {user_name}")
            return True, goal_id
            
        except Exception as e:
            logger.error(f"Failed to add goal: {e}", exc_info=True)
            return False, None
    
    def mark_goal_done(self, goal_id, user_name):
        """Mark a goal as complete with race condition protection"""
        try:
            sheets_client = get_sheets_client()
            sheet = sheets_client.open_by_key(SHEET_ID)
            goals_ws = sheet.worksheet("Goals")
            
            # Fetch all rows
            all_rows = goals_ws.get_all_values()
            
            # Find goal by ID (Column H = index 7)
            goal_row_idx = None
            goal_name = None
            timestamp = None
            current_status = None
            
            for i, row in enumerate(all_rows[1:], start=2):  # Start at row 2
                if len(row) > 7 and row[7] == goal_id:
                    goal_row_idx = i
                    goal_name = row[2] if len(row) > 2 else "Unknown"
                    timestamp = row[0] if len(row) > 0 else None
                    current_status = row[5] if len(row) > 5 else None
                    break
            
            if goal_row_idx is None:
                logger.warning(f"Goal not found: {goal_id}")
                return False, "Goal not found"
            
            # Check if already done
            if current_status == 'Done':
                return False, "Already completed"
            
            # Race condition protection: Re-fetch and verify
            current_rows = goals_ws.get_all_values()
            if goal_row_idx - 1 >= len(current_rows):
                return False, "Goal was deleted"
            
            current_row = current_rows[goal_row_idx - 1]
            
            if len(current_row) > 0 and current_row[0] != timestamp:
                logger.warning(f"Race condition detected for goal {goal_id}")
                return False, "Goal was modified"
            
            # Update status (Column F = 6) and completion date (Column I = 9)
            goals_ws.update_cell(goal_row_idx, 6, 'Done')
            goals_ws.update_cell(goal_row_idx, 9, datetime.now().strftime("%Y-%m-%d"))
            
            # Invalidate cache
            self.goals_cache['data'] = None
            
            logger.info(f"Goal completed: {goal_id} by {user_name}")
            return True, goal_name
            
        except Exception as e:
            logger.error(f"Failed to mark goal done: {e}", exc_info=True)
            return False, "Error updating goal"
    
    def format_goals_message(self, goals):
        """Format goals list into a readable message"""
        if not goals:
            return "🎯 **Family Goals**\n\nNo active goals yet!\n\nAdd one with:\n`/goal Trip to Italy €2000 by June`"
        
        report = "🎯 **Family Goals**\n"
        report += f"📅 {datetime.now().strftime('%B %Y')}\n\n"
        
        # Separate financial and non-financial goals
        financial_goals = [g for g in goals if float(g.get('Target_Amount', 0)) > 0]
        task_goals = [g for g in goals if float(g.get('Target_Amount', 0)) == 0]
        
        if financial_goals:
            report += f"**💰 Financial Goals ({len(financial_goals)}):**\n"
            for goal in sorted(financial_goals, key=lambda x: x.get('Target_Date', '9999-99-99'))[:10]:
                amount = float(goal.get('Target_Amount', 0))
                name = goal.get('Goal_Name', 'Unknown')
                target_date = goal.get('Target_Date', '')
                
                report += f"• {name}: €{amount:,.2f}"
                if target_date and target_date != 'null':
                    report += f" (Due: {self._format_date(target_date)})"
                report += "\n"
        
        if task_goals:
            report += f"\n**📝 Tasks ({len(task_goals)}):**\n"
            for goal in sorted(task_goals, key=lambda x: x.get('Target_Date', '9999-99-99'))[:10]:
                name = goal.get('Goal_Name', 'Unknown')
                target_date = goal.get('Target_Date', '')
                
                report += f"• {name}"
                if target_date and target_date != 'null':
                    report += f" (Due: {self._format_date(target_date)})"
                report += "\n"
        
        if len(goals) > 20:
            report += f"\n_Showing first 20 of {len(goals)} goals_"
        
        return report
    
    def _format_date(self, date_str):
        """Format date string to readable format"""
        if not date_str or date_str == 'null':
            return None
        
        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            return date_obj.strftime("%b %d, %Y")
        except:
            return date_str

# Initialize goals manager
goals_manager = GoalsManager()

# --- VALIDATION FUNCTIONS ---
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

def validate_goal_data(parsed):
    """Validate AI-parsed goal data"""
    errors = []
    
    # Check required fields
    if 'goal' not in parsed or not parsed['goal']:
        errors.append("Goal name is required")
    
    if 'type' not in parsed:
        errors.append("Goal type is required")
    
    # Validate goal name
    goal_name = parsed.get('goal', '')
    if len(goal_name) < 3:
        errors.append("Goal name must be at least 3 characters")
    if len(goal_name) > 100:
        errors.append("Goal name must be less than 100 characters")
    
    # Validate type
    goal_type = parsed.get('type', 'Other')
    if goal_type not in ALLOWED_GOAL_TYPES:
        parsed['type'] = 'Other'
        logger.warning(f"Unknown goal type '{goal_type}', defaulting to 'Other'")
    
    # Validate amount
    try:
        amount = float(parsed.get('target_amount', 0))
        if amount < 0:
            errors.append("Amount cannot be negative")
        if amount > MAX_GOAL_AMOUNT:
            errors.append(f"Amount exceeds maximum (€{MAX_GOAL_AMOUNT:,})")
        parsed['target_amount'] = round(amount, 2)
    except (ValueError, TypeError):
        errors.append("Invalid amount format")
    
    # Validate date
    target_date = parsed.get('target_date')
    if target_date and target_date != 'null' and target_date:
        try:
            date_obj = datetime.strptime(target_date, "%Y-%m-%d")
            if date_obj < datetime.now():
                errors.append("Target date must be in the future")
        except ValueError:
            errors.append("Invalid date format (expected YYYY-MM-DD)")
    
    return len(errors) == 0, errors

# --- EXPENSE FUNCTIONS ---
def save_expense(parsed, user_name):
    """Save expense to Google Sheets"""
    try:
        sheets_client = get_sheets_client()
        sh = sheets_client.open_by_key(SHEET_ID)
        expenses_ws = sh.worksheet("Expenses")  # Explicit worksheet
        
        expenses_ws.append_row([
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
    user_name = callback_query['from'].get('first_name', 'Unknown')
    user_id = callback_query['from'].get('id')
    
    # Handle main menu callbacks
    if data_val.startswith('menu:'):
        menu_action = data_val[5:]
        
        if menu_action == 'summary':
            # Show expense dashboard
            report, _ = dashboard.generate_summary("overview")
            keyboard = build_dashboard_keyboard("overview")
            edit_telegram_message(chat_id, message_id, report, keyboard)
            answer_callback(callback_id, "Loading expenses...")
            
        elif menu_action == 'goals':
            # Show goals
            handle_view_goals_internal(chat_id, message_id)
            answer_callback(callback_id, "Loading goals...")
            
        elif menu_action == 'goal_help':
            # Show goal creation help
            goal_help_text = """
**🎯 How to Add Goals**

**Format:**
`/goal [description] [amount] [deadline]`

**Examples:**

💰 **With Money & Date:**
`/goal Trip to Italy 2000 by June`
`/goal Buy car €15000 by December 2026`

💰 **Money Only:**
`/goal Emergency fund 10000`
`/goal Save for wedding 5000`

📅 **Task with Deadline:**
`/goal Renew passport next month`
`/goal File taxes by April`

📝 **Simple Task:**
`/goal Learn Spanish`
`/goal Call insurance company`

**Tips:**
• Amount can be with or without € symbol
• Dates like "June", "summer", "next month" work!
• Just describe what you want - AI figures it out

**Ready?** Type `/goal` followed by your goal!
"""
            back_keyboard = {
                "inline_keyboard": [[
                    {"text": "⬅️ Back to Menu", "callback_data": "menu:main"}
                ]]
            }
            edit_telegram_message(chat_id, message_id, goal_help_text, back_keyboard)
            answer_callback(callback_id)
            
        elif menu_action == 'share':
            # Show share info
            back_keyboard = {
                "inline_keyboard": [[
                    {"text": "⬅️ Back to Menu", "callback_data": "menu:main"}
                ]]
            }
            edit_telegram_message(chat_id, message_id, SHARE_TEXT, back_keyboard)
            answer_callback(callback_id)
            
        elif menu_action == 'main':
            # Back to main menu
            main_menu_keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "💰 Expense Dashboard", "callback_data": "menu:summary"},
                        {"text": "🎯 View Goals", "callback_data": "menu:goals"}
                    ],
                    [
                        {"text": "📖 How to Add Goal", "callback_data": "menu:goal_help"},
                        {"text": "📤 Share Bot", "callback_data": "menu:share"}
                    ]
                ]
            }
            edit_telegram_message(chat_id, message_id, HELP_TEXT, main_menu_keyboard)
            answer_callback(callback_id)
        
        return
    
    # Handle goal completion
    if data_val.startswith('d:'):
        goal_id = data_val[2:]
        success, result = goals_manager.mark_goal_done(goal_id, user_name)
        
        if success:
            send_telegram(chat_id, f"🎉 Completed: {result}!")
            edit_telegram_message(
                chat_id,
                message_id,
                "Goal completed! Click 🔄 Refresh to update the list.",
                {"inline_keyboard": [[{"text": "🔄 Refresh", "callback_data": "goals:refresh"}]]}
            )
            answer_callback(callback_id, "✅ Marked as done!")
        else:
            answer_callback(callback_id, f"⚠️ {result}")
        return
    
    # Handle goal refresh
    if data_val == 'goals:refresh':
        handle_view_goals_internal(chat_id, message_id)
        answer_callback(callback_id, "Refreshed!")
        return
    
    # Handle expense dashboard callbacks
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
    
    if text == '/start' or text == '/help':
        # Build main menu keyboard
        main_menu_keyboard = {
            "inline_keyboard": [
                [
                    {"text": "💰 Expense Dashboard", "callback_data": "menu:summary"},
                    {"text": "🎯 View Goals", "callback_data": "menu:goals"}
                ],
                [
                    {"text": "📖 How to Add Goal", "callback_data": "menu:goal_help"},
                    {"text": "📤 Share Bot", "callback_data": "menu:share"}
                ]
            ]
        }
        send_telegram(chat_id, HELP_TEXT, main_menu_keyboard)
        
    elif text == '/share':
        send_telegram(chat_id, SHARE_TEXT)
        
    elif text == '/summary':
        send_telegram(chat_id, "⏳ Loading Dashboard...")
        report, _ = dashboard.generate_summary("overview")
        keyboard = build_dashboard_keyboard("overview")
        send_telegram(chat_id, report, keyboard)

def handle_undo(chat_id, user_name):
    """Handle /undo command with race condition protection"""
    try:
        sheets_client = get_sheets_client()
        sh = sheets_client.open_by_key(SHEET_ID)
        expenses_ws = sh.worksheet("Expenses")
        rows = expenses_ws.get_all_values()
        
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
            current_rows = expenses_ws.get_all_values()
            
            # Verify nothing changed
            if len(current_rows) == last_row_index and \
               (not timestamp or current_rows[-1][0] == timestamp):
                expenses_ws.delete_rows(last_row_index)
                
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

def handle_add_goal(msg):
    """Handle /goal command to add a new goal"""
    user_id = msg['from']['id']
    user_name = msg['from'].get('first_name', msg['from'].get('username', 'Unknown'))
    chat_id = msg['chat']['id']
    text = msg['text']
    
    # Clean input
    goal_input = text.replace('/goal', '', 1).strip()
    
    if not goal_input:
        send_telegram(
            chat_id,
            "**🎯 How to Add a Goal**\n\n"
            "**Format:**\n"
            "`/goal [description] [amount] [deadline]`\n\n"
            "**Examples:**\n"
            "• `/goal Trip to Italy 2000 by June`\n"
            "• `/goal Emergency fund 10000`\n"
            "• `/goal Learn Spanish by summer`\n"
            "• `/goal Renew insurance next month`\n\n"
            "💡 **Tip:** Amount and deadline are optional!"
        )
        return
    
    # Send "processing" message
    send_telegram(chat_id, "⏳ Processing your goal...")
    
    # Call AI to parse goal
    try:
        current_date = datetime.now()
        prompt = GOAL_SYSTEM_PROMPT.format(current_date=current_date.strftime("%Y-%m-%d"))
        
        response = client.chat.completions.create(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": goal_input}
            ],
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            temperature=0,
            response_format={"type": "json_object"}
        )
        
        response_content = response.choices[0].message.content
        logger.info(f"AI raw response: {response_content}")
        
        parsed = json.loads(response_content)
        logger.info(f"Goal parsed: {parsed}")
        
    except json.JSONDecodeError as e:
        logger.error(f"JSON parsing failed: {e}. Raw response: {response_content if 'response_content' in locals() else 'N/A'}")
        send_telegram(
            chat_id,
            "⚠️ Could not parse goal (invalid AI response).\n\n"
            "Please try again with a clearer format:\n"
            "`/goal Trip to Italy 2000 by June`"
        )
        return
    except Exception as e:
        logger.error(f"AI request failed: {e}", exc_info=True)
        send_telegram(
            chat_id,
            "⚠️ AI service error. Please try again.\n\n"
            "If this persists, try a simpler format:\n"
            "`/goal Save money 5000`"
        )
        return
    
    # Validate parsed data
    is_valid, errors = validate_goal_data(parsed)
    if not is_valid:
        error_msg = "⚠️ **Invalid goal:**\n\n"
        for error in errors:
            error_msg += f"• {error}\n"
        error_msg += "\n**Try again with correct format:**\n"
        error_msg += "`/goal [description] [amount] [deadline]`"
        send_telegram(chat_id, error_msg)
        return
    
    # Save goal
    success, goal_id = goals_manager.add_goal(parsed, user_name)
    
    if success:
        # Build confirmation message
        goal_name = parsed.get('goal', 'Unknown')
        goal_type = parsed.get('type', 'Other')
        amount = float(parsed.get('target_amount', 0))
        target_date = parsed.get('target_date')
        
        confirm_msg = f"🎉 **Goal Created!**\n\n"
        confirm_msg += f"📝 **Name:** {goal_name}\n"
        confirm_msg += f"🏷️ **Type:** {goal_type}\n"
        
        if amount > 0:
            confirm_msg += f"💰 **Target:** €{amount:,.2f}\n"
        
        if target_date and target_date != 'null':
            try:
                date_obj = datetime.strptime(target_date, "%Y-%m-%d")
                confirm_msg += f"📅 **Due:** {date_obj.strftime('%b %d, %Y')}\n"
            except:
                pass
        
        confirm_msg += f"\n✅ View all goals: `/goals`"
        
        send_telegram(chat_id, confirm_msg)
    else:
        send_telegram(chat_id, "⚠️ Failed to save goal. Please try again or contact support.")

def handle_view_goals(msg):
    """Handle /goals command to view all goals"""
    chat_id = msg['chat']['id']
    handle_view_goals_internal(chat_id)

def handle_view_goals_internal(chat_id, message_id=None):
    """Internal function to view goals (used for both command and refresh)"""
    try:
        # Fetch pending goals
        goals = goals_manager.get_goals(force_refresh=True, status_filter='Pending')
        
        # Format message
        report = goals_manager.format_goals_message(goals)
        
        # Build buttons (max 20 goals to avoid overload)
        buttons = []
        display_goals = goals[:20]
        
        for goal in display_goals:
            goal_name = goal.get('Goal_Name', 'Unknown')
            goal_id = goal.get('Goal_ID', '')
            
            if goal_id:
                # Truncate name for button
                button_name = goal_name[:18] + "..." if len(goal_name) > 18 else goal_name
                buttons.append({
                    "text": f"✅ {button_name}",
                    "callback_data": f"d:{goal_id}"
                })
        
        # Group buttons in rows of 2
        keyboard_rows = []
        for i in range(0, len(buttons), 2):
            keyboard_rows.append(buttons[i:i+2])
        
        # Add refresh button
        keyboard_rows.append([{"text": "🔄 Refresh", "callback_data": "goals:refresh"}])
        
        keyboard = {"inline_keyboard": keyboard_rows}
        
        # Edit existing message or send new one
        if message_id:
            edit_telegram_message(chat_id, message_id, report, keyboard)
        else:
            send_telegram(chat_id, report, keyboard)
        
    except ValueError as e:
        # Goals worksheet doesn't exist
        error_msg = (
            "⚠️ **Goals feature not set up**\n\n"
            "Please create a worksheet named 'Goals' in your Google Sheet with these columns:\n"
            "`Created_Date | Type | Goal_Name | Target_Amount | Target_Date | Status | Created_By | Goal_ID | Completed_Date | Notes`"
        )
        if message_id:
            edit_telegram_message(chat_id, message_id, error_msg)
        else:
            send_telegram(chat_id, error_msg)
    except Exception as e:
        logger.error(f"Failed to view goals: {e}", exc_info=True)
        send_telegram(chat_id, "⚠️ Error loading goals. Please try again.")

def handle_expense_message(msg):
    """Handle expense input (text or image)"""
    chat_id = msg['chat']['id']
    user_name = msg.get('from', {}).get('first_name', 
                        msg.get('from', {}).get('username', 'Unknown'))
    
    try:
        prompt_text = EXPENSE_SYSTEM_PROMPT.format(date=datetime.now().strftime("%Y-%m-%d"))
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
                model="meta-llama/llama-4-scout-17b-16e-instruct",
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
            
            # Handle callback queries (button clicks)
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
            user_name = msg.get('from', {}).get('first_name', 'Unknown')
            
            # Security check
            if user_id not in ALLOWED_USERS:
                logger.warning(f"Unauthorized access attempt from user {user_id}")
                self.send_response(200)
                self.end_headers()
                return
            
            # Route to appropriate handler
            if 'text' in msg:
                text = msg['text'].strip()
                
                # Goal commands
                if text.startswith('/goal '):
                    handle_add_goal(msg)
                
                elif text == '/goals':
                    handle_view_goals(msg)
                
                # Expense commands
                elif text in ['/start', '/help']:
                    handle_command(msg)
                
                elif text == '/summary':
                    handle_command(msg)
                
                elif text == '/undo':
                    handle_undo(chat_id, user_name)
                
                elif text == '/share':
                    handle_command(msg)
                
                # Regular text (expense)
                elif not text.startswith('/'):
                    handle_expense_message(msg)
            
            # Photo (expense receipt)
            elif 'photo' in msg:
                handle_expense_message(msg)
            
            self.send_response(200)
            self.end_headers()
            
        except Exception as e:
            logger.error(f"Webhook handler error: {e}", exc_info=True)
            self.send_response(500)
            self.end_headers()
