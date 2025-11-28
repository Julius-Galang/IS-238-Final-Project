import imaplib               
import email                 
import boto3                 
import os                    
from datetime import datetime
import json

# Create S3 client to upload email files
s3 = boto3.client('s3')

def lambda_handler(event, context):

    # Read Gmail username from environment variables (NOT hard-coded)
    gmail_user = os.environ['email_user_name']

    # Read Gmail App Password from environment variables
    gmail_pass = os.environ['email_password']

    # S3 bucket name where emails will be saved
    bucket_name = os.environ['s3_for_email'] # need to make sure this is the name of the bucket to be created in the final

    # Connect to Gmail securely using IMAP
    mail = imaplib.IMAP4_SSL("imap.gmail.com")

    # Log in to Gmail using the app password
    mail.login(gmail_user, gmail_pass)

    # Select inbox (readonly=False so we can mark emails as seen)
    mail.select("inbox")

    # Search for all UNREAD emails
    status, messages = mail.search(None, 'UNSEEN')

    # Convert message IDs to a list
    email_ids = messages[0].split()

    # Loop through each unread email
    for e_id in email_ids:

        # Fetch the email by ID
        status, msg_data = mail.fetch(e_id, "(RFC822)")

        # Extract raw email content
        raw_email = msg_data[0][1]
        
        # Create a timestamped filename for S3 (example: email_20250101_123000.json)
        filename = f"email_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{e_id.decode()}.eml"

        # Upload raw email to S3
        s3.put_object(
            Bucket=bucket_name,
            Key=filename,
            Body=raw_email
        )

    # Close inbox
    mail.close()

    # Logout Gmail session
    mail.logout()

    # Return a success message
    return {
        "statusCode": 200,
        "body": f"Fetched and stored {len(email_ids)} emails."
    }

# To be updated with actual gmail to be used
# The entire lambda functions should be triggered every minute using Amazon EventBridge
# The S3 Bucket should have a lifecyle policy
# Last thing to do it error notifications using SNS


