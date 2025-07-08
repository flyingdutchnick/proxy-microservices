from fastapi import FastAPI, Request
from mangum import Mangum
import pdfplumber
import io
import requests

app = FastAPI()

@app.post("/extract-text")
async def extract_text(request: Request):
    try:
        body = await request.json()
        file_url = body.get("file_url")
        if not file_url or not isinstance(file_url, str):
            return {"error": "Missing or invalid 'file_url' in request."}

        # Try to download the PDF
        try:
            r = requests.get(file_url)
            r.raise_for_status()
            pdf_bytes = r.content
        except Exception as e:
            return {"error": f"Failed to download file: {str(e)}"}

        # Check it's a real PDF by header
        if not pdf_bytes.startswith(b'%PDF'):
            return {"error": "Downloaded file is not a valid PDF."}

        # Extract text
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = ""
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"

        return {"text": text}

    except Exception as e:
        print("EXCEPTION:", e)
        return {"error": str(e)}

handler = Mangum(app)
