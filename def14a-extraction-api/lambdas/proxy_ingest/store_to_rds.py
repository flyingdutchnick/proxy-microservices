import json, os, boto3
from shared.utils import get_connection
from pgvector.psycopg import register_vector

s3 = boto3.client("s3")

S3_BUCKET = os.environ["S3_BUCKET"]

def store_filing(conn, filing_record):
    with conn.cursor() as cur:
        columns = ','.join(filing_record.keys())
        placeholders = ','.join(['%s'] * len(filing_record))
        update_stmt = ','.join([f"{col}=EXCLUDED.{col}" for col in filing_record.keys() if col != 'proxy_id'])
        sql = (
            f"INSERT INTO proxy_filings ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT (proxy_id) DO UPDATE SET {update_stmt} "
            f"RETURNING id"
        )
        cur.execute(sql, tuple(filing_record.values()))
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("INSERT/UPDATE returned no row – check proxy_filings PK")
        return int(row["id"])

def handler(event, context):
    embed_key = event["embed_key"]
    obj = s3.get_object(Bucket=S3_BUCKET, Key=embed_key)
    embeddings = json.loads(obj["Body"].read().decode("utf-8"))

    filing_metadata = {
        "proxy_id": event["proxy_id"],
        "cik": event["cik"],
        "accession_number": event["accession_number"],
        "primary_document": event["primary_document"],
        "filing_date": event["filing_date"]
    }

    with get_connection() as conn:
        register_vector(conn)
        filing_id = store_filing(conn, filing_metadata)

        # Prevent duplicate chunks if this filing_id already existed
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM proxy_chunks WHERE filing_id = %s",
                (filing_id,)
            )

        # Bulk-prepare records (Vector from pgvector)
        chunk_records = [
            (
                filing_id,
                chunk["chunk_index"],
                chunk["chunk_text"],
                chunk["embedding"],
            )
            for chunk in embeddings
        ]

        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO proxy_chunks
                    (filing_id, chunk_index, chunk_text, embedding)
                VALUES (%s, %s, %s, %s)
                """,
                chunk_records,
            )

        conn.commit()   # one commit for both tables

    # -- Return enriched event ---------------------------------------------- #
    event.update(
        {
            "db_status":  "chunks_inserted",
            "filing_id":  filing_id,
            "num_chunks": len(chunk_records),
        }
    )
    return event
