import os
import json
import pytest
from unittest.mock import patch, MagicMock

# Patch boto3.client before handler import
boto3_client_patch = patch("boto3.client")
mock_boto3_client = boto3_client_patch.start()

# Setup mock secretsmanager
mock_sm = MagicMock()
mock_boto3_client.return_value = mock_sm
mock_sm.get_secret_value.return_value = {"SecretString": json.dumps({"NBIM_API_KEY": "fake-key"})}

import os
os.environ["NBIM_API_URL"] = "https://api.example.com"
os.environ["NBIM_API_KEY"] = "test"
os.environ["S3_BUCKET"] = "test-bucket"

from handler import list_meetings, fetch_and_upload

# --- list_meetings tests ---

def test_list_meetings_missing_isin():
    event = {"queryStringParameters": {"ticker": "AAPL"}}
    resp = list_meetings(event, None)
    assert resp["statusCode"] == 400
    assert "isin is required" in resp["body"]

def test_list_meetings_missing_ticker():
    event = {"queryStringParameters": {"isin": "US1234567890"}}
    resp = list_meetings(event, None)
    assert resp["statusCode"] == 400
    assert "ticker is required" in resp["body"]

@patch("handler.list_meetings_for_isin")
def test_list_meetings_not_found(mock_list):
    mock_list.return_value = []
    event = {"queryStringParameters": {"isin": "US1", "ticker": "AAPL"}}
    resp = list_meetings(event, None)
    assert resp["statusCode"] == 404
    assert "no company" in resp["body"].lower()

@patch("handler.list_meetings_for_isin")
def test_list_meetings_happy_path(mock_list):
    mock_list.return_value = [{"meetingId": "M1", "meetingDate": "2024-01-01"}]
    event = {"queryStringParameters": {"isin": "US1", "ticker": "AAPL"}}
    resp = list_meetings(event, None)
    assert resp["statusCode"] == 200
    data = json.loads(resp["body"])
    assert isinstance(data, list)
    assert data[0]["meetingId"] == "M1"

# --- fetch_and_upload tests ---

def test_fetch_and_upload_missing_isin():
    event = {"body": json.dumps({"ticker": "AAPL"})}
    resp = fetch_and_upload(event, None)
    assert resp["statusCode"] == 400
    assert "isin is a required field" in resp["body"]

def test_fetch_and_upload_missing_ticker():
    event = {"body": json.dumps({"isin": "US1"})}
    resp = fetch_and_upload(event, None)
    assert resp["statusCode"] == 400
    assert "ticker is required" in resp["body"]

@patch("handler.list_meetings_for_isin")
def test_fetch_and_upload_no_meetings_found(mock_list):
    mock_list.return_value = []
    event = {"body": json.dumps({"isin": "US1", "ticker": "AAPL"})}
    resp = fetch_and_upload(event, None)
    assert resp["statusCode"] == 404
    assert "no meetings found" in resp["body"]

@patch("handler.put_presign")
@patch("handler.download_meeting")
@patch("handler.list_meetings_for_isin")
def test_fetch_and_upload_happy_path(mock_list, mock_download, mock_presign):
    mock_list.return_value = [{"meetingId": "M1"}]
    mock_download.return_value = {"meeting": "data"}
    mock_presign.return_value = "https://presigned-url"
    event = {"body": json.dumps({"isin": "US1", "ticker": "AAPL"})}
    resp = fetch_and_upload(event, None)
    assert resp["statusCode"] == 200
    result = json.loads(resp["body"])
    assert "presigned" in result
    assert result["presigned"] == ["https://presigned-url"]

