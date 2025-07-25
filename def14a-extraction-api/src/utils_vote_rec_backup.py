# utils_vote_rec.py
import os, datetime, json
import psycopg
from psycopg.rows import dict_row

PG_DSN = os.environ["PG_DSN"]

def _get_conn():
    """
    psycopg3 automatically commits on `conn.commit()` or when the context
    manager exits.
    """
    return psycopg.connect(PG_DSN, row_factory=dict_row) #type: ignore


def fetch_pending(limit=100, specific_ids=None):
    where_extra, params = "", []
    if specific_ids:
        where_extra = "AND question_id = ANY(%s)"
        params.append(specific_ids)

    params.append(limit)

    sql = f"""
        SELECT
            q.question_id,
            q.filing_id,
            q.question_text AS proxy_question,
            TO_CHAR(f.filing_date, 'FMMonth DD YYYY') AS formatted_date,  -- ★
            q.status
        FROM   proxy_questions q
        JOIN   proxy_filings   f ON f.id = q.filing_id
        WHERE  q.status IN ('NEW','ERROR')
        {where_extra}
        LIMIT  %s
        FOR UPDATE SKIP LOCKED
    """

    with _get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()

def update_status(qid: int, status: str, err: str | None = None):
    with _get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE proxy_questions
            SET status = %s,
                last_attempt = NOW(),
                error_msg   = %s
            WHERE question_id = %s
            """,
            (status, err, qid),
        )
        conn.commit()
