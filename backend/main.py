import os, time, hashlib, json, sqlite3, asyncio, logging
from datetime import datetime, timedelta
from typing import Optional
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import FastAPI, BackgroundTasks, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

NEWS_API_KEY   = os.getenv("NEWS_API_KEY", "")
LLM_PROVIDER   = os.getenv("LLM_PROVIDER", "ollama").lower()
OPENAI_KEY     = os.getenv("OPENAI_API_KEY", "")
GROQ_KEY       = os.getenv("GROQ_API_KEY", "")
OLLAMA_URL     = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
LLM_MODEL      = os.getenv("LLM_MODEL", "qwen2.5:7b")
FETCH_INTERVAL = int(os.getenv("FETCH_INTERVAL_MINUTES", "10"))
DB_PATH        = os.getenv("DB_PATH", "news.db")
SERPAPI_KEY    = os.getenv("SERPAPI_KEY", "")
GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")
DEBUG_MODE     = os.getenv("DEBUG", "false").lower() == "true"
BATCH_SIZE     = 5

# Default models to use when falling back to a provider
PROVIDER_MODELS = {
    "ollama": LLM_MODEL,
    "groq":   os.getenv("GROQ_MODEL",   "llama-3.3-70b-versatile"),
    "openai": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ai-news")

_pipeline_lock = asyncio.Lock()

# Mutable pipeline status — updated after every run, exposed via /api/health
_pipeline_status: dict = {
    "ran_at": None,
    "status": "idle",
    "sources": {},
    "llm_provider_used": None,
    "llm_fallbacks": 0,
    "items_added": 0,
    "errors": [],
    "duration_s": 0,
}

# ─────────────────────────────────────────────────────────────────────────────
# LLM Prompts
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert AI technology journalist and analyst. Your task is to transform raw news article data into comprehensive, well-structured editorial articles about artificial intelligence.

STRICT RULES:
1. ONLY process articles that are directly about AI, ML, LLMs, AI tools, AI research, AI companies, AI funding, AI regulation, robotics AI, or AI chips.
2. REJECT and SKIP any article not directly about artificial intelligence.
3. Write like a professional tech journalist — clear, authoritative, structured.
4. Do NOT copy raw article text verbatim. Synthesize, expand, and contextualize based on the provided content.
5. Do NOT fabricate specific facts such as precise dollar amounts, product names, or dates not supported by the source.
6. Each included article must have a substantive overview, detailed explanation, key insights, and why it matters.
7. Return ONLY valid JSON — no markdown fences, no preamble, no trailing text."""


def build_user_prompt(articles: list[dict]) -> str:
    snippets = "\n\n".join(
        f"[{i+1}] TITLE: {a['title']}\nSOURCE: {a['source']}\nURL: {a['url']}\n"
        f"PUBLISHED: {a.get('published_at','')}\nCONTENT: {a['content'][:600]}"
        for i, a in enumerate(articles)
    )
    return f"""Transform these {len(articles)} news articles into comprehensive editorial content. Include ONLY strictly AI-related articles.

Return this exact JSON structure (no extra text outside the JSON):
{{
  "items": [
    {{
      "title": "Clear, refined, professional headline",
      "category": "Research|Product Launch|Funding|Company Update|Regulation|Trending",
      "priority": "High|Medium|Low",
      "overview": "Two full paragraphs. First: introduce the development, what happened, who is involved. Second: immediate context — why now, what led to it.",
      "detailed_explanation": "Four to six paragraphs: (1) what happened and key announcement, (2) technical/business specifics, (3) background context, (4) key players, (5) industry context and competitors, (6) challenges or open questions.",
      "key_insights": [
        "Most important single takeaway",
        "Key business or strategic implication",
        "Non-obvious signal industry watchers should note",
        "Technical or scientific significance if applicable",
        "Connection to broader AI industry trajectory"
      ],
      "why_it_matters": "Two paragraphs: (1) near-term implications for AI industry, developers, businesses, end users; (2) longer-term outlook and who stands to benefit or be disrupted.",
      "technical_breakdown": "2-3 paragraphs explaining technical aspects in accessible language. Empty string if article is not primarily technical.",
      "source": "source publication name",
      "url": "original article url",
      "published_at": "ISO 8601 datetime string",
      "alert": false
    }}
  ],
  "trends": [
    {{ "topic": "Short Topic Name", "reason": "One sentence why this is trending across articles" }}
  ]
}}

Priority: High = major model releases / funding >$50M / breakthrough research / significant policy.
           Medium = new tools / smaller funding / company updates / applied research.
           Low = minor updates / opinion pieces.
Alert = true ONLY for funding >$100M, landmark model release, or major government AI legislation.
Trends: 5-7 short topic names (2-4 words) from patterns visible across this batch.
SKIP any article not directly about AI.

Articles:
{snippets}"""


# ─────────────────────────────────────────────────────────────────────────────
# LLM Provider System — Fallback Chain
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LLMProvider:
    name:       str
    url:        str
    headers:    dict
    model:      str
    max_tokens: int
    timeout:    float
    available:  bool


def build_provider_chain() -> list[LLMProvider]:
    """
    Returns ordered list of available LLM providers.
    The env-configured LLM_PROVIDER is placed first; remaining act as fallbacks.
    """
    all_providers = [
        LLMProvider(
            name="ollama",
            url=f"{OLLAMA_URL}/v1/chat/completions",
            headers={"Content-Type": "application/json"},
            model=PROVIDER_MODELS["ollama"],
            max_tokens=8192,
            timeout=360.0,
            available=True,             # always attempt — fails fast if not running
        ),
        LLMProvider(
            name="groq",
            url="https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            model=PROVIDER_MODELS["groq"],
            max_tokens=4096,
            timeout=60.0,
            available=bool(GROQ_KEY),
        ),
        LLMProvider(
            name="openai",
            url="https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
            model=PROVIDER_MODELS["openai"],
            max_tokens=4096,
            timeout=60.0,
            available=bool(OPENAI_KEY),
        ),
    ]

    available = [p for p in all_providers if p.available]
    # Put the configured primary first, rest follow as ordered fallbacks
    primary = next((p for p in available if p.name == LLM_PROVIDER), None)
    if primary:
        chain = [primary] + [p for p in available if p.name != LLM_PROVIDER]
    else:
        chain = available
    return chain


async def _call_provider(provider: LLMProvider, articles: list[dict]) -> dict:
    """Single call to one LLM provider. Raises on any failure."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            provider.url,
            headers=provider.headers,
            json={
                "model":      provider.model,
                "max_tokens": provider.max_tokens,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": build_user_prompt(articles)},
                ],
            },
            timeout=provider.timeout,
        )
    if r.status_code != 200:
        raise httpx.HTTPStatusError(
            f"HTTP {r.status_code}: {r.text[:200]}",
            request=r.request, response=r,
        )
    raw = r.json()["choices"][0]["message"]["content"].strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw
    raw = raw.rstrip("```").strip()
    if not raw:
        raise ValueError("LLM returned empty content")
    return json.loads(raw)


