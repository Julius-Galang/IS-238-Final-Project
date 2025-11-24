import json
import boto3
import os
import random
import string
import requests

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['DYNAMODB_TABLE'])

def generate_random_email():
    # Generate random 8-letter address
    rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    domain = os.environ['EMAIL_DOMAIN']
    return f"{rand}@{domain}"

def lambda_handler(event, context):

    body = json.loads(event['body'])

    # Detect callback button (deactivate)
    if "callback_query" in body:
        callback = body['callback_query']
        data = callback['data']  # example: deactivate:random123@domain.com
        chat_id = callback['message']['chat']['id']

        if data.startswith("deactivate:"):
            email_to_delete = data.split("deactivate:")[1]

            # Delete from DynamoDB
            table.delete_item(Key={'email': email_to_delete})

            # Reply to user
            send_message(chat_id, f"Email address *{email_to_delete}* has been deactivated.")
            return {"statusCode": 200}

    # Handle normal chat messages
    message = body.get('message', {})
    chat_id = message['chat']['id']
    text = message.get('text', '').strip()

    if text.lower() == "/start":
        send_message(chat_id, "Welcome! Choose an action:\n\n• /newemail → Generate new email\n• /list → View your emails")
        return

    if text.lower() == "/newemail":
        new_email = generate_random_email()

        # Save to DynamoDB
        table.put_item(Item={
            'email': new_email,
            'telegram_id': chat_id
        })

        send_message(chat_id, f"Your new email address:\n\n `{new_email}`\n\nAll emails sent here will be summarized.")
        return

    if text.lower() == "/list":
        # Query all emails belonging to this user
        items = table.scan()['Items']
        user_emails = [i['email'] for i in items if i['telegram_id'] == chat_id]

        if not user_emails:
            send_message(chat_id, "You have no active email addresses.")
        else:
            send_message(chat_id, " *Your Emails:*\n" + "\n".join(user_emails))
        return

    # Unknown command
    send_message(chat_id, "Unknown command. Use /newemail or /list")

    return {"statusCode": 200}


def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
