import os
import sys
import json
from dotenv import load_dotenv

sys.path.append(os.getcwd())
from context_builder import build_context

load_dotenv()

db_path = os.getenv("DB_PATH", "garmin_data.db")
context, summary = build_context(db_path)

print(json.dumps(context, indent=2))
