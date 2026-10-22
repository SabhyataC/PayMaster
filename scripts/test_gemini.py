import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

if "--models" in sys.argv:
    for m in client.models.list():
        if "flash" in m.name:
           print(m.name.removeprefix("models/"))
    sys.exit()

model = os.environ["GEMINI_MODEL"]
img = Path("samples/scans/05_summit_INV-5001.png").read_bytes()
r = client.models.generate_content(
   model=model,
   contents=[types.Part.from_bytes(data=img, mime_type="image/png"),
             "What is the invoice number and the total? Answer in one line."],
)
print(model, "->", r.text)