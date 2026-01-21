# 🤖 AI-Powered Family Finance Bot

![Python](https://img.shields.io/badge/Python-3.9-blue?style=for-the-badge&logo=python&logoColor=white)
![Netlify](https://img.shields.io/badge/Netlify-Serverless-00C7B7?style=for-the-badge&logo=netlify&logoColor=white)
![Google Gemini](https://img.shields.io/badge/AI-Google%20Gemini%20Flash-8E75B2?style=for-the-badge)
![Status](https://img.shields.io/badge/Status-Operational-success?style=for-the-badge)

A serverless Telegram bot that uses multimodal AI (Google Gemini) to parse natural language expenses and receipt photos, automatically logging them into a shared Google Sheet. Designed for zero-cost operation and maximum privacy.

## ✨ Key Features

* **🗣️ Natural Language Processing:** Type `45 Rewe groceries` and the AI extracts amount (€45), merchant (Rewe), and category (Groceries).
* **📸 Receipt Scanning:** Snap a photo of a paper receipt. The bot reads the total and merchant using Google Gemini Vision.
* **🧠 Intelligent Categorization:** Auto-categorizes merchants (e.g., "Aldi" → Groceries, "Shell" → Transport).
* **⚡ Serverless Architecture:** Hosted on Netlify Functions (Free Tier) with no 24/7 server costs.
* **🛡️ Secure & Private:** Strict user whitelisting and local environment variable management.
* **🔙 Undo Function:** Mistake? Type `/undo` to delete the last entry.

## 🛠️ Tech Stack

* **Frontend:** Telegram Bot API (Webhooks)
* **Backend:** Python 3.9 (Flask-style handler) via Netlify Functions
* **AI Engine:** Google Gemini 1.5 Flash (via `google-generativeai`)
* **Database:** Google Sheets (via `gspread`)
* **Image Processing:** Pillow (PIL) for compression

## 🚀 How It Works

1.  **User** sends a message or photo to the Telegram Bot.
2.  **Telegram** forwards the data to the Netlify Webhook URL.
3.  **Netlify Function** wakes up, verifies the User ID against a whitelist.
4.  **Google Gemini AI** analyzes the text/image and extracts a structured JSON object.
5.  **Google Sheets API** appends the data to the secure spreadsheet.
6.  **Bot** replies with a confirmation: "✅ Saved €45 to Groceries".

## 📦 Setup & Deployment

### Prerequisites
* Telegram Bot Token (via @BotFather)
* Google Cloud Service Account (`credentials.json`)
* Google AI Studio API Key
* Netlify Account

### Installation
1.  Clone the repository:
    ```bash
    git clone [https://github.com/yourusername/family-bot.git](https://github.com/yourusername/family-bot.git)
    ```
2.  Install dependencies locally for testing:
    ```bash
    pip install -r requirements.txt
    ```

### Deployment (Netlify)
1.  Import repository to Netlify.
2.  Set the following Environment Variables:
    * `TELEGRAM_TOKEN`
    * `GOOGLE_API_KEY`
    * `GOOGLE_SHEET_ID`
    * `ALLOWED_USERS` (List of Telegram User IDs)
    * `GOOGLE_JSON_KEY` (Content of service account JSON)
3.  Deploy and set the Telegram Webhook URL.

## 📸 Usage Examples

| Action | Command / Input | Result |
| :--- | :--- | :--- |
| **Log Expense** | `12.50 Pizza` | Logs €12.50 to "Food Takeout" |
| **Log Transport** | `Uber 25` | Logs €25.00 to "Transport" |
| **Scan Receipt** | *[Photo of Receipt]* | OCR extracts Total & Merchant |
| **Delete Last** | `/undo` | Removes last row from Sheet |
| **Help** | `/start` | Shows instructions |

## 🔒 Security
* **No Database:** Data lives only in your Google Sheet.
* **Whitelist:** Unrecognized Telegram users are silently ignored.
* **Secrets:** API keys are injected at runtime via Netlify Environment Variables.

---
*Built as a personal finance tool.*