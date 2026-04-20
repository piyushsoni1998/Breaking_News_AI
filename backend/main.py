"""
AI Pulse — Intelligence Pipeline v2
Multi-source AI news fetching with cascade fallback, LLM enrichment, and structured output.
"""

import os, re, time, hashlib, json, sqlite3, asyncio, logging, html as _html
from datetime import datetime, timedelta, timezone
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

NEWS_API_KEY    = os.getenv("NEWS_API_KEY", "")
LLM_PROVIDER    = os.getenv("LLM_PROVIDER", "ollama").lower()   # default: ollama → groq → openai
OPENAI_KEY      = os.getenv("OPENAI_API_KEY", "")
GROQ_KEY        = os.getenv("GROQ_API_KEY", "")
OLLAMA_URL      = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
LLM_MODEL       = os.getenv("LLM_MODEL", "qwen2.5:7b")
FETCH_INTERVAL  = int(os.getenv("FETCH_INTERVAL_MINUTES", "10"))
DB_PATH         = os.getenv("DB_PATH", "news.db")
SERPAPI_KEY     = os.getenv("SERPAPI_KEY", "")
GITHUB_TOKEN    = os.getenv("GITHUB_TOKEN", "")
DEBUG_MODE      = os.getenv("DEBUG", "false").lower() == "true"
BATCH_SIZE      = 8
# Trigger secondary sources when Wave 1 yields fewer than this many raw articles
MIN_THRESHOLD   = int(os.getenv("MIN_ARTICLES_THRESHOLD", "10"))

# NewsAPI daily quota guard (free tier = 100 req/day)
_newsapi_calls_today: int = 0
_newsapi_reset_date:  str = ""
NEWSAPI_DAILY_LIMIT: int = int(os.getenv("NEWSAPI_DAILY_LIMIT", "90"))

