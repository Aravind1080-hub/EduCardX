# EduCardX: smaller build for Render Free

This version replaces the local embedding model and Chroma index with BM25
keyword search over the same 12 Word documents. It avoids installing PyTorch,
CUDA/NVIDIA libraries, sentence-transformers, Chroma, NumPy, or LangChain.
AI-generated answers and Serper web search still use your existing providers.

## Upload the replacement files

1. Open https://github.com/Aravind1080-hub/EduCardX and select the `main` branch.
2. Use **Add file > Upload files** to upload `main9.py` and `requirements.txt`
   from this download, replacing the files with those exact names in the root.
3. Commit the changes to `main`. Keep all 12 existing `.docx` files alongside
   `main9.py`. Replace the entire requirements file, rather than appending to it.

The GitHub connection used to prepare this change only has read access, so
these files have not been pushed automatically.

## Render settings

Use the existing Python web service and its Free compute plan:

| Setting | Value |
| --- | --- |
| Repository | `https://github.com/Aravind1080-hub/EduCardX.git` |
| Branch | `main` |
| Root directory | Leave blank |
| Build command | `pip install -r requirements.txt` |
| Start command | `uvicorn main9:app --host 0.0.0.0 --port $PORT --workers 1` |
| Health check path | `/health` |

Keep `OX_API_KEY` and `SERPER_API_KEY` in Render's Environment settings. If your
local app needs specific `OX_MODEL` or `OX_BASE_URL` settings, use those same
values on Render. Never upload your `.env` file or API keys to GitHub.

After uploading the files, choose **Manual Deploy > Clear build cache & deploy**.
This rebuilds without the old machine-learning dependencies.

When the service is live, open its URL followed by `/health`. The response
should include `"status":"ok"`, `"retrieval":"bm25"`, and `"documents":12`.
Use `/docs` to test the API, including `/chat` and `/chat/stream`.

## Behavior and limits

- Document search now matches words, including some common word forms. It can
  miss paraphrases that the previous semantic embedding search would find.
  With no matching words, it explicitly tells the answer model that no matching
  passages were found.
- The in-memory chat store retains at most 100 sessions, expires history after
  two hours, and bounds characters per session. Older sessions can be evicted.
  History is lost when Render restarts or sleeps. An optional `REDIS_URL` enables
  an existing Redis service; none is required.
- Chat input is limited to 8,000 characters and session IDs to 128 characters.
  Model responses default to 2,048 output tokens; optional `OX_MAX_TOKENS` can
  change this within 256–8,192. This replaces the old one-million-token limit.
- The existing voice routes listen on the server's microphone, which isn't
  available on Render. Use speech-to-text on the phone/browser and send the
  resulting text to `/chat`; local microphone support still needs PyAudio.
- No new paid service is required by this change. Your existing AI and Serper
  API usage may have separate charges or quotas. Free Render hosting does not
  make those providers free.

## Verification

All 18 local tests passed, covering document retrieval, normal and streamed
chat, history expiry/limits, input validation, and API failures. The tests
explicitly block imports of the removed machine-learning dependencies.

A real Uvicorn process loaded all 12 local Word documents (96 chunks), started
in about 16 seconds, and returned successful `/health` and `/openapi.json`
responses. Its observed peak working set was 86.4 MiB on Windows / Python 3.12.
This is a local measurement, not a guarantee of Render memory usage under load.

The checks use mock external AI/search responses, so they do not call paid
APIs. Live provider responses and a Render deployment still need to be checked
after uploading. The original local app and documents were not overwritten.

Render references:
- https://render.com/docs/deploy-fastapi
- https://render.com/docs/deploys#manual-deploys
- https://render.com/docs/free