async def llm_process(articles: list[dict]) -> tuple[dict, str, int]:
    """
    Try each provider in the fallback chain with one retry on transient errors.
    Returns (result_dict, provider_name_used, fallback_count).
    Raises RuntimeError if every provider fails.
    """
    chain = build_provider_chain()
    if not chain:
        raise RuntimeError("No LLM providers are configured or available")

    fallback_count = 0
    last_error     = "unknown error"

    for i, provider in enumerate(chain):
        for attempt in range(2):          # attempt 0 = first try, attempt 1 = retry
            try:
                result = await _call_provider(provider, articles)
                log.info(f"[LLM] {provider.name} success"
                         + (f" (fallback #{i})" if i > 0 else " (primary)"))
                return result, provider.name, fallback_count

            except httpx.TimeoutException:
                reason = f"timeout after {provider.timeout}s"

            except httpx.ConnectError:
                reason = "connection refused — service not running"
                # No retry useful for connection errors
                break

            except httpx.HTTPStatusError as e:
                code   = e.response.status_code
                reason = f"HTTP {code}"
                # Only retry rate-limit (429) or service-unavailable (503)
                if code not in (429, 503) or attempt == 1:
                    break
                log.info(f"[LLM] {provider.name} rate-limited — retrying in 3s")
                await asyncio.sleep(3)

            except json.JSONDecodeError as e:
                reason = f"JSON parse error: {str(e)[:80]}"
                break

            except ValueError as e:
                reason = str(e)
                break

            except Exception as e:
                reason = str(e)[:120]
                break

            log.warning(f"[LLM] {provider.name} failed → {reason} (attempt {attempt + 1})")
            if attempt == 0:
                log.info(f"[LLM] Retrying {provider.name}...")
            else:
                last_error = reason

        # Provider exhausted — move to next
        fallback_count += 1
        log.warning(f"[LLM] {provider.name} giving up — last error: {last_error}")
        if i + 1 < len(chain):
            log.info(f"[LLM] Switching to → {chain[i + 1].name}")

    raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")


