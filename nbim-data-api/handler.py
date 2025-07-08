import os, json, time, random, requests, boto3, logging
from botocore.exceptions import ClientError

class NBIMError(Exception):
    def __init__(self, payload, status: int):
        super().__init__(str(payload))
        self.payload = payload
        self.status  = status

def get_nbim_api_key():
    secret_arn = os.environ["NBIM_API_KEY"]  # ARN of the secret in Lambda env
    region = os.environ.get("AWS_REGION", "us-east-1")
    sm = boto3.client("secretsmanager", region_name=region)
    sec = sm.get_secret_value(SecretId=secret_arn)
    sec_json = sec.get("SecretString")
    try:
        secret = json.loads(sec_json)
        return secret.get("NBIM_API_KEY", sec_json)
    except Exception:
        return sec_json


# ─── constants from env vars ──────────────────────────────────────────────
NBIM_API_URL = os.environ["NBIM_API_URL"]
NBIM_API_KEY = get_nbim_api_key()
S3_BUCKET    = os.environ["S3_BUCKET"]
S3_PREFIX    = "nbim_meetings"
EXPIRY       = int(os.environ.get("SIGNED_URL_EXPIRY", "604800"))

HDRS = {"x-api-key": NBIM_API_KEY}
s3   = boto3.client("s3")

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ─── tiny helpers ─────────────────────────────────────────────────────────
def response(status, payload):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }

def retryable(fn):
    """Simple linear back-off retry decorator (3 tries)."""
    def wrapper(*args, **kw):
        for attempt in range(3):
            try:
                return fn(*args, **kw)
            except (requests.RequestException, ClientError) as e:
                if attempt == 2:
                    raise
                time.sleep(0.6 * (attempt + 1) + random.random())
    return wrapper

# ─── NBIM helper calls ────────────────────────────────────────────────────
def _get_json(url: str) -> dict:
    """GET + JSON decode.
    • raises NBIMError on HTTP 4xx/5xx
    • raises NBIMError on NBIM’s {'message': 'Not found', 'status': 'warning'} payload
    """
    try:
        r = requests.get(url, headers=HDRS, timeout=10)
        r.raise_for_status()          # classic HTTP error handling (4xx/5xx)

        data = r.json()

        # NBIM sometimes sends 200 with: {'meeting':'', 'message':'Not found', 'status':'warning'}
        if data.get("status") == "warning" and data.get("message") == "Not found":
            raise NBIMError(data, 200)

        return data

    except requests.HTTPError as e:
        try:
            payload = e.response.json()
        except ValueError:
            payload = {"error": e.response.text}

        raise NBIMError(payload, e.response.status_code)


def list_meetings_for_isin(isin: str, ticker: str, limit: int = 5):
    """Return the newest <limit> meetingIds for this ISIN."""
    url = f"{NBIM_API_URL}/v1/query/ticker/{ticker}"
    companies = _get_json(url).get("companies", [])
    company   = next((c for c in companies if c.get("isin") == isin), None)
    if not company:
        return []

    meetings = sorted(
        company.get("meetings", []),
        key=lambda m: m["meetingDate"],
        reverse=True
    )[:limit]
    return meetings

def download_meeting(meeting_id):
    url = f"{NBIM_API_URL}/v1/query/meeting/{meeting_id}"
    return _get_json(url)

def put_presign(payload: dict, key: str):
    body = json.dumps(payload).encode("utf-8")
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=body,
        ContentType="application/json",
    )
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=EXPIRY,
    )


# ─── Lambda #1: GET /nbim/meetings ────────────────────────────────────────
@retryable
def list_meetings(event, _):
    qs      = event.get("queryStringParameters") or {}
    isin    = qs.get("isin")
    ticker  = qs.get("ticker")
    limit   = int(qs.get("limit", 5) or 5)

    if not isin:
        return response(400, {"error": "isin is required"})
    if not ticker:
        return response(400, {"error": "ticker is required"})

    try:
        meetings = list_meetings_for_isin(isin, ticker, limit)

        # ── NEW: handle “ticker OK, isin doesn’t match” ───────────────
        if not meetings:      # ← the only time you ever get []
            msg = {
                "message": (
                    f"Ticker '{ticker}' exists but no company "
                    f"with ISIN '{isin}' mathches the ticker '{ticker}'."
                ),
                "status": "warning"
            }
            return response(404, msg)           # or 200 if you prefer

        return response(200, meetings)

    except NBIMError as e:
        # NBIM’s own “Not found / warning” payload is still bubbled
        logger.warning("NBIM error for %s/%s → %s", isin, ticker, e.payload)
        return response(e.status, e.payload)


# ─── Lambda #2: POST /nbim/meetings/fetch ────────────────────────────────
@retryable
def fetch_and_upload(event, _):
    body = json.loads(event.get("body") or "{}")
    isin      = body.get("isin")
    ticker    = body.get("ticker")
    meeting_ids = body.get("meetingIds")   # optional explicit list
    num       = int(body.get("num", 5))

    if not isin:
        return response(400, {"error": "isin is a required field. Check with your service provider if it's missing"})

    if not ticker:
        return response(400, {"error": "ticker is required to use this excellent service"})

    ids = meeting_ids or [
        m["meetingId"] for m in list_meetings_for_isin(isin, ticker, num)
    ]
    if not ids:
        return response(404, {"error": "no meetings found"})

    presigned = []
    for m_id in ids:
        payload  = download_meeting(m_id)
        key      = f"{S3_PREFIX}/{ticker}_{isin}/{m_id}.json"
        url      = put_presign(payload, key)
        presigned.append(url)

    return response(200, {"presigned": presigned})
