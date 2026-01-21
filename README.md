# 🤖 Family Finance Bot (AI-Powered)

![Build Status](https://img.shields.io/badge/build-passing-brightgreen)
![Platform](https://img.shields.io/badge/platform-Vercel-black)
![AI Engine](https://img.shields.io/badge/AI-Groq%20LPU-orange)
![Database](https://img.shields.io/badge/database-Google%20Sheets-green)
![License](https://img.shields.io/badge/license-MIT-blue)

A serverless, event-driven Telegram bot that utilizes Large Language Models (LLMs) to automate personal finance tracking. It parses unstructured natural language and receipt images into structured data, syncing in real-time with Google Sheets.

## 📋 Table of Contents
- [Overview](#overview)
- [Key Features](#key-features)
- [Tech Stack](#tech-stack)
- [Architecture](#architecture)
- [Setup & Deployment](#setup--deployment)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)

## 🧐 Overview
The Family Finance Bot solves the friction of manual expense tracking. Instead of navigating complex UI/UX in finance apps, users simply text their expenses or send photos of receipts. The system uses Computer Vision and NLP to extract:
- **Amount** (Currency normalized to EUR)
- **Category** (Auto-classified)
- **Merchant** (Entity extraction)
- **User Identity** (Who spent the money)

## ✨ Key Features
* ** multimodal Input:** Supports both text (`"15 Lunch"`) and images (OCR/Vision for receipts).
* **Zero-Shot Classification:** AI intelligently categorizes expenses (e.g., "Netflix" → "Subscription") without training data.
* **Identity Awareness:** Automatically tags the spender based on Telegram metadata.
* **Smart Currency Logic:** Detects legacy currencies (e.g., "DM") or foreign currencies and normalizes them.
* **CRUD Operations:** Includes an `/undo` command to safely revert the last transaction.

## 🛠 Tech Stack
| Component | Technology | Rationale |
| :--- | :--- | :--- |
| **Runtime** | Python 3.9+ | Native support for AI libraries and robust HTTP handling. |
| **Hosting** | Vercel Serverless | Event-driven architecture with zero idle costs. |
| **AI Inference** | Groq Cloud API | Ultra-low latency LPU inference using **Llama 4 Vision**. |
| **Database** | Google Sheets | Accessible UI for non-technical stakeholders; easy export/analysis. |
| **Interface** | Telegram Bot API | High availability, mobile-first interface. |

## 🏗 Architecture
The system follows a **Serverless Webhook Pattern**:
1.  **Event:** User sends message → Telegram API.
2.  **Trigger:** Telegram pushes payload to Vercel Endpoint (`/api/webhook`).
3.  **Auth:** Middleware validates `user_id` against the Allowlist.
4.  **Processing:**
    * *Text:* Passed to LLM with System Prompt.
    * *Image:* Base64 encoded and passed to Vision Model.
5.  **Persistence:** Structured JSON written to Google Sheets via `gspread`.
6.  **Response:** Async callback sent to Telegram UI.

## 🚀 Setup & Deployment

### 1. Prerequisites
* **Telegram:** Create a bot via [@BotFather](https://t.me/botfather) and get the `TOKEN`.
* **Groq:** Get an API Key from [Groq Console](https://console.groq.com).
* **Google Cloud:** Create a Service Account, enable Sheets API, and download the JSON key.

### 2. Environment Variables
Configure the following in your Vercel Project Settings:

```bash
TELEGRAM_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
GROQ_API_KEY=gsk_...
GOOGLE_SHEET_ID=1A2B3C... (Found in URL of your Sheet)
ALLOWED_USERS=[12345678, 87654321]
GOOGLE_JSON_KEY={"type": "service_account", ...} # Full JSON content