# ─────────────────────────────────────────────────────────────────────────────
# Data Sources — Parallel Fetching with Isolation
# ─────────────────────────────────────────────────────────────────────────────

NEWS_QUERIES = [
    "artificial intelligence",
    "OpenAI Anthropic Google DeepMind LLM",
    "AI tools product launch",
    "AI startup funding investment",
    "AI regulation policy government",
    "machine learning research",
    "generative AI",
]

ARXIV_CATS = ["cs.AI", "cs.LG", "cs.CL"]


async def fetch_newsapi(client: httpx.AsyncClient) -> list[dict]:
    if not NEWS_API_KEY:
        raise ValueError("NEWS_API_KEY not configured")
    articles  = []
    cutoff    = (datetime.utcnow() - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S")
    fail_count = 0
    for q in NEWS_QUERIES:
        try:
            r = await client.get(
                "https://newsapi.org/v2/everything",
                params={"q": q, "sortBy": "publishedAt", "pageSize": 10,
                        "from": cutoff, "language": "en", "apiKey": NEWS_API_KEY},
                timeout=10,
            )
            r.raise_for_status()
            for a in r.json().get("articles", []):
                if a.get("title") and a.get("title") != "[Removed]":
                    articles.append({
                        "title":        a["title"],
                        "source":       a.get("source", {}).get("name", "NewsAPI"),
                        "url":          a.get("url", ""),
                        "content":      (a.get("description") or "") + " " + (a.get("content") or ""),
                        "published_at": a.get("publishedAt", ""),
                        "_data_source": "newsapi",
                    })
        except Exception as e:
            fail_count += 1
            log.warning(f"[SOURCE] NewsAPI query '{q}' failed: {e}")

    if fail_count == len(NEWS_QUERIES):
        raise RuntimeError(f"NewsAPI: all {len(NEWS_QUERIES)} queries failed")
    if fail_count:
        log.warning(f"[SOURCE] NewsAPI: {fail_count}/{len(NEWS_QUERIES)} queries failed, {len(articles)} articles recovered")
    return articles


async def fetch_arxiv(client: httpx.AsyncClient) -> list[dict]:
    papers = []
    for cat in ARXIV_CATS:
        try:
            r = await client.get(
                "https://export.arxiv.org/api/query",
                params={"search_query": f"cat:{cat}", "start": 0,
                        "max_results": 5, "sortBy": "submittedDate", "sortOrder": "descending"},
                timeout=15,
            )
            r.raise_for_status()
            import xml.etree.ElementTree as ET
            ns   = "{http://www.w3.org/2005/Atom}"
            root = ET.fromstring(r.text)
            for entry in root.findall(f"{ns}entry"):
                title     = entry.findtext(f"{ns}title",   "").replace("\n", " ").strip()
                summary   = entry.findtext(f"{ns}summary", "").replace("\n", " ").strip()[:600]
                url       = entry.findtext(f"{ns}id",      "")
                published = entry.findtext(f"{ns}published","")
                papers.append({"title": title, "source": f"arXiv/{cat}",
                               "url": url, "content": summary,
                               "published_at": published, "_data_source": "arxiv"})
        except Exception as e:
            log.warning(f"[SOURCE] arXiv {cat} failed: {e}")

    if not papers:
        raise RuntimeError("arXiv returned no papers across all categories")
    return papers


async def fetch_github(client: httpx.AsyncClient) -> list[dict]:
    """
    Fetch recently created AI repositories from GitHub.
    Used as a supplemental source for product launch signals.
    """
    cutoff  = (datetime.utcnow() - timedelta(hours=72)).strftime("%Y-%m-%d")
    queries = [
        f"topic:llm topic:ai created:>{cutoff} stars:>5",
        f"topic:machine-learning created:>{cutoff} stars:>20",
    ]
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    articles   = []
    fail_count = 0
    for q in queries:
        try:
            r = await client.get(
                "https://api.github.com/search/repositories",
                params={"q": q, "sort": "updated", "per_page": 8, "order": "desc"},
                headers=headers,
                timeout=10,
            )
            r.raise_for_status()
            for repo in r.json().get("items", []):
                desc   = (repo.get("description") or "").strip()
                topics = " ".join(repo.get("topics", []))
                if not desc:
                    continue
                articles.append({
                    "title":        f"{repo['name']}: {desc[:120]}",
                    "source":       "GitHub",
                    "url":          repo.get("html_url", ""),
                    "content":      f"{desc} Topics: {topics}. Stars: {repo.get('stargazers_count', 0)}",
                    "published_at": repo.get("created_at", ""),
                    "_data_source": "github",
                })
        except Exception as e:
            fail_count += 1
            log.warning(f"[SOURCE] GitHub query failed: {e}")

    if fail_count == len(queries):
        raise RuntimeError("GitHub: all repository queries failed")
    return articles


async def fetch_serpapi(client: httpx.AsyncClient) -> list[dict]:
    """Fetch AI news via SerpAPI (Google News). Optional — requires SERPAPI_KEY."""
    if not SERPAPI_KEY:
        raise ValueError("SERPAPI_KEY not configured — skipping")

    articles   = []
    queries    = ["artificial intelligence news", "AI tools launched", "AI research breakthrough"]
    fail_count = 0
    for q in queries:
        try:
            r = await client.get(
                "https://serpapi.com/search",
                params={"q": q, "api_key": SERPAPI_KEY, "tbm": "nws", "num": 8},
                timeout=10,
            )
            r.raise_for_status()
            for item in r.json().get("news_results", []):
                articles.append({
                    "title":        item.get("title", ""),
                    "source":       item.get("source", "SerpAPI"),
                    "url":          item.get("link", ""),
                    "content":      item.get("snippet", ""),
                    "published_at": item.get("date", ""),
                    "_data_source": "serpapi",
                })
        except Exception as e:
            fail_count += 1
            log.warning(f"[SOURCE] SerpAPI query failed: {e}")

    if fail_count == len(queries):
        raise RuntimeError("SerpAPI: all queries failed")
    return articles


async def fetch_all_sources(client: httpx.AsyncClient) -> tuple[list[dict], dict]:
    """
    Fetch all sources in parallel. Each source failure is fully isolated — one
    failing source never blocks others. Returns merged articles + per-source status.
    """
    source_tasks: dict[str, asyncio.coroutine] = {
        "newsapi": fetch_newsapi(client),
        "arxiv":   fetch_arxiv(client),
        "github":  fetch_github(client),
    }
    if SERPAPI_KEY:
        source_tasks["serpapi"] = fetch_serpapi(client)

    raw_results = await asyncio.gather(*source_tasks.values(), return_exceptions=True)

    all_articles: list[dict] = []
    source_status: dict      = {}

    for name, result in zip(source_tasks.keys(), raw_results):
        if isinstance(result, Exception):
            source_status[name] = {"success": False, "count": 0, "error": str(result)[:200]}
            log.warning(f"[SOURCE] {name.upper()} FAILED → {result}")
        else:
            count = len(result)
            source_status[name] = {"success": True, "count": count, "error": None}
            all_articles.extend(result)
            log.info(f"[SOURCE] {name.upper()} → {count} articles")

    if DEBUG_MODE:
        summary = {k: v["count"] for k, v in source_status.items() if v["success"]}
        log.info(f"[DEBUG] Source totals: {summary}")

    return all_articles, source_status


def dedup_articles(articles: list[dict]) -> list[dict]:
    seen, out = set(), []
    for a in articles:
        key = hashlib.md5(a["title"].lower().strip().encode()).hexdigest()
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# AI Keyword Pre-filter
# ─────────────────────────────────────────────────────────────────────────────

_AI_KEYWORDS = [
    "artificial intelligence", " ai ", "ai-", "machine learning", "deep learning",
    "neural network", "large language model", " llm", "gpt-", "chatgpt", "openai",
    "anthropic", "google deepmind", "deepmind", "gemini", "claude ai", "mistral",
    "llama model", "transformer model", "generative ai", "gen ai", "diffusion model",
    "stable diffusion", "text-to-image", "text-to-video", "text-to-speech",
    "multimodal", "foundation model", "ai model", "ai tool", "ai startup",
    "ai funding", "ai regulation", "ai safety", "ai research", "ai chip",
    "nvidia ai", "gpu cluster", "ai agent", "autonomous ai", "ai assistant",
    "hugging face", "pytorch", "tensorflow", "agi", "language model",
    "embedding model", "vector database", "rag pipeline", "ai company",
    "ai investment", "ai policy", "ai lab", "ai benchmark", "ai ethics",
    "ai governance", "ai inference", "copilot", "meta ai", "microsoft ai",
    "amazon ai", "ai robotics", "computer vision ai", "speech recognition",
    "natural language processing", "nlp model", "ai compute", "ai hardware",
    "ai software", "ai platform", "ai application", "ai integration",
]


def is_ai_related(article: dict) -> bool:
    ds = article.get("_data_source", "")
    # arXiv and GitHub AI-topic repos are always AI-related
    if ds in ("arxiv", "github"):
        return True
    text = (article.get("title", "") + " " + article.get("content", "")).lower()
    return any(kw in text for kw in _AI_KEYWORDS)


# ─────────────────────────────────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS news_items (
                id                   TEXT PRIMARY KEY,
                title                TEXT,
                category             TEXT,
                priority             TEXT,
                summary              TEXT,
                impact               TEXT,
                source               TEXT,
                url                  TEXT,
                confidence           INTEGER DEFAULT 80,
                alert                INTEGER DEFAULT 0,
                raw_content          TEXT,
                published_at         TEXT,
                fetched_at           TEXT,
                overview             TEXT,
                detailed_explanation TEXT,
                key_insights         TEXT,
                why_it_matters       TEXT,
                technical_breakdown  TEXT
            );
            CREATE TABLE IF NOT EXISTS trends (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                topic      TEXT,
                reason     TEXT,
                fetched_at TEXT
            );
            CREATE TABLE IF NOT EXISTS fetch_log (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                status         TEXT,
                items_added    INTEGER,
                message        TEXT,
                ran_at         TEXT,
                source_status  TEXT,
                llm_used       TEXT,
                llm_fallbacks  INTEGER DEFAULT 0,
                duration_s     REAL    DEFAULT 0
            );
        """)
        # Non-destructive migrations for older DBs
        migrations = [
            "ALTER TABLE news_items ADD COLUMN published_at TEXT",
            "ALTER TABLE news_items ADD COLUMN overview TEXT",
            "ALTER TABLE news_items ADD COLUMN detailed_explanation TEXT",
            "ALTER TABLE news_items ADD COLUMN key_insights TEXT",
            "ALTER TABLE news_items ADD COLUMN why_it_matters TEXT",
            "ALTER TABLE news_items ADD COLUMN technical_breakdown TEXT",
            "ALTER TABLE fetch_log ADD COLUMN source_status TEXT",
            "ALTER TABLE fetch_log ADD COLUMN llm_used TEXT",
            "ALTER TABLE fetch_log ADD COLUMN llm_fallbacks INTEGER DEFAULT 0",
            "ALTER TABLE fetch_log ADD COLUMN duration_s REAL DEFAULT 0",
        ]
        for m in migrations:
            try:
                db.execute(m)
            except Exception:
                pass
    log.info("Database ready.")
    if DEBUG_MODE:
        chain = build_provider_chain()
        log.info(f"[DEBUG] LLM provider chain: {[p.name for p in chain]}")
        log.info(f"[DEBUG] Active sources: newsapi={'yes' if NEWS_API_KEY else 'NO KEY'}, "
                 f"serpapi={'yes' if SERPAPI_KEY else 'no key'}, "
                 f"github={'authenticated' if GITHUB_TOKEN else 'anonymous'}")


def _log_fetch(status, items_added, message, source_status=None,
               llm_used=None, llm_fallbacks=0, duration_s=0.0):
    with get_db() as db:
        db.execute(
            """INSERT INTO fetch_log
               (status, items_added, message, ran_at, source_status, llm_used, llm_fallbacks, duration_s)
               VALUES (?,?,?,?,?,?,?,?)""",
            (status, items_added, message, datetime.utcnow().isoformat(),
             json.dumps(source_status or {}), llm_used, llm_fallbacks, duration_s),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Core Pipeline
# ─────────────────────────────────────────────────────────────────────────────

async def run_pipeline():
    if _pipeline_lock.locked():
        log.info("Pipeline already running — skipping duplicate trigger")
        return
    async with _pipeline_lock:
        await _run_pipeline()


async def _run_pipeline():
    global _pipeline_status

    log.info("━" * 60)
    log.info("[PIPELINE] Started")
    start = time.time()

    errors:        list[str] = []
    source_status: dict      = {}
    llm_used                 = None
    llm_fallbacks            = 0
    total_added              = 0

    # ── Step 1: Fetch from all sources in parallel ────────────────────────────
    async with httpx.AsyncClient() as client:
        all_articles, source_status = await fetch_all_sources(client)

    any_source_ok = any(v["success"] for v in source_status.values())
    if not any_source_ok:
        msg = "ALL data sources failed — pipeline cannot proceed"
        log.error(f"[PIPELINE] {msg}")
        _finalize_status("all_sources_failed", source_status, None, 0, 0, [msg], time.time() - start)
        _log_fetch("all_sources_failed", 0, msg, source_status, None, 0, round(time.time() - start, 1))
        return

    # ── Step 2: Deduplicate and AI keyword pre-filter ─────────────────────────
    all_articles = dedup_articles(all_articles)
    log.info(f"[PIPELINE] {len(all_articles)} unique articles after dedup")

    ai_articles = [a for a in all_articles if is_ai_related(a)]
    dropped     = len(all_articles) - len(ai_articles)
    log.info(f"[PIPELINE] AI filter: {len(ai_articles)} pass, {dropped} dropped")

    if not ai_articles:
        # Genuine empty result — sources worked, no AI content found
        msg = "Sources successful but no AI articles passed keyword filter"
        log.info(f"[PIPELINE] {msg}")
        _finalize_status("no_ai_content", source_status, None, 0, 0, [], time.time() - start)
        _log_fetch("no_ai_content", 0, msg, source_status, None, 0, round(time.time() - start, 1))
        return

    # ── Step 3: LLM batch processing with automatic fallback ─────────────────
    latest_trends: list[dict] = []

    for batch_start in range(0, len(ai_articles), BATCH_SIZE):
        batch     = ai_articles[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        log.info(f"[PIPELINE] Batch {batch_num}: {len(batch)} articles")

        try:
            result, provider_used, fallbacks = await llm_process(batch)
            llm_used       = provider_used
            llm_fallbacks += fallbacks

            if DEBUG_MODE:
                log.info(
                    f"[DEBUG] Batch {batch_num} complete — provider: {provider_used}, "
                    f"fallbacks: {fallbacks}, items returned: {len(result.get('items', []))}"
                )

        except RuntimeError as e:
            err = f"Batch {batch_num} — all LLM providers failed: {e}"
            log.error(f"[PIPELINE] {err}")
            errors.append(err)
            continue

        items  = result.get("items", [])
        trends = result.get("trends", [])
        if trends:
            latest_trends = trends

        with get_db() as db:
            now = datetime.utcnow().isoformat()
            for item in items:
                uid               = hashlib.md5(item.get("title", "").lower().strip().encode()).hexdigest()
                key_insights_json = json.dumps(item.get("key_insights") or [])
                overview          = item.get("overview", "")
                try:
                    db.execute(
                        """INSERT OR IGNORE INTO news_items
                           (id, title, category, priority, summary, impact, source, url,
                            confidence, alert, raw_content, published_at, fetched_at,
                            overview, detailed_explanation, key_insights, why_it_matters, technical_breakdown)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (uid,
                         item.get("title"),
                         item.get("category"),
                         item.get("priority"),
                         overview[:300],                  # summary = overview excerpt for backward compat
                         item.get("why_it_matters", ""),
                         item.get("source"),
                         item.get("url"),
                         80,
                         1 if item.get("alert") else 0,
                         "",
                         item.get("published_at", ""),
                         now,
                         overview,
                         item.get("detailed_explanation", ""),
                         key_insights_json,
                         item.get("why_it_matters", ""),
                         item.get("technical_breakdown", "")),
                    )
                    if db.execute("SELECT changes()").fetchone()[0]:
                        total_added += 1
                except Exception as e:
                    log.warning(f"[PIPELINE] DB insert error: {e}")

    # ── Step 4: Persist trends ────────────────────────────────────────────────
    if latest_trends:
        with get_db() as db:
            db.execute("DELETE FROM trends")
            now = datetime.utcnow().isoformat()
            for t in latest_trends:
                db.execute("INSERT INTO trends (topic, reason, fetched_at) VALUES (?,?,?)",
                           (t.get("topic"), t.get("reason"), now))

    # ── Step 5: Finalize ──────────────────────────────────────────────────────
    elapsed      = round(time.time() - start, 1)
    final_status = "partial_error" if errors else "success"

    _finalize_status(final_status, source_status, llm_used, llm_fallbacks,
                     total_added, errors, elapsed)
    _log_fetch(final_status, total_added,
               f"Added {total_added} items in {elapsed}s | LLM: {llm_used} | fallbacks: {llm_fallbacks}",
               source_status, llm_used, llm_fallbacks, elapsed)

    log.info(
        f"[PIPELINE] Done — {total_added} new | LLM: {llm_used} | "
        f"fallbacks: {llm_fallbacks} | {elapsed}s"
    )
    log.info("━" * 60)


