# EduCardX Assistant

> AI chatbot for students that answers EduCardX questions from company documents (RAG) and live web search, built with FastAPI, ChromaDB and an LLM API.

A FastAPI chatbot for students. It answers questions about EduCardX (financing, KYC, repayment, scholarships, colleges, careers) using the company's own documents, and uses live web search for current topics such as visas, deadlines and government schemes.

## Features

- **Smart routing (no extra LLM call):** a keyword router sends each question to one of three paths:
  - `RAG`: answers from the EduCardX `.docx` documents
  - `WEB_SEARCH`: answers from live Google results via Serper
  - `GENERAL`: greetings and general questions
- **RAG pipeline:** documents are split into chunks, embedded with `all-MiniLM-L6-v2`, stored in ChromaDB and retrieved with MMR.
- **Chat memory:** per-session history, kept in Redis if available, otherwise in memory.
- **Streaming:** Server-Sent Events so the first words appear quickly.
- **Voice input:** microphone speech-to-text (local use only).
- **Timing logs:** every step is timed and printed, and returned in the API response.

## Project structure

```
.
├── main8.py              # the whole application
├── requirements.txt
├── README.md
├── .gitignore
├── .env                  # your keys (never commit this)
├── 01_EduCardX_Terms_Conditions.docx
├── ...                   # the other EduCardX .docx files (01 to 12)
└── chroma_db/            # created automatically on first run
```

## Setup

**1. Install dependencies** (Python 3.10 to 3.12 recommended)

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

**2. Add the documents**

Place the 12 EduCardX `.docx` files in the same folder as `main8.py`.

**3. Create a `.env` file**

```
SERPER_API_KEY=your_serper_key
OX_API_KEY=your_llm_api_key
OX_BASE_URL=https://openrouter.ai/api/v1
OX_MODEL=z-ai/glm-5.3-flash
```

| Variable | Required | Description |
|---|---|---|
| `SERPER_API_KEY` | Yes | Key from [serper.dev](https://serper.dev) for web search |
| `OX_API_KEY` | Yes | API key for the LLM provider |
| `OX_BASE_URL` | No | OpenAI-compatible endpoint (default: OpenRouter) |
| `OX_MODEL` | No | Model name (default: `z-ai/glm-5.3-flash`) |

**4. Run**

```bash
python main8.py
# or
uvicorn main8:app --host 0.0.0.0 --port 8000
```

Interactive API docs are available at `http://localhost:8000/docs`.

## API

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Server status and whether Redis is connected |
| POST | `/chat` | Send a message, get the full answer |
| POST | `/chat/stream` | Same, streamed as Server-Sent Events |
| POST | `/voice-chat` | Listen on the server's microphone, then answer |
| POST | `/voice-chat/stream` | Voice input with a streamed answer |
| GET | `/history/{session_id}` | Read a session's saved history |
| DELETE | `/history/{session_id}` | Clear a session's history |

### Example

Request:

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "What documents do I need for KYC?"}'
```

Response:

```json
{
  "session_id": "3f2b...",
  "intent": "RAG",
  "answer": "...",
  "response_time_seconds": 2.41,
  "timings": { "1_intent_detection": 0.0, "2_rag_retrieval": 0.12, "5_ox_response": 2.2 }
}
```

Send the returned `session_id` with your next message to keep the conversation context. Leave it out to start a new conversation.

### Streaming events

`/chat/stream` sends these events in order:

| Event | Data |
|---|---|
| `meta` | `{"session_id", "intent"}` |
| `token` | `{"text": "..."}` (many) |
| `done` | `{"answer", "timings"}` |
| `error` | `{"detail"}` (only on failure) |

## Deploying on Render

1. Push the project to a GitHub repository (private if your documents are confidential). Do **not** commit `.env` or `chroma_db/`.
2. On Render, create a **New Web Service** and select the repo.
3. Settings:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn main8:app --host 0.0.0.0 --port $PORT`
4. Add the environment variables from the table above under **Environment**, plus `PYTHON_VERSION=3.11.9`. Render's default Python (3.14) is too new for some of the dependencies.
5. Check `https://<your-app>.onrender.com/health`.

Notes for deployment:

- Do not install `PyAudio` on Render (it is intentionally not in `requirements.txt`). The voice endpoints need a microphone on the server, so they only work locally, where you can run `pip install pyaudio`. In production, do speech-to-text in the app and send text to `/chat/stream`.
- Keep `main8.py`, `requirements.txt` and the `.docx` files in the repository root, or set **Root Directory** in Render to the folder that holds them.
- The embedding model uses PyTorch, which needs a good amount of RAM. If the service crashes at startup, use an instance with 2 GB or more.
- Restrict `allow_origins` in the CORS settings to your real frontend domain.
- Without a Redis server, chat history lives in memory and is lost on restart.

## Troubleshooting

| Problem | Likely cause |
|---|---|
| `Set SERPER_API_KEY and OX_API_KEY` on start | Missing environment variables or `.env` file |
| `404` or model-not-found error | Wrong `OX_MODEL` for your provider |
| Empty answers | `max_tokens` too low for a reasoning model. Raise it in `OX_PARAMS`. |
| Request rejected for `max_tokens` | Value is above the model's output limit. Use about 1500 to 4000. |
| `no .docx found ... skipping` | Documents are not in the same folder as `main8.py` |
| Follow-up questions give generic answers | The router decides per message, so a short follow-up with no keywords is treated as `GENERAL` |
| `/voice-chat` fails | No microphone available (for example on a cloud server) |
| `Could not import module "main8"` on Render | `main8.py` is missing from the deployed folder or has a different name. Check `git ls-files` and the start command. |
| `Failed building wheel for PyAudio` | `PyAudio` is in `requirements.txt`. Remove it for cloud deployment. |
| `Invalid requirement` during build | A line in `requirements.txt` is missing its `#` or is not a package name |

## Tech stack

FastAPI, LangChain, ChromaDB, Sentence-Transformers, Serper (Google Search), Redis (optional), SpeechRecognition, and an OpenAI-compatible LLM API.