PROVIDER_MODELS = {
    "ollama": LLM_MODEL,
    "groq":   os.getenv("GROQ_MODEL",   "llama-3.3-70b-versatile"),
    "openai": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ai-news")

_pipeline_lock = asyncio.Lock()
_pipeline_status: dict = {
    "ran_at": None, "status": "idle", "sources": {},
    "llm_provider_used": None, "llm_fallbacks": 0,
    "items_added": 0, "errors": [], "duration_s": 0,
}

# ─────────────────────────────────────────────────────────────────────────────
# LLM Prompts
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert AI technology journalist and analyst.

RELEVANCE RULES:
✅ INCLUDE articles covering ANY of these AI topics:
   - AI/ML/LLM tools, models, agents, assistants, chatbots
   - New AI product launches, releases, announcements, features, updates
   - AI research papers, benchmarks, scientific discoveries, datasets
   - AI startup funding, investments, acquisitions, valuations, IPOs
   - AI regulation, government policy, legislation, ethics, compliance, lawsuits
   - AI chips, hardware, compute infrastructure, datacenters
   - AI company strategies, partnerships, hires, layoffs
   - Open-source AI repositories and frameworks
   - Any company integrating AI into their products

✅ INCLUDE borderline cases — when in doubt, include rather than exclude.
❌ EXCLUDE only articles where AI is a single passing word and the story is entirely unrelated.

CLASSIFICATION (pick the single best fit):
- Research        → academic papers, ML breakthroughs, benchmarks, datasets
- Product Launch  → new AI tools, models, apps, platforms, features, updates going live
- Funding         → investments, acquisitions, funding rounds, valuations, IPOs
- Company Update  → strategy, partnerships, personnel, company news
- Regulation      → policy, government, ethics, legal, compliance, lawsuits
- Trending        → viral AI discussions, community trends, broad industry shifts

PRIORITY:
- High   → landmark model release, funding >$50M, breakthrough research, major policy
- Medium → new tools, smaller funding, notable company news, applied research
- Low    → minor updates, opinion pieces, incremental improvements

IMPORTANT: Return ONLY valid JSON — no markdown fences, no preamble, no trailing text."""


def build_user_prompt(articles: list[dict]) -> str:
    snippets = "\n\n".join(
        f"[{i+1}] TITLE: {a['title']}\n"
        f"SOURCE: {a['source']}\nURL: {a['url']}\n"
        f"PUBLISHED: {a.get('published_at', '')}\n"
        f"HINT_CATEGORY: {a.get('_hint_category', '')}\n"
        f"CONTENT: {a['content'][:800]}"
        for i, a in enumerate(articles)
    )
    return f"""Analyze these {len(articles)} articles. Include ONLY articles where AI is a primary or significant focus.

Return this exact JSON (no extra text):
{{
  "items": [
    {{
      "title": "Refined professional headline — clear and specific",
      "category": "Research|Product Launch|Funding|Company Update|Regulation|Trending",
      "priority": "High|Medium|Low",
      "company": "Primary company or product name (empty string if not applicable)",
      "overview": "Two full paragraphs. First: what happened and who is involved. Second: why now and immediate context.",
      "detailed_explanation": "Four to six paragraphs: (1) full announcement details, (2) technical or business specifics, (3) background and prior context, (4) key players and their roles, (5) competitive landscape, (6) open questions or risks.",
      "key_insights": [
        "Most important single takeaway from this development",
        "Key business, strategic, or competitive implication",
        "Non-obvious signal that industry watchers should note",
        "Technical or scientific significance if applicable",
        "Connection to the broader AI industry trajectory"
      ],
      "why_it_matters": "Two paragraphs: (1) near-term implications for developers, businesses, and users; (2) longer-term outlook and who benefits or gets disrupted.",
      "technical_breakdown": "2-3 paragraphs explaining technical aspects in plain language. Empty string if not primarily technical.",
      "source": "Source publication name",
      "url": "Original article URL",
      "published_at": "ISO 8601 datetime",
      "alert": false
    }}
  ],
  "trends": [
    {{ "topic": "Short Topic Name (2-4 words)", "reason": "One sentence why this is trending across articles" }}
  ]
}}

Rules:
- Priority High   = landmark model release / funding >$50M / breakthrough research / major policy
- Priority Medium = new tools / smaller funding / company news / applied research
- Priority Low    = minor updates / opinion / incremental changes
- Alert = true ONLY for: funding >$100M, landmark model release, major government AI legislation
- Trends: 5-7 cross-cutting topics visible across this batch of articles
- HINT_CATEGORY is a pre-analysis hint — use it to inform but not override your own judgment
- When in doubt, INCLUDE the article — it is better to classify than to skip

Articles:
{snippets}"""


# ─────────────────────────────────────────────────────────────────────────────
# Date Utilities
# ─────────────────────────────────────────────────────────────────────────────

def normalize_date(raw: str) -> str:
    """Normalize any date string to UTC ISO 8601. Returns empty string on failure."""
    if not raw:
        return ""
    raw = raw.strip()

    # ISO 8601 variants
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S+00:00",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass

    # RFC 2822 (Reddit, RSS)
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw).astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        pass

    # Unix timestamp (Reddit created_utc)
    try:
        ts = float(raw)
        if ts > 0:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OSError):
        pass

    return raw  # return as-is if nothing worked


# ─────────────────────────────────────────────────────────────────────────────
# LLM Provider System — Priority: OpenAI → Groq → Ollama
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
    Build the LLM fallback chain.

    Natural fallback order (cheapest/local first → free cloud → paid cloud):
        ollama → groq → openai

    LLM_PROVIDER moves the chosen provider to position 0;
    the remaining providers keep their natural relative order.
    This ensures that with LLM_PROVIDER=ollama the chain is always
    ollama → groq → openai, never ollama → openai → groq.
    """
    # Define in natural fallback order — DO NOT reorder
    all_providers = [
        LLMProvider(
            name="ollama",
            url=f"{OLLAMA_URL}/v1/chat/completions",
            headers={"Content-Type": "application/json"},
            model=PROVIDER_MODELS["ollama"],
            max_tokens=8192,
            timeout=8.0,        # fast fail: if no response in 8s, skip to next provider
            available=True,     # always attempt — ConnectError fires fast if not running
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
            timeout=90.0,
            available=bool(OPENAI_KEY),
        ),
    ]

    available = [p for p in all_providers if p.available]
    primary   = next((p for p in available if p.name == LLM_PROVIDER), None)
    if primary:
        # Keep natural order for the remaining providers
        return [primary] + [p for p in available if p.name != LLM_PROVIDER]
    return available


async def _call_provider(provider: LLMProvider, articles: list[dict]) -> dict:
    """Single LLM call. Raises on any failure."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            provider.url,
            headers=provider.headers,
            json={
                "model":       provider.model,
                "max_tokens":  provider.max_tokens,
                "temperature": 0.3,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": build_user_prompt(articles)},
                ],
            },
            timeout=provider.timeout,
        )
    if r.status_code != 200:
        raise httpx.HTTPStatusError(
            f"HTTP {r.status_code}: {r.text[:300]}",
            request=r.request, response=r,
        )
    raw = r.json()["choices"][0]["message"]["content"].strip()
    # Strip markdown fences robustly
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$",          "", raw)
    raw = raw.strip()
    if not raw:
        raise ValueError("LLM returned empty content")
    return json.loads(raw)


async def llm_process(articles: list[dict]) -> tuple[dict, str, int]:
    """
    Try each provider in the chain with one retry on transient errors.
    Returns (result_dict, provider_name_used, fallback_count).
    """
    chain = build_provider_chain()
    if not chain:
        raise RuntimeError("No LLM providers configured or available")

    fallback_count = 0
    last_error     = "unknown"

    for i, provider in enumerate(chain):
        for attempt in range(2):
            try:
                result = await _call_provider(provider, articles)
                log.info(
                    f"[LLM] {provider.name} success"
                    + (f" (fallback #{i})" if i > 0 else " (primary)")
                    + (f" — {len(result.get('items', []))} items" if DEBUG_MODE else "")
                )
                return result, provider.name, fallback_count

            except httpx.ConnectError:
                last_error = "connection refused — service not running"
                break   # no point retrying a connection error

            except httpx.TimeoutException:
                last_error = f"timeout after {provider.timeout}s"
                break   # no retry on timeout — move to next provider immediately

            except httpx.HTTPStatusError as e:
                code       = e.response.status_code
                last_error = f"HTTP {code}"
                if code not in (429, 503) or attempt == 1:
                    break
                log.info(f"[LLM] {provider.name} rate-limited — retrying in 5s")
                await asyncio.sleep(5)

            except json.JSONDecodeError as e:
                last_error = f"JSON parse error: {str(e)[:100]}"
                break

            except ValueError as e:
                last_error = str(e)
                break

            except Exception as e:
                last_error = str(e)[:120]
                break

            log.warning(f"[LLM] {provider.name} attempt {attempt+1} failed → {last_error}")

        fallback_count += 1
        log.warning(f"[LLM] {provider.name} exhausted — {last_error}")
        if i + 1 < len(chain):
            log.info(f"[LLM] Switching to → {chain[i+1].name}")

    raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")


# ─────────────────────────────────────────────────────────────────────────────
# AI Keyword Filter & Pre-classification
# ─────────────────────────────────────────────────────────────────────────────

_AI_KEYWORDS = [
    # Core concepts
    "artificial intelligence", " ai ", "ai-", "ai,", "ai.", "(ai)",
    "machine learning", "deep learning", "neural network",
    "large language model", " llm", "llm ", "llms",
    # Company / model names
    "openai", "anthropic", "google deepmind", "deepmind", "hugging face",
    "mistral", "meta ai", "microsoft ai", "amazon ai", "nvidia ai",
    "xai", "grok ai", "cohere", "stability ai", "inflection",
    # Popular models
    "gpt-", "chatgpt", "gemini", "claude", "llama", "grok",
    "copilot", "dall-e", "midjourney", "stable diffusion", "sora",
    "whisper", "codex", "palm ", "bard",
    # Techniques / architectures
    "generative ai", "gen ai", "diffusion model", "transformer model",
    "foundation model", "multimodal", "rag pipeline", "embedding model",
    "vector database", "fine-tun", "reinforcement learning",
    "retrieval augmented", "attention mechanism", "mixture of experts",
    "ai agent", "autonomous ai", "ai assistant", "agentic",
    # Domains
    "text-to-image", "text-to-video", "text-to-speech", "text-to-code",
    "computer vision ai", "speech recognition",
    "natural language processing", "nlp model", "nlp ",
    # Products / platforms
    "ai tool", "ai model", "ai platform", "ai application",
    "ai integration", "ai-powered", "ai powered", "ai feature",
    "ai product", "ai startup", "ai company", "ai lab",
    # Business / policy
    "ai funding", "ai investment", "ai regulation", "ai safety",
    "ai research", "ai chip", "ai compute", "ai hardware",
    "ai software", "ai policy", "ai benchmark", "ai ethics",
    "ai governance", "ai inference", "ai robotics", "gpu cluster",
    # Frameworks
    "pytorch", "tensorflow", "jax ", "huggingface", "langchain",
    "llamaindex", "agi",
]

# Keywords that strongly suggest "Product Launch" category
_LAUNCH_KEYWORDS = [
    "launch", "launched", "release", "released", "releases",
    "introduce", "introduced", "announces", "announced",
    "unveil", "unveiled", "debut", "debuted",
    "now available", "available now", "goes live", "went live",
    "ships", "shipped", "rolls out", "rolled out",
    "beta", "open source", "open-source", "v2", "v3", "2.0", "3.0",
    "new ai tool", "new ai model", "new ai feature", "new model",
    "new product", "new feature", "new tool",
]

# Keywords for Funding category
_FUNDING_KEYWORDS = [
    "funding", "raises", "raised", "investment", "investor",
    "series a", "series b", "series c", "seed round",
    "valuation", "billion", " million", "ipo", "acquisition",
    "acquires", "acquired", "merge", "merger",
]

# Keywords for Regulation category
_REGULATION_KEYWORDS = [
    "regulation", "policy", "legislation", "government", "congress",
    "senate", "eu ai", "ai act", "ban", "compliance", "legal",
    "lawsuit", "fine", "penalty", "ethics board", "safety board",
]


def _hint_category(title: str, content: str) -> str:
    """Pre-classify article to give the LLM a helpful hint."""
    text = (title + " " + content).lower()
    if any(kw in text for kw in _LAUNCH_KEYWORDS):
        return "Product Launch"
    if any(kw in text for kw in _FUNDING_KEYWORDS):
        return "Funding"
    if any(kw in text for kw in _REGULATION_KEYWORDS):
        return "Regulation"
    if "arxiv" in text or "research" in text or "paper" in text or "benchmark" in text:
        return "Research"
    return ""


def is_ai_related(article: dict) -> bool:
    """
    Return True if the article is sufficiently AI-related to send to the LLM.
    arXiv and GitHub AI repos are always accepted.
    Reddit posts are accepted if title/content pass the keyword check.
    NewsAPI / SerpAPI require explicit keyword match.
    """
    ds = article.get("_data_source", "")
    if ds in ("arxiv", "github", "rss"):
        return True     # these sources are AI-specific by construction
    text = (article.get("title", "") + " " + article.get("content", "")).lower()
    return any(kw in text for kw in _AI_KEYWORDS)


def dedup_articles(articles: list[dict]) -> list[dict]:
    """Deduplicate by title hash and by URL."""
    seen_titles, seen_urls, out = set(), set(), []
    for a in articles:
        title_key = hashlib.md5(a["title"].lower().strip().encode()).hexdigest()
        url_key   = a.get("url", "").rstrip("/").lower()
        if title_key in seen_titles:
            continue
        if url_key and url_key in seen_urls:
            continue
        seen_titles.add(title_key)
        if url_key:
            seen_urls.add(url_key)
        out.append(a)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Data Sources
# ─────────────────────────────────────────────────────────────────────────────

NEWS_QUERIES = [
    "artificial intelligence OpenAI Anthropic Google Gemini Claude GPT LLM",
    "AI product launch release model tool feature announced",
    "AI startup funding investment acquisition billion",
    "AI regulation policy government law compliance ethics",
]

ARXIV_CATS = ["cs.AI", "cs.LG", "cs.CL"]

# Free RSS feeds — no API key, no quota, refreshed hourly
RSS_FEEDS = [
    ("TechCrunch AI",       "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("VentureBeat AI",      "https://venturebeat.com/category/ai/feed/"),
    ("The Decoder",         "https://the-decoder.com/feed/"),
    ("The Verge AI",        "https://www.theverge.com/ai-artificial-intelligence/rss/index.xml"),
    ("MIT Tech Review AI",  "https://www.technologyreview.com/topic/artificial-intelligence/feed/"),
    ("Wired AI",            "https://www.wired.com/feed/tag/ai/latest/rss"),
    ("Ars Technica AI",     "https://feeds.arstechnica.com/arstechnica/technology-lab"),
    ("ZDNet AI",            "https://www.zdnet.com/topic/artificial-intelligence/rss.xml"),
]

REDDIT_SUBS  = ["MachineLearning", "artificial", "LocalLLaMA", "ChatGPT", "singularity", "AINews"]
_REDDIT_UA   = "AI-Pulse-NewsBot/2.0 (+https://github.com/ai-pulse)"


def _clean_html(text: str) -> str:
    """Strip HTML tags and unescape HTML entities from RSS content."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    return " ".join(text.split())


def _newsapi_quota_ok() -> bool:
    """Return True if we still have daily NewsAPI quota remaining."""
    global _newsapi_calls_today, _newsapi_reset_date
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if _newsapi_reset_date != today:
        _newsapi_calls_today = 0
        _newsapi_reset_date  = today
    return _newsapi_calls_today < NEWSAPI_DAILY_LIMIT


def _newsapi_record_call():
    global _newsapi_calls_today
    _newsapi_calls_today += 1


async def fetch_newsapi(client: httpx.AsyncClient) -> list[dict]:
    global _newsapi_calls_today
    if not NEWS_API_KEY:
        raise ValueError("NEWS_API_KEY not configured")
    if not _newsapi_quota_ok():
        raise RuntimeError(f"NewsAPI: daily quota reached ({_newsapi_calls_today}/{NEWSAPI_DAILY_LIMIT})")
    articles   = []
    cutoff     = (datetime.utcnow() - timedelta(hours=168)).strftime("%Y-%m-%dT%H:%M:%S")
    fail_count = 0
    for q in NEWS_QUERIES:
        if not _newsapi_quota_ok():
            log.warning(f"[SOURCE] NewsAPI: quota reached mid-run, stopping at {_newsapi_calls_today} calls today")
            break
        try:
            r = await client.get(
                "https://newsapi.org/v2/everything",
                params={"q": q, "sortBy": "publishedAt", "pageSize": 20,
                        "from": cutoff, "language": "en", "apiKey": NEWS_API_KEY},
                timeout=12,
            )
            r.raise_for_status()
            _newsapi_record_call()
            for a in r.json().get("articles", []):
                if not a.get("title") or a.get("title") == "[Removed]":
                    continue
                articles.append({
                    "title":        a["title"],
                    "source":       a.get("source", {}).get("name", "NewsAPI"),
                    "url":          a.get("url", ""),
                    "content":      (a.get("description") or "") + " " + (a.get("content") or ""),
                    "published_at": normalize_date(a.get("publishedAt", "")),
                    "_data_source": "newsapi",
                })
        except RuntimeError:
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                _newsapi_calls_today = NEWSAPI_DAILY_LIMIT
                log.warning("[SOURCE] NewsAPI 429 — daily quota exhausted, skipping remaining queries")
                break
            fail_count += 1
            log.warning(f"[SOURCE] NewsAPI query '{q}' failed: {e}")
        except Exception as e:
            fail_count += 1
            log.warning(f"[SOURCE] NewsAPI query '{q}' failed: {e}")

    if fail_count == len(NEWS_QUERIES):
        raise RuntimeError(f"NewsAPI: all {len(NEWS_QUERIES)} queries failed")
    if DEBUG_MODE and fail_count:
        log.info(f"[DEBUG] NewsAPI: {fail_count}/{len(NEWS_QUERIES)} queries failed, {len(articles)} recovered")
    log.info(f"[SOURCE] NewsAPI daily usage: {_newsapi_calls_today}/{NEWSAPI_DAILY_LIMIT}")
    return articles


async def fetch_arxiv(client: httpx.AsyncClient) -> list[dict]:
    papers = []
    for cat in ARXIV_CATS:
        try:
            r = await client.get(
                "https://export.arxiv.org/api/query",
                params={"search_query": f"cat:{cat}", "start": 0,
                        "max_results": 5, "sortBy": "submittedDate", "sortOrder": "descending"},
                timeout=20,
            )
            r.raise_for_status()
            import xml.etree.ElementTree as ET
            ns   = "{http://www.w3.org/2005/Atom}"
            root = ET.fromstring(r.text)
            for entry in root.findall(f"{ns}entry"):
                title     = entry.findtext(f"{ns}title",    "").replace("\n", " ").strip()
                summary   = entry.findtext(f"{ns}summary",  "").replace("\n", " ").strip()[:800]
                url       = entry.findtext(f"{ns}id",       "")
                published = entry.findtext(f"{ns}published", "")
                if title:
                    papers.append({
                        "title":        title,
                        "source":       f"arXiv/{cat}",
                        "url":          url,
                        "content":      summary,
                        "published_at": normalize_date(published),
                        "_data_source": "arxiv",
                    })
        except Exception as e:
            log.warning(f"[SOURCE] arXiv {cat} failed: {e}")
    if not papers:
        raise RuntimeError("arXiv: no papers returned across all categories")
    return papers


async def fetch_reddit(client: httpx.AsyncClient) -> list[dict]:
    """Fetch recent posts from AI subreddits via public JSON API (no auth required)."""
    articles   = []
    fail_count = 0
    headers    = {"User-Agent": _REDDIT_UA, "Accept": "application/json"}

    for sub in REDDIT_SUBS:
        try:
            r = await client.get(
                f"https://www.reddit.com/r/{sub}/new.json",
                params={"limit": 20, "t": "day"},
                headers=headers,
                timeout=15,
                follow_redirects=True,
            )
            r.raise_for_status()
            children = r.json().get("data", {}).get("children", [])
            added = 0
            for post in children:
                p     = post.get("data", {})
                title = (p.get("title") or "").strip()
                if not title or p.get("score", 0) < 3:
                    continue
                text    = (p.get("selftext") or "").strip()[:600]
                flair   = (p.get("link_flair_text") or "").strip()
                url     = p.get("url", "")
                selfurl = f"https://reddit.com{p.get('permalink', '')}"
                content = f"{text} [flair: {flair}]" if (text and flair) else (text or title)
                articles.append({
                    "title":        title,
                    "source":       f"Reddit/r/{sub}",
                    "url":          url if url.startswith("http") else selfurl,
                    "content":      content,
                    "published_at": normalize_date(str(p.get("created_utc", ""))),
                    "_data_source": "reddit",
                })
                added += 1
            if DEBUG_MODE:
                log.info(f"[DEBUG] Reddit r/{sub} → {added} posts")
        except Exception as e:
            fail_count += 1
            log.warning(f"[SOURCE] Reddit r/{sub} failed: {e}")

    if fail_count == len(REDDIT_SUBS):
        raise RuntimeError("Reddit: all subreddits failed")
    return articles


async def fetch_github(client: httpx.AsyncClient) -> list[dict]:
    cutoff  = (datetime.utcnow() - timedelta(hours=72)).strftime("%Y-%m-%d")
    queries = [
        f"topic:llm topic:ai created:>{cutoff} stars:>5",
        f"topic:machine-learning created:>{cutoff} stars:>20",
        f"topic:generative-ai created:>{cutoff} stars:>10",
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
                timeout=12,
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
                    "content":      f"{desc} Topics: {topics}. Stars: {repo.get('stargazers_count', 0)}.",
                    "published_at": normalize_date(repo.get("created_at", "")),
                    "_data_source": "github",
                })
        except Exception as e:
            fail_count += 1
            log.warning(f"[SOURCE] GitHub query failed: {e}")

    if fail_count == len(queries):
        raise RuntimeError("GitHub: all queries failed")
    return articles


