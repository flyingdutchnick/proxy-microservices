import os
from bs4 import BeautifulSoup
import requests
import pandas as pd
import json
import importlib.resources as pkg_resources
import shared
from shared.utils import get_connection, upload_to_s3


UA_HEADERS = {"User-Agent": "Nicolaas (nkoster724@gmail.com)"}
SEC_JSON = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"

S3_BUCKET = os.environ["S3_BUCKET"]

def get_filing_text(url: str, ses: requests.Session | None = None) -> tuple[str, str]:
    ses = ses or requests.Session()
    res = ses.get(url, headers=UA_HEADERS, timeout=30)
    res.raise_for_status()

    # feed raw bytes, not res.text
    soup = BeautifulSoup(res.content, "html.parser")  # BS4 sniffs encoding
    text = soup.get_text(" ", strip=True)
    return text, soup.decode()        # soup.decode() == cleaned HTML


# at import‐time or inside your function
_companies_txt = pkg_resources.read_text(shared, "companies.json")
companies_data = json.loads(_companies_txt)

# Build a mapping from CIK (as int or str, zero-padded to 10 digits) to company info
cik_to_company = {
    str(int(row[0])).zfill(10): {
        "name": row[1],
        "ticker": row[2],
        "exchange": row[3],
    }
    for row in companies_data["data"]
}

def get_company_info(cik):
    cik_str = str(int(cik)).zfill(10)
    return cik_to_company.get(cik_str, None)

def _load_json(url: str, ses: requests.Session) -> dict:
    res = ses.get(url, timeout=30)
    res.raise_for_status()
    try:
        return res.json()
    except Exception:
        # Print to CloudWatch for debugging
        print(f"Failed to parse JSON at {url}, status {res.status_code}")
        print("Response text (truncated):", res.text[:500])
        raise

def accession_url(cik, acc, doc):
    return SEC_ARCHIVE.format(cik=int(cik), acc=acc.replace("-", ""), doc=doc)

def _normalise_filings(obj: dict) -> pd.DataFrame:
    if "filings" in obj and "recent" in obj["filings"]:
        rows = obj["filings"]["recent"]
    else:
        lens = {len(v) for v in obj.values() if isinstance(v, list)}
        n = lens.pop()
        rows = [{k: v[i] for k, v in obj.items()} for i in range(n)]
    df = pd.DataFrame(rows)
    if "primaryDocument" not in df.columns:
        df["primaryDocument"] = "primary_doc.htm"
    return df

def collect_all_proxy_filings(
    cik: str | int,
    year: int,
    ses: requests.Session,
) -> pd.DataFrame:
    base = _load_json(SEC_JSON.format(cik=str(cik).zfill(10)), ses)
    frames = [_normalise_filings(base)]
    frames += [
        _normalise_filings(_load_json("https://data.sec.gov/submissions/" + f["name"], ses))
        for f in base.get("filings", {}).get("files", [])
        if int(f["filingFrom"][:4]) >= year
    ]
    df = pd.concat(frames, ignore_index=True)
    # Now filter for all DEF 14A & DEFR14A filings
    mask = (
        pd.to_datetime(df["filingDate"]).dt.year.eq(year) &
        df["form"].str.match(r"^DEFR? ?14A$", case=False, na=False)
    )
    return df[mask].reset_index(drop=True)

def handler(event, context):
    cik   = event["cik"]
    year  = event["year"]
    delete_existing = event.get("delete_existing", False)

    ses = requests.Session()
    ses.headers.update({"User-Agent": "ProxyAdvisor AI (nkoster724@gmail.com)"})
    df  = collect_all_proxy_filings(cik, year, ses)

    if df.empty:
        return {"results": []}

    # ── 1️⃣  Fetch existing proxy_ids ONCE  ───────────────────────────

    # 🔹 Pull the company metadata once per CIK
    company_meta = get_company_info(cik) or {}

    results = []

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT proxy_id
            FROM   proxy_filings
            WHERE  cik = %s
              AND   EXTRACT(year FROM filing_date) = %s
            """,
            (cik, year),
        )
        already_done = {row[0] for row in cur.fetchall()}

        for _, row in df.iterrows():
            proxy_id = f"{cik}_{row['accessionNumber']}"

            if delete_existing and proxy_id in already_done:
                cur.execute(
                    "DELETE FROM proxy_filings WHERE proxy_id = %s",
                    (proxy_id,),
                )
                conn.commit()
                already_done.remove(proxy_id)   # so we re-ingest it

            if proxy_id in already_done:
                continue   # skip everything for this filing

            url = accession_url(cik, row["accessionNumber"], row["primaryDocument"])
            filing_text, html = get_filing_text(url, ses)
            if not filing_text or len(filing_text.split()) < 500:
                continue

            s3_key = f"filings/{cik}/{row['accessionNumber']}/{row['primaryDocument']}"
            upload_to_s3(html, S3_BUCKET, s3_key)

            results.append(
                {
                    "proxy_id":         f"{cik}_{row['accessionNumber']}",
                    "s3_key":           s3_key,
                    "cik":              cik,
                    "accession_number": row["accessionNumber"],
                    "primary_document": row["primaryDocument"],
                    "filing_date":      row["filingDate"],
                    # new fields
                    "company_name":     company_meta.get("name"),
                    "ticker":           company_meta.get("ticker"),
                    "exchange":         company_meta.get("exchange"),
                }
            )

    return {"results": results}
