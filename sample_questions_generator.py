"""
AkadVerse — Sample Questions Generator
Tier 5 | Microservice Port: 8009
========================================================================
v1.0 — Initial build.

What this service does:
  A faculty tool that generates exam-representative sample questions for
  student exam preparation. Faculty provides course details, topic weights,
  format preferences (MCQ, short-answer, essay, calculation), difficulty
  distribution, and optionally an uploaded past exam paper PDF.

  Gemini generates a structured batch of questions calibrated to the
  lecturer's exam style. Each question is individually stored in a SQLite
  question bank (simulating PostgreSQL) for reuse and filtering.

  A PDF export endpoint formats any batch into a clean, student-ready
  exam paper handout with an answer key on a separate page.

Architecture:
  - No RAG, no vector stores. Pure LLM structured generation.
  - Past paper PDF is injected directly as prompt context (no FAISS).
  - Questions are stored individually for fine-grained querying.
  - fpdf2 handles PDF export — lightweight, no Java, Windows-compatible.
  - Difficulty uses Bloom's Taxonomy (Nigerian university standard).
  - Port: 8009

Endpoints:
  POST /generate-questions      — Generate a batch of sample questions
  GET  /questions               — Query question bank with filters
  GET  /export-pdf/{batch_id}   — Export a batch as a student PDF handout
  GET  /batches                 — List all generation runs
  GET  /health                  — Service status
"""

import io
import json
import os
import sqlite3
import textwrap
import threading
from datetime import datetime
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, List, Optional
from uuid import uuid4

import pypdf
from fastapi import FastAPI, HTTPException, Form, File, UploadFile, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from fpdf import FPDF

from google import genai
from google.genai.types import Content, Part, Blob
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate


# =========================================================
# CONSTANTS
# =========================================================

DB_PATH               = "akadverse_questions.db"
MAX_PAST_PAPER_CHARS  = 6000   # Characters of past paper injected into the prompt.
                                # Keeps prompt size manageable while giving Gemini
                                # enough style context to calibrate its output.
MAX_QUESTIONS_PER_RUN = 50     # Hard cap to prevent runaway API costs per call.
MIN_QUESTIONS_PER_RUN = 1

# Supported question formats
VALID_FORMATS = {"MCQ", "SHORT_ANSWER", "ESSAY", "CALCULATION", "TRUE_FALSE"}

# Bloom's Taxonomy difficulty levels — the standard framework used by the
# NUC and Nigerian professional bodies (COREN, etc.) for exam design.
VALID_DIFFICULTIES = {"KNOWLEDGE", "COMPREHENSION", "APPLICATION", "ANALYSIS", "SYNTHESIS"}

# Threading lock guards concurrent SQLite writes
db_lock = threading.Lock()

# Local DejaVu font files expected in the repository fonts/ directory.
PDF_FONT_FAMILY = "DejaVu"
PDF_FONT_FILES = {
    "": "DejaVuSans.ttf",
    "B": "DejaVuSans-Bold.ttf",
    "I": "DejaVuSans-Oblique.ttf",
    "BI": "DejaVuSans-BoldOblique.ttf",
}


# =========================================================
# PYDANTIC SCHEMAS
# =========================================================

class SampleQuestion(BaseModel):
    """
    Schema for a single generated sample question.
    Every field supports both database storage and PDF rendering.
    """
    topic: str = Field(
        description="The specific topic this question tests, e.g. 'BST Insertion'."
    )
    format: str = Field(
        description="Question format: MCQ, SHORT_ANSWER, ESSAY, CALCULATION, or TRUE_FALSE."
    )
    difficulty: str = Field(
        description=(
            "Bloom's Taxonomy level: KNOWLEDGE, COMPREHENSION, "
            "APPLICATION, ANALYSIS, or SYNTHESIS."
        )
    )
    question_text: str = Field(
        description="The full question text as it would appear on an exam paper."
    )
    options: Optional[List[str]] = Field(
        default=None,
        description=(
            "MCQ only: exactly four options labelled A, B, C, D. "
            "e.g. ['A. O(n)', 'B. O(log n)', 'C. O(n²)', 'D. O(1)']. "
            "Must be null for all other formats."
        )
    )
    correct_answer: str = Field(
        description=(
            "MCQ: the correct option letter, e.g. 'B'. "
            "SHORT_ANSWER/CALCULATION: concise model answer. "
            "ESSAY: key points expected in a full-mark response. "
            "TRUE_FALSE: 'True' or 'False' plus brief justification."
        )
    )
    mark: int = Field(
        description="Marks allocated to this question (1–20)."
    )
    explanation: str = Field(
        description=(
            "A clear explanation of why the correct answer is correct. "
            "Written for students — educational and thorough."
        )
    )


class QuestionBatch(BaseModel):
    """Root schema returned by Gemini — a list of SampleQuestion objects."""
    questions: List[SampleQuestion] = Field(
        description="The generated sample questions."
    )


class GenerationSuccessResponse(BaseModel):
    """API response returned after a successful generation run."""
    status: str
    batch_id: str
    message: str
    course_code: str
    total_questions_generated: int
    questions_by_format: dict
    questions_by_difficulty: dict
    pdf_export_url: str


# =========================================================
# DATABASE
# =========================================================

