import boto3
import os, json, uuid, psycopg
from psycopg.rows import dict_row
from datetime import datetime, timezone
from scraper import extract_item507_votes, VotingResults
from typing import Dict, Any

def get_pg_dsn():
    # Expect your Lambda env var: PG_SECRET_ARN=arn:aws:secretsmanager:region:acct:secret:your_secret_name
    secret_arn = os.environ["PG_SECRET_ARN"]
    region = os.environ.get("AWS_REGION", "us-east-1")
    sm = boto3.client("secretsmanager", region_name=region)
    sec = sm.get_secret_value(SecretId=secret_arn)
    sec_json = sec.get("SecretString")
    dsn = json.loads(sec_json)["pg_dsn"]
    return dsn

def _insert_votes(conn, vr: VotingResults):
    sql = """
      INSERT INTO votes (cik,ticker,meeting_date,question_id,resolution,
                         total_votes,for_votes,against_votes,abstentions,
                         broker_non_votes,raw_json)
      VALUES (%(cik)s,%(ticker)s,%(meeting_date)s,%(questionId)s,%(resolution)s,
              %(total_votes)s,%(for)s,%(against)s,%(abstentions)s,
              %(broker_non_votes)s,%(raw)s)
      ON CONFLICT (cik,meeting_date,question_id) DO NOTHING;
    """
    with conn.cursor() as cur:
        for row in vr.voting_results:
            payload = row.model_dump(by_alias=True) | {
                "cik": vr.ticker,              # change if you store true CIK
                "ticker": vr.ticker,
                "meeting_date": vr.meeting_date,
                "raw": json.dumps(row.model_dump(by_alias=True))
            }
            print("payload about to be saved to the db:", payload)
            cur.execute(sql, payload)
    conn.commit()

def create_job_handler(event, _ctx):
    body = json.loads(event.get("body", "{}"))
    cik = body.get("cik")
    year = body.get("year")
    if not cik or not year:
        return {"statusCode": 400, "body": "Missing cik/year"}

    params = {"cik": cik, "year": year}
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with psycopg.connect(get_pg_dsn(), row_factory=dict_row) as conn: # type: ignore[arg-type]
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO jobs (id, params, status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
            """, (job_id, json.dumps(params), 'pending', now, now))
            conn.commit()

    return {
        "statusCode": 201,
        "body": json.dumps({"job_id": job_id, "status": "pending"})
    }

def get_job_handler(event, _ctx):
    job_id = event.get("pathParameters", {}).get("job_id")
    if not job_id:
        return {"statusCode": 400, "body": "Missing job_id"}

    with psycopg.connect(get_pg_dsn(), row_factory=dict_row) as conn: # type: ignore[arg-type]
        with conn.cursor() as cur:
            cur.execute("SELECT id, status, result, error, created_at, updated_at FROM jobs WHERE id = %s", (job_id,))
            job = cur.fetchone()
    if not job:
        return {"statusCode": 404, "body": "Job not found"}

    # Return job status and results
    out = {
        "job_id": str(job["id"]), # type: ignore
        "status": job["status"], # type: ignore
        "result": job["result"],# type: ignore
        "error": job["error"],# type: ignore
        "created_at": job["created_at"].isoformat(),# type: ignore
        "updated_at": job["updated_at"].isoformat(),# type: ignore
    }
    return {"statusCode": 200, "body": json.dumps(out)}

def worker_handler(event, _ctx):
    with psycopg.connect(get_pg_dsn(), row_factory=dict_row) as conn:# type: ignore
        with conn.cursor() as cur:
            # Fetch one pending job (oldest)
            cur.execute("SELECT * FROM jobs WHERE status = 'pending' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED")
            job = cur.fetchone()
            if not job:
                print("No pending jobs.")
                return {"statusCode": 200, "body": "No jobs"}

            job_id = job["id"] # type: ignore
            params = job["params"] # type: ignore
            if isinstance(params, str):
                params = json.loads(params)
            print("Working job:", job_id, params)

            # Mark as running
            cur.execute("UPDATE jobs SET status = %s, updated_at = now() WHERE id = %s", ('running', job_id))
            conn.commit()

            try:
                results = extract_item507_votes(params["cik"], company="", year=int(params["year"]), polite_delay=0)
                for data in results:
                  print("Number of voting_results to insert:", len(data.voting_results))
                  print("First item:", data.voting_results[0] if data.voting_results else "None")
                  _insert_votes(conn, data)
                cur.execute(
                    "UPDATE jobs SET status = %s, result = %s, updated_at = now() WHERE id = %s",
                    ('done', json.dumps([r.model_dump(by_alias=True) for r in results]), job_id)
                )
                conn.commit()
            except Exception as e:
                cur.execute(
                    "UPDATE jobs SET status = %s, error = %s, updated_at = now() WHERE id = %s",
                    ('error', str(e), job_id)
                )
                conn.commit()
    return {"statusCode": 200, "body": "Worker ran"}
