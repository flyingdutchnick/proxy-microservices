from shared.utils import update_status
def handler(event, context):
    update_status(event["question_id"], "PROCESSING")
    return event
