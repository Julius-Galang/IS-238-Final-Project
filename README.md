# IS - 238 Final Project

Project Overview

This repository contains the Telegram Email Summarizer MVP that was developed as a IS238 project.
A Telegram bot is used so that users can receive summaries of incoming emails directly in a chat, together with a link to the raw .eml file and controls to deactivate their email address.

Architecture

The system is designed as a pipeline on AWS:

Emails are routed through Cloudflare Email Routing to a shared Gmail inbox.

A scheduled Lambda #1 (BotGmailIngest) is used to log into Gmail via IMAP and download new messages.

Each email is stored as a .eml file in an S3 bucket with lifecycle policies.

An S3 event is used to trigger Lambda 2 (BotGmailProcessor), where:

The .eml file is parsed.

A summary is generated via an OpenAI-compatible API (with a safe fallback to truncated body text).

Metadata and state are written to DynamoDB.

A DynamoDB stream is used to trigger Lambda #3 (BotTelegramWebhook), which:

Sends the summary and controls to the user via Telegram Bot API.

Provides buttons to disable the email address and a pre-signed S3 URL to download the raw email.

All secrets (Telegram bot token, Gmail credentials, OpenAI key, Cloudflare data) are stored in AWS Secrets Manager and are loaded at runtime.

Key Features

Dynamic aliases
A registration flow is used so that a random email address under the project domain is assigned to a Telegram user. This address is stored with a unique chat_id in DynamoDB.

Automatic summarization
For each new email, a short summary is requested from an OpenAI-compatible endpoint. If the API is unavailable, a fallback summary (truncated body) is generated so the pipeline remains robust.

Telegram notifications
For each processed email, a Telegram message is sent that includes:

Subject and summary

A pre-signed S3 link to download the raw .eml

Inline buttons to disable the address and to access the download link

Address deactivation
When the user taps “Disable this address”, a callback query is handled and the address is marked as DISABLED in DynamoDB, so further summaries are no longer sent.

Technology Stack

AWS: Lambda, S3, DynamoDB, Secrets Manager, EventBridge

Email: Cloudflare Email Routing, Gmail (IMAP access)

Messaging: Telegram Bot API

AI Summarization: OpenAI-compatible chat completions endpoint

Language: Python 3.11

How the Flow Works (Step by Step)

A user registers with the Telegram bot and is assigned a unique email alias.

An external sender sends an email to that alias.

Cloudflare forwards the email to the shared Gmail inbox.

On schedule, Lambda 1 fetches new emails and writes them as .eml to S3.

The S3 ObjectCreated event triggers Lambda 2.

Lambda 2 parses the email, calls the summarization API, and writes a record to DynamoDB (including summary, subject, S3 key, and state).

The DynamoDB stream triggers Lambda 3, and a Telegram message is sent to the correct chat with summary, buttons, and download link.

If the user disables the address, subsequent messages for that alias are not sent.

