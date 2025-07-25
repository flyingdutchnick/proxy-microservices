# lambdas/get_pending_questions.py
import json, os
from shared.utils import get_connection

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

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()

def handler(event, context):
    # When called from Step Functions we may pass {"question_ids":[...]} in input

    rows = fetch_pending(limit=100, specific_ids=event.get("question_ids"))
    return [dict(row) for row in rows]   # RowMapping → plain dict