def _finalize_status(status, source_status, llm_used, llm_fallbacks,
                     items_added, errors, duration_s):
    global _pipeline_status
    _pipeline_status = {
        "ran_at":            datetime.utcnow().isoformat(),
        "status":            status,
        "sources":           source_status,
        "llm_provider_used": llm_used,
        "llm_fallbacks":     llm_fallbacks,
        "items_added":       items_added,
        "errors":            errors,
        "duration_s":        duration_s,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler
# ─────────────────────────────────────────────────────────────────────────────

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    chain = build_provider_chain()
    log.info(f"[STARTUP] LLM provider chain: {[p.name for p in chain]}")
    log.info(f"[STARTUP] Data sources: newsapi, arxiv, github"
             + (", serpapi" if SERPAPI_KEY else "") )
    scheduler.add_job(run_pipeline, "interval", minutes=FETCH_INTERVAL, id="pipeline")
    scheduler.start()
    log.info(f"[STARTUP] Scheduler: every {FETCH_INTERVAL} minutes")
    asyncio.create_task(run_pipeline())
    yield
    scheduler.shutdown()


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI Application
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="AI News Intelligence", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


@app.get("/api/news")
def get_news(
    priority: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    limit:    int            = Query(30, le=100),
    hours:    int            = Query(48),
    page:     int            = Query(1),
):
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    where, params = ["fetched_at >= ?"], [cutoff]
    if priority:
        where.append("priority = ?"); params.append(priority)
    if category:
        where.append("category = ?"); params.append(category)
    base   = f"SELECT * FROM news_items WHERE {' AND '.join(where)}"
    offset = (page - 1) * limit
    with get_db() as db:
        rows  = db.execute(base + " ORDER BY fetched_at DESC LIMIT ? OFFSET ?",
                           params + [limit, offset]).fetchall()
        total = db.execute(f"SELECT COUNT(*) FROM news_items WHERE {' AND '.join(where)}",
                           params).fetchone()[0]
    items = []
    for r in rows:
        row = dict(r)
        if row.get("key_insights"):
            try:
                row["key_insights"] = json.loads(row["key_insights"])
            except Exception:
                row["key_insights"] = []
        else:
            row["key_insights"] = []
        items.append(row)

    ps = _pipeline_status
    return {
        "items":       items,
        "total":       total,
        "page":        page,
        # pipeline_ok = False means sources failed, so "no data" UI should warn, not just say "empty"
        "pipeline_ok": ps.get("status") not in ("all_sources_failed",),
        "data_status": ps.get("status", "idle"),
    }


@app.get("/api/trends")
def get_trends():
    with get_db() as db:
        rows = db.execute("SELECT * FROM trends ORDER BY id DESC LIMIT 20").fetchall()
    return {"trends": [dict(r) for r in rows]}


@app.get("/api/stats")
def get_stats():
    today = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    with get_db() as db:
        total_today    = db.execute("SELECT COUNT(*) FROM news_items WHERE fetched_at >= ?", (today,)).fetchone()[0]
        tools_launched = db.execute("SELECT COUNT(*) FROM news_items WHERE category='Product Launch' AND fetched_at >= ?", (today,)).fetchone()[0]
        research       = db.execute("SELECT COUNT(*) FROM news_items WHERE category='Research' AND fetched_at >= ?", (today,)).fetchone()[0]
        funding        = db.execute("SELECT COUNT(*) FROM news_items WHERE category='Funding' AND fetched_at >= ?", (today,)).fetchone()[0]
        trend_count    = db.execute("SELECT COUNT(*) FROM trends").fetchone()[0]
        alerts         = db.execute("SELECT * FROM news_items WHERE alert=1 ORDER BY fetched_at DESC LIMIT 3").fetchall()
        last_run       = db.execute("SELECT ran_at,status,items_added,llm_used FROM fetch_log ORDER BY id DESC LIMIT 1").fetchone()
        cat_counts     = db.execute(
            "SELECT category, COUNT(*) as cnt FROM news_items WHERE fetched_at >= ? GROUP BY category ORDER BY cnt DESC",
            (today,)
        ).fetchall()

    ps = _pipeline_status
    return {
        "total_today":     total_today,
        "tools_launched":  tools_launched,
        "research_papers": research,
        "funding_news":    funding,
        "trending_count":  trend_count,
        "alerts":          [dict(a) for a in alerts],
        "last_run":        dict(last_run) if last_run else None,
        "categories":      [dict(c) for c in cat_counts],
        # Pipeline health fields
        "pipeline_status": ps.get("status", "idle"),
        "source_health":   {k: v.get("success", False) for k, v in ps.get("sources", {}).items()},
        "llm_used":        ps.get("llm_provider_used"),
        "llm_fallbacks":   ps.get("llm_fallbacks", 0),
    }


@app.get("/api/health")
def get_health():
    """Full system health — pipeline state, source status, LLM chain, errors."""
    chain = build_provider_chain()
    ps    = _pipeline_status
    return {
        "status":           ps.get("status", "idle"),
        "last_run_at":      ps.get("ran_at"),
        "items_added_last": ps.get("items_added", 0),
        "duration_s":       ps.get("duration_s", 0),
        "llm_chain":        [p.name for p in chain],
        "llm_used_last":    ps.get("llm_provider_used"),
        "llm_fallbacks":    ps.get("llm_fallbacks", 0),
        "sources":          ps.get("sources", {}),
        "errors":           ps.get("errors", []),
        "debug_mode":       DEBUG_MODE,
    }


@app.get("/api/debug/logs")
def get_debug_logs(limit: int = Query(10, le=50)):
    """Recent pipeline run history with per-source and per-provider detail."""
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM fetch_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for r in rows:
        row = dict(r)
        if row.get("source_status"):
            try:
                row["source_status"] = json.loads(row["source_status"])
            except Exception:
                pass
        result.append(row)
    return {"logs": result, "total": len(result)}


@app.post("/api/refresh")
async def force_refresh(background_tasks: BackgroundTasks):
    background_tasks.add_task(run_pipeline)
    return {"status": "refresh triggered"}


# Serve frontend
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
