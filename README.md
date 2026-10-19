# EduCardX Mentor

FastAPI backend (`main10.py`) plus a chat page in `static/` served from the same URL.

## Folder layout (put this in your GitHub repo)

```
main10.py
requirements.txt
render.yaml
.gitignore
static/
  index.html
  style.css
  script.js
01_EduCardX_Terms_Conditions.docx
...                      <- all 12 .docx files, next to main10.py
```

The server refuses to start if no `.docx` files are found, so commit them too.

## Run locally

```
pip install -r requirements.txt
copy .env.example .env      (then fill in the two keys)
python main10.py
```

Open http://localhost:8000

## Deploy on Render

1. Push this folder to GitHub.
2. Render dashboard > New > Blueprint (uses `render.yaml`), or New > Web Service with:
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn main10:app --host 0.0.0.0 --port $PORT`
3. Add environment variables: `SERPER_API_KEY` and `OX_API_KEY`.
4. Deploy, then open your `onrender.com` URL.

Notes:
- Free instances sleep when idle; the first request can take up to a minute.
- Chat history is kept in memory on Render and resets on restart. Add a Redis
  instance and set `REDIS_URL` to keep it.
- The `/voice-chat` endpoints use the server's microphone and do nothing on Render.
  The web page uses the browser's own speech recognition (Chrome and Edge) instead.