async def fetch_rss_feeds(client: httpx.AsyncClient) -> list[dict]:
    """
    Fetch from curated AI news RSS feeds — free, no auth, no quota.
    Covers product launches, funding, regulation, and company news.
    """
    import xml.etree.ElementTree as ET
    _ATOM = "{http://www.w3.org/2005/Atom}"
    _CONTENT = "{http://purl.org/rss/1.0/modules/content/}"

    articles    = []
    fail_count  = 0
    headers     = {"User-Agent": "AI-Pulse-NewsBot/2.0 (+https://github.com/ai-pulse)"}

    for source_name, feed_url in RSS_FEEDS:
        try:
            r = await client.get(feed_url, timeout=15, follow_redirects=True, headers=headers)
            r.raise_for_status()
            root = ET.fromstring(r.content)

            # Support both RSS 2.0 (channel/item) and Atom (feed/entry)
            items = root.findall(".//item")
            if not items:
                items = root.findall(f".//{_ATOM}entry")

            count = 0
            for item in items[:20]:
                # Title
                title = (
                    item.findtext("title") or
                    item.findtext(f"{_ATOM}title") or ""
                ).strip()
                title = _clean_html(title)

                if not title:
                    continue

                # URL
                link = (
                    item.findtext("link") or
                    item.findtext(f"{_ATOM}id") or ""
                ).strip()
                # Atom link is often an element with href attribute, not text
                if not link:
                    link_el = item.find(f"{_ATOM}link")
                    if link_el is not None:
                        link = link_el.get("href", "")

                # Published date
                pub = (
                    item.findtext("pubDate") or
                    item.findtext("dc:date") or
                    item.findtext(f"{_ATOM}published") or
                    item.findtext(f"{_ATOM}updated") or ""
                ).strip()

                # Content / description
                desc = (
                    item.findtext(f"{_CONTENT}encoded") or
                    item.findtext("description") or
                    item.findtext(f"{_ATOM}summary") or
                    item.findtext(f"{_ATOM}content") or ""
                )
                desc = _clean_html(desc)[:800]

                articles.append({
                    "title":        title,
                    "source":       source_name,
                    "url":          link,
                    "content":      desc,
                    "published_at": normalize_date(pub),
                    "_data_source": "rss",
                })
                count += 1

            if DEBUG_MODE:
                log.info(f"[DEBUG] RSS {source_name} → {count} articles")

        except Exception as e:
            fail_count += 1
            log.warning(f"[SOURCE] RSS {source_name} failed: {e}")

    if fail_count == len(RSS_FEEDS):
        raise RuntimeError("RSS: all feeds failed")
    return articles