def init_db() -> None:
    """
    Creates the SQLite tables on startup if they do not exist.

    Tables:
      generation_batches — one row per /generate-questions call.
      questions          — one row per question; the actual question bank.

    Indexes on batch_id, topic, format, difficulty, and course_code
    make filtering fast even as the bank grows to thousands of questions.
    """
    try:
        with get_db_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS generation_batches (
                    batch_id        TEXT PRIMARY KEY,
                    course_code     TEXT NOT NULL,
                    course_title    TEXT NOT NULL,
                    topics_json     TEXT NOT NULL,
                    total_questions INTEGER NOT NULL,
                    created_at      TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS questions (
                    question_id    TEXT PRIMARY KEY,
                    batch_id       TEXT NOT NULL,
                    course_code    TEXT NOT NULL,
                    topic          TEXT NOT NULL,
                    format         TEXT NOT NULL,
                    difficulty     TEXT NOT NULL,
                    question_text  TEXT NOT NULL,
                    options_json   TEXT,
                    correct_answer TEXT NOT NULL,
                    mark           INTEGER NOT NULL,
                    explanation    TEXT NOT NULL,
                    created_at     TEXT NOT NULL,
                    FOREIGN KEY (batch_id) REFERENCES generation_batches(batch_id)
                );

                CREATE INDEX IF NOT EXISTS idx_q_batch      ON questions(batch_id);
                CREATE INDEX IF NOT EXISTS idx_q_topic      ON questions(topic);
                CREATE INDEX IF NOT EXISTS idx_q_format     ON questions(format);
                CREATE INDEX IF NOT EXISTS idx_q_difficulty ON questions(difficulty);
                CREATE INDEX IF NOT EXISTS idx_q_course     ON questions(course_code);
            """)
            conn.commit()
        print("[DB] Question bank initialised successfully.")
    except sqlite3.Error as e:
        print(f"[DB ERROR] Initialisation failed: {e}")
        raise


@contextmanager
def get_db_connection():
    """
    Context manager for SQLite connections.
    Guarantees the connection is always closed, row_factory enables
    column-name access: row["question_text"].
    """
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        yield conn
    except sqlite3.Error as e:
        if conn:
            conn.rollback()
        print(f"[DB ERROR] {e}")
        raise
    finally:
        if conn:
            conn.close()


def save_batch_to_db(
    batch_id: str,
    course_code: str,
    course_title: str,
    topics: dict,
    total_questions: int
) -> None:
    """Persists generation batch metadata to generation_batches."""
    with db_lock:
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO generation_batches
                  (batch_id, course_code, course_title, topics_json, total_questions, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (batch_id, course_code, course_title,
                 json.dumps(topics), total_questions,
                 datetime.utcnow().isoformat())
            )
            conn.commit()


def save_questions_to_db(
    batch_id: str,
    course_code: str,
    questions: List[SampleQuestion]
) -> None:
    """
    Saves every question individually to the questions table.
    Each gets its own UUID primary key for independent querying.
    Uses db_lock to prevent concurrent write corruption.
    """
    with db_lock:
        with get_db_connection() as conn:
            now = datetime.utcnow().isoformat()
            for q in questions:
                conn.execute(
                    """
                    INSERT INTO questions
                      (question_id, batch_id, course_code, topic, format, difficulty,
                       question_text, options_json, correct_answer, mark, explanation, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uuid4().hex,
                        batch_id,
                        course_code,
                        q.topic,
                        q.format,
                        q.difficulty,
                        q.question_text,
                        json.dumps(q.options) if q.options else None,
                        q.correct_answer,
                        q.mark,
                        q.explanation,
                        now
                    )
                )
            conn.commit()
    print(f"[DB] {len(questions)} questions saved for batch '{batch_id}'.")


# =========================================================
# MODEL DISCOVERY
# =========================================================

def get_valid_model_name(api_key_str: str) -> str:
    """
    Dynamically discovers the best available Gemini generative model.
    Consistent with the pattern used across all Tier 5 microservices.
    Falls back to 'gemini-1.5-flash' if discovery fails.
    """
    try:
        client = genai.Client(api_key=api_key_str)
        all_models = [
            m.name.replace("models/", "")
            for m in client.models.list()
            if m.name
        ]
        priority = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash", "gemini-pro"]
        for preferred in priority:
            if preferred in all_models:
                print(f"[Model] Using: {preferred}")
                return preferred
        if all_models:
            return all_models[0]
    except Exception as e:
        print(f"[Model WARNING] Discovery failed ({e}). Defaulting to 'gemini-1.5-flash'.")
    return "gemini-1.5-flash"


# =========================================================
# PAST PAPER TEXT EXTRACTION
# =========================================================

