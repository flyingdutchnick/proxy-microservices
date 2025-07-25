import os
import boto3
from bs4 import BeautifulSoup
from openai import OpenAI
import tiktoken
import re, html as html_entities
import psycopg
from psycopg.rows import dict_row

UA_HEADERS = {"User-Agent": "DEF14A-Scraper (your@email.com)"}
SEC_JSON = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"

ENCODING = tiktoken.get_encoding("cl100k_base")

s3 = boto3.client("s3")

S3_BUCKET = os.environ["S3_BUCKET"]
PG_DSN = os.environ["PG_DSN"]

# add an openAI client
client = OpenAI()

# utils for voting recommendations
def get_connection():
    """
    psycopg3 automatically commits on `conn.commit()` or when the context
    manager exits.
    """
    return psycopg.connect(PG_DSN, row_factory=dict_row) #type: ignore


# Create one module‐level client
_s3 = boto3.client("s3")

def get_s3_client():
    """
    Returns a singleton S3 client.
    """
    return _s3

def upload_to_s3(content: str, bucket: str, key: str) -> str:
    """
    Uploads UTF-8 text to S3 and returns the s3:// URI.
    """
    client = get_s3_client()
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=content.encode("utf-8"),
        ContentType="text/plain; charset=utf-8"
    )
    return f"s3://{bucket}/{key}"

def download_from_s3(bucket: str, key: str) -> bytes:
    """
    Downloads an object from S3 and returns its raw bytes.
    """
    client = get_s3_client()
    obj = client.get_object(Bucket=bucket, Key=key)
    return obj["Body"].read()

# 5. Embedding wrapper (OpenAI)
def get_embedding(text):
    response = client.embeddings.create(
        input=text,
        model="text-embedding-3-small"
    )
    return response.data[0].embedding

def update_status(qid: int, status: str, err: str | None = None):
    with get_connection() as conn, conn.cursor() as cur:
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
