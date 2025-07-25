import os, boto3
from bs4 import BeautifulSoup
import re, html as html_entities

s3 = boto3.client("s3")

S3_BUCKET = os.environ["S3_BUCKET"]

## super advanced get_filing_text_from_html
def get_filing_text_from_html(html_bytes: bytes) -> str:
    """
    Robust HTML → plain-text for SEC filings.

    1. Try strict UTF-8.
    2. If it blows up, fall back to Windows-1252 *with* 'replace'.
    3. Strip <script>/<style>/<noscript>, unescape entities, collapse whitespace.
    """
    try:
        html_str = html_bytes.decode("utf-8")          # strict
    except UnicodeDecodeError:
        print('going off the rails with some encoding errors, call the helpdesk for support')
        html_str = html_bytes.decode("windows-1252", errors="replace")

    soup = BeautifulSoup(html_str, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text = soup.get_text(" ", strip=True)
    text = html_entities.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def handler(event, context):
    cik = event["cik"]
    accession_number = event["accession_number"]
    s3_key_html = event["s3_key"]

    # Fetch HTML from S3
    obj = s3.get_object(Bucket=S3_BUCKET, Key=s3_key_html)
    html_bytes = obj["Body"].read()

    # Extract plain text
    text = get_filing_text_from_html(html_bytes)

    # Construct a proper S3 key for the extracted text
    text_s3_key = f"filings/{cik}/{accession_number}/filing_text.txt"

    # Save plain text to S3 (overwrite if already exists)
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=text_s3_key,
        Body=text.encode("utf-8"),
        ContentType="text/plain; charset=utf-8"
    )
    # Pass the pointer to the text for downstream
    event["filing_text_key"] = text_s3_key
    return event
