# Testing Guide

This project uses **Python** and **pytest** for testing.

The goal is to check Telegram Email Summarizer MVP does the
Problem Specs:

- Cloudflare → Gmail → Lambda #1 → S3 → Lambda #2 → OpenAI → Telegram → Lambda #3/DynamoDB
- Users can:
  - Register one or more email addresses
  - Receive summaries on Telegram
  - Download emails from S3 via pre-signed URL
  - Deactivate addresses
- The bot does **not** allow replying to emails.

---

## 1. Types of tests

We use three levels of tests:

- **Unit tests**
  - Test one small piece of code at a time.
  - No real network calls.
  - Example: “HTML parser extracts text correctly from a simple email.”

- **Integration tests**
  - Test components working together.
  - May use mocked AWS services (via [moto](https://github.com/getmoto/moto)).
  - Example: “Lambda #1 writes to S3 and Lambda #2 can read that S3 object.”

- **End-to-end (E2E) tests**
  - Test the whole flow, as a real user would use it.
  - Example: “User runs `/register`, sends an email, receives a summary in Telegram within 2 minutes.”

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
