import os
import sys
import time
 
# Prevent UnicodeEncodeError when printing emojis on Windows consoles
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
 
import re
import json
import uuid
import traceback
import math
from collections import Counter, OrderedDict
from dataclasses import dataclass
from threading import RLock
import docx2txt
import requests
import redis
from typing import Optional
import speech_recognition as sr
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from openai import OpenAI
from pydantic import BaseModel, Field
 
 
# ==========================================
# KEYS (set these as environment variables or in .env)
#   SERPER_API_KEY=...
#   OX_API_KEY=...
#   OX_BASE_URL=https://openrouter.ai/api/v1     (optional, this is the default)
#   OX_MODEL=stealth/ox-alpha                    (optional, this is the default)
#
# Other gateways that serve OX Alpha:
#   AI/ML API : OX_BASE_URL=https://api.aimlapi.com/v1   OX_MODEL=stealth/ox-alpha
# ==========================================
 
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))
DOCUMENTS_DIR = os.path.abspath(os.getenv("DOCUMENTS_DIR", BASE_DIR))
 
SERPER_API_KEY = os.getenv("SERPER_API_KEY")
OX_API_KEY = os.getenv("OX_API_KEY")
OX_BASE_URL = os.getenv("OX_BASE_URL", "https://openrouter.ai/api/v1")
OX_MODEL = os.getenv("OX_MODEL", "z-ai/glm-5.3-flash")
 
if not SERPER_API_KEY or not OX_API_KEY:
    raise RuntimeError("Set SERPER_API_KEY and OX_API_KEY environment variables.")
 
# OX Alpha client (OpenAI-compatible API)
client = OpenAI(
    api_key=OX_API_KEY,
    base_url=OX_BASE_URL,
    timeout=60,
)
 
 
# ==========================================
# OX ALPHA SPEED SETTINGS
# ==========================================
 
OX_PARAMS = {
    "temperature": 0.3,
    # Bound each response for the small instance; configurable for your model.
    "max_tokens": max(256, min(int(os.getenv("OX_MAX_TOKENS", "2048")), 8192)),
}
 
# Asks the provider for less hidden reasoning = faster (OpenRouter-style).
# If the provider rejects it, call_ox() automatically retries without it.
OX_EXTRA_BODY = {"reasoning": {"effort": "low"}}
 
PROMPT_HISTORY_MESSAGES = 7       # only the last 6 messages go into the prompt
PROMPT_HISTORY_CHARS = 250        # long old answers are trimmed to this
 
_use_extra_body = True            # switches off automatically if the provider rejects the settings
 
 
def call_ox(prompt, stream=False):
    """Call OX Alpha with the speed settings; fall back to a plain call if rejected."""
    global _use_extra_body
 
    messages = [{"role": "user", "content": prompt}]
 
    if _use_extra_body:
        try:
            return client.chat.completions.create(
                model=OX_MODEL,
                messages=messages,
                stream=stream,
                extra_body=OX_EXTRA_BODY,
                **OX_PARAMS,
            )
        except Exception as e:
            print(f"⚠ OX speed settings rejected, using defaults: {e}")
            _use_extra_body = False
 
    return client.chat.completions.create(
        model=OX_MODEL,
        messages=messages,
        stream=stream,
        **OX_PARAMS,
    )
 
 
# ==========================================
# TIMING HELPERS
# ==========================================
 
def lap(timings, key, start):
    """Store how many seconds have passed since `start` under `key`."""
    timings[key] = round(time.perf_counter() - start, 3)
 
 
def log_timings(label, timings):
    print(f"\n----- TIMINGS ({label}) -----")
    for k, v in timings.items():
        print(f"  {k:<36} {v:>8.3f}s")
    print("-----------------------------\n")
 
 
# ==========================================
# CHAT HISTORY (short-term memory)
# Uses Redis if it is running, otherwise an in-memory dict.
# Each session_id has its own list of messages.
# ==========================================
 
