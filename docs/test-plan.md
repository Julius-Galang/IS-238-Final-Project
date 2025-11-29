# Test Plan – Telegram Email Summarizer

This plan explains **what we will test** for the Telegram Email Summarizer, and **when we consider a feature “done”**.

---

## 1. Purpose and Scope

Goal of testing: check that the system really behaves as described in the **updated Problem Specs**:

a. Incoming emails go through this pipeline:  
  Cloudflare (catch-all) → Gmail → Lambda #1 → S3 → Lambda #2 → OpenAI → Telegram → Lambda #3/DynamoDB
  
b. Users can:
  - Register one or more random email addresses under the company domain
  - Receive summaries of new emails on Telegram
  - Download the raw email from S3 using a pre-signed URL (valid for about 12 hours)
  - Deactivate any address from Telegram

c. The bot does **not** allow replying to emails.

d. The total delay between email arrival in Gmail and summary appearing in Telegram is **up to 2 minutes**, which is acceptable.

This test plan covers:

- Unit tests (small pieces of code)
- Integration tests (components working together)
- End-to-end (E2E) tests (full user flows)

---

## 2. Types of Tests

Three levels of tests.

### 2.1 Unit Tests

a. Test one small piece of code at a time.

b. No real network calls.

c. External services (Gmail, S3, DynamoDB, OpenAI, Telegram) are **mocked**.

  Examples:
  - HTML parser extracts text from simple and complex emails.
  - Function that builds S3 object keys.
  - Function that builds the OpenAI request body.
  - Function that builds Telegram messages and buttons.

### 2.2 Integration Tests

a. How multiple components work together will be tested.

b. **mocks** or **local emulation** will be used for AWS where possible (e.g., moto for S3 and DynamoDB).

  Examples:
  - Gmail → Lambda #1 → S3: an email is fetched and stored in S3 correctly.
  - S3 → Lambda #2 → OpenAI (mock) → Telegram (mock): a stored email triggers a summary.
  - Telegram → Lambda #3 → DynamoDB: `/register`, `/list`, `/deactivate` change the data correctly.

### 2.3 End-to-End (E2E) Tests

a. The **whole** flow will be tested as a real user would use it.

b. The real test environment will be used: Cloudflare catch-all, Gmail test inbox, AWS account, real Telegram bot, real DynamoDB and S3.

  Examples:
  - User registers, sends an email, and receives the summary within 2 minutes.
  - User downloads the email from the pre-signed S3 URL.
  - User deactivates an address and no longer recieves emails for that address.

---

## 3. Features and Behaviours to Test

The main features we must test:

1. **Catch-all ruoting and email reception**
2. **Registering system-generated email addresses**
3. **Handling multiple addresses per Telegram user**
4. **Fetching emails from Gmail and storing them in S3 (Lambda #1)**
5. **Parsing and summarizing emails and sending them to Telegram (Lambda #2)**
6. **Generating pre-signed S3 URLs for downloading raw emails (about 12-hour expiry)**
7. **Deactivating addresses from Telegram (Lambda #3)**
8. **Listing addresses associated with a Telegram user**
9. **Blocking reply features (no reply from Telegram)**
10. **Proper HTML parsing and ignoring attachments/inline images**
11. **Performance / latency (≤ 2 minutes)**
12. **Security basics (secrets handling, IAM, lifecycle policies)**

---

## 4. Definition of “Done”

A feature is **done** only if it passes tests in four dimensions:

### 4.1 Correctness

a. The right Telegram user receives the right summary for the right email.

b. S3 objects contain the expected raw email data (and are linked to the correct user/address).

c. DynamoDB mappings between Telegram user and email addresses are correct (`active` / `inactive`).

d. Multiple addresses work correctly for one Telegram user.

e. Deactivated addresses really stop generating new Telegram summaries.


### 4.2 Latency (Performance)

a. For normal test emails, the delay from **email arrival in Gmail** to **summary received in Telegram** is **≤ 2 minutes** in almost all runs (for example, 95% of test runs).

b. Any major delays or timeouts are logged clearly (CloudWatch logs).

### 4.3 Security

a. No secrets (Gmail password or app password, Telegram bot token, OpenAI API key) are hard-coded in source code or committed to GitHub.

b. Lambda execution roles follow the **least-privilege** principle:

  - Lambda #1 can only read Gmail secrets and write to the email S3 bucket.
  - Lambda #2 can only read from the email S3 bucket, call OpenAI, and call Telegram.
  - Lambda #3 can only read and write to the DynamoDB table and call Telegram.
    
c. S3 buckets that store emails have:

  - Blocked public access.
  - A **lifecycle policy** to delete emails after a set number of days (MVP acceptable value).
    
d. Pre-signed S3 URLs used for downloading emails:

  - Have an expiry of around **12 hours** (as allowed by AWS tokens, according to updated specs).
  - Are only generated for the relevant user and object.

### 4.4 User Experience (UX)

a. Telegram bot offers clear commands and messages:

  - `/register`, `/list`, `/deactivate`

b. When an email is summarized, the Telegram message clearly shows:

  - Summary text.
  - A **Download Email** button (pre-signed S3 URL).
  - A **Deactivate Address** button.

c. Download and Deactivate are separated and clearly labelled, so users do not deactivate by mistake.

d. There is **no** option to reply to the original email from inside the Telegram bot.


---

## 5. Mapping Specs to Test Types

Below is a short mapping of each key requirement to unit, integration, and end-to-end tests.

### 5.1 Catch-all routing and email reception

a. **Integration tests**

  - Simulate an email to a random address under the domain: `random123@companydomain.com`.
  - Confirm it appears in the Gmail test inbox (or via a mock/label check).

b. **End-to-end tests**

  - Send an email to a randomly generated address (one of the addresses registered by the bot).
  - Confirm it appears as a summary in Telegram.

### 5.2 Registering email addresses from Telegram

a. **Unit tests (Lambda #3)**

  - Given a Telegram user ID, generate a unique email under the company domain (correct format, no duplicates).

b. **Integration tests**

  - `/register` → Lambda #3 → DynamoDB: verify new item with `active = true`.

c. **E2E tests**

  - User sends `/register` in Telegram and receives a new address plus basic usage instructions.

### 5.3 Multiple addresses per Telegram user

a. **Unit**

  - Storing more than one address per `telegramUserId` in DynamoDB without overwriting old ones.

b. **Integration**

  - `/register` called twice → DynamoDB shows two active addresses for the same user.

c. **E2E**

  - Send separate emails to both addresses and verify both summaries appear in the same Telegram chat.

### 5.4 Gmail → S3 (Lambda #1)

a. **Unit**

  - Function that converts a Gmail message into an S3 object key (e.g. `userId/raw/<messageId>.eml`).
  - Function that reads Gmail credentials from environment or Secrets, not hard-coded.

b. **Integration**

  - Put a fake email into the Gmail test inbox (or mocked IMAP).
  - Run Lambda #1 and confirm an S3 object is created in the correct bucket and key.

### 5.5 S3 → summary → Telegram (Lambda #2)

a. **Unit**

  - HTML parsing: given sample HTML emails, the parser returns plain text. Attachments and inline images are ignored.
  - Summarization request: given subject + body, build the expected OpenAI request JSON.
  - Telegram message builder: given summary text and pre-signed URL, build a message with “Download Email” and “Deactivate Address”.

b. **Integration**

  - Put a test email object in S3, trigger Lambda #2, mock OpenAI and Telegram:
    - OpenAI mock receives request.
    - Telegram mock receives correct message.

c. **E2E**

  - Send a real email to a test address and confirm the summary appears in Telegram with the correct content and buttons.

### 5.6 Download email via pre-signed S3 URL (~12-hour expiry)

a. **Unit**

  - Function that generates pre-signed URLs with an expiry (test using a smaller value, e.g. 5 minutes, in test config).

b. **Integration**

  - Lambda #2 attaches a valid pre-signed URL to the Telegram message.
  - Following the URL downloads the original stord email object.

c. **E2E**

  - In test environment, verify:
    - Immediately after receiving the Telegram message, the Download button works.
    - After the expiry window (shortened for testing), the URL no longer works.

### 5.7 Deactivating addresses from Telegram

a. **Unit**

  - Given `telegramUserId` + email address, Lambda #3 marks `active = false` in DynamoDB.

b. **Integration**

  - Clicking “Deactivate Address” button or sending `/deactivate <address>` updates DynamoDB.

c. **E2E**

  - After deactivation, sending new emails to that address does *not* create new Telegram summaries.

### 5.8 Listing addresses

a. **Unit**

  - Listing function returns all addresses for a Telegram user, inclding active status.

b. **Integration**

  - `/list` command returns correct addresses from DynamoDB.

c. **E2E**

  - User uses `/list` and sees all their addresses and possibly which ones are active.

### 5.9 No reply feature

a. **Unit / review**

  - Codebase has no SMTP/SES or email-sending functions for replying.

b. **Integration**

  - No handlers or endpionts send email to the original sender.

c. **E2E**

  - Bot menus and commands do not offer any “Reply” or “Respond” functionality.

### 5.10 HTML parsing and ignoring attachments

a. **Unit**

  - Parser ignores attachments and inline images even if they appear in MIME parts.

b. **Integration**

  - An email with attachments still produces a valid summary and does not crash Lambda #2.

c. **E2E**

  - User sends an email with attachments and confirms that:
    - The summary is based on the email body.
    - Attachments are not shown or used by the bot.

### 5.11 Performance / latency

a. **Integration / E2E**

  - For several test emials with different sizes and content:
    - Record the time the email reaches Gmail.
    - Record the time the summary arrives in Telegram.
    - Confirm that delay ≤ 2 minutes in most runs.

### 5.12 Security and lifecycle

a. **Integration**

  - Check AWS IAM policies to confirm least-privilege access.
  - Check S3 lifecycle policy exists for the email bucket.
  - 
b. **E2E (longer-term or test bucket)**

  - In a test bucket with very short lifecycle, verify that old objects are auto-deleted after the configured period.

---

## 6. Relationship to Other Documents

a. `docs/integration-checklist.md`  

  Describes the **wiring and configuration** of each service (Cloudflare catch-all, Gmail, S3 buckets, IAM roles, DynamoDB table, OpenAI endpoint, Telegram bot).

b. `tests/README.md`  

  Describes **how to run tests** (install dependencies, run `pytest`, structure of test files).

c. Root `README.md`  

  Provides the **overall project overview** and links to both this test plan and the testing guide.

## References


GitHub. (2025). *Biulding and testing Python* [Documentation]. GitHub Docs. https://docs.github.com/actions/guides/building-and-testing-python

Amazon Web Services. (2025). *Download and upload objects using presigned URLs* [Documentation]. AWS Amazon Simple Storage Service User Guide. https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html

Amazon Web Services. (2025). *Sharing objects with presigned URLs* [Documentation]. AWS Amazon Simple Storage Service User Guide. https://docs.aws.amazon.com/AmazonS3/latest/userguide/ShareObjectPreSignedURL.html

Amazon Web Services. (2025). *Managing the Lifecycle of objects* [Documentation]. AWS Amazon Simple Storage Service User Guide. https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lifecycle-mgmt.html

Amazon Web Services. (2025). *Security best practices in IAM* [Documentation]. AWS Identity and Access Management User Guide. https://docs.aws.amazon.com/IAM/latest/UserGuide/best-practices.html

Amazon Web Services. (2025). *Prepare for least-privilege permissions* [Documentation]. AWS Identity and Access Management User Guide. https://docs.aws.amazon.com/IAM/latest/UserGuide/getting-started-reduce-permissions.html

Amazon Web Services. (2020). *Techniques for writing least-privilege IAM policies* [Blog post]. AWS Security Blog. https://aws.amazon.com/blogs/security/techniques-for-writing-least-privilege-iam-policies/

Amazon Web Services. (2023(. *Why does the presigned URL for my Amazon S3 bucket expire before the expiration time that I specified?* [Knowledge center article]. AWS re:Post. https://repost.aws/knowledge-center/presigned-url-s3-bucket-expiration

Cloudflare, Inc. (2025). *Cloudflare Email Routing* [Documentation]. Cloudflare Developers. https://developers.cloudflare.com/email-routing/

Cloudflare, Inc. (2025). *Configure rules and addresses* [Documentation]. Cloudflare Developers. https://developers.cloudflare.com/email-routing/setup/email-routing-addresses/

pytest-dev. (2015). *pytest: helps you write better programs* [Documentation]. pytest. https://docs.pytest.org/

pytest-dev. (2025). *pytest* [Repository]. GitHub. https://github.com/pytest-dev/pytest

Schlusser, M. et al. (2015). *Moto: Mock AWS services* [Documentation]. Moto. https://docs.getmoto.org/

Moto maintainers. (n.d.). *moto* [Repository]. GitHub. https://github.com/getmoto/moto

Amazon Web Services. (2023). *Unit testing AWS Lambda with Python and mock AWS services* [Blog post]. AWS DevOps Blog. https://aws.amazon.com/blogs/devops/unit-testing-aws-lambda-with-python-and-mock-aws-services/

Stack Exchange, Inc. (2022). *AWS presigned URL valid for more than 7 days* [Question and answers]. Stack Overflow. https://stackoverflow.com/questions/73493060/aws-presigned-url-valid-for-more-than-7-days

Telegram Messenger LLP. (2025). *Telegram Bot API* [Documentation]. Telegram Core. https://core.telegram.org/bots/api

Telegram Messenger LLP. (2025). *Telegram APIs for developers* [Overview]. Telegram Core. https://core.telegram.org/

Python Telegram Bot contributors. (2025). *python-telegram-bot documentation* [Documentation]. https://docs.python-telegram-bot.org/