def extract_past_paper_text(file_bytes: bytes, api_key: str = "") -> str:
    """
    Extracts text from an uploaded past exam paper PDF using a two-stage strategy:

    Stage 1 — PyPDF (fast, free, works on text-based PDFs):
        Attempts to read the embedded text layer directly. Most PDFs generated
        digitally (e.g. typed and exported from Word) have this layer.
        If this yields more than MIN_TEXT_CHARS characters, it is used directly
        and Gemini vision is skipped entirely (saves API quota).

    Stage 2 — Gemini Vision OCR (for scanned / image-based PDFs):
        Nigerian university past exam papers are frequently printed and scanned,
        producing image-only PDFs with no text layer. PyPDF returns near-zero
        characters for these. In that case, we:
          1. Convert the first MAX_PAGES_FOR_OCR pages to JPEG images using
             pdf2image (requires poppler on system PATH).
          2. Send each page image to Gemini with an OCR prompt asking it to
             extract all visible exam questions and text.
          3. Concatenate the extracted text from all pages.

        Gemini vision is available on the free tier and handles handwritten
        annotations, printed text, and mixed content reliably.

    If both stages fail (e.g. poppler not installed, API error), returns ""
    gracefully -- the past paper is optional and generation still proceeds.

    Args:
        file_bytes: Raw bytes of the uploaded PDF file.
        api_key:    Gemini API key, used only if Stage 1 yields insufficient text.

    Returns:
        Extracted text truncated to MAX_PAST_PAPER_CHARS, or "" on failure.
    """
    # ---- Stage 1: PyPDF text extraction (fast path) ----
    MIN_TEXT_CHARS = 100   # Below this we treat the PDF as image-only

    try:
        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        pages_text = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages_text.append(text.strip())
        full_text = "\n\n".join(pages_text).strip()

        if len(full_text) >= MIN_TEXT_CHARS:
            # Text layer is present and readable -- use it directly
            truncated = full_text[:MAX_PAST_PAPER_CHARS]
            print(
                f"[PastPaper] Stage 1 (PyPDF): Extracted {len(truncated)} chars "
                f"from text-based PDF. Gemini OCR not needed."
            )
            return truncated
        else:
            print(
                f"[PastPaper] Stage 1 (PyPDF): Only {len(full_text)} chars found. "
                f"PDF is likely scanned. Escalating to Stage 2 (Gemini OCR)..."
            )
    except Exception as e:
        print(f"[PastPaper] Stage 1 (PyPDF) failed: {e}. Escalating to Stage 2...")

    # ---- Stage 2: Gemini Vision OCR (for scanned PDFs) ----
    MAX_PAGES_FOR_OCR = 3   # Limit to first 3 pages to stay within free tier limits

    if not api_key:
        print("[PastPaper WARNING] No API key available for Gemini OCR. Returning empty context.")
        return ""

    try:
        # pdf2image converts PDF pages to PIL Image objects.
        # Requires poppler installed on the system.
        # Windows install: download from https://github.com/oschwartz10612/poppler-windows/releases
        # then add the bin/ folder to your system PATH.
        from pdf2image import convert_from_bytes
        import base64

        print(f"[PastPaper] Stage 2 (Gemini OCR): Converting PDF pages to images...")
        images = convert_from_bytes(
            file_bytes,
            first_page=1,
            last_page=MAX_PAGES_FOR_OCR,
            dpi=200,          # 200 DPI gives clear text without excessive file size
            fmt="jpeg"
        )
        print(f"[PastPaper] Stage 2: Converted {len(images)} page(s). Sending to Gemini Vision...")

        client = genai.Client(api_key=api_key)

        # Discover the best available vision-capable model
        vision_model = "gemini-1.5-flash"   # Fallback -- supports vision on free tier
        try:
            available = [
                m.name.replace("models/", "")
                for m in client.models.list()
                if m.name
            ]
            for candidate in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]:
                if candidate in available:
                    vision_model = candidate
                    break
        except Exception:
            pass   # Keep the fallback model

        print(f"[PastPaper] Stage 2: Using vision model: {vision_model}")

        ocr_results = []
        for page_num, image in enumerate(images, start=1):
            # Convert PIL image to base64 JPEG for the Gemini API
            img_buffer = io.BytesIO()
            image.save(img_buffer, format="JPEG", quality=85)
            img_b64 = base64.b64encode(img_buffer.getvalue()).decode("utf-8")

            # Send to Gemini with a targeted OCR prompt using proper API objects
            response = client.models.generate_content(
                model=vision_model,
                contents=[
                    Content(
                        parts=[
                            Part(
                                inline_data=Blob(
                                    mime_type="image/jpeg",
                                    data=base64.b64decode(img_b64)
                                )
                            ),
                            Part(
                                text=(
                                    "This is page of a university exam paper. "
                                    "Extract ALL visible text exactly as it appears, "
                                    "preserving question numbers, options (A/B/C/D), "
                                    "marks allocations, and instructions. "
                                    "Do not summarise or paraphrase -- reproduce the text faithfully."
                                )
                            )
                        ]
                    )
                ]
            )

            page_text = response.text.strip() if response.text else ""
            print(f"[PastPaper] Stage 2: Page {page_num} OCR returned {len(page_text)} chars.")
            if page_text:
                ocr_results.append(page_text)

        combined = "\n\n".join(ocr_results).strip()
        if not combined:
            print("[PastPaper WARNING] Stage 2 (Gemini OCR): No text extracted. Returning empty context.")
            return ""

        truncated = combined[:MAX_PAST_PAPER_CHARS]
        print(
            f"[PastPaper] Stage 2 (Gemini OCR): Successfully extracted {len(truncated)} chars "
            f"from {len(images)} scanned page(s)."
        )
        return truncated

    except ImportError:
        print(
            "[PastPaper WARNING] pdf2image is not installed. "
            "Install it with: pip install pdf2image\n"
            "Also install poppler for Windows from: "
            "https://github.com/oschwartz10612/poppler-windows/releases\n"
            "Then add the poppler bin/ folder to your system PATH."
        )
        return ""
    except Exception as e:
        print(f"[PastPaper WARNING] Stage 2 (Gemini OCR) failed: {e}. Returning empty context.")
        return ""


