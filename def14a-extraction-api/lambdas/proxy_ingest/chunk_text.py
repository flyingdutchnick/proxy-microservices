import os
import json
import tiktoken

from shared.utils import upload_to_s3, download_from_s3

ENCODING = tiktoken.get_encoding("cl100k_base")
S3_BUCKET = os.environ["S3_BUCKET"]

def tokenize(text: str) -> list[int]:
    return ENCODING.encode(text)

def detokenize(tokens: list[int]) -> str:
    return ENCODING.decode(tokens)

def chunk_text_tokens(
    text: str,
    max_tokens: int = 800,
    overlap_tokens: int = 200,
) -> list[str]:
    assert 0 < overlap_tokens < max_tokens, "overlap must be smaller than window"
    token_ids = tokenize(text)
    stride    = max_tokens - overlap_tokens
    chunks    = []

    for start in range(0, len(token_ids), stride):
        end   = start + max_tokens
        chunk = detokenize(token_ids[start:end])
        chunks.append(chunk)
        if end >= len(token_ids):
            break

    return chunks

def handler(event, context):
    # 1. Get the plain-text file key
    text_key = event["filing_text_key"]

    # 2. Download it via shared util
    raw_bytes   = download_from_s3(S3_BUCKET, text_key)
    filing_text = raw_bytes.decode("utf-8")

    # 3. Chunk into overlapping token windows
    chunks = chunk_text_tokens(
        filing_text,
        max_tokens=800,
        overlap_tokens=200
    )

    # 4. Serialize & upload via shared util
    chunk_key = text_key.replace("filing_text.txt", "chunks.json")
    upload_to_s3(
        content = json.dumps(chunks, ensure_ascii=False),
        bucket  = S3_BUCKET,
        key     = chunk_key
    )

    # 5. Return updated event
    event.update({
        "chunk_key":   chunk_key,
        "num_chunks":  len(chunks),
    })
    return event
