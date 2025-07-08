import boto3
import os, json

def get_api_key():
    secret_arn = os.environ["LAMBDA_API_KEY"]  # Set this in Lambda env
    region = os.environ.get("AWS_REGION", "us-east-1")
    sm = boto3.client("secretsmanager", region_name=region)
    sec = sm.get_secret_value(SecretId=secret_arn)
    sec_string = sec.get("SecretString")
    # If stored as a JSON object:
    try:
        value = json.loads(sec_string)
        return value["lambda_api_key"]  # or your key name
    except Exception:
        return sec_string  # If stored as plain string

def lambda_handler(event, context):
    # Fetch the allowed key from Secrets Manager
    allowed_key = get_api_key()
    api_key = event["headers"].get("x-api-key")
    print("API key received:", api_key)
    if api_key == allowed_key:
        print("entry has been granted!")
        return {
            "isAuthorized": True,
            "context": {}
        }
    else:
        print("entry has been DENIED!")
        return {
            "isAuthorized": False
        }