# =========================================================
# GENERATION PROMPT
# =========================================================

GENERATION_PROMPT = PromptTemplate(
    template="""You are an experienced Nigerian university examiner generating sample exam questions
for student preparation. These questions must be representative of the actual exam in terms of
format, style, difficulty, and topic coverage.

COURSE INFORMATION:
  Course Title:   {course_title}
  Course Code:    {course_code}
  Academic Level: {academic_level}

TOPIC WEIGHTS (generate questions proportionally across these topics):
{topic_weights}

QUESTION FORMAT REQUIREMENTS (generate exactly this many of each format):
{format_requirements}

DIFFICULTY DISTRIBUTION (Bloom's Taxonomy — use as approximate percentages):
{difficulty_requirements}

TOTAL QUESTIONS TO GENERATE: {total_questions}

{past_paper_section}

GENERATION RULES:
1. Tag each question with its topic (from the list above), format, and Bloom's difficulty level.
2. MCQ: exactly four options labelled A, B, C, D. One clearly correct. Distractors plausible.
3. SHORT_ANSWER: answerable in 2-4 sentences. Provide a concise model answer.
4. CALCULATION: state what working is required. Provide a full model solution.
5. ESSAY: scoped appropriately for the level. List key marking points in correct_answer.
6. TRUE_FALSE: state True or False and include a one-sentence justification.
7. The 'explanation' field must be educational — explain WHY, not just WHAT.
8. Mark allocation guide: MCQ=1-2, SHORT_ANSWER=3-5, CALCULATION=5-10,
   TRUE_FALSE=1-2, ESSAY=10-20.
9. Distribute questions across topics proportionally to the stated weights.
10. Match the academic conventions of Nigerian universities.

Respond with a JSON object containing a 'questions' array of exactly {total_questions} questions.
""",
    input_variables=[
        "course_title", "course_code", "academic_level",
        "topic_weights", "format_requirements", "difficulty_requirements",
        "total_questions", "past_paper_section"
    ]
)


# =========================================================
# PDF EXPORT
# =========================================================

def _get_font_path(filename: str) -> str:
    """Resolves a font file in the local fonts directory and validates existence."""
    font_path = os.path.join("fonts", filename)
    if not os.path.isfile(font_path):
        raise FileNotFoundError(
            f"Required font file not found: '{font_path}'. "
            "Place DejaVu .ttf files in the fonts directory before exporting PDFs."
        )
    return font_path


def register_pdf_fonts(pdf: FPDF) -> None:
    """
    Registers local DejaVu font variants with FPDF before any set_font calls.

    Expected files in fonts/:
      - DejaVuSans.ttf
      - DejaVuSans-Bold.ttf
      - DejaVuSans-Oblique.ttf
      - DejaVuSans-BoldOblique.ttf
    """
    for style, filename in PDF_FONT_FILES.items():
        font_path = _get_font_path(filename)
        try:
            # Production-safe explicit registration, matching fpdf2 guidance.
            pdf.add_font(PDF_FONT_FAMILY, style=style, fname=font_path)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to register font '{filename}' (style='{style or 'regular'}'): {exc}"
            ) from exc


# =========================================================