MAX_HISTORY_MESSAGES = 15     # last 15 exchanges, also bounded by characters
HISTORY_TTL_SECONDS = 7200
MAX_MEMORY_SESSIONS = 100
MAX_HISTORY_MESSAGE_CHARS = 4000
MAX_HISTORY_SESSION_CHARS = 20000
 
try:
    redis_url = os.getenv("REDIS_URL")
    if not redis_url and os.getenv("RENDER"):
        raise redis.exceptions.ConnectionError("Using bounded in-memory history on Render.")
    redis_client = redis.Redis.from_url(
        redis_url or "redis://localhost:6379/0", decode_responses=True,
        socket_connect_timeout=2, socket_timeout=2,
    )
    redis_client.ping()
    USE_REDIS = True
    print("Connected to Redis.")
except (redis.exceptions.RedisError, OSError):
    redis_client = None
    USE_REDIS = False
    print("Redis not available, using in-memory chat history.")
 
_memory_store = OrderedDict()
_history_lock = RLock()


def prune_expired_history():
    now = time.monotonic()
    for session_id, (expires_at, _) in list(_memory_store.items()):
        if expires_at <= now:
            del _memory_store[session_id]
 
 
def get_history(session_id):
    if USE_REDIS:
        data = redis_client.get(f"chat:{session_id}")
        return json.loads(data) if data else []
    with _history_lock:
        prune_expired_history()
        entry = _memory_store.get(session_id)
        if entry is None:
            return []
        _memory_store.move_to_end(session_id)
        return [dict(message) for message in entry[1]]
 
 
def save_history(session_id, history):
    bounded = []
    remaining = MAX_HISTORY_SESSION_CHARS
    for message in reversed(history[-MAX_HISTORY_MESSAGES * 2:]):
        content = str(message.get("content", ""))[:MAX_HISTORY_MESSAGE_CHARS]
        if len(content) > remaining:
            break
        bounded.append({"role": message["role"], "content": content})
        remaining -= len(content)
    history = list(reversed(bounded))
    if USE_REDIS:
        redis_client.set(f"chat:{session_id}", json.dumps(history), ex=HISTORY_TTL_SECONDS)
    else:
        with _history_lock:
            prune_expired_history()
            _memory_store[session_id] = (time.monotonic() + HISTORY_TTL_SECONDS, history)
            _memory_store.move_to_end(session_id)
            while len(_memory_store) > MAX_MEMORY_SESSIONS:
                _memory_store.popitem(last=False)
 
 
def format_history(history):
    """Turn recent messages into short text for the model prompt."""
    if not history:
        return "No previous conversation."
    lines = []
    for m in history[-PROMPT_HISTORY_MESSAGES:]:
        who = "Student" if m["role"] == "user" else "Assistant"
        content = m["content"]
        if len(content) > PROMPT_HISTORY_CHARS:
            content = content[:PROMPT_HISTORY_CHARS] + "..."
        lines.append(f"{who}: {content}")
    return "\n".join(lines)
 
 
# Documents
documents = [
    "01_EduCardX_Terms_Conditions",
    "02_EduCardX_Privacy_Policy",
    "03_EduCardX_KYC_Student_Verification",
    "04_EduCardX_Financing_Explanation_Guide",
    "05_EduCardX_Repayment_Policy",
    "06_EduCardX_FAQ",
    "07_EduCardX_Student_Guide",
    "08_EduCardX_Scholarship_Information_Guide",
    "09_EduCardX_College_Information_Guide",
    "10_EduCardX_Career_Placement_Guide",
    "11_EduCardX_Financial_Literacy_Guide",
    "12_EduCardX_AI_Mentor_Operating_Policy_ML_Spec",
]
 
 
# Lightweight document search. BM25 ranks matching words without downloading
# or loading PyTorch, sentence-transformers, Chroma, or any embedding model.
# This is lexical retrieval: paraphrases can match less well than embeddings.
@dataclass(frozen=True)
class DocumentChunk:
    page_content: str
    source: str


