# Proxy Voting APIs

A set of serverless APIs for:

* Querying and caching NBIM shareholder meeting data
* Extracting text from PDFs
* Orchestrating jobs to scrape and analyze SEC voting disclosures
* With more to come...

All APIs are designed for secure, scalable deployment on AWS Lambda, using AWS Secrets Manager for credentials and S3/Postgres for storage.

## **Table of Contents**

* [API Overview](#api-overview)
* [Architecture & Security](#architecture--security)
* [API Endpoints](#api-endpoints)
  * [1. NBIM Meetings API](#1-nbim-meetings-api)
  * [2. PDF Text Extraction API](#2-pdf-text-extraction-api)
  * [3. Proxy Voting Results API](#3-proxy-voting-results-api)
* [Environment Variables & Secrets](#environment-variables--secrets)
* [Deployment Notes](#deployment-notes)
* [License](#license)

---

## API Overview

This repo contains three loosely-coupled APIs:

1. [NBIM Meetings API](#1-nbim-meetings-api)
   Query, fetch, and cache NBIM shareholder meeting data to S3.

2. [PDF Text Extraction API](#2-pdf-text-extraction-api)
   Extracts text from user-provided PDF URLs (via FastAPI), with PDF validation.

3. [Proxy Voting Results API](#3-proxy-voting-results-api)
   Async jobs to fetch, parse, and store SEC proxy voting results into Postgres.

---

## Architecture & Security

* **Secrets** (API keys, database credentials) are never hard-coded; they are referenced via environment variables as [ARNs](https://docs.aws.amazon.com/secretsmanager/latest/userguide/reference_arn.html) to AWS Secrets Manager. Your Lambda functions resolve these at runtime.
* **S3** is used for staging meeting data.
* **Postgres** (managed by RDS) is used for job/result persistence.
* **PDF parsing** is done in-memory via [`pdfplumber`](https://github.com/jsvine/pdfplumber).
* **OpenAI integration** leverages a securely fetched API key for LLM-based parsing of complex filings.

---

## API Endpoints

### 1. NBIM Meetings API
(GET `list_meetings`, POST `fetch_and_upload`)

**Purpose:**

* Query latest NBIM shareholder meetings by ISIN/ticker.
* Cache meeting payloads to S3 and return presigned download links.

**Endpoints (as AWS Lambda handlers):**

* `GET /nbim/meetings?isin=...&ticker=...`
  Returns the latest meetings for a given security.
* `POST /nbim/meetings/fetch`
  Body: `{ "isin": "...", "ticker": "...", "meetingIds": [ ... ] }`
  Fetches and caches meetings, returns presigned S3 links.

**Security:**

* Uses `NBIM_API_KEY` pulled from Secrets Manager.
* S3 bucket/prefix is configurable.

---

### 2. PDF Text Extraction API
(POST `/extract-text`)

**Purpose:**

* Accepts a JSON payload with a `file_url` (PDF).
* Validates and downloads the PDF, extracts all text, and returns it as plain text.

**Endpoint (FastAPI):**

* `POST /extract-text`

  ```json
  { "file_url": "https://example.com/file.pdf" }
  ```

**Security:**

* No secrets required (public endpoint or can be protected with API Gateway, Cognito, etc.).
* Validates PDF magic bytes to prevent abuse.

---
Here’s an improved README section for your **Proxy Voting Results API**, making the **job polling pattern, async flow, and SEC filing parsing** clear and professional.
It uses consistent, user-friendly language and explains exactly what happens behind the scenes.

---

### 3. Proxy Voting Results API
(POST `/jobs`, GET `/jobs/{job_id}`)

**Purpose:**
Manage end-to-end, **asynchronous scraping and parsing** of SEC proxy voting results (Item 5.07 in 8-K filings) using a scalable job-based architecture.

**How it works:**

1. **Job Creation:**
   Clients start a new proxy voting extraction by submitting a job via `POST /jobs` (with a company CIK and year).
   The API creates a job in the database with a status of `pending` and returns a unique `job_id`.

2. **Polling for Status/Results:**
   The client polls `GET /jobs/{job_id}` to check the job’s status (`pending`, `running`, `done`, or `error`) and fetch results when available.

3. **Asynchronous Processing:**
   A background worker (triggered via `POST /worker` or by an event source) picks up the oldest pending job, downloads relevant SEC 8-K filings for the given company/year, parses all Item 5.07 voting tables and disclosures using an LLM-powered extraction routine, and stores the structured results in the database.
   On completion (or error), the job’s status and output are updated.

4. **Result Retrieval:**
   When the job is `done`, the client’s polling call returns the full extracted voting results, including all proposals and tallies.

**Key Endpoints:**

| Method & Path        | Description                                                       |
| -------------------- | ----------------------------------------------------------------- |
| `POST /jobs`         | Submit a new proxy voting results extraction job                  |
| `GET /jobs/{job_id}` | Poll the status and retrieve results of a specific extraction job |
| `POST /worker`       | Trigger background processing of pending jobs (optional/manual)   |

**Polling Pattern:**
This API is designed for “fire-and-forget” data processing:

* You **submit a job** (async)
* **Poll for status**
* **Retrieve results** when ready
  This pattern is robust for slow or high-latency document analysis.

**Security:**

* **Database connection string** (Postgres DSN) is never exposed in code; it’s pulled from AWS Secrets Manager via the `PG_SECRET_ARN` environment variable.
* **OpenAI API key** for LLM-based parsing is also securely fetched from Secrets Manager.

**Example Flow:**

1. `POST /jobs`
   Body: `{ "cik": "0000320193", "year": 2024 }`
   → Returns `{ "job_id": "...", "status": "pending" }`

2. Client periodically calls:
   `GET /jobs/{job_id}`
   → Returns `{ "job_id": "...", "status": "running" | "done" | "error", ... }`

3. When `"status": "done"`, the response includes all parsed proxy voting results.

**Note:**

* If desired, the `/worker` endpoint can be called on a schedule (e.g., via EventBridge or cron) to process pending jobs automatically.
* LLM parsing is robust to varied HTML/filing layouts and standardizes all voting tallies in a common schema.

---

## Environment Variables & Secrets

**Never commit real secrets or ARNs to source control.**
Reference environment variables as follows:

| Variable            | Purpose                           | Source (example)           |
| ------------------- | --------------------------------- | -------------------------- |
| NBIM\_API\_KEY      | NBIM API key                      | Secrets Manager ARN        |
| NBIM\_API\_URL      | NBIM base API URL                 | Plaintext env var          |
| S3\_BUCKET          | S3 bucket for cached payloads     | Plaintext env var          |
| PG\_SECRET\_ARN     | Postgres DSN secret ARN           | Secrets Manager ARN        |
| OPENAI\_API\_KEY    | OpenAI API key                    | Secrets Manager ARN        |
| SIGNED\_URL\_EXPIRY | Presigned S3 URL expiry (seconds) | (optional, default=604800) |

**Best practice:**

* Use `.env.example` for templates.
* Inject real values via CI/CD or shell (never in source code).

---

## Deployment Notes

* Configure environment variables/ARNs using your deployment tool (e.g., AWS Lambda console, Serverless Framework, CDK).
* Ensure your Lambda IAM roles have `GetSecretValue` permission for any referenced secrets, and access to S3/Postgres as needed.
* For local testing, use the [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-envvars.html) to set environment variables.

---

## License

MIT license.

---

## Contact

For questions, raise an issue or contact me!

---