def generate_pdf(
    questions: list,
    batch_id: str,
    course_code: str,
    course_title: str,
    include_answers: bool = False
) -> bytes:
    """
    Renders a formatted exam-paper-style PDF from a list of question rows.

    Layout:
      - Header with course details and batch ID
      - Questions grouped by format into labelled sections
      - Response spaces sized per format (lined boxes for essays, etc.)
      - Optional answer key section on a new page

    Uses fpdf2 — lightweight pure-Python PDF library, no Java or system
    dependencies. Works cleanly on Windows i5 development machines.

    Returns the PDF as raw bytes for streaming to the caller.
    """
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    
    # Register DejaVu font variants before using them.
    register_pdf_fonts(pdf)

    # ---- Header ----
    pdf.set_font(PDF_FONT_FAMILY, style="B", size=14)
    pdf.cell(0, 10, f"{course_code} - {course_title}", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font(PDF_FONT_FAMILY, size=10)
    pdf.cell(0, 6, f"Sample Examination Questions  |  Batch: {batch_id[:8].upper()}", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.cell(0, 5, f"Date: {datetime.utcnow().strftime('%d %B %Y')}", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(4)
    pdf.set_draw_color(180, 180, 180)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)

    # ---- Student instructions ----
    pdf.set_font(PDF_FONT_FAMILY, style="I", size=9)
    pdf.multi_cell(
        0, 5,
        "These sample questions are representative of the style, format, and difficulty "
        "of the actual examination. Use them for revision purposes. They will not appear "
        "verbatim in the examination paper.",
        align="L"
    )
    pdf.ln(5)

    # ---- Group questions by format for clean section layout ----
    # The ORDER BY in the SQL query already sorts by format priority,
    # so we just group them here for section header rendering.
    sections: dict = {}
    for row in questions:
        fmt = row["format"]
        if fmt not in sections:
            sections[fmt] = []
        sections[fmt].append(row)

    section_labels = {
        "MCQ":          "Section A: Multiple Choice Questions",
        "TRUE_FALSE":   "Section B: True or False",
        "SHORT_ANSWER": "Section C: Short Answer Questions",
        "CALCULATION":  "Section D: Calculation Questions",
        "ESSAY":        "Section E: Essay Questions",
    }

    q_number = 1   # Global question number across all sections

    for fmt, section_qs in sections.items():
        # ---- Section heading with total marks ----
        pdf.set_font(PDF_FONT_FAMILY, style="B", size=11)
        total_marks = sum(r["mark"] for r in section_qs)
        label = section_labels.get(fmt, f"Section: {fmt}")
        pdf.cell(0, 8, f"{label}  [{total_marks} marks]", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

        for row in section_qs:
            # ---- Question number and text ----
            pdf.set_font(PDF_FONT_FAMILY, style="B", size=10)
            pdf.cell(10, 6, f"Q{q_number}.", new_x="RIGHT", new_y="TOP")
            pdf.set_font(PDF_FONT_FAMILY, size=10)

            # Wrap long question text to page width
            for i, line in enumerate(textwrap.wrap(row["question_text"], width=88)):
                if i == 0:
                    pdf.multi_cell(0, 6, line, align="L")
                else:
                    pdf.set_x(22)
                    pdf.multi_cell(0, 6, line, align="L")

            # ---- MCQ options ----
            if fmt == "MCQ" and row["options_json"]:
                try:
                    for opt in json.loads(row["options_json"]):
                        pdf.set_x(22)
                        pdf.set_font(PDF_FONT_FAMILY, size=9)
                        pdf.cell(0, 5, str(opt), new_x="LMARGIN", new_y="NEXT")
                except Exception:
                    pass  # Malformed options — skip gracefully

            # ---- Response spaces ----
            if fmt == "ESSAY":
                # Larger lined space for extended written responses
                pdf.ln(2)
                for _ in range(14):
                    y = pdf.get_y()
                    pdf.line(15, y + 5, 200, y + 5)
                    pdf.ln(7)

            elif fmt in ("SHORT_ANSWER", "CALCULATION"):
                # Smaller space for shorter responses
                pdf.ln(2)
                for _ in range(5):
                    y = pdf.get_y()
                    pdf.line(15, y + 5, 200, y + 5)
                    pdf.ln(7)

            # ---- Marks tag (right-aligned) ----
            pdf.set_font(PDF_FONT_FAMILY, style="I", size=8)
            pdf.cell(0, 4, f"[{row['mark']} mark{'s' if row['mark'] != 1 else ''}]",
                     new_x="LMARGIN", new_y="NEXT", align="R")
            pdf.ln(4)
            q_number += 1

        pdf.ln(4)

    # ---- Answer key (lecturer copy only) ----
    if include_answers:
        pdf.add_page()
        pdf.set_font(PDF_FONT_FAMILY, style="B", size=13)
        pdf.cell(0, 10, "ANSWER KEY", new_x="LMARGIN", new_y="NEXT", align="C")
        pdf.set_font(PDF_FONT_FAMILY, style="I", size=9)
        pdf.cell(0, 5, "Lecturer copy - do not distribute to students", new_x="LMARGIN", new_y="NEXT", align="C")
        pdf.ln(4)
        pdf.line(10, pdf.get_y(), 200, pdf.get_y())
        pdf.ln(6)

        q_number = 1
        for fmt, section_qs in sections.items():
            for row in section_qs:
                # Question number and answer
                pdf.set_font(PDF_FONT_FAMILY, style="B", size=10)
                pdf.cell(12, 6, f"Q{q_number}.", new_x="RIGHT", new_y="TOP")
                pdf.set_font(PDF_FONT_FAMILY, size=10)
                pdf.multi_cell(0, 6, f"Answer: {row['correct_answer']}", align="L")

                # Explanation for each answer
                pdf.set_x(22)
                pdf.set_font(PDF_FONT_FAMILY, style="I", size=9)
                pdf.multi_cell(0, 5, f"Explanation: {row['explanation']}", align="L")
                pdf.ln(3)
                q_number += 1

    return bytes(pdf.output())


# =========================================================
# FASTAPI APPLICATION
# =========================================================

@asynccontextmanager
async def lifespan(_: "FastAPI") -> AsyncIterator[None]:
    """Initialises application resources on startup."""
    print("[Startup] AkadVerse Sample Questions Generator initialising...")
    init_db()
    print("[Startup] Ready. Run: uvicorn sample_questions_generator:app --host 127.0.0.1 --port 8009 --reload")
    yield
    print("[Shutdown] AkadVerse Sample Questions Generator stopped.")


app = FastAPI(
    title="AkadVerse — Sample Questions Generator API",
    description=(
        "Tier 5 faculty tool. Generates exam-representative sample questions "
        "stored in a reusable question bank. Supports PDF export for student handouts."
    ),
    version="1.0",
    lifespan=lifespan
)


# =========================================================
# ENDPOINT 1: Generate questions
# =========================================================

@app.post("/generate-questions", response_model=GenerationSuccessResponse, tags=["Generation"])
async def generate_questions(
    course_title: str = Form(..., description="Full course name, e.g. 'Data Structures and Algorithms'."),
    course_code: str  = Form(..., description="Course code, e.g. 'CSC 301'."),
    academic_level: str = Form(..., description="e.g. '300 Level', 'Postgraduate'."),
    topics_and_weights: str = Form(
        ...,
        description=(
            "Topics and percentage weights, one per line.\n"
            "Format: 'TopicName: Weight'\n"
            "Example:\n"
            "Binary Search Trees: 30\n"
            "Graph Algorithms: 40\n"
            "Sorting Algorithms: 30"
        )
    ),
    format_mix: str = Form(
        ...,
        description=(
            "Number of questions per format, one per line.\n"
            "Format: 'FORMAT: Count'\n"
            "Supported: MCQ, SHORT_ANSWER, ESSAY, CALCULATION, TRUE_FALSE\n"
            "Example:\n"
            "MCQ: 10\n"
            "SHORT_ANSWER: 5\n"
            "ESSAY: 2\n"
            "CALCULATION: 3"
        )
    ),
    difficulty_mix: str = Form(
        default="KNOWLEDGE: 20\nCOMPREHENSION: 30\nAPPLICATION: 30\nANALYSIS: 15\nSYNTHESIS: 5",
        description=(
            "Approximate percentage distribution across Bloom's Taxonomy levels.\n"
            "Format: 'LEVEL: Percentage'"
        )
    ),
    google_api_key: str = Form(..., description="Your Google Gemini API key."),
    past_paper: Optional[UploadFile] = File(
        default=None,
        description=(
            "Optional: Upload a past exam paper PDF. Gemini will calibrate "
            "question style and difficulty to match your exam conventions."
        )
    )
):
    """
    Generates a batch of sample exam questions and stores them in the question bank.

    The total number of questions is the sum of the format_mix counts.
    Topics are weighted proportionally. An optional past exam paper PDF
    can be uploaded to calibrate Gemini's output to your exam style.

    After generation, use GET /export-pdf/{batch_id} to download the PDF handout.
    """

    # -- Parse format_mix --
    # Supports both newline and semicolon separators so Swagger UI's
    # single-line text field works: "MCQ: 10; SHORT_ANSWER: 5; CALCULATION: 3"
    format_counts = {}
    try:
        for line in format_mix.replace(";", "\n").strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(":")]
            if len(parts) != 2:
                raise ValueError(f"Invalid line: '{line}'")
            fmt, count_str = parts[0].upper(), parts[1].strip()
            if fmt not in VALID_FORMATS:
                raise ValueError(f"Unknown format '{fmt}'. Valid: {VALID_FORMATS}")
            format_counts[fmt] = int(count_str)
    except (ValueError, IndexError) as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid format_mix: {e}. Use 'FORMAT: Count' per line."
        )

    total_questions = sum(format_counts.values())

    if total_questions < MIN_QUESTIONS_PER_RUN:
        raise HTTPException(status_code=400, detail="Total questions must be at least 1.")
    if total_questions > MAX_QUESTIONS_PER_RUN:
        raise HTTPException(
            status_code=400,
            detail=f"Total ({total_questions}) exceeds maximum of {MAX_QUESTIONS_PER_RUN} per run."
        )

    # -- Parse topics and weights --
    # Supports both newline and semicolon separators so Swagger UI's
    # single-line text field works: "Binary Search Trees: 30; Graph Algorithms: 40"
    topics_dict = {}
    try:
        for line in topics_and_weights.replace(";", "\n").strip().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(":")]
            if len(parts) != 2:
                raise ValueError(f"Invalid line: '{line}'")
            topics_dict[parts[0]] = parts[1].rstrip("%").strip()
    except (ValueError, IndexError) as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid topics_and_weights: {e}. Use 'Topic: Weight' per line."
        )

    if not topics_dict:
        raise HTTPException(status_code=400, detail="At least one topic must be provided.")

    # -- Extract past paper context if provided --
    past_paper_context = ""
    if past_paper and past_paper.filename:
        if not past_paper.filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="Past paper must be a PDF file.")
        file_bytes = await past_paper.read()
        past_paper_context = extract_past_paper_text(file_bytes, api_key=google_api_key)

    # -- Build the past paper section of the prompt --
    if past_paper_context:
        past_paper_section = (
            "PAST EXAM PAPER CONTEXT (calibrate your output to match this style):\n"
            f"---\n{past_paper_context}\n---\n"
            "Generate questions that feel like they come from the same examiner as above.\n"
        )
    else:
        past_paper_section = (
            "No past paper provided. Generate questions typical for the stated "
            "academic level and appropriate for Nigerian university examinations.\n"
        )

    # -- Format prompt strings --
    topic_weights_str  = "\n".join(f"  - {t}: {w}%" for t, w in topics_dict.items())
    format_req_str     = "\n".join(f"  - {f}: {c} question{'s' if c != 1 else ''}" for f, c in format_counts.items())
    difficulty_req_str = difficulty_mix.strip()

    # -- Invoke Gemini with structured output --
    selected_model = get_valid_model_name(google_api_key)
    llm = ChatGoogleGenerativeAI(
        model=selected_model,
        api_key=google_api_key,
        temperature=0.5   # Moderate: diverse questions without incoherence
    )
    structured_llm = llm.with_structured_output(QuestionBatch, method="json_mode")

    prompt_text = GENERATION_PROMPT.format(
        course_title=course_title,
        course_code=course_code,
        academic_level=academic_level,
        topic_weights=topic_weights_str,
        format_requirements=format_req_str,
        difficulty_requirements=difficulty_req_str,
        total_questions=total_questions,
        past_paper_section=past_paper_section
    )

    print(f"[Generate] {total_questions} questions for {course_code} ({academic_level})")
    print(f"[Generate] Formats: {format_counts} | Model: {selected_model}")

    try:
        result: QuestionBatch = structured_llm.invoke(prompt_text)  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gemini generation failed: {e}")

    if not result.questions:
        raise HTTPException(status_code=500, detail="Gemini returned an empty question list.")

    print(f"[Generate] {len(result.questions)} questions generated. Saving to bank...")

    # -- Save to database --
    batch_id = uuid4().hex[:12]
    try:
        save_batch_to_db(batch_id, course_code, course_title, topics_dict, len(result.questions))
        save_questions_to_db(batch_id, course_code, result.questions)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error while saving questions: {e}")

    # -- Build summary statistics --
    by_format: dict     = {}
    by_difficulty: dict = {}
    for q in result.questions:
        by_format[q.format]         = by_format.get(q.format, 0) + 1
        by_difficulty[q.difficulty] = by_difficulty.get(q.difficulty, 0) + 1

    print(f"[KAFKA MOCK] Published event 'questions.generated' — batch: {batch_id}, course: {course_code}, count: {len(result.questions)}")

    return GenerationSuccessResponse(
        status="success",
        batch_id=batch_id,
        message=(
            f"{len(result.questions)} sample questions generated for {course_code}. "
            f"Use GET /export-pdf/{batch_id} to download the student handout."
        ),
        course_code=course_code,
        total_questions_generated=len(result.questions),
        questions_by_format=by_format,
        questions_by_difficulty=by_difficulty,
        pdf_export_url=f"/export-pdf/{batch_id}"
    )