async def fetch_hackernews(client: httpx.AsyncClient) -> list[dict]:
    """Fetch AI-related stories from Hacker News via Algolia — free, no auth, no rate limits."""
    articles   = []
    hn_queries = [
        "artificial intelligence",
        "LLM language model",
        "AI launch release",
        "machine learning",
    ]
    cutoff_ts = int((datetime.utcnow() - timedelta(hours=168)).timestamp())
    for q in hn_queries:
        try:
            r = await client.get(
                "https://hn.algolia.com/api/v1/search",
                params={"query": q, "tags": "story", "hitsPerPage": 15,
                        "numericFilters": f"created_at_i>{cutoff_ts}"},
                timeout=12,
            )
            r.raise_for_status()
            for hit in r.json().get("hits", []):
                title = (hit.get("title") or "").strip()
                url   = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID','')}"
                if not title:
                    continue
                articles.append({
                    "title":        title,
                    "source":       "Hacker News",
                    "url":          url,
                    "content":      f"{title}. Points: {hit.get('points', 0)}. Comments: {hit.get('num_comments', 0)}.",
                    "published_at": normalize_date(str(hit.get("created_at_i", ""))),
                    "_data_source": "hackernews",
                })
        except Exception as e:
            log.warning(f"[SOURCE] HackerNews query '{q}' failed: {e}")

    if not articles:
        raise RuntimeError("HackerNews: no articles returned")
    return articles


