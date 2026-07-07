from supabase import create_client
from config import SUPABASE_URL, SUPABASE_KEY

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL atau SUPABASE_KEY belum diisi")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