# =========================================================
# ENDPOINT 2: Query the question bank
# =========================================================

@app.get("/questions", tags=["Question Bank"])
async def get_questions(
    batch_id: Optional[str]    = Query(default=None, description="Filter by batch ID."),
    course_code: Optional[str] = Query(default=None, description="Filter by course code (partial match)."),
    topic: Optional[str]       = Query(default=None, description="Filter by topic (partial match)."),
    format: Optional[str]      = Query(default=None, description="MCQ, SHORT_ANSWER, ESSAY, CALCULATION, or TRUE_FALSE."),
    difficulty: Optional[str]  = Query(default=None, description="KNOWLEDGE, COMPREHENSION, APPLICATION, ANALYSIS, or SYNTHESIS."),
    limit: int  = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0)
):
    """
    Queries the question bank with optional combinable filters.
    All filters are optional. Returns questions with their full content
    including correct answers and explanations.
    """
    try:
        conditions, params = [], []

        if batch_id:
            conditions.append("batch_id = ?")
            params.append(batch_id)
        if course_code:
            conditions.append("course_code LIKE ?")
            params.append(f"%{course_code}%")
        if topic:
            conditions.append("topic LIKE ?")
            params.append(f"%{topic}%")
        if format:
            conditions.append("format = ?")
            params.append(format.upper())
        if difficulty:
            conditions.append("difficulty = ?")
            params.append(difficulty.upper())

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        with get_db_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT question_id, batch_id, course_code, topic, format, difficulty,
                       question_text, options_json, correct_answer, mark, explanation, created_at
                FROM questions
                {where}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                params + [limit, offset]
            ).fetchall()

        if not rows:
            return {"questions": [], "message": "No questions found matching the given filters."}

        return {
            "questions": [
                {
                    "question_id":    row["question_id"],
                    "batch_id":       row["batch_id"],
                    "course_code":    row["course_code"],
                    "topic":          row["topic"],
                    "format":         row["format"],
                    "difficulty":     row["difficulty"],
                    "question_text":  row["question_text"],
                    "options":        json.loads(row["options_json"]) if row["options_json"] else None,
                    "correct_answer": row["correct_answer"],
                    "mark":           row["mark"],
                    "explanation":    row["explanation"],
                    "created_at":     row["created_at"]
                }
                for row in rows
            ],
            "total_returned": len(rows)
        }

    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=f"Database query error: {e}")


