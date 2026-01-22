# 🤖 Family Finance Bot (AI-Powered) v2.0

A serverless, event-driven Telegram bot that utilizes Large Language Models (LLMs) to automate personal finance tracking **and family goal setting**. It parses unstructured natural language and receipt images into structured data, syncing in real-time with Google Sheets.

## 📋 Table of Contents

* [Overview](https://www.google.com/search?q=%23-overview)
* [Key Features](https://www.google.com/search?q=%23-key-features)
* [Expense Tracking Engine](https://www.google.com/search?q=%23-expense-tracking-engine)
* [Goal Management Engine](https://www.google.com/search?q=%23-goal-management-engine)
* [Dashboards & Analytics](https://www.google.com/search?q=%23-dashboards--analytics)
* [Tech Stack](https://www.google.com/search?q=%23-tech-stack)
* [Google Sheets Schema](https://www.google.com/search?q=%23-google-sheets-schema)
* [Setup & Deployment](https://www.google.com/search?q=%23-setup--deployment)
* [Command Reference](https://www.google.com/search?q=%23-command-reference)

## 🧐 Overview

The Family Finance Bot solves the friction of manual family administration. Instead of navigating complex UI/UX in finance apps, users simply text their expenses ("15 Lunch") or their goals ("Trip to Italy 2000 by June"). The system uses Computer Vision and NLP to extract structured data and manage it via persistent, interactive dashboards directly in Telegram.

## ✨ Key Features

* **Multimodal Inputs:** Supports text messages and receipt images (OCR/Vision).
* **Zero-Shot Classification:** AI intelligently categorizes expenses (e.g., "Netflix" → "Subscription") without training data.
* **Context Aware:** Automatically tags the spender based on Telegram metadata and handles date logic (e.g., "by next summer").
* **Race-Condition Protection:** Safely handles concurrent edits and deletions for shared family sheets.
* **Smart Currency Logic:** Detects legacy currencies (e.g., "DM" → "Drugstore", not "Deutsche Mark") and normalizes to EUR.

## 💰 Expense Tracking Engine

The bot uses a specialized system prompt to parse expenses. You can simply type naturally or upload a photo.

### 1. Smart Parsing

Input: `45 Rewe`
Output: `{"amount": 45.0, "category": "Groceries", "merchant": "Rewe"}`

Input: `12,50 pizza`
Output: `{"amount": 12.5, "category": "Food Takeout", "note": "pizza"}`

### 2. Receipt Scanning (Computer Vision)

Simply upload a photo of a receipt. The bot will:

1. Scan the total amount.
2. Identify the merchant name.
3. Categorize the purchase automatically.

### 3. Automatic Categories

The AI strictly maps expenses to these buckets for consistent reporting:

* 🛒 **Groceries** (Rewe, Aldi, Lidl, etc.)
* 🍕 **Food Takeout** (Restaurants, Delivery)
* ✈️ **Travel** (Flights, Uber, DB, Hotels)
* 📺 **Subscription** (Netflix, Spotify, Gym)
* 💰 **Investment** (Stocks, ETFs)
* 🏠 **Household** (Furniture, Drugstore/dm)
* 🚌 **Transport** (Fuel, Parking, Public Transit)
* 🤷 **Other**

## 🎯 Goal Management Engine

*New in v2.0*

The bot now tracks financial goals and to-do lists.

### 1. Natural Language Creation

Create goals without strict syntax:

* **Financial:** `/goal Emergency fund 10000`
* **Vacation:** `/goal Trip to Japan 5000 by December 2026`
* **Skill:** `/goal Learn Spanish by summer`
* **Task:** `/goal Renew car insurance next month`

### 2. Interactive Editing

Clicking any goal in the dashboard opens an **Edit Menu** where you can:

* ✏️ **Update Details:** `/editgoal [ID] amount 5000`
* 📝 **Add Notes:** `/editgoal [ID] note Booked flights!`
* 🔄 **Change Status:** Pending → In Progress → Done
* 🗑️ **Delete:** Remove goals safely.

## 📊 Dashboards & Analytics

The bot features interactive drill-down dashboards powered by `pandas`.

### Expense Dashboard (`/summary`)

* **Overview:** Total spent, transaction count, average.
* **Categorization:** Breakdown by category percentages.
* **User Split:** See who spent what (useful for family splitting).
* **Drill-Down:** Click on a User to see *their* specific category breakdown.
* **Merchants:** Top 10 places you shop.

### Goal Dashboard (`/goals`)

* Separates **Financial Goals** (with amounts) from **Tasks** (to-dos).
* Shows progress deadlines.
* Visual indicators for deadlines (e.g., "Due: Jun 30, 2026").

## 🛠 Tech Stack

| Component | Technology | Rationale |
| --- | --- | --- |
| **Runtime** | Python 3.9+ | Native support for AI libraries and robust HTTP handling. |
| **Hosting** | Vercel Serverless | Event-driven architecture with zero idle costs. |
| **AI Inference** | Groq Cloud API | Ultra-low latency LPU inference using **Llama 4 Vision**. |
| **Database** | Google Sheets | Accessible UI for non-technical stakeholders; easy export. |
| **Interface** | Telegram Bot API | High availability, mobile-first interface. |

## 📊 Google Sheets Schema

**CRITICAL:** You must create two tabs in your Google Sheet with the exact headers below.

### Tab 1: `Expenses`

| Date | Amount | Category | Merchant | Note | User |
| --- | --- | --- | --- | --- | --- |
| *YYYY-MM-DD* | *Float* | *String* | *String* | *String* | *String* |

### Tab 2: `Goals`

| Created_Date | Type | Goal_Name | Target_Amount | Target_Date | Status | Created_By | Goal_ID | Completed_Date | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| *YYYY-MM-DD* | *String* | *String* | *Float* | *YYYY-MM-DD* | *Pending* | *String* | *UUID* | *YYYY-MM-DD* | *String* |

## 🚀 Setup & Deployment

1. **Environment Variables:**
```bash
TELEGRAM_TOKEN=...
GROQ_API_KEY=...
GOOGLE_SHEET_ID=...
ALLOWED_USERS=[12345678]
GOOGLE_JSON_KEY={"type": "service_account", ...}

```


2. **BotFather Configuration:**
Send `/setcommands` to `@BotFather`:
```text
start - Show help and main menu
goal - Add a new goal
goals - View and manage all goals
summary - View expense dashboard
undo - Delete your last expense
undogoal - Delete your last goal
editgoal - Edit goal details
share - Share bot with family

```



## ⌨️ Command Reference

| Context | Command | Description |
| --- | --- | --- |
| **Expenses** | `[Text]` | Log expense (e.g., `15 Lunch`) |
|  | `[Photo]` | Log receipt via OCR |
|  | `/summary` | View Analytics Dashboard |
|  | `/undo` | Delete *your* last expense |
| **Goals** | `/goal [text]` | Add goal (e.g., `/goal Save 5k`) |
|  | `/goals` | View/Manage Goals Dashboard |
|  | `/editgoal` | Edit goal fields (amount, date, note) |
|  | `/undogoal` | Delete *your* last goal |
| **General** | `/share` | Get invite link for family |
|  | `/start` | Open Main Menu |
