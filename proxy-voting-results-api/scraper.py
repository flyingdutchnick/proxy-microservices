from __future__ import annotations

import re
import time
from typing import Any, Dict, List

import pandas as pd
import requests
from bs4 import BeautifulSoup, Tag
from openai import OpenAI
from pydantic import BaseModel, Field
from tqdm import tqdm
import boto3, os, json

def get_openai_api_key():
    secret_arn = os.environ["OPENAI_API_KEY"]
    region = os.environ.get("AWS_REGION", "us-east-1")
    sm = boto3.client("secretsmanager", region_name=region)
    sec = sm.get_secret_value(SecretId=secret_arn)
    sec_json = sec.get("SecretString")
    try:
        secret = json.loads(sec_json)
        return secret["OPENAI_API_KEY"]  # Stored as {"OPENAI_API_KEY": "..."}
    except Exception:
        return sec_json  # If stored as just the string

os.environ["OPENAI_API_KEY"] = get_openai_api_key()
client = OpenAI()  # relies on OPENAI_API_KEY

PROMPT = """
You will be given a document containing an 8-K EDGAR filing. Your task is to **identify and extract all shareholder voting results** disclosed in the document. These results typically appear in connection with Item 5.07 (Submission of Matters to a Vote of Security Holders) and include all resolutions voted on, vote tallies, and other relevant data.

---

### 🔍 Extraction Steps

1. **Locate Voting Results Section**
   Identify the section(s) reporting the outcome of shareholder votes. This may appear as paragraphs, bullet points, or tables—typically under Item 5.07 or similar language.

2. **Extract Key Fields for Each Resolution**
   For each voting matter disclosed, extract:

   * `"questionId"`: A label or identifier (e.g., "1a" for director elections, "2", "3", etc. for other proposals)
   * `"resolution"`: A brief description of the proposal or item voted on
   * `"total_votes"`: Sum of all shares voted (including For, Against, Abstain, etc.)
   * `"for"`: Number of shares voted in favor
   * `"against"`: Number of shares voted against
   * `"abstentions"`: Number of abstentions or withheld votes
   * `"broker_non_votes"`: Number of broker non-votes (if applicable)

3. **Standardize Output**
   Return a JSON object with three keys: "ticker", "meeting_date" (in YYYY-MM-DD format) and "voting_results", whose value is the array of all vote objects found in the document. Do NOT return markdown.

---

### ✅ Output Format

```json
{
"ticker": "F",
"meeting_date": "2022-06-01",
"voting_results": [
  {
    "questionId": "1a",
    "resolution": "Election of Jane Smith as Class I Director",
    "total_votes": 1000000,
    "for": 850000,
    "against": 120000,
    "abstentions": 30000,
    "broker_non_votes": 0
  },
  {
    "questionId": "2",
    "resolution": "Ratification of PricewaterhouseCoopers LLP as Independent Auditors for the Fiscal Year Ending December 31, 2022",
    "total_votes": 950000,
    "for": 880000,
    "against": 50000,
    "abstentions": 20000,
    "broker_non_votes": 2000
  }
]}
```

---

### 📝 Notes

* Director elections often use sub-labels (e.g., "1a", "1b", "1c") per nominee. Other resolutions (e.g., executive compensation, auditor ratification) may be numbered sequentially (e.g., "2", "3").
* Totals should be computed only if all vote types are available; otherwise, use `null` or exclude the field.
* Maintain numerical accuracy. Use `0` if a category is explicitly listed as zero; use `null` if not reported.
* Disregard formatting styles (tables vs. bullet points) and focus on semantic meaning.
""".strip()

# ────────────────────────────────
# 0.  Data‑models (unchanged)
# ────────────────────────────────
class VotingResult(BaseModel):
    questionId: str
    resolution: str
    total_votes: int | None = None
    for_: int = Field(..., alias="for")  # preserve “for” key
    against: int
    abstentions: int
    broker_non_votes: int

class VotingResults(BaseModel):  # top‑level *object*
    ticker: str
    meeting_date: str
    voting_results: List[VotingResult]

# ────────────────────────────────
# 1.  Constants & helpers
# ────────────────────────────────
UA_HEADERS = {"User-Agent": "Item507-Scraper (nkoster724@gmail.com)"}

SEC_JSON = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"

itm507_re = re.compile(r"\bITEM\s*5\.07\b", re.I)


# •–––– JSON loader (tiny & reusable) ––––•
def _load_json(url: str, ses: requests.Session) -> Dict[str, Any]:
    """GET & `.json()` with 30‑second timeout."""
    return ses.get(url, timeout=30).json()