# =========================================================
# ENDPOINT 3: Export a batch as a PDF handout
# =========================================================

@app.get("/export-pdf/{batch_id}", tags=["Export"])
async def export_pdf(
    batch_id: str,
    include_answers: bool = Query(
        default=False,
        description=(
            "false = student copy (questions + response spaces only). "
            "true = lecturer copy (questions + answer key section)."
        )
    )
):
    """
    Generates and streams a formatted PDF of all questions in a batch.

    Two versions:
      Student copy (include_answers=false): questions with blank response spaces.
      Lecturer copy (include_answers=true): questions + answer key on a new page.

    The PDF is streamed directly as a download — no file is saved to disk.
    Open the URL directly in your browser or use the link from /generate-questions.
    """
    try:
        with get_db_connection() as conn:
            batch = conn.execute(
                "SELECT course_code, course_title FROM generation_batches WHERE batch_id = ?",
                (batch_id,)
            ).fetchone()

            if not batch:
                raise HTTPException(
                    status_code=404,
                    detail=f"Batch '{batch_id}' not found. Use GET /batches to see all batch IDs."
                )

            # Fetch questions ordered by format section then difficulty
            rows = conn.execute(
                """
                SELECT question_id, format, difficulty, topic, question_text,
                       options_json, correct_answer, mark, explanation
                FROM questions
                WHERE batch_id = ?
                ORDER BY
                  CASE format
                    WHEN 'MCQ'          THEN 1
                    WHEN 'TRUE_FALSE'   THEN 2
                    WHEN 'SHORT_ANSWER' THEN 3
                    WHEN 'CALCULATION'  THEN 4
                    WHEN 'ESSAY'        THEN 5
                    ELSE 6
                  END,
                  difficulty
                """,
                (batch_id,)
            ).fetchall()

        if not rows:
            raise HTTPException(status_code=404, detail=f"No questions found for batch '{batch_id}'.")

        copy_type = "with_answers" if include_answers else "student_copy"
        print(f"[Export] PDF for batch '{batch_id}' ({len(rows)} questions, {copy_type})...")

        pdf_bytes = generate_pdf(
            questions=rows,
            batch_id=batch_id,
            course_code=batch["course_code"],
            course_title=batch["course_title"],
            include_answers=include_answers
        )

        safe_code = batch["course_code"].replace(" ", "_")
        filename  = f"{safe_code}_{batch_id[:8]}_{copy_type}.pdf"

        print(f"[Export] PDF ready — {len(pdf_bytes)} bytes, filename: '{filename}'.")

        return StreamingResponse(
            io.BytesIO(pdf_bytes),
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {e}")


# =========================================================
# ENDPOINT 4: List generation batches
# =========================================================

@app.get("/batches", tags=["Question Bank"])
async def list_batches(
    course_code: Optional[str] = Query(default=None, description="Filter by course code (partial match)."),
    limit: int  = Query(default=10, ge=1, le=50),
    offset: int = Query(default=0, ge=0)
):
    """
    Lists all generation batches, most recent first.
    Each row represents one /generate-questions call.
    Use batch_id to filter questions or export a PDF.
    """
    try:
        with get_db_connection() as conn:
            base_query = """
                SELECT b.batch_id, b.course_code, b.course_title,
                       b.total_questions, b.created_at,
                       COUNT(q.question_id) as questions_stored
                FROM generation_batches b
                LEFT JOIN questions q ON b.batch_id = q.batch_id
                {where}
                GROUP BY b.batch_id
                ORDER BY b.created_at DESC
                LIMIT ? OFFSET ?
            """
            if course_code:
                rows = conn.execute(
                    base_query.format(where="WHERE b.course_code LIKE ?"),
                    (f"%{course_code}%", limit, offset)
                ).fetchall()
            else:
                rows = conn.execute(
                    base_query.format(where=""),
                    (limit, offset)
                ).fetchall()

        if not rows:
            return {"batches": [], "message": "No generation batches found."}

        return {
            "batches": [
                {
                    "batch_id":         row["batch_id"],
                    "course_code":      row["course_code"],
                    "course_title":     row["course_title"],
                    "total_questions":  row["total_questions"],
                    "questions_stored": row["questions_stored"],
                    "created_at":       row["created_at"],
                    "pdf_export_url":   f"/export-pdf/{row['batch_id']}"
                }
                for row in rows
            ],
            "total_returned": len(rows)
        }

    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


# =========================================================
# ENDPOINT 5: Health check
# =========================================================

@app.get("/health", tags=["System"])
async def health_check():
    """Reports service status and question bank statistics."""
    try:
        with get_db_connection() as conn:
            total_q       = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
            total_batches = conn.execute("SELECT COUNT(*) FROM generation_batches").fetchone()[0]
            by_format     = conn.execute(
                "SELECT format, COUNT(*) as count FROM questions GROUP BY format"
            ).fetchall()
    except Exception:
        total_q = total_batches = 0
        by_format = []

    return {
        "status":                    "ok",
        "version":                   "1.0",
        "total_questions_in_bank":   total_q,
        "total_generation_batches":  total_batches,
        "questions_by_format":       {row["format"]: row["count"] for row in by_format},
        "endpoints": {
            "POST /generate-questions":     "Generate a batch of sample questions.",
            "GET  /questions":              "Query the question bank with filters.",
            "GET  /export-pdf/{batch_id}":  "Export a batch as a student PDF handout.",
            "GET  /batches":                "List all generation batches.",
            "GET  /health":                 "This endpoint."
        }
    }


# =========================================================
# Run: uvicorn sample_questions_generator:app --host 127.0.0.1 --port 8009 --reload
# =========================================================
