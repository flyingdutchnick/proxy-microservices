import os, json
from shared.utils import download_from_s3, get_embedding, get_s3_client

S3_BUCKET = os.environ["S3_BUCKET"]

def handler(event, context):
    chunk_key = event["chunk_key"]
    raw_bytes = download_from_s3(S3_BUCKET, chunk_key)
    chunks = json.loads(raw_bytes.decode("utf-8"))

    # For each chunk, get embedding
    embeddings = []
    for idx, chunk in enumerate(chunks):
        embedding = get_embedding(chunk)
        embeddings.append({
            "chunk_index": idx,
            "chunk_text": chunk,
            "embedding": embedding
        })

    # Save embeddings/metadata as JSON to S3
    embed_key = chunk_key.replace('chunks.json', 'embeddings.json')

    s3 = get_s3_client()
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=embed_key,
        Body=json.dumps(embeddings).encode("utf-8"),
        ContentType="application/json"
    )

    event["embed_key"] = embed_key
    event["num_embeddings"] = len(embeddings)
    return event
