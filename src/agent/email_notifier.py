import os
import smtplib
from email.message import EmailMessage
from dotenv import dotenv_values
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

class EmailNotifier:
    def __init__(self):
        # Allow loading from environment variables or .env file (useful for VPS vs local)
        env = dotenv_values(ROOT / ".env")
        
        self.smtp_server = os.environ.get("SMTP_SERVER") or env.get("SMTP_SERVER", "smtp.gmail.com")
        self.smtp_port = int(os.environ.get("SMTP_PORT") or env.get("SMTP_PORT", "587"))
        self.smtp_user = os.environ.get("SMTP_USER") or env.get("SMTP_USER")
        self.smtp_password = os.environ.get("SMTP_PASSWORD") or env.get("SMTP_PASSWORD")
        self.notification_email = os.environ.get("NOTIFICATION_EMAIL") or env.get("NOTIFICATION_EMAIL")
        
        if not self.smtp_user or not self.smtp_password or not self.notification_email:
            raise ValueError("SMTP_USER, SMTP_PASSWORD, or NOTIFICATION_EMAIL not found in .env")

    def send_alert(self, subject: str, content: str):
        msg = EmailMessage()
        msg.set_content(content)
        msg["Subject"] = subject
        msg["From"] = self.smtp_user
        msg["To"] = self.notification_email

        try:
            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(self.smtp_user, self.smtp_password)
                server.send_message(msg)
            print(f"[EmailNotifier] Sent alert: {subject}")
            return True
        except Exception as e:
            print(f"[EmailNotifier] Failed to send email: {e}")
            return False

if __name__ == "__main__":
    notifier = EmailNotifier()
    notifier.send_alert("Test Alert", "This is a test message from Aegis EmailNotifier.")