STOP_WORDS = set("""
a an and are as at be been but by can could did do does for from had has have
how i if in into is it its me my of on or our please should that the their
them there these they this those to us was we were what when where which
who why will with would you your educardx
""".split())

WORD_FORMS = {
    "repayment": "repay", "repayments": "repay", "repaying": "repay",
    "eligibility": "eligible", "financing": "finance", "financial": "finance",
    "policies": "policy", "universities": "university",
    "verification": "verify", "verified": "verify",
    "instalment": "installment", "instalments": "installment",
}


def search_tokens(text):
    tokens = []
    for word in re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE):
        if word in STOP_WORDS:
            continue
        word = WORD_FORMS.get(word, word)
        if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
            word = word[:-1]
        tokens.append(word)
    return tokens


def split_document(text, source, size=700, overlap=100):
    if size <= 0 or not 0 <= overlap < size:
        raise ValueError("Chunk overlap must be smaller than its positive size.")
    text = text.strip()
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # Prefer paragraph boundaries, then words, in the latter half.
            boundary = text.rfind("\n\n", start + size // 2, end)
            if boundary < 0:
                boundary = text.rfind(" ", start + size // 2, end)
            if boundary >= 0:
                end = boundary
        chunk = text[start:end].strip()
        if chunk:
            yield DocumentChunk(chunk, source)
        if end == len(text):
            break
        next_start = max(start + 1, end - overlap)
        # Start the overlap at a word boundary, without leaving a gap.
        while next_start < end and not text[next_start].isspace():
            next_start += 1
        start = next_start


class KeywordRetriever:
    def __init__(self, chunks, k=4):
        if not chunks:
            raise ValueError("Document search needs at least one chunk.")
        self.chunks = list(chunks)
        self.k = k
        self.postings = {}
        lengths = []
        for index, chunk in enumerate(self.chunks):
            frequencies = Counter(search_tokens(chunk.page_content))
            # Later chunks still belong to their document's topic. Include its
            # filename as a lightly boosted title so a repayment-policy section
            # remains findable even when that section only says "late payments".
            title = os.path.splitext(os.path.basename(chunk.source))[0]
            frequencies.update(search_tokens(title) * 2)
            lengths.append(sum(frequencies.values()))
            for word, count in frequencies.items():
                self.postings.setdefault(word, []).append((index, count))
        average = sum(lengths) / len(lengths) or 1.0
        self.length_penalties = [1.5 * (0.25 + 0.75 * length / average) for length in lengths]
        self.idf = {
            word: math.log1p((len(self.chunks) - len(entries) + 0.5) / (len(entries) + 0.5))
            for word, entries in self.postings.items()
        }

    def invoke(self, query):
        scores = Counter()
        for word in set(search_tokens(query)):
            for index, count in self.postings.get(word, ()):
                scores[index] += self.idf[word] * count * 2.5 / (count + self.length_penalties[index])
        # No matching words means no evidence, not unrelated fallback chunks.
        return [self.chunks[index] for index, score in scores.most_common(self.k) if score > 0]
 
 
def find_docx_files(name):
    """Resolve an entry from `documents` to real .docx file path(s) in DOCUMENTS_DIR.
    Works if the entry is a .docx file (with or without extension, even with a
    suffix like "[1]") or a folder containing .docx files."""
    path = os.path.join(DOCUMENTS_DIR, name)
 
    # Entry is a folder -> every .docx inside it
    if os.path.isdir(path):
        return [
            os.path.join(path, f)
            for f in sorted(os.listdir(path))
            if f.lower().endswith(".docx") and not f.startswith("~$")
        ]
 
    # Entry is already a full file name
    if os.path.isfile(path):
        return [path]
 
    # Entry is a file name without extension / with a suffix like "[1]"
    return [
        os.path.join(DOCUMENTS_DIR, f)
        for f in sorted(os.listdir(DOCUMENTS_DIR))
        if f.lower().endswith(".docx")
        and not f.startswith("~$")
        and f.startswith(name)
    ]
 
 
chunks = []
loaded_document_count = 0
for file in documents:
 
    paths = find_docx_files(file)
 
    if not paths:
        print(f"Warning: no .docx found for '{file}' in {DOCUMENTS_DIR}, skipping.")
        continue
 
    for file_path in paths:
        print(f"Loading document: {file_path}")
        document_text = docx2txt.process(file_path)
        chunks.extend(split_document(document_text, file_path))
        loaded_document_count += 1
        del document_text
 
if not chunks:
    raise RuntimeError(
        f"No document content was loaded from {DOCUMENTS_DIR}. "
        "Place the EduCardX .docx files next to this script."
    )
 
retriever = KeywordRetriever(chunks)
print(f"Loaded {loaded_document_count} documents, {len(chunks)} chunks (lightweight keyword search).")
 
 
# ==========================================
# SPEECH RECOGNITION (live microphone)
# Listens on the microphone of the machine running this server,
# so it works for local use / testing on your PC.
#
# Behaviour:
#   - waits up to VOICE_START_TIMEOUT seconds for the user to start speaking
#   - stops VOICE_SILENCE_SECONDS after the user stops speaking
#   - if the user keeps speaking, stops at VOICE_MAX_SECONDS
#   - then converts the recorded audio to text
#   - background noise is calibrated ONCE (not on every request)
# ==========================================
 
VOICE_START_TIMEOUT = 8        # seconds to wait for the user to begin speaking
VOICE_SILENCE_SECONDS = 2      # silence after speech that ends the recording
VOICE_MAX_SECONDS = 25         # maximum length of one recording
 
_recognizer = sr.Recognizer()
_recognizer.pause_threshold = VOICE_SILENCE_SECONDS
_recognizer.non_speaking_duration = 0.5      # must be <= pause_threshold
_recognizer.dynamic_energy_threshold = True  # keeps adapting to noise while listening
_mic_calibrated = False
 
 
def voice_to_text(timings=None):
    global _mic_calibrated
    recognizer = _recognizer
 
    try:
        with sr.Microphone() as source:
 
            # Calibrate background noise only the first time (saves ~0.5 s per request)
            if not _mic_calibrated:
                print("Calibrating microphone (first time only)...")
                recognizer.adjust_for_ambient_noise(source, duration=0.5)
                _mic_calibrated = True
 
            print("\n Listening...")
            print(
                f"Speak now... (stops after {VOICE_SILENCE_SECONDS}s of silence, "
                f"max {VOICE_MAX_SECONDS}s)"
            )
 
            listen_start = time.perf_counter()
 
            audio = recognizer.listen(
                source,
                timeout=VOICE_START_TIMEOUT,
                phrase_time_limit=VOICE_MAX_SECONDS
            )
 
            if timings is not None:
                lap(timings, "mic_listening", listen_start)
 
        print("Converting speech to text...")
 
        stt_start = time.perf_counter()
 
        text = recognizer.recognize_google(
            audio,
            language="en-IN"
        )
 
        if timings is not None:
            lap(timings, "speech_to_text", stt_start)
 
        print(text)
 
        return text
 
    except sr.WaitTimeoutError:
        print("❌ No speech detected.")
        return None
 
    except sr.UnknownValueError:
        print("❌ Sorry, I couldn't understand your speech.")
        return None
 
    except sr.RequestError as e:
        print(f"❌ Speech recognition error: {e}")
        return None
 
    except Exception as e:
        print(f"❌ Microphone error: {e}")
        return None
 
 
# ==========================================
# SERPER WEB SEARCH
# - one reused connection (no new TLS handshake each time)
# - 5 results instead of 8 (less to download, less for the model to read)
# - 10-minute cache for repeated questions
# ==========================================
 
SERPER_NUM_RESULTS = 5
SERPER_CACHE_SECONDS = 600
SNIPPET_MAX_CHARS = 220
 
_http = requests.Session()
_search_cache = {}      # query -> (time, data)
 
 
def serper_search(query, num_results=SERPER_NUM_RESULTS):
 
    cached = _search_cache.get(query)
    if cached and time.time() - cached[0] < SERPER_CACHE_SECONDS:
        print("Serper: cache hit")
        return cached[1]
 
    url = "https://google.serper.dev/search"
 
    headers = {
        "X-API-KEY": SERPER_API_KEY,
        "Content-Type": "application/json"
    }
 
    payload = {
        "q": query,
        "gl": "in",
        "hl": "en",
        "num": num_results
    }
 
    response = _http.post(
        url,
        headers=headers,
        json=payload,
        timeout=8
    )
 
    response.raise_for_status()
 
    data = response.json()
    _search_cache[query] = (time.time(), data)
 
    # keep the cache from growing forever
    if len(_search_cache) > 200:
        _search_cache.pop(next(iter(_search_cache)))
 
    return data
 
 
# ==========================================
# FORMAT WEB RESULTS
# ==========================================
 
def format_web_results(data):
 
    results = data.get("organic", [])
 
    if not results:
        return "No web search results found."
 
    formatted_results = []
 
    for i, result in enumerate(results[:SERPER_NUM_RESULTS], start=1):
 
        title = result.get("title", "")
        link = result.get("link", "")
        snippet = (result.get("snippet", "") or "")[:SNIPPET_MAX_CHARS]
 
        formatted_results.append(
            f"SOURCE {i}\nTitle: {title}\nURL: {link}\nDescription: {snippet}\n"
        )
 
    return "\n".join(formatted_results)
 
 
# search engine query
 
def create_search_query(user):
    """No LLM call: build the Google query with plain Python."""
 
    query = user.strip()
 
    # Add India + the current year so results are current and relevant
    if "india" not in query.lower():
        query += " India"
 
    if not re.search(r"\b20\d{2}\b", query):
        query += " 2026"
 
    return query
 
 
# intent function (no LLM call: simple keyword router)
 
GREETING_PATTERN = re.compile(
    r"^\s*(hi|hii+|hello|hey|heyy+|good\s+(morning|afternoon|evening|night)|"
    r"thanks|thank\s+you|ok|okay|bye|goodbye|who\s+are\s+you|what\s+can\s+you\s+do)\b[\s!.?]*$",
    re.IGNORECASE,
)
 
WEB_KEYWORDS = [
    "latest", "newest", "recent", "currently", "current", "today", "this year",
    "upcoming", "2025", "2026", "2027",
    "deadline", "last date", "visa", "ielts", "pte", "toefl", "gre", "gmat",
    "abroad", "overseas", "study in", "universities in", "university in",
    "government scholarship", "government scheme", "scholarship scheme",
    "new scholarship", "student scheme", "tuition fee", "tuition fees",
    "admission open", "admissions open",
]
 
RAG_KEYWORDS = [
    "educardx", "terms", "conditions", "privacy", "kyc", "verification",
    "repay", "emi", "installment", "financing", "finance", "loan", "interest",
    "eligib", "scholarship", "course", "college", "placement", "career",
    "application", "apply", "policy", "policies", "fee", "refund",
    "card", "credit", "limit", "document", "student", "mentor", "faq",
]
 
 
def detect_intent(user):
 
    text = user.lower().strip()
 
    # 1. Greetings / small talk
    if GREETING_PATTERN.match(text):
        return "GENERAL"
 
    # 2. Questions that clearly name EduCardX always use the documents
    if "educardx" in text:
        return "RAG"
 
    # 3. Needs current information from the internet
    for word in WEB_KEYWORDS:
        if re.search(r"\b" + re.escape(word) + r"\b", text):
            return "WEB_SEARCH"
 
    # 4. EduCardX document topics
    for word in RAG_KEYWORDS:
        if word in text:
            return "RAG"
 
    # 5. Everything else: general question
    return "GENERAL"
 
 
# --------------------------------------------------
# PROMPT BUILDER
# Only the rules for the detected intent are sent
# (a shorter prompt = faster response).
# --------------------------------------------------
 
INTENT_RULES = {
    "RAG": """- Use the supplied EduCardX information.
- Do not invent EduCardX policies.
- If the required information is not in it, say the available EduCardX information does not specify it.""",
 
    "WEB_SEARCH": """- Use the supplied live web search results.
- Do not invent facts, URLs, deadlines or scholarship amounts.
- Prefer official government, university, embassy and scholarship sources.
- Include useful source URLs from the results.
- Say the information comes from current web search results.
- If the results are insufficient, say the information could not be verified.""",
 
    "GENERAL": """- Answer normally.
- If the user is greeting you, respond naturally.
- Do not mention RAG or web search.""",
}
 
 
def build_prompt(user, history=None, timings=None):
    """Steps 1-5: detect intent, get RAG / web context, build the model prompt."""
 
    if timings is None:
        timings = {}
 
    # STEP 1: DETECT INTENT
    t = time.perf_counter()
    intent = detect_intent(user)
    lap(timings, "1_intent_detection", t)
 
    context = ""
 
    # STEP 2: RAG
    if intent == "RAG":
 
        t = time.perf_counter()
        results = retriever.invoke(user)
        lap(timings, "2_rag_retrieval", t)
 
        context = "\n\n".join(
            doc.page_content
            for doc in results
        ) or "No matching EduCardX document passages were found for this question."
 
    # STEP 3: WEB SEARCH
    elif intent == "WEB_SEARCH":
 
        search_query = create_search_query(user)
 
        t = time.perf_counter()
        search_data = serper_search(search_query)
        lap(timings, "3_web_search", t)
 
        context = format_web_results(search_data)
 
    # STEP 4: GENERAL
    else:
        context = "No document retrieval required."
 
    # STEP 5: FINAL PROMPT
    prompt = f"""
You are EduCardX Assistant.
 
Conversation history (most recent messages):
{format_history(history or [])}
 
User question:
{user}
 
Detected intent:
{intent}
 
Retrieved information:
{context}
 
Instructions:
- Use the conversation history to understand follow-up questions. Do not repeat earlier answers unless asked.
- Answer the user's question directly.
{INTENT_RULES[intent]}
 
STYLE RULES:
- Be brief. Usually 60-150 words; never more than 250 unless the user asks for detail.
- Answer the question directly in the first sentence.
- Use Markdown, short paragraphs, and bullets only when listing several items.
- Bold only the key terms.
- No long introductions and no repeating the question.
- Use at most one emoji.
- Do not invent information that is not present in the provided information.
 
Return only the final answer, in a friendly, student-friendly tone.
"""
 
    return intent, prompt
 
 
# --------------------------------------------------
# BOT (one question in -> one answer out)
# `timings` (optional dict) is filled with seconds per step.
# --------------------------------------------------
 
def answer_question(user, history=None, timings=None):
 
    if timings is None:
        timings = {}
 
    intent, prompt = build_prompt(user, history, timings)
 
    t = time.perf_counter()
    response = call_ox(prompt)
    lap(timings, "5_ox_response", t)
 
    # Shows input/output token counts so you can see what drives the time
    print("OX usage:", getattr(response, "usage", "not provided"))
 
    answer = ""
    if getattr(response, "choices", None):
        answer = (response.choices[0].message.content or "").strip()
 
    return intent, answer
 
 
# --------------------------------------------------
# STREAMING
# Same prompt and model, but the answer is sent piece by piece,
# so the user sees the first words after ~1-2 s instead of waiting
# for the whole answer.
# --------------------------------------------------
 
def generate_stream(prompt, timings):
    """Yield the answer text in small pieces from OX Alpha."""
 
    start = time.perf_counter()
    first = True
 
    stream = call_ox(prompt, stream=True)
 
    for chunk in stream:
        # Some chunks (e.g. a final usage chunk) have no choices
        if not getattr(chunk, "choices", None):
            continue
 
        delta = chunk.choices[0].delta
        if delta is None:
            continue
 
        # Only visible answer text (reasoning text arrives in a separate field)
        text = getattr(delta, "content", None)
        if text:
            if first:
                lap(timings, "5a_ox_FIRST_token", start)
                first = False
            yield text
 
    lap(timings, "5b_ox_total_stream", start)
 
 
def sse(event, data):
    """Format one Server-Sent Event."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
 
 
def stream_answer(user, session_id, history, timings, request_start,
                  transcript=None, voice_start=None):
    """Events sent to the client:
      meta  -> {"session_id", "intent", ("transcript")}   (first)
      token -> {"text": "..."}                            (many)
      done  -> {"answer": "...", "timings": {...}}        (last)
      error -> {"detail": "..."}                          (only on failure)
    """
    try:
        intent, prompt = build_prompt(user, history, timings)
 
        meta = {"session_id": session_id, "intent": intent}
        if transcript is not None:
            meta["transcript"] = transcript
        yield sse("meta", meta)
 
        parts = []
        for text in generate_stream(prompt, timings):
            parts.append(text)
            yield sse("token", {"text": text})
 
        answer = "".join(parts)
 
        history.append({"role": "user", "content": user})
        history.append({"role": "assistant", "content": answer})
 
        t = time.perf_counter()
        save_history(session_id, history)
        lap(timings, "6_save_history", t)
 
        lap(timings, "TOTAL_response_time", request_start)
        if voice_start is not None:
            lap(timings, "TOTAL_including_voice", voice_start)
        log_timings("stream", timings)
 
        yield sse("done", {"answer": answer, "timings": timings})
 
    except Exception as e:
        traceback.print_exc()
        yield sse("error", {"detail": f"Assistant error: {e}"})
 
 
STREAM_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",   # stops nginx from buffering the stream
}
 
 
# ==========================================
# FASTAPI APP
# ==========================================
 
app = FastAPI(title="EduCardX Assistant API", version="1.0.0")
 
# Mobile apps don't need CORS, but it is useful for web testing.
# Restrict allow_origins to your real domains in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
 
 
class ChatRequest(BaseModel):
    session_id: Optional[str] = Field(default=None, max_length=128)
    message: str = Field(max_length=8000)
 
 
class ChatResponse(BaseModel):
    session_id: str
    intent: str
    answer: str
    response_time_seconds: float                 # time to produce the answer
    timings: Optional[dict] = None               # time per step
 
 
class VoiceChatResponse(BaseModel):
    session_id: str
    transcript: str
    intent: str
    answer: str
    response_time_seconds: float                 # time to produce the answer (after the text is ready)
    total_time_seconds: float                    # including microphone listening + speech-to-text
    timings: Optional[dict] = None               # time per step
 
 
@app.get("/health")
def health():
    return {
        "status": "ok", "redis": USE_REDIS,
        "retrieval": "bm25", "documents": loaded_document_count, "chunks": len(chunks),
    }
 
 
# Text input (also use this when the app does on-device speech-to-text)
@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
 
    user = req.message.strip()
 
    if not user:
        raise HTTPException(status_code=400, detail="Message is empty.")
 
    request_start = time.perf_counter()
    timings = {}
 
    session_id = req.session_id or str(uuid.uuid4())
    history = get_history(session_id)
 
    try:
        intent, answer = answer_question(user, history, timings)
 
        history.append({"role": "user", "content": user})
        history.append({"role": "assistant", "content": answer})
 
        t = time.perf_counter()
        save_history(session_id, history)
        lap(timings, "6_save_history", t)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"Assistant error: {e}")
 
    lap(timings, "TOTAL_response_time", request_start)
    log_timings("chat", timings)
 
    return ChatResponse(
        session_id=session_id,
        intent=intent,
        answer=answer,
        response_time_seconds=timings["TOTAL_response_time"],
        timings=timings,
    )
 
 
# Text input, streamed (first words appear much sooner)
@app.post("/chat/stream")
def chat_stream(req: ChatRequest):
 
    user = req.message.strip()
 
    if not user:
        raise HTTPException(status_code=400, detail="Message is empty.")
 
    request_start = time.perf_counter()
    timings = {}
 
    session_id = req.session_id or str(uuid.uuid4())
    history = get_history(session_id)
 
    return StreamingResponse(
        stream_answer(user, session_id, history, timings, request_start),
        media_type="text/event-stream",
        headers=STREAM_HEADERS,
    )
 
 
# Read a session's saved history
@app.get("/history/{session_id}")
def read_history(session_id: str):
    return {"session_id": session_id, "history": get_history(session_id)}
 
 
# Clear a session's history (start a fresh conversation)
@app.delete("/history/{session_id}")
def clear_history(session_id: str):
    if USE_REDIS:
        redis_client.delete(f"chat:{session_id}")
    else:
        with _history_lock:
            _memory_store.pop(session_id, None)
    return {"session_id": session_id, "cleared": True}
 
 
# Voice input: the mic starts listening, stops 2 seconds after the user
# stops speaking (or at 25 seconds), converts speech to text and answers
@app.post("/voice-chat", response_model=VoiceChatResponse)
def voice_chat(session_id: Optional[str] = None):
 
    voice_start = time.perf_counter()
    timings = {}
 
    user = voice_to_text(timings)
 
    lap(timings, "VOICE_total (mic + speech_to_text)", voice_start)
 
    if not user:
        raise HTTPException(
            status_code=422,
            detail="Could not capture or understand speech. Please try again.",
        )
 
    # Response timing starts once the text is ready
    response_start = time.perf_counter()
 
    session_id = session_id or str(uuid.uuid4())
    history = get_history(session_id)
 
    try:
        intent, answer = answer_question(user, history, timings)
 
        history.append({"role": "user", "content": user})
        history.append({"role": "assistant", "content": answer})
 
        t = time.perf_counter()
        save_history(session_id, history)
        lap(timings, "6_save_history", t)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=502, detail=f"Assistant error: {e}")
 
    lap(timings, "TOTAL_response_time", response_start)
    lap(timings, "TOTAL_including_voice", voice_start)
    log_timings("voice-chat", timings)
 
    return VoiceChatResponse(
        session_id=session_id,
        transcript=user,
        intent=intent,
        answer=answer,
        response_time_seconds=timings["TOTAL_response_time"],
        total_time_seconds=timings["TOTAL_including_voice"],
        timings=timings,
    )
 
 
# Voice input, streamed (first words appear much sooner)
@app.post("/voice-chat/stream")
def voice_chat_stream(session_id: Optional[str] = None):
 
    voice_start = time.perf_counter()
    timings = {}
 
    user = voice_to_text(timings)
 
    lap(timings, "VOICE_total (mic + speech_to_text)", voice_start)
 
    if not user:
        raise HTTPException(
            status_code=422,
            detail="Could not capture or understand speech. Please try again.",
        )
 
    response_start = time.perf_counter()
 
    session_id = session_id or str(uuid.uuid4())
    history = get_history(session_id)
 
    return StreamingResponse(
        stream_answer(user, session_id, history, timings, response_start,
                      transcript=user, voice_start=voice_start),
        media_type="text/event-stream",
        headers=STREAM_HEADERS,
    )
 
 
# Run: python main9.py (or: uvicorn main9:app --host 0.0.0.0 --port $PORT)
if __name__ == "__main__":
    import uvicorn
 
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
