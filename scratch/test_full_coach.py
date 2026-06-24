import os
import sys
import json
from dotenv import load_dotenv

sys.path.append(os.getcwd())
from context_builder import build_context
from coach import COACH_SYSTEM_PROMPT
from google import genai
from google.genai import types

load_dotenv()

db_path = os.getenv("DB_PATH", "garmin_data.db")
context, summary = build_context(db_path)

api_key = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=api_key)

context_json = json.dumps(context, indent=2)
prompt = f"Here is the pre-computed readiness context: \n\n```json\n{context_json}\n```\n\nPlease output the coaching report matching the template exactly."

print("Running with gemini-3.5-flash...")
try:
    response = client.models.generate_content(
        model="gemini-3.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=COACH_SYSTEM_PROMPT
        )
    )
    print("Success!")
    print(response.text)
except Exception as e:
    print(f"Error: {e}")