async def fetch_serpapi(client: httpx.AsyncClient) -> list[dict]:
    if not SERPAPI_KEY:
        raise ValueError("SERPAPI_KEY not configured")
    articles   = []
    queries    = [
        "artificial intelligence news today",
        "AI tools launched this week",
        "AI research breakthrough",
    ]
    fail_count = 0
    for q in queries:
        try:
            r = await client.get(
                "https://serpapi.com/search",
                params={"q": q, "api_key": SERPAPI_KEY, "tbm": "nws", "num": 10},
                timeout=12,
            )
            r.raise_for_status()
            for item in r.json().get("news_results", []):
                if not item.get("title"):
                    continue
                articles.append({
                    "title":        item["title"],
                    "source":       item.get("source", "Google News"),
                    "url":          item.get("link", ""),
                    "content":      item.get("snippet", ""),
                    "published_at": normalize_date(item.get("date", "")),
                    "_data_source": "serpapi",
                })
        except Exception as e:
            fail_count += 1
            log.warning(f"[SOURCE] SerpAPI query '{q}' failed: {e}")

    if fail_count == len(queries):
        raise RuntimeError("SerpAPI: all queries failed")
    return articles


async def fetch_all_sources(client: httpx.AsyncClient) -> tuple[list[dict], dict]:
    """
    Cascade fetch strategy:

    Wave 1 (always): NewsAPI + arXiv in parallel
    Wave 2 (if Wave 1 < MIN_THRESHOLD): Reddit + GitHub (+ SerpAPI if key set) in parallel
    Wave 3 (if still < MIN_THRESHOLD): log a warning — pipeline continues with what it has

    Each source failure is fully isolated.
    """
    source_status: dict      = {}
    all_articles: list[dict] = []

    async def _safe(name: str, coro) -> tuple[str, list | Exception]:
        try:
            return name, await coro
        except Exception as e:
            return name, e

    def _collect(results):
        for name, result in results:
            if isinstance(result, Exception):
                source_status[name] = {"success": False, "count": 0, "error": str(result)[:200]}
                log.warning(f"[SOURCE] {name.upper()} FAILED → {result}")
            else:
                source_status[name] = {"success": True, "count": len(result), "error": None}
                all_articles.extend(result)
                log.info(f"[SOURCE] {name.upper()} → {len(result)} articles")

    # ── Wave 1: Primary sources — RSS feeds are free/unlimited, always run ───
    wave1 = await asyncio.gather(
        _safe("rss",     fetch_rss_feeds(client)),   # 8 AI news sites, no quota
        _safe("newsapi", fetch_newsapi(client)),      # bonus when quota available
        _safe("arxiv",   fetch_arxiv(client)),        # research papers
    )
    _collect(wave1)

    if DEBUG_MODE:
        log.info(f"[DEBUG] Wave 1 complete — {len(all_articles)} raw articles")

    # ── Wave 2: Always run — HN/Reddit/GitHub for launch and community signals ──
    wave2_coros = [
        _safe("hackernews", fetch_hackernews(client)),
        _safe("reddit",     fetch_reddit(client)),
        _safe("github",     fetch_github(client)),
    ]
    if len(all_articles) < MIN_THRESHOLD:
        log.info(f"[SOURCE] Wave 1 low ({len(all_articles)} articles) — activating all secondary sources")
    if SERPAPI_KEY:
        wave2_coros.append(_safe("serpapi", fetch_serpapi(client)))
    wave2 = await asyncio.gather(*wave2_coros)
    _collect(wave2)
    if DEBUG_MODE:
        log.info(f"[DEBUG] Wave 2 complete — {len(all_articles)} total articles")

    if not any(v["success"] for v in source_status.values()):
        log.error("[SOURCE] ALL sources failed")
    elif DEBUG_MODE:
        ok  = {k: v["count"] for k, v in source_status.items() if v["success"]}
        err = [k for k, v in source_status.items() if not v["success"]]
        log.info(f"[DEBUG] Source totals: {ok}" + (f" | failed: {err}" if err else ""))

    return all_articles, source_status


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
                company              TEXT    DEFAULT '',
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
        # Non-destructive migrations for older databases
        for sql in [
            "ALTER TABLE news_items ADD COLUMN published_at TEXT",
            "ALTER TABLE news_items ADD COLUMN overview TEXT",
            "ALTER TABLE news_items ADD COLUMN detailed_explanation TEXT",
            "ALTER TABLE news_items ADD COLUMN key_insights TEXT",
            "ALTER TABLE news_items ADD COLUMN why_it_matters TEXT",
            "ALTER TABLE news_items ADD COLUMN technical_breakdown TEXT",
            "ALTER TABLE news_items ADD COLUMN company TEXT DEFAULT ''",
            "ALTER TABLE fetch_log  ADD COLUMN source_status TEXT",
            "ALTER TABLE fetch_log  ADD COLUMN llm_used TEXT",
            "ALTER TABLE fetch_log  ADD COLUMN llm_fallbacks INTEGER DEFAULT 0",
            "ALTER TABLE fetch_log  ADD COLUMN duration_s REAL DEFAULT 0",
        ]:
            try:
                db.execute(sql)
            except Exception:
                pass  # column already exists

    log.info("Database ready.")
    if DEBUG_MODE:
        chain = build_provider_chain()
        log.info(f"[DEBUG] LLM chain: {[p.name for p in chain]}")
        log.info(
            f"[DEBUG] Sources configured — "
            f"newsapi={'YES' if NEWS_API_KEY else 'NO KEY'} | "
            f"serpapi={'YES' if SERPAPI_KEY else 'no key'} | "
            f"github={'auth' if GITHUB_TOKEN else 'anon'} | "
            f"reddit=YES | arxiv=YES"
        )


