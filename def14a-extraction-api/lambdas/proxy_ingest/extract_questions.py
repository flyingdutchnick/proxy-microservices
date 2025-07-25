# extract_proxy_questions.py
import os, textwrap, tiktoken, psycopg
from typing import Sequence
from openai import OpenAI
from typing import List
from pydantic import BaseModel, Field
import boto3  # adapter registers on import
from pgvector.psycopg import register_vector, Vector
from shared.utils import get_connection

s3 = boto3.client("s3")

from enum import Enum
from pydantic import BaseModel, Field
from typing import List

# ── 1. ENUMS ──────────────────────────────────────────────────────────
class BoardVote(str, Enum):
    FOR = "For"
    AGAINST = "Against"
    ABSTAIN = "Abstain"
    NOT_STATED = "Not Stated"


class QuestionType(str, Enum):
    BOARD_COMPOSITION   = "board_composition"
    COMPENSATION        = "compensation"
    SHAREHOLDER_RIGHTS  = "shareholder_rights"
    ENVIRONMENTAL_SOCIAL = "environmental_social"
    TRANSACTIONS        = "transactions"
    OTHER               = "other"

# ── Queries
# ─────────────────────────────────────────────────────────

MULTI_QUERIES = [
  "proxy proposals table",
  "list of proposals",
  "shareholder proposals",
  "director nominees",
  "proxy voting items",
]

def _embed_queries(queries: list[str]) -> list[list[float]]:
    result = client.embeddings.create(
        input=queries,
        model="text-embedding-3-small",
    )
    return [d.embedding for d in result.data]

def _multi_vector_search(conn, filing_id: int, q_embeds: list[list[float]], k: int):
    register_vector(conn)

    seen, context = set(), []
    with conn.cursor() as cur:
        for e in q_embeds:
            cur.execute(
                """
                SELECT chunk_index, chunk_text,
                       1 - (embedding <#> %s) AS cosine_similarity   -- optional
                FROM   proxy_chunks
                WHERE  filing_id = %s
                ORDER  BY embedding <#> %s           -- <#> = cosine *distance*
                LIMIT  %s
                """,
                (Vector(e), filing_id, Vector(e), k),
            )
            for idx, txt, *_ in cur.fetchall():
                if idx not in seen:
                    seen.add(idx)
                    context.append(txt)
    return context[:k]

# ── 2. SCHEMA ─────────────────────────────────────────────────────────
class ProxyVotingQuestion(BaseModel):
    question_id: str  = Field(description="e.g., '1a'")
    question_text: str
    board_vote_recommendation: BoardVote
    question_type: QuestionType
    is_shareholder_proposal: bool


class ProxyVotingQuestionsResponse(BaseModel):
    proxy_questions: List[ProxyVotingQuestion]

PG_DSN        = os.environ["PG_DSN"]
OPENAI_MODEL  = "gpt-4.1"
RAG_TOP_K     = 20

client   = OpenAI()