# •–––– filings ⇢ DataFrame normaliser ––––•
def _normalise_filings(obj: Dict[str, Any]) -> pd.DataFrame:
    """Return a tidy DataFrame from either SEC‑JSON shape."""

    # Shape 1: {"filings": {"recent": […]}}
    if "filings" in obj and "recent" in obj["filings"]:
        rows = obj["filings"]["recent"]

    # Shape 2: column‑wise lists
    else:
        lens = {len(v) for v in obj.values() if isinstance(v, list)}
        if len(lens) != 1:
            raise ValueError("Column‑wise filing lists have unequal lengths")
        n = lens.pop()
        rows = [{k: v[i] for k, v in obj.items()} for i in range(n)]

    df = pd.DataFrame(rows)
    # guarantee a doc name
    if "primaryDocument" in df.columns:
        df["primaryDocument"] = df["primaryDocument"].fillna("primary_doc.htm")
    else:
        df["primaryDocument"] = "primary_doc.htm"
    return df


# •–––– 8‑K slice by year ––––•
def yearly_8k_slice(
    cik: str | int,
    year: int,
    ses: requests.Session,
    form_pattern: str = r"8-K",
) -> pd.DataFrame:
    """Return all 8‑K rows *inside* `year`, aggregating chunk files."""

    base = _load_json(SEC_JSON.format(cik=str(cik).zfill(10)), ses)

    # collect main + chunk files (only those touching `year`)
    frames = [_normalise_filings(base)]
    frames += [
        _normalise_filings(_load_json("https://data.sec.gov/submissions/" + f["name"], ses))
        for f in base.get("filings", {}).get("files", [])
        if int(f["filingFrom"][:4]) >= year  # stop once chunks pre‑date year
    ]

    df = pd.concat(frames, ignore_index=True)

    mask = (
        pd.to_datetime(df["filingDate"]).dt.year.eq(year) &
        df["form"].str.contains(form_pattern, case=False, na=False)
    )
    return df[mask].reset_index(drop=True)

# •–––– HTML cleaner ––––•

def cleaned_item507_html(url: str, ses: requests.Session) -> str | None:
    """Download & strip inline styles *iff* the doc contains Item 5.07."""
    res = ses.get(url)
    if res.status_code != 200:
        return None

    soup = BeautifulSoup(res.text, "html.parser")
    if not itm507_re.search(soup.get_text(" ", strip=True)):
        return None

    for tag in soup.find_all(True):
      if isinstance(tag, Tag):
          tag.attrs.pop("style", None)
    return str(soup)


# •–––– helper to build accession URL ––––•

def accession_url(cik: str | int, acc: str, doc: str) -> str:
    return SEC_ARCHIVE.format(cik=int(cik), acc=acc.replace("-", ""), doc=doc)
# ────────────────────────────────
# 2.  Main orchestrator
# ────────────────────────────────

def extract_item507_votes(
    cik: str | int,
    company: str,
    year: int,
    polite_delay: float = 0.2,
):
    ses = requests.Session(); ses.headers.update(UA_HEADERS)
    df = yearly_8k_slice(cik, year, ses)
    df = df[df["items"].str.contains(r"5\.07", na=False)]
    if df.empty:
        raise RuntimeError(f"No voting results (8K - Item 5.07) filings found for {cik} in {year}")

    # As before: pick latest per meeting
    if "meetingDate" in df.columns:
        group_col = "meetingDate"
    elif "filingDate" in df.columns:
        group_col = "filingDate"
    else:
        raise RuntimeError("No meetingDate or filingDate column found in filings DataFrame")

    df_sorted = df.sort_values("accessionNumber", ascending=False)
    df_latest = df_sorted.groupby(group_col, as_index=False).first()

    results = []
    for _, row in df_latest.iterrows():
        html = cleaned_item507_html(
            accession_url(cik, row["accessionNumber"], row["primaryDocument"]),
            ses,
        )
        if not html:
            continue

        user_msg = f"Extract the shareholder voting results for {company} from this document:\n\n" + html
        resp = client.responses.parse(
            model="gpt-4.1-mini",
            input=[{"role": "system", "content": PROMPT}, {"role": "user", "content": user_msg}],
            text_format=VotingResults,
        )

        data = resp.output_parsed

        if data and getattr(data, "ticker", None) and getattr(data, "meeting_date", None):
            results.append(data)
        else:
            print(f"Warning: Missing or invalid parsed output, skipping. Data: {data}")

        time.sleep(polite_delay)

    return results
