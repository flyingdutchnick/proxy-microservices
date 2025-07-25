# lambdas/store_rec.py
from shared.utils import get_connection
import json, os

def handler(event, context):
    rec = event["recommendation"]
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO proxy_vote_engine_results
              (filing_id, question_id, voting_recommendation,
              rationale, citation, confidence)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (filing_id, question_id)
            DO UPDATE SET
              voting_recommendation = EXCLUDED.voting_recommendation,
              rationale             = EXCLUDED.rationale,
              citation              = EXCLUDED.citation,
              confidence            = EXCLUDED.confidence,
              updated_at            = now();
            """,
            (
              event["filing_id"],
              event["question_id"],
              rec["voting_recommendation"],
              rec["rationale"],
              rec["citation"],
              rec["confidence"],
            ),
        )
        # still flip the question’s pipeline status to DONE:
        cur.execute(
            "UPDATE proxy_questions SET status='DONE' "
            "WHERE filing_id=%s AND question_id=%s",
            (event["filing_id"], event["question_id"]),
        )
        conn.commit()

    return {"question_id": event["question_id"], "status": "DONE"}
