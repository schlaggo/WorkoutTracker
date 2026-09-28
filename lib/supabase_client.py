import os
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]


def get_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


def get_authenticated_client(access_token: str) -> Client:
    client = get_client()
    client.postgrest.auth(access_token)
    return client
