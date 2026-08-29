import os
import json
import logging
import asyncio
import socket
from pathlib import Path

# --- DNS Patch for api.telegram.org ---
_orig_getaddrinfo = socket.getaddrinfo
def custom_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host == 'api.telegram.org':
        return _orig_getaddrinfo('149.154.166.110', port, family, type, proto, flags)
    return _orig_getaddrinfo(host, port, family, type, proto, flags)
socket.getaddrinfo = custom_getaddrinfo
# --------------------------------------

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from google.antigravity import Agent, LocalAgentConfig, CapabilitiesConfig

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load config
load_dotenv(Path(__file__).parent / ".env")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
USERS_JSON_PATH = Path(__file__).parent / "telegram_users.json"
DATASET_PATH = Path(__file__).parent.parent / "datasets" / "Global_100k_Investment_Database.csv"
REPORTS_PATH = Path(__file__).parent.parent / "dad_reports"

def load_allowed_users():
    if not USERS_JSON_PATH.exists():
        logger.warning("No telegram_users.json found, denying all access.")
        return set()
    with open(USERS_JSON_PATH, "r") as f:
        data = json.load(f)
    return {str(u["chat_id"]) for u in data.get("users", []) if u.get("enabled")}

ALLOWED_USERS = load_allowed_users()

# Antigravity Agent Configuration
AGENT_CONFIG = LocalAgentConfig(
    system_instructions=f"""
You are Dad's Investment Strategy Assistant.
You have read access to the local investment database at {DATASET_PATH}.
You also have access to previously generated reports at {REPORTS_PATH}.
Use your tools to read files and synthesize investment insights when asked.
Keep responses concise, accurate, and suitable for a Telegram chat interface.
""",
    model="gemini-3.6-flash-high",
    capabilities=CapabilitiesConfig(allow_read=True)
)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id not in ALLOWED_USERS:
        logger.info(f"Unauthorized access attempt from user ID: {user_id}")
        await update.message.reply_text("⛔ Unauthorized access.")
        return

    user_text = update.message.text
    logger.info(f"Received message from {user_id}: {user_text[:50]}...")

    # Send a typing indicator
    await update.message.chat.send_action(action="typing")

    try:
        # Spawn an Antigravity agent per message
        async with Agent(AGENT_CONFIG) as agent:
            response = await agent.chat(user_text)
            
            # Consume the async iterator to get the full response text
            full_text = ""
            async for token in response:
                full_text += token
            
            if not full_text:
                full_text = "No response generated."
            
            # Telegram has a 4096 character limit per message
            for i in range(0, len(full_text), 4000):
                await update.message.reply_text(full_text[i:i+4000])

    except Exception as e:
        logger.error(f"Error generating response: {e}")
        await update.message.reply_text("⚠️ An error occurred while generating the response.")

def main():
    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set in .env")
        return

    app = ApplicationBuilder().token(TOKEN).build()
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("Starting Antigravity Telegram Bot...")
    app.run_polling()

if __name__ == "__main__":
    main()
