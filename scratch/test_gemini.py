import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=api_key)

try:
    response = client.models.generate_content(
        model="gemini-3.5-flash",
        contents="Hello"
    )
    print("Success!")
    print(response.text)
except Exception as e:
    print(f"Error calling model: {e}")
