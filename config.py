import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TOKEN_BOT", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
