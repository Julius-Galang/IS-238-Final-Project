# Testing Guide

This project uses **Python** and **pytest** for testing.

The goal is to check Telegram Email Summarizer MVP does the
Problem Specs:

• Cloudflare (catch-all) → Gmail → Lambda #1 → S3 → Lambda #2 → OpenAI → Telegram → Lambda #3 → DynamoDB  
• Users can:  
  • Register one or more email addresses  
  • Receive summaries of new emails on Telegram  
  • Download raw emails from S3 via pre-signed URL (valid for about 12 hours)
  • Deactivate any address from Telegram 
• The bot does **not** allow replying to emails.
• The delay from email arrival in Gmail to summary in Telegram is **up to 2 minutes**

---

## 1. Types of tests

We use three levels of tests:

### 1.1 **Unit tests**

   • Test one small piece of code at a time.
   • They do **not** call real external services (no real Gmail, S3, Dynamo DB, OpenAI, Telegram).
   • They use **mocks** and **moto** (fake AWS services) to simulate behavior.
   • Example: “HTML parser extracts text correctly from a simple or complex email.”

### 1.2 **Integration tests**

   • Test components working together.  
   • Can combine multiple parts of the system, e.g. Lambda function plus S3 or DynamoDB
   • They still mock **external HTTP services** like OpenAI and Telegram.
   • May use mocked AWS services (via [moto](https://github.com/getmoto/moto)).  
   • Example: “Lambda #1 writes to S3 and Lambda #2 can read that S3 object.”

### 1.3. **End-to-end (E2E) tests**

   • Test the whole flow, as a real user would use it (real or demo environment).
   • They use the **real deployed system** (Cloudflare catch-all, project Gmail inbox, AWS Lambdas, S3, DynamoDB, Real Telegram bot, Real OpenAI API)
   • Slower and more fragile, but they show what a real user experiences.
   • Example: “User runs `/register`, sends an email, receives a summary in Telegram within 2 minutes.”

---

## 2. Folder structure

Tests are stored under the `tests/` folder:

```text
tests/
  unit/
    test_lambda1_*.py
    test_lambda2_*.py
    test_lambda3_*.py
  integration/
    test_gmail_to_s3.py
    test_s3_to_telegram.py
    test_telegram_commands.py
