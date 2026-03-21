# AkadVerse Sample Questions Generator
### Tier 5 Learning AI Tool | Microservice Port: `8009`

> A faculty-facing AI tool that generates exam-representative sample questions
> for student exam preparation. Produces structured question banks across four
> formats (MCQ, short answer, essay, calculation) organised by topic and
> calibrated to Bloom's Taxonomy difficulty levels. Exports printable PDF
> handouts on demand, with an optional answer key for the lecturer.

---

## Table of Contents

1. [What This Microservice Does](#what-this-microservice-does)
2. [Architecture Overview](#architecture-overview)
3. [Prerequisites](#prerequisites)
4. [Getting Your API Key](#getting-your-api-key)
5. [Critical Setup: DejaVu Fonts](#critical-setup-dejavu-fonts)
6. [Optional Setup: Poppler for Scanned PDFs](#optional-setup-poppler-for-scanned-pdfs)
7. [Installation](#installation)
8. [Running the Server](#running-the-server)
9. [API Endpoints](#api-endpoints)
   - [POST /generate-questions](#1-post-generate-questions)
   - [GET /questions](#2-get-questions)
   - [GET /export-pdf/{batch\_id}](#3-get-export-pdfbatch_id)
   - [GET /batches](#4-get-batches)
   - [GET /health](#5-get-health)
10. [Testing with Swagger UI](#testing-with-swagger-ui)
11. [Example Test Inputs](#example-test-inputs)
12. [Understanding the Responses](#understanding-the-responses)
13. [Question Formats and Difficulty Levels](#question-formats-and-difficulty-levels)
14. [Past Exam Paper Upload](#past-exam-paper-upload)
15. [Generated Files](#generated-files)
16. [Common Errors and Fixes](#common-errors-and-fixes)
17. [Project Structure](#project-structure)

---

## What This Microservice Does

This service is a **Tier 5 component** of the AkadVerse AI-first e-learning
platform. It lives inside the *My Teaching* module and serves as an exam
preparation assistant for faculty.

A lecturer provides their course title, course code, academic level, topic
weights, format preferences, and difficulty distribution. In one API call,
Gemini generates a complete structured question bank covering all requested
formats and topics. Each question is stored individually in a SQLite database
for reuse and fine-grained querying. A PDF export endpoint turns any batch
into a formatted, printable exam paper handout on demand.

Optionally, a faculty member can upload a past exam paper PDF. If the PDF
is text-based (digitally typed), the text is extracted directly. If it is
a scanned image PDF (common for Nigerian university exam papers), Gemini
Vision performs OCR and extracts the text automatically. This extracted
context calibrates Gemini to generate questions that match the department's
specific house style and difficulty standard.

---

## Architecture Overview

```
Faculty input (form fields)
        │
        ├── Optional: past exam PDF
        │     ├── Stage 1: PyPDF text extraction (fast, for digital PDFs)
        │     └── Stage 2: Gemini Vision OCR (for scanned/image PDFs)
        │
        ▼
Gemini structured generation
(single API call → QuestionBatch schema)
        │
        ▼
Questions stored individually in SQLite
(one row per question — 2 tables)
        │
        ├── GET /questions   → filter and query the bank
        ├── GET /export-pdf  → generate formatted PDF on demand
        └── Kafka mock: questions.generated event published
```

**Key design decisions:**

- **No RAG, no FAISS.** Past paper text is injected directly into the
  prompt as style context. It is not indexed or retrieved.
- **PDF is generated on demand**, not saved at generation time. The database
  is the source of truth; the PDF is a rendered view over it.
- **DejaVu Sans font** is used throughout the PDF export to support Unicode
  characters -- mathematical symbols, Greek letters, accented text -- which
  Helvetica cannot handle in fpdf2.
- **Two-stage past paper extraction** means the service handles both digital
  and scanned PDFs without requiring separate workflows.
- **Bloom's Taxonomy difficulty levels** rather than simple easy/medium/hard,
  matching Nigerian university and NUC examination standards.

---

## Prerequisites

- **Python 3.10 or higher**
- **pip** (Python package manager)
- A **Google Gemini API key** (free tier sufficient for generation and OCR)
- **DejaVu font files** in a `fonts/` folder (required for PDF export --
  see [Critical Setup: DejaVu Fonts](#critical-setup-dejavu-fonts))
- **Poppler** (optional -- only needed for scanned past exam PDFs --
  see [Optional Setup: Poppler](#optional-setup-poppler-for-scanned-pdfs))

> **Windows users:** All commands below work in VS Code's integrated terminal
> or Windows PowerShell. Use `python` instead of `python3` if needed.

---

## Getting Your API Key

1. Go to [https://aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2. Sign in with a Google account.
3. Click **Create API Key**.
4. Copy the key -- you will paste it as a form field in Swagger UI.

> The free tier includes Gemini 2.5 Flash access with sufficient quota for
> question generation and Gemini Vision OCR on past exam papers.

---

## Critical Setup: DejaVu Fonts

**This step is required before the PDF export endpoint will work.**

The `GET /export-pdf/{batch_id}` endpoint uses DejaVu Sans to render the
PDF. This font supports the full Unicode character set, which means exam
questions containing mathematical notation, Greek letters, superscripts,
and accented characters all render correctly. fpdf2's built-in Helvetica
does not have this capability.

The font files must be placed in a `fonts/` folder inside your project
directory. The service will raise a clear error on startup if they are
missing.

### Step 1 -- Download the DejaVu font package

Go to: [https://dejavu-fonts.github.io](https://dejavu-fonts.github.io) or [https://www.fontsquirrel.com/fonts/dejavu-sans](https://www.fontsquirrel.com/fonts/dejavu-sans)

Click **Download** and download the latest release zip, for example
`dejavu-fonts-ttf-2.37.zip`.

### Step 2 -- Extract and copy the required files

Extract the zip. Inside the extracted folder, go into the `ttf/` subfolder.
You need exactly these four files:

```
DejaVuSans.ttf
DejaVuSans-Bold.ttf
DejaVuSans-Oblique.ttf
DejaVuSans-BoldOblique.ttf
```

### Step 3 -- Create the fonts folder and paste the files

Inside your project folder, create a subfolder called `fonts` and paste all
four `.ttf` files into it:

```
akadverse-sample-questions-generator/
├── sample_questions_generator.py
├── requirements.txt
├── fonts/
│   ├── DejaVuSans.ttf
│   ├── DejaVuSans-Bold.ttf
│   ├── DejaVuSans-Oblique.ttf
│   └── DejaVuSans-BoldOblique.ttf
```

### Step 4 -- Verify

Start the server and call `GET /health`. If the font files are missing
or in the wrong location, the `export-pdf` endpoint will return a
`500` error with the message `Required font file not found: 'fonts/DejaVuSans.ttf'`.
The generation and query endpoints work regardless of font availability.

> **Note:** The `fonts/` folder is listed in `.gitignore` because font
> files are large binaries that should not be committed to version control.
> Every developer cloning the repository must complete this setup step
> independently.

---

## Optional Setup: Poppler for Scanned PDFs

This step is only needed if you intend to upload **scanned** past exam
papers for style calibration. If your past exam papers are digital
(typed and exported from Word or similar), skip this section.

Poppler is a PDF rendering library used by `pdf2image` to convert PDF
pages into images before sending them to Gemini Vision for OCR.

### Windows Installation

1. Go to:
   `https://github.com/oschwartz10612/poppler-windows/releases`
2. Download the latest release zip, for example `Release-24.08.0-0.zip`.
3. Extract the zip anywhere, for example `C:\poppler`.
4. Add the `bin` folder to your system PATH:
   - Open the Start menu and search for **"Environment Variables"**
   - Click **"Edit the system environment variables"**
   - Click **"Environment Variables"**
   - Under **System variables**, find `Path` and click **Edit**
   - Click **New** and add the path to the `bin` folder,
     e.g. `C:\poppler\Library\bin`
   - Click **OK** on all dialogs
5. Restart your VS Code terminal so the PATH change takes effect.
6. Verify: run `pdftoppm -v` in your terminal. You should see a version
   number, not an error.

If poppler is not installed and you upload a scanned PDF, the service
logs a clear warning and proceeds without style calibration. Generation
still succeeds.

---

## Installation

### Step 1 -- Create your project folder

```
akadverse-sample-questions-generator/
├── sample_questions_generator.py
├── requirements.txt
└── fonts/              ← must be populated before using PDF export
```

### Step 2 -- Create and activate a virtual environment

```bash
# Create
python -m venv venv

# Activate — Windows
venv\Scripts\activate

# Activate — macOS/Linux
source venv/bin/activate
```

### Step 3 -- Install Python dependencies

```bash
pip install -r requirements.txt
```

Full dependency reference:

| Package | Purpose |
|---|---|
| `fastapi` | Web framework for the API |
| `uvicorn` | ASGI server to run FastAPI |
| `google-genai>=1.67.0` | Gemini SDK for generation and Vision OCR |
| `langchain-google-genai` | LangChain wrapper for structured output |
| `langchain-core` | LangChain prompt templates |
| `pydantic` | Data validation and response schemas |
| `pypdf` | Stage 1 text extraction from digital PDFs |
| `fpdf2` | PDF generation for question paper export |
| `pdf2image` | Converts scanned PDF pages to images for OCR |

### Step 4 -- Place the DejaVu font files

See [Critical Setup: DejaVu Fonts](#critical-setup-dejavu-fonts) above.
This must be done before testing the PDF export endpoint.

---

## Running the Server

From inside your project folder with the virtual environment activated:

```bash
uvicorn sample_questions_generator:app --host 127.0.0.1 --port 8009 --reload
```

**Expected startup output:**

```
[Startup] AkadVerse Sample Questions Generator initialising...
[DB] Question bank initialised successfully.
[Startup] Ready. Run: uvicorn sample_questions_generator:app --host 127.0.0.1 --port 8009 --reload
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8009 (Press CTRL+C to quit)
```

Two files are created automatically on first startup:

- `akadverse_questions.db` -- SQLite database with two tables

---

## API Endpoints

### 1. `POST /generate-questions`

**What it does:** Accepts course context and question requirements, generates
a structured batch of exam-representative questions in one Gemini API call,
and stores every question individually in the SQLite question bank.

**Form fields:**

| Field | Required | Default | Description |
|---|---|---|---|
| `course_title` | Yes | -- | e.g. `Data Structures and Algorithms` |
| `course_code` | Yes | -- | e.g. `CSC 301` |
| `academic_level` | Yes | -- | e.g. `300 Level` |
| `topics_and_weights` | Yes | -- | Topics with weights -- see format note below |
| `format_mix` | Yes | -- | Question counts per format -- see format note below |
| `difficulty_mix` | No | see below | Bloom's Taxonomy distribution |
| `google_api_key` | Yes | -- | Your Gemini API key |
| `past_paper` | No | -- | Optional past exam PDF for style calibration |

**Format note -- Swagger UI single-line workaround:**

Swagger UI does not support multiline text in form fields. Use semicolons
as separators instead of newlines. Both work identically:

`topics_and_weights`:
```
Binary Search Trees: 30; Graph Algorithms: 40; Sorting Algorithms: 30
```

`format_mix`:
```
MCQ: 5; SHORT_ANSWER: 3; CALCULATION: 2
```

`difficulty_mix` (default value -- copy and edit as needed):
```
KNOWLEDGE: 20; COMPREHENSION: 30; APPLICATION: 30; ANALYSIS: 15; SYNTHESIS: 5
```

**Success response (200 OK):**

```json
{
  "status": "success",
  "batch_id": "a3f1b2c4d5e6",
  "message": "10 sample questions generated for CSC 301. Use GET /export-pdf/a3f1b2c4d5e6 to download the student handout.",
  "course_code": "CSC 301",
  "total_questions_generated": 10,
  "questions_by_format": {
    "MCQ": 5,
    "SHORT_ANSWER": 3,
    "CALCULATION": 2
  },
  "questions_by_difficulty": {
    "KNOWLEDGE": 2,
    "COMPREHENSION": 3,
    "APPLICATION": 3,
    "ANALYSIS": 2
  },
  "pdf_export_url": "/export-pdf/a3f1b2c4d5e6"
}
```

> **Note on generation time:** This endpoint takes 20 to 45 seconds
> depending on the number of questions and whether a past paper was uploaded.

---

### 2. `GET /questions`

**What it does:** Queries the stored question bank with optional filters.
All filters are optional and combinable. Returns full question content
including model answers and explanations.

**Query parameters:**

| Parameter | Description |
|---|---|
| `batch_id` | Filter to questions from one specific generation run |
| `course_code` | Partial match on course code |
| `topic` | Partial match on topic name |
| `format` | Exact match: `MCQ`, `SHORT_ANSWER`, `ESSAY`, `CALCULATION`, `TRUE_FALSE` |
| `difficulty` | Exact match: `KNOWLEDGE`, `COMPREHENSION`, `APPLICATION`, `ANALYSIS`, `SYNTHESIS` |
| `limit` | Max records returned (default 20, max 100) |
| `offset` | Records to skip for pagination (default 0) |

**Success response (200 OK):**

```json
{
  "questions": [
    {
      "question_id": "abc123...",
      "batch_id": "a3f1b2c4d5e6",
      "course_code": "CSC 301",
      "topic": "Binary Search Trees",
      "format": "MCQ",
      "difficulty": "APPLICATION",
      "question_text": "Which traversal of a BST yields nodes in sorted order?",
      "options": ["A. Pre-order", "B. In-order", "C. Post-order", "D. Level-order"],
      "correct_answer": "B",
      "mark": 2,
      "explanation": "In-order traversal visits the left subtree, then root, then right subtree...",
      "created_at": "2026-03-21T10:30:00"
    }
  ],
  "total_returned": 1
}
```

---

### 3. `GET /export-pdf/{batch_id}`

**What it does:** Generates and streams a formatted PDF of all questions in
a batch. The PDF is produced on demand from the database -- nothing is saved
to disk. Two versions are available via the `include_answers` parameter.

**Path parameter:**

| Parameter | Description |
|---|---|
| `batch_id` | The batch ID from the generation response |

**Query parameter:**

| Parameter | Default | Description |
|---|---|---|
| `include_answers` | `false` | `false` = student copy (questions + response spaces). `true` = lecturer copy (questions + full answer key). |

**PDF layout:**

- Cover header with course code, title, batch ID, and date
- Student instructions note
- Questions grouped into labelled sections by format (Section A: MCQ,
  Section B: True/False, Section C: Short Answer, etc.)
- Each section shows its total marks
- MCQ options printed as A/B/C/D
- Blank lined response spaces for short answer, calculation, and essay
- Marks tag shown for each question
- Lecturer copy: full answer key on a separate page with explanations

**How to download:**

Open this URL directly in your browser (replace the batch ID):

```
http://127.0.0.1:8009/export-pdf/YOUR_BATCH_ID?include_answers=false
```

Your browser will prompt you to download the PDF immediately. In Swagger
UI, click Execute and then click the **Download file** link that appears.

---

### 4. `GET /batches`

**What it does:** Lists all generation batches, most recent first.
Each row represents one `/generate-questions` call.

**Query parameters:**

| Parameter | Default | Description |
|---|---|---|
| `course_code` | -- | Optional partial match filter |
| `limit` | `10` | Max records returned |
| `offset` | `0` | Records to skip for pagination |

**Success response (200 OK):**

```json
{
  "batches": [
    {
      "batch_id": "a3f1b2c4d5e6",
      "course_code": "CSC 301",
      "course_title": "Data Structures and Algorithms",
      "total_questions": 10,
      "questions_stored": 10,
      "created_at": "2026-03-21T10:30:00",
      "pdf_export_url": "/export-pdf/a3f1b2c4d5e6"
    }
  ],
  "total_returned": 1
}
```

---

### 5. `GET /health`

**What it does:** Reports service status and question bank statistics.

**Success response (200 OK):**

```json
{
  "status": "ok",
  "version": "1.0",
  "total_questions_in_bank": 45,
  "total_generation_batches": 4,
  "questions_by_format": {
    "MCQ": 20,
    "SHORT_ANSWER": 15,
    "CALCULATION": 10
  },
  "endpoints": { ... }
}
```

---

## Testing with Swagger UI

With the server running, open:

```
http://127.0.0.1:8009/docs
```

To test any endpoint: click its name, click **"Try it out"**, fill in the
fields, and click **"Execute"**. Keep your terminal open alongside the
browser to watch logs in real time.

---

## Example Test Inputs

Run these tests in order for a complete end-to-end verification.

---

### Test 1 -- Health check

`GET /health` -- confirm `status: ok` and `total_questions_in_bank: 0`.

---

### Test 2 -- Generate a question bank

`POST /generate-questions`:

| Field | Value |
|---|---|
| `course_title` | `Data Structures and Algorithms` |
| `course_code` | `CSC 301` |
| `academic_level` | `300 Level` |
| `topics_and_weights` | `Binary Search Trees: 30; Graph Algorithms: 40; Sorting Algorithms: 30` |
| `format_mix` | `MCQ: 5; SHORT_ANSWER: 3; CALCULATION: 2` |
| `difficulty_mix` | `KNOWLEDGE: 20; COMPREHENSION: 30; APPLICATION: 30; ANALYSIS: 15; SYNTHESIS: 5` |
| `google_api_key` | Your key |
| `past_paper` | Leave empty |

**Expected:** `200 OK` with `total_questions_generated: 10`, a `batch_id`,
and a `pdf_export_url`. Copy the `batch_id` -- you will need it for Tests 3, 4, and 5.

---

### Test 3 -- Query the question bank

`GET /questions` with these filters:

| Parameter | Value |
|---|---|
| `topic` | `Binary Search Trees` |
| `format` | `MCQ` |
| `limit` | `10` |

**Expected:** MCQ questions for the Binary Search Trees topic with full
content including options, correct answer, and explanation.

Then try `format: SHORT_ANSWER` with no topic filter to see all short
answer questions across all topics.

---

### Test 4 -- Export student PDF

Open this URL in your browser (replace `YOUR_BATCH_ID`):

```
http://127.0.0.1:8009/export-pdf/YOUR_BATCH_ID?include_answers=false
```

**Expected:** Your browser downloads a PDF. Open it and verify:
- Cover header with course name, batch ID, and date
- Questions grouped into sections by format
- MCQ options shown as A/B/C/D
- Blank lines for short answer and calculation responses
- Marks tags on each question
- No answer key (student copy)

---

### Test 5 -- Export lecturer PDF

```
http://127.0.0.1:8009/export-pdf/YOUR_BATCH_ID?include_answers=true
```

**Expected:** Same PDF as Test 4 plus an **ANSWER KEY** page at the end
with model answers and explanations for every question.

---

### Test 6 -- List batches

`GET /batches` -- confirm one batch appears with `questions_stored` matching
`total_questions_generated` from Test 2.

---

### Test 7 -- Upload a past exam paper

Re-run `POST /generate-questions` with the same inputs as Test 2 but this
time upload a past exam paper PDF in the `past_paper` field.

**If the PDF is text-based (digital):**
```
[PastPaper] Stage 1 (PyPDF): Extracted 2847 chars from text-based PDF. Gemini OCR not needed.
```

**If the PDF is scanned (image-only):**
```
[PastPaper] Stage 1 (PyPDF): Only 10 chars found. PDF is likely scanned. Escalating to Stage 2 (Gemini OCR)...
[PastPaper] Stage 2 (Gemini OCR): Converting PDF pages to images...
[PastPaper] Stage 2: Converted 3 page(s). Sending to Gemini Vision...
[PastPaper] Stage 2: Page 1 OCR returned 1842 chars.
[PastPaper] Stage 2 (Gemini OCR): Successfully extracted 3000 chars from 3 scanned page(s).
```

The generated questions in this batch will be calibrated to match your
department's exam style.

---

### Test 8 -- Filter by batch ID

`GET /questions` with the `batch_id` from Test 7 and all other filters
empty. Confirm only questions from that specific batch are returned.

---

## Understanding the Responses

### Where is the generated PDF saved?

It is not saved to disk. The PDF is generated on demand from the database
when you call `GET /export-pdf/{batch_id}`. The database is the source of
truth; the PDF is a view rendered over it. This means you can export the
same batch multiple times and it will always reflect the stored questions.

### The `pdf_export_url` field

The `pdf_export_url` in the generation response, e.g.
`/export-pdf/a3f1b2c4d5e6`, is a relative path. To use it, prepend
the server address: `http://127.0.0.1:8009/export-pdf/a3f1b2c4d5e6`.

### The `batch_id`

Every generation run produces a unique 12-character hex ID. This ID is the
primary key in the `generation_batches` table, appears as a foreign key on
every question row in the `questions` table, and is used to filter questions
and export PDFs. Copy it from the generation response and keep it handy.

### Why `10 chars` from my past exam PDF?

Your past exam paper is a scanned image PDF with no embedded text layer.
PyPDF (Stage 1) reads text characters directly from the file structure --
it cannot interpret images. This triggers Stage 2, which sends the page
images to Gemini Vision for OCR. Poppler must be installed for Stage 2 to
work. If poppler is missing, a clear warning is logged and generation
proceeds without style calibration.

---

## Question Formats and Difficulty Levels

### Supported formats for `format_mix`

| Format | What Gemini generates |
|---|---|
| `MCQ` | 4-option multiple choice with correct letter and explanation |
| `SHORT_ANSWER` | Concise question with model answer (2-4 sentences) |
| `ESSAY` | Extended question with key marking points |
| `CALCULATION` | Step-by-step worked solution with formula used |
| `TRUE_FALSE` | Statement with True/False answer and justification |

### Bloom's Taxonomy levels for `difficulty_mix`

| Level | What it tests | Typical question type |
|---|---|---|
| `KNOWLEDGE` | Pure recall and definitions | "Define a Binary Search Tree" |
| `COMPREHENSION` | Explaining concepts in own words | "Explain how in-order traversal works" |
| `APPLICATION` | Applying a known method to a new problem | "Insert these values into a BST" |
| `ANALYSIS` | Breaking down, comparing, evaluating | "Compare BST and AVL tree complexity" |
| `SYNTHESIS` | Creating, designing, combining | "Design an algorithm that..." |

**Recommended distributions:**

Standard 300-level exam:
```
KNOWLEDGE: 20; COMPREHENSION: 30; APPLICATION: 30; ANALYSIS: 15; SYNTHESIS: 5
```

Application-heavy CS exam:
```
KNOWLEDGE: 10; COMPREHENSION: 20; APPLICATION: 40; ANALYSIS: 20; SYNTHESIS: 10
```

Theory-heavy exam:
```
KNOWLEDGE: 30; COMPREHENSION: 35; APPLICATION: 20; ANALYSIS: 10; SYNTHESIS: 5
```

The numbers are percentage guidelines to Gemini, not hard constraints.

---

## Past Exam Paper Upload

The `past_paper` field in `/generate-questions` accepts a PDF of any
previous exam paper for the course. Gemini uses it to calibrate the style,
difficulty phrasing, and question conventions of the generated questions.

**Two extraction paths:**

| PDF type | Detection | Method | Requires |
|---|---|---|---|
| Digital (typed) | PyPDF extracts 100+ chars | PyPDF direct text extraction | Nothing extra |
| Scanned (image) | PyPDF extracts fewer than 100 chars | Gemini Vision OCR via pdf2image | Poppler on system PATH |

Only the first 3 pages are processed for OCR to stay within free tier
rate limits. Up to 6000 characters of extracted text are injected into
the generation prompt.

If extraction fails for any reason, a warning is logged and generation
proceeds without past paper context.

---

## Generated Files

The following are created at runtime. **Do not commit them to version
control** -- list them in `.gitignore`.

| File | What it is |
|---|---|
| `akadverse_questions.db` | SQLite database -- question bank and batch metadata |

**Suggested `.gitignore`:**

```
akadverse_questions.db
__pycache__/
*.pyc
.env
.vscode/
fonts/
```

> Note that `fonts/` is gitignored because `.ttf` files are large binaries.
> Every developer cloning this repo must download and place the DejaVu fonts
> manually as described in the setup section.

---

## Common Errors and Fixes

**`Required font file not found: 'fonts/DejaVuSans.ttf'`**

The DejaVu font files are missing from the `fonts/` folder. Follow the
steps in [Critical Setup: DejaVu Fonts](#critical-setup-dejavu-fonts).
This error only affects the PDF export endpoint -- generation and querying
still work.

**`ModuleNotFoundError: No module named 'pdf2image'`**
```bash
pip install pdf2image
```

**`pdf2image` works but OCR stage still fails with poppler error**

Poppler is not installed or not on your system PATH. Follow the steps in
[Optional Setup: Poppler](#optional-setup-poppler-for-scanned-pdfs).

**`Gemini generation failed`**

Usually a quota or API key issue. Verify your key at
[https://aistudio.google.com/apikey](https://aistudio.google.com/apikey).
Check your Gemini free tier quota if it persists.

**`Invalid format_mix` error**

Check that your format names are exactly `MCQ`, `SHORT_ANSWER`, `ESSAY`,
`CALCULATION`, or `TRUE_FALSE` in uppercase. Other spellings like
`Short Answer` or `short_answer` will fail validation.

**`[Generate WARNING] Topic weights sum to X%, not 100%`**

Your topic weights do not sum to 100. This is a warning, not an error --
generation still proceeds. Gemini uses the weights as proportional guidance
regardless. Check that your semicolon-separated entries are all being parsed
correctly (e.g. `Binary Search Trees: 30; Graph Algorithms: 40; Sorting: 30`
sums to 100).

**`Address already in use` on startup**

Port 8009 is occupied. Use a different port:
```bash
uvicorn sample_questions_generator:app --host 127.0.0.1 --port 8010 --reload
```

---

## Project Structure

```
akadverse-sample-questions-generator/
│
├── sample_questions_generator.py    # Main microservice — all logic here
├── requirements.txt                 # Python dependencies
├── README.md                        # This file
├── .gitignore
│
├── fonts/                           # DejaVu font files — DO NOT COMMIT
│   ├── DejaVuSans.ttf               # Must be placed here manually
│   ├── DejaVuSans-Bold.ttf          # Download from dejavu-fonts.github.io
│   ├── DejaVuSans-Oblique.ttf
│   └── DejaVuSans-BoldOblique.ttf
│
└── akadverse_questions.db           # SQLite database — DO NOT COMMIT
                                     # Created automatically on first run
```

---

## Part of the AkadVerse Platform

This microservice is **Tier 5** in the AkadVerse AI architecture, operating
within the *My Teaching* module alongside:

- Concept Explainer (Port 8006)
- External Resources Puller (Port 8007)
- Assignment Generator (Port 8008)
- Notes Creator (Port 8010)

The `questions.generated` Kafka event published by this service is consumed
by the platform's Insight Engine and student notification services in
production. During local development it is simulated as a terminal log line.

---

*AkadVerse AI Architecture v1.0*