def extract_proxy_questions(filing_id: int, company_name: str) -> dict:
    with psycopg.connect(PG_DSN) as conn:
        q_embeds = _embed_queries(MULTI_QUERIES)
        rag_chunks = _multi_vector_search(conn, filing_id, q_embeds, RAG_TOP_K)

    context = "\n\n".join(rag_chunks)

    # The data extraction prompt
    sys_msg = ("You are an expert in parsing U.S. corporate proxy statements (DEF 14A) and extracting proxy voting questions.\n"
    "You are an autonomous agent: your goal is to extract every individual proxy voting question from the proxy statement as structured data. Do not end your turn until you are certain you have exhaustively extracted and verified all proxy voting questions, including those that are grouped, referenced, or require searching supplementary tables or appendices.\n"
    "Process Requirements:\n"
    "- Reflect extensively before and after each action. Plan your next step thoughtfully and review previous outputs for completeness or errors before continuing.\n"
    "- Use all available tools to inspect file content, structure, or tables—never guess or hallucinate.\n"
    "- Do not rely solely on function calls; use reasoning and evidence-gathering to ensure accuracy.\n"
    "- Whenever a table or summary of proposals is provided, treat it as your authoritative source for numbering, question order, and grouping. Return to this table to cross-check before finalizing your answer.\n"
    "Question Extraction Rules:\n"
    "- If any question is grouped (e.g., director slates, bundled shareholder proposals), split it into individual sub-questions and assign a unique sub-identifier (e.g., “1a”, “1b”).\n"
    "- Director slates: If a proposal refers to a group of nominees (e.g., “elect the following 13 nominees”), find the list of names (usually in a table/section like “Nominees”, “Board of Director Nominees\", or “Election of Directors”). Only include nominees and exclude any directors who are retiring. Extract one question per nominee.\n"
    "- Shareholder proposals: If a proposal refers to multiple or grouped shareholder items, search the rest of the document for the detailed proposal text, which is always present, typically in a section or appendix. Extract each proposal as a separate item.\n"
    "- Never assume a summary table omits underlying details—verify all references by inspecting relevant sections.\n"
    "Fields to Extract for Each Question:\n"
    "- `question_id` – Use the exact printed identifier from the proxy (e.g., “Proposal 2”, “Item 4”). For sub-questions, append a letter (“1a”, “1b”…). If missing, assign a sequential number.\n"
    "- `question_text` – Full proposal text. If too long, provide a clear, accurate paraphrase that captures the substance of the proposal.\n"
    "- `board_vote_recommendation` – “For”, “Against”, “Abstain”, or “Not Stated” if missing.\n"
    "- `question_type` – One of: \"board_composition\", \"compensation\", \"shareholder_rights\", \"environmental_social\", \"transactions\", \"other\".\n"
    "- `is_shareholder_proposal` – true if proposed by a shareholder, false otherwise.\n"
    "\n"
    "### Agent Workflow:\n"
    "1. Plan: Read the full proxy statement, focusing first on the main proposals table (if present).\n"
    "2. Extract: List all proposal questions, preserving exact numbering/labels, and split out grouped questions.\n"
    "3. Investigate: For grouped or referenced items, follow links or section references to extract all sub-questions.\n"
    "4. Reflect: After extraction, compare your result to the original proposals table. Double-check that all items and sub-items are included, correctly labeled, and nothing is missed.\n"
    "5. Only finish when all questions have been extracted, numbered correctly, and verified.\n"
    "\n"
    "### Output format:\n"
    "Return only a JSON object with a key \"proxy_questions\" containing an array of question objects. No commentary or explanations.\n"
    "Example:\n"
    "{\n"
    "  \"proxy_questions\": [\n"
    "    {\n"
    "      \"question_id\": \"1a\",\n"
    "      \"question_text\": \"Re-elect Mr. John Doe to the board\",\n"
    "      \"board_vote_recommendation\": \"For\",\n"
    "      \"question_type\": \"board_composition\",\n"
    "      \"is_shareholder_proposal\": false\n"
    "    },\n"
    "    {\n"
    "      \"question_id\": \"1b\",\n"
    "      \"question_text\": \"Elect Mrs. Elizabeth Taylor to the board\",\n"
    "      \"board_vote_recommendation\": \"For\",\n"
    "      \"question_type\": \"board_composition\",\n"
    "      \"is_shareholder_proposal\": false\n"
    "    },\n"
    "    {\n"
    "      \"question_id\": \"2\",\n"
    "      \"question_text\": \"Approve executive compensation (Say-on-Pay)\",\n"
    "      \"board_vote_recommendation\": \"For\",\n"
    "      \"question_type\": \"compensation\",\n"
    "      \"is_shareholder_proposal\": false\n"
    "    },\n"
    "    {\n"
    "      \"question_id\": \"3\",\n"
    "      \"question_text\": \"Shareholder proposal to publish an annual report on political donations\",\n"
    "      \"board_vote_recommendation\": \"Against\",\n"
    "      \"question_type\": \"environmental_social\",\n"
    "      \"is_shareholder_proposal\": true\n"
    "    }\n"
    "  ]\n"
    "}\n")


    user_msg = (
        f"Extract the proxy questions for {company_name}."
    )

    resp = client.responses.parse(
        model=OPENAI_MODEL,
        input=[
            {"role": "system", "content": sys_msg},
            {"role": "user",    "content": user_msg},
            {"role": "assistant", "content": context},
        ],
        text_format=ProxyVotingQuestionsResponse,
    )

    parsed = resp.output_parsed
    if parsed is None:
        return {"success": False, "error": "Structured output null"}

    return {
        "success": True,
        "questions": [q.model_dump() for q in parsed.proxy_questions],
    }

def store_questions(conn, filing_id: int, questions: list[dict]):
    """
    Write/​upsert all extracted questions for a filing.

    Each q in *questions* **must** use the keys produced by
    `extract_proxy_questions()`:

        {
          "question_id": "1a",
          "question_text": "...",
          "board_vote_recommendation": "For",
          "question_type": "board_composition",
          "is_shareholder_proposal": False,
          "embedding": [...]            # optional
        }
    """
    sql = """
    INSERT INTO proxy_questions (
        filing_id,
        question_id,
        question_text,
        board_vote_recommend,
        question_type,
        is_shareholder,
        question_embedding
    )
    VALUES (%s,%s,%s,%s,%s,%s,%s)
    ON CONFLICT (filing_id, question_id)
    DO UPDATE SET
        question_text       = EXCLUDED.question_text,
        board_vote_recommend = EXCLUDED.board_vote_recommend,
        question_type       = EXCLUDED.question_type,
        is_shareholder      = EXCLUDED.is_shareholder,
        question_embedding  = COALESCE(EXCLUDED.question_embedding,
                                        proxy_questions.question_embedding)
    """

    rows = [
        (
            filing_id,
            q["question_id"],
            q["question_text"],
            q["board_vote_recommendation"],
            q["question_type"],
            q["is_shareholder_proposal"],
            q.get("embedding"),          # None keeps column NULL
        )
        for q in questions
    ]

    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()

def handler(event, context):
    """
    Expects these keys in *event* (add them in the previous state):
        filing_id        – integer PK in proxy_filings
        filing_text_key  – S3 key to plain-text file
        company_name     – for prompt
    """
    filing_id       = event["filing_id"]
    text_key        = event["filing_text_key"]
    company_name    = event["company_name"]

    # ── 1. run the extractor (RAG over pgvector)
    result = extract_proxy_questions(
        filing_id   = filing_id,
        company_name= company_name,
    )
    if not result["success"]:
        raise RuntimeError(f"Q-extraction failed: {result['error']}")

    questions = result["questions"]

    # ── 2. persist to Postgres
    with get_connection() as conn, conn.cursor() as cur:
        store_questions(conn, filing_id, questions)

    # ── 3. bubble metadata forward in the SFN state
    event["num_questions"] = len(questions)
    event["db_status"]     = "questions_inserted"
    return event
