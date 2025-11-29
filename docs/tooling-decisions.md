# Tooling Decisions – Telegram Email Summarizer

## 1. Language and Runtime

### 1.1 Python for Lambdas and Tests

We use **Python** for:

- AWS Lambda functions
- Automated tests (unit, integration, and some E2E helpers)

Reasons:

- Python - supported in **AWS Lambda**.
- Many examples and libraries exist for:
  - Working with Gmail, S3, DynamoDB, and HTTP APIs
  - Writing tests with `pytest`
- Team already using Python and can share code and helpers.

Runtime target (for reference):

- AWS Lambda runtime: for example `python3.11`.

---

## 2. Testing Framework

### 2.1 pytest

**pytest** - main test framework.

Reasons:

- Simple syntax.
- Automatic test discovery in the `tests/` folder.
- Can be used for unit tests, integration tests, and some E2E checks.

Layout:

- `tests/unit/` – tests for single functions or single Lambdas.
- `tests/integration/` – tests that combine multiple components (for example: Lambda + S3 + DynamoDB using mocks).

### 2.2 Test Dependencies

- `pytest` – core test runner.
- (Optional) `pytest-cov` – coverage, if we need it later.

These are listed in:

- `requirements.txt` – runtime dependencies (for Lambdas).
- `requirements-dev.txt` – development and testing dependencies.

---

## 3. AWS Mocks and Local Testing

### 3.1 Moto (Mock AWS Services)

**moto** to simulate AWS services in tests.

- It lets us “fake” S3 and DynamoDB so tests can run locally and in CI without touching real AWS.
- This is important for:
  - Fast, cheap tests
  - Safe testing (no risk of modifying real data)

Typical use:

- Unit tests:
  - Mock S3 when testing Lambda #1 and Lambda #2.
  - Mock DynamoDB when testing Lambda #3.
- Integration tests:
  - Use moto to combine Lambda logic with fake S3 and fake DynamoDB.

---

## 4. Continuous Integration (CI)

### 4.1 GitHub Actions

We use **GitHub Actions** to run tests automatically.

Workflow file:

- `.github/workflows/tests.yml`

GitHub Actiosn:

- Runs on each push and/or pull request (depending on the triggers we set).
- Steps:
  - Check out the repository.
  - Set up Python.
  - Install `requirements-dev.txt`.
  - Run `pytest`.

Reasons:

- CI makes sure the tests are not only run locally.
- The whole group can see if the current code passes tests.
- Helps catch broken changes early.

---

## 5. Project Structure


- `src/`
  - `lambda1/` – Gmail → S3
  - `lambda2/` – S3 → OpenAI → Telegram
  - `lambda3/` – Telegram → DynamoDB

- `tests/`
  - `unit/` – small, focused tests for each Lambda or function.
  - `integration/` – tests that combine multiple pieces.
  - `README.md` – Testing Guide (how to run tests and how the structure works).

- `docs/`
  - `test-plan.md` – what we test and when a feature is “done”.
  - `integration-checklist.md` – how the services are wired together.
  - `tooling-decisions.md` – this file (why we chose these tools).

Reasons for this layout:

- Separation between **production code** (`src/`), **tests** (`tests/`), and **documentation** (`docs/`).


---

## 6. External Services

### 6.1 Telegram Bot API

- Telegram as an HTTP API.
- The actual library (for example `python-telegram-bot` or plain `requests`) can be chosen by Lambda developer.
- In tests, Telegram as an **external HTTP service** and mock it in tests.

### 6.2 OpenAI API

- For summarizing email content.
- Only called from **Lambda #2**.
- In tests, **mock** OpenAI calls:
  - Unit tests: fake a simple summary response.
  - Integration tests: confirm that requests are built correctly and that failures are handled gracefully.

---

## 7. Logging and Observability (Basic)

- **CloudWatch Logs** for each Lambda function.

Decisions:

- Each Lambda should log:
  - When it starts and finishes.
  - Key events (email fetched, email summarized, Telegram message sent).
  - Errors and exceptions with enough context (but without secrets).

Logging helps:

- Debug failed tests.
- Trace end-to-end flows when something goes wrong.

---

## 8. Summary

In short:

- **Python + pytest** → main language and test framework.
- **moto** → mock AWS services (S3, DynamoDB) for safe tests.
- **GitHub Actions** → automatic test runs on each push.
- **Structured repo** → `src/`, `tests/`, `docs/` with clear roles.
- **External APIs (Telegram, OpenAI)** → treated as HTTP services and mocked in tests.

