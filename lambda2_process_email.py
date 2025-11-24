import boto3
import email
import html2text               
import requests                
import os
import json
from botocore.exceptions import ClientError

s3 = boto3.client('s3')
dynamodb = boto3.resource('dynamodb')

# DynamoDB table for storing user emails
table = dynamodb.Table(os.environ['DYNAMODB_TABLE'])

def lambda_handler(event, context):

    # Read bucket + filename from S3 event trigger
    bucket = event['Records'][0]['s3']['bucket']['name']
    key = event['Records'][0]['s3']['object']['key']

    # Download the raw email file from S3
    response = s3.get_object(Bucket=bucket, Key=key)
    raw_email_bytes = response['Body'].read()

    # Parse email
    msg = email.message_from_bytes(raw_email_bytes)

    # Extract subject
    subject = msg['Subject']

    # Extract sender "To" address → find who this belongs to
    to_email = msg['To']

    # Lookup Telegram user who owns this email
    user_data = table.get_item(Key={'email': to_email})
    telegram_chat_id = user_data['Item']['telegram_id']

    # Extract HTML or plain text body
    body = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()

            if content_type == "text/html":
                html_body = part.get_payload(decode=True).decode("utf-8", "ignore")
                body = html2text.html2text(html_body)

            elif content_type == "text/plain":
                body = part.get_payload(decode=True).decode("utf-8", "ignore")
    else:
        body = msg.get_payload(decode=True).decode("utf-8", "ignore")

    # Send to OpenAI summary API
    openai_url = os.environ["OPENAI_ENDPOINT"]
    openai_key = os.environ["OPENAI_API_KEY"]

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "Summarize the following email:"},
            {"role": "user", "content": f"Subject: {subject}\n\nBody:\n{body}"}
        ]
    }

    summary = requests.post(
        openai_url,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {openai_key}"
        },
        json=payload
    ).json()['choices'][0]['message']['content']

    # Generate S3 pre-signed URL (expires in 12 hours)
    presigned_url = s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket, 'Key': key},
        ExpiresIn=43200  # 12 hours in seconds
    )

    # Message body to send to Telegram
    message_text = f"*New Email Summary*\n\n" \
                   f"*Subject:* {subject}\n\n" \
                   f"*Summary:*\n{summary}\n\n" \
                   f"[Download Raw Email]({presigned_url})\n\n" \
                   f"Click below to deactivate this email address."

    # Inline button to deactivate email address
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "Deactivate Email", "callback_data": f"deactivate:{to_email}"}
            ]
        ]
    }

    # Send to Telegram
    telegram_url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage"

    requests.post(
        telegram_url,
        json={
            "chat_id": telegram_chat_id,
            "text": message_text,
            "parse_mode": "Markdown",
            "reply_markup": keyboard
        }
    )

    return {"statusCode": 200, "body": "Email processed"}