def _keyword_classify(articles: list[dict]) -> dict:
    """
    Fallback classification using keyword hints when all LLM providers fail.
    Produces structured items good enough to display; LLM will enrich on retry.
    """
    items = []
    for a in articles:
        cat = _hint_category(a.get("title", ""), a.get("content", "")) or "Company Update"
        content = a.get("content", "").strip()
        items.append({
            "title":                a.get("title", ""),
            "category":             cat,
            "priority":             "Medium",
            "company":              "",
            "overview":             content[:500] if content else a.get("title", ""),
            "detailed_explanation": "",
            "key_insights":         [],
            "why_it_matters":       "",
            "technical_breakdown":  "",
            "source":               a.get("source", ""),
            "url":                  a.get("url", ""),
            "published_at":         a.get("published_at", ""),
            "alert":                False,
        })
    return {"items": items, "trends": []}


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

    # ── Step 1: Fetch from all sources (cascade) ──────────────────────────────
    async with httpx.AsyncClient() as client:
        all_articles, source_status = await fetch_all_sources(client)

    if not any(v["success"] for v in source_status.values()):
        msg = "ALL data sources failed — pipeline cannot proceed"
        log.error(f"[PIPELINE] {msg}")
        _finalize_status("all_sources_failed", source_status, None, 0, 0, [msg], time.time() - start)
        _log_fetch("all_sources_failed", 0, msg, source_status, None, 0, round(time.time() - start, 1))
        return

    # ── Step 2: Deduplicate (title + URL) ─────────────────────────────────────
    raw_count    = len(all_articles)
    all_articles = dedup_articles(all_articles)
    log.info(f"[PIPELINE] {raw_count} raw → {len(all_articles)} after dedup")

    # ── Step 3: AI keyword pre-filter ─────────────────────────────────────────
    ai_articles = [a for a in all_articles if is_ai_related(a)]
    dropped     = len(all_articles) - len(ai_articles)
    log.info(f"[PIPELINE] AI filter: {len(ai_articles)} pass, {dropped} dropped")

    if DEBUG_MODE:
        by_source = {}
        for a in ai_articles:
            s = a.get("_data_source", "unknown")
            by_source[s] = by_source.get(s, 0) + 1
        log.info(f"[DEBUG] AI articles by source: {by_source}")

    if not ai_articles:
        msg = "Sources returned data but no articles passed the AI keyword filter"
        log.info(f"[PIPELINE] {msg}")
        _finalize_status("no_ai_content", source_status, None, 0, 0, [], time.time() - start)
        _log_fetch("no_ai_content", 0, msg, source_status, None, 0, round(time.time() - start, 1))
        return

    # ── Step 4: Attach pre-classification hints ───────────────────────────────
    for a in ai_articles:
        a["_hint_category"] = _hint_category(a.get("title", ""), a.get("content", ""))

    if DEBUG_MODE:
        hints = {}
        for a in ai_articles:
            h = a["_hint_category"] or "unclassified"
            hints[h] = hints.get(h, 0) + 1
        log.info(f"[DEBUG] Pre-classification hints: {hints}")

    # ── Step 5: LLM batch processing ──────────────────────────────────────────
    latest_trends: list[dict] = []

    for batch_start in range(0, len(ai_articles), BATCH_SIZE):
        batch     = ai_articles[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        log.info(f"[PIPELINE] Batch {batch_num}/{(len(ai_articles) + BATCH_SIZE - 1) // BATCH_SIZE}: {len(batch)} articles")

        try:
            result, provider_used, fallbacks = await llm_process(batch)
            llm_used       = provider_used
            llm_fallbacks += fallbacks

            if DEBUG_MODE:
                cats = [item.get("category", "?") for item in result.get("items", [])]
                log.info(
                    f"[DEBUG] Batch {batch_num} — provider: {provider_used}, "
                    f"fallbacks: {fallbacks}, returned: {len(result.get('items', []))} items, "
                    f"categories: {cats}"
                )

        except RuntimeError as e:
            err = f"Batch {batch_num}: all LLM providers failed — {e}"
            log.warning(f"[PIPELINE] {err} — using keyword fallback")
            errors.append(err)
            result = _keyword_classify(batch)   # save with keyword hints, LLM enriches later

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
                company           = (item.get("company") or "").strip()
                try:
                    db.execute(
                        """INSERT OR IGNORE INTO news_items
                           (id, title, category, priority, summary, impact, source, url,
                            company, confidence, alert, raw_content,
                            published_at, fetched_at,
                            overview, detailed_explanation, key_insights,
                            why_it_matters, technical_breakdown)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            uid,
                            item.get("title"),
                            item.get("category"),
                            item.get("priority"),
                            overview[:300],                     # backward-compat summary excerpt
                            item.get("why_it_matters", ""),
                            item.get("source"),
                            item.get("url"),
                            company,
                            80,
                            1 if item.get("alert") else 0,
                            "",
                            item.get("published_at", ""),
                            now,
                            overview,
                            item.get("detailed_explanation", ""),
                            key_insights_json,
                            item.get("why_it_matters", ""),
                            item.get("technical_breakdown", ""),
                        ),
                    )
                    if db.execute("SELECT changes()").fetchone()[0]:
                        total_added += 1
                except Exception as e:
                    log.warning(f"[PIPELINE] DB insert error: {e}")

    # ── Step 6: Persist trends ────────────────────────────────────────────────
    if latest_trends:
        with get_db() as db:
            db.execute("DELETE FROM trends")
            now = datetime.utcnow().isoformat()
            for t in latest_trends:
                db.execute(
                    "INSERT INTO trends (topic, reason, fetched_at) VALUES (?,?,?)",
                    (t.get("topic"), t.get("reason"), now),
                )
        if DEBUG_MODE:
            log.info(f"[DEBUG] Trends saved: {[t.get('topic') for t in latest_trends]}")

    # ── Step 7: Finalize ──────────────────────────────────────────────────────
    elapsed      = round(time.time() - start, 1)
    final_status = "partial_error" if errors else "success"

    _finalize_status(final_status, source_status, llm_used, llm_fallbacks,
                     total_added, errors, elapsed)
    _log_fetch(
        final_status, total_added,
        f"Added {total_added} items in {elapsed}s | LLM: {llm_used} | fallbacks: {llm_fallbacks}",
        source_status, llm_used, llm_fallbacks, elapsed,
    )
    log.info(
        f"[PIPELINE] Done — {total_added} new articles | LLM: {llm_used} | "
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
    log.info(
        "[STARTUP] Sources: rss(8 feeds) + newsapi + arxiv + hackernews + reddit + github"
        + (", serpapi" if SERPAPI_KEY else "")
        + f" | cascade threshold: {MIN_THRESHOLD}"
    )
    scheduler.add_job(run_pipeline, "interval", minutes=FETCH_INTERVAL, id="pipeline")
    scheduler.start()
    log.info(f"[STARTUP] Scheduler: every {FETCH_INTERVAL} minutes")
    asyncio.create_task(run_pipeline())
    yield
    scheduler.shutdown()


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI Application
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="AI Pulse Intelligence", version="2.0", lifespan=lifespan)
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
    week = (datetime.utcnow() - timedelta(hours=168)).isoformat()
    with get_db() as db:
        total_today    = db.execute("SELECT COUNT(*) FROM news_items WHERE fetched_at >= ?", (week,)).fetchone()[0]
        tools_launched = db.execute("SELECT COUNT(*) FROM news_items WHERE category='Product Launch' AND fetched_at >= ?", (week,)).fetchone()[0]
        research       = db.execute("SELECT COUNT(*) FROM news_items WHERE category='Research' AND fetched_at >= ?", (week,)).fetchone()[0]
        funding        = db.execute("SELECT COUNT(*) FROM news_items WHERE category='Funding' AND fetched_at >= ?", (week,)).fetchone()[0]
        trend_count    = db.execute("SELECT COUNT(*) FROM trends").fetchone()[0]
        alerts         = db.execute("SELECT * FROM news_items WHERE alert=1 ORDER BY fetched_at DESC LIMIT 3").fetchall()
        last_run       = db.execute("SELECT ran_at,status,items_added,llm_used FROM fetch_log ORDER BY id DESC LIMIT 1").fetchone()
        cat_counts     = db.execute(
            "SELECT category, COUNT(*) as cnt FROM news_items WHERE fetched_at >= ? GROUP BY category ORDER BY cnt DESC",
            (week,)
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
        "pipeline_status": ps.get("status", "idle"),
        "source_health":   {k: v.get("success", False) for k, v in ps.get("sources", {}).items()},
        "llm_used":        ps.get("llm_provider_used"),
        "llm_fallbacks":   ps.get("llm_fallbacks", 0),
    }


@app.get("/api/health")
def get_health():
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
        "min_threshold":    MIN_THRESHOLD,
    }


@app.get("/api/debug/logs")
def get_debug_logs(limit: int = Query(10, le=50)):
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


# Serve frontend static files
frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
