from shared.utils import update_status
def handler(event, context):
    q = event
    update_status(q["question_id"], "ERROR", err=q.get("error", "unknown"))
    return {"question_id": q["question_id"], "status": "ERROR"}
