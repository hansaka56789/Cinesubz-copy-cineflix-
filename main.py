#python
import os  
import re
from urllib.parse import urljoin, urlparse, quote_plus

from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

# You can override this with an environment variable if the source domain changes.
BASE = os.getenv("CINESUBZ_BASE", "https://cinesubz.co").rstrip("/")
IMPERSONATE = "chrome124"

app = FastAPI(title="CineSubz Catalog API", version="4.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

session = AsyncSession(impersonate=IMPERSONATE, timeout=25)

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,si;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}


async def fetch(url: str, **kwargs):
    try:
        r = await session.get(url, headers=HEADERS, **kwargs)
    except Exception as e:
        raise HTTPException(502, f"Upstream request failed: {e}")

    if r.status_code == 403:
        raise HTTPException(502, "Source returned 403 Forbidden")
    if r.status_code >= 400:
        raise HTTPException(502, f"Source returned HTTP {r.status_code}")

    return r


def soup_of(html: str):
    return BeautifulSoup(html, "lxml")


def absolute_url(value):
    if not value:
        return None
    return urljoin(BASE + "/", value)


def internal_url(url):
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
        base_host = (urlparse(BASE).hostname or "").lower()
        return host == base_host or host.endswith("." + base_host)
    except Exception:
        return False


def content_type_from_url(url):
    u = (url or "").lower()
    if "/tvshows/" in u:
        return "tv"
    if "/movies/" in u:
        return "movie"
    return None


def image_url(img):
    if not img:
        return None

    for attr in (
        "data-src",
        "data-lazy-src",
        "data-original",
        "data-image",
        "src",
    ):
        value = img.get(attr)
        if value and not value.startswith("data:"):
            return absolute_url(value)

    srcset = img.get("data-srcset") or img.get("srcset")
    if srcset:
        # Prefer the largest candidate.
        candidate = srcset.split(",")[-1].strip().split(" ")[0]
        if candidate:
            return absolute_url(candidate)

    return None


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def find_card_container(anchor):
    # Try common card/article wrappers first.
    for parent in anchor.parents:
        if not getattr(parent, "name", None):
            continue

        classes = " ".join(parent.get("class") or []).lower()

        if parent.name == "article":
            return parent

        if any(
            word in classes
            for word in (
                "item",
                "movie",
                "tvshow",
                "post",
                "result",
                "card",
                "listing",
            )
        ):
            return parent

        # Avoid walking too far up the document.
        if parent.name in ("main", "body"):
            break

    return anchor.parent


def extract_rating(container):
    if not container:
        return None

    text = clean(container.get_text(" ", strip=True))

    for pattern in (
        r"(?:IMDb|IMDB|Rating)\s*[:\-]?\s*(\d+(?:\.\d+)?)",
        r"â˜…\s*(\d+(?:\.\d+)?)",
        r"\b(10(?:\.0)?|[0-9](?:\.[0-9])?)\s*/\s*10\b",
    ):
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1)

    return None


def title_from_anchor(anchor, container):
    title = clean(anchor.get_text(" ", strip=True))

    if len(title) >= 2:
        return title

    if container:
        heading = container.find(["h1", "h2", "h3", "h4", "h5"])
        if heading:
            title = clean(heading.get_text(" ", strip=True))

    if len(title) >= 2:
        return title

    img = container.find("img") if container else None
    if img:
        return clean(img.get("alt", ""))

    return ""


def make_item(anchor):
    href = absolute_url(anchor.get("href"))
    if not href or not internal_url(href):
        return None

    kind = content_type_from_url(href)
    if not kind:
        return None

    container = find_card_container(anchor)
    title = title_from_anchor(anchor, container)

    if not title:
        return None

    img = container.select_one("img") if container else None

    return {
        "title": title,
        "url": href,
        "poster": image_url(img),
        "rating": extract_rating(container),
        "type": kind,
    }


def parse_listing(html: str):
    soup = soup_of(html)
    results = []
    seen = set()

    # Main strategy: every internal /movies/ and /tvshows/ link.
    # This is deliberately not tied to a specific h2/h3/card class.
    for anchor in soup.select("a[href]"):
        item = make_item(anchor)
        if not item:
            continue

        url = item["url"]
        if url in seen:
            continue

        # Skip pagination/category/genre links if they accidentally match.
        low = url.lower()
        if "/page/" in low or "/genre/" in low:
            continue

        seen.add(url)
        results.append(item)

    return results


def parse_wp_search_json(data):
    results = []
    seen = set()

    if not isinstance(data, list):
        return results

    for row in data:
        url = absolute_url(row.get("url"))
        if not url or not internal_url(url):
            continue

        kind = content_type_from_url(url)
        if not kind or url in seen:
            continue

        raw_title = row.get("title")
        if isinstance(raw_title, dict):
            title = clean(raw_title.get("rendered", ""))
        else:
            title = clean(str(raw_title or ""))

        if not title:
            continue

        seen.add(url)
        results.append({
            "title": title,
            "url": url,
            "poster": None,
            "rating": None,
            "type": kind,
        })

    return results


def pagination_info(soup):
    pages = []

    for a in soup.select("a[href]"):
        href = a.get("href", "") or ""

        for pattern in (
            r"/page/(\d+)",
            r"[?&]paged=(\d+)",
            r"[?&]page=(\d+)",
        ):
            match = re.search(pattern, href)
            if match:
                n = int(match.group(1))
                if 1 <= n < 5000:
                    pages.append(n)

    return {"last_page": max(pages) if pages else 1}


async def search_source(query):
    # Strategy 1: WordPress REST search, if enabled.
    try:
        r = await fetch(
            BASE + "/wp-json/wp/v2/search",
            params={
                "search": query,
                "per_page": 100,
                "_fields": "id,title,url,type,subtype",
            },
        )
        if "application/json" in (r.headers.get("content-type") or "").lower():
            results = parse_wp_search_json(r.json())
            if results:
                return results
    except Exception:
        pass

    # Strategy 2: normal site search (?s=...)
    encoded = quote_plus(query)
    urls = [
        f"{BASE}/?s={encoded}",
        f"{BASE}/search/?s={encoded}",
    ]

    for url in urls:
        try:
            r = await fetch(url)
            results = parse_listing(r.text)
            if results:
                return results
        except Exception:
            continue

    # Strategy 3: use the homepage as a final fallback and filter its
    # catalog by the requested words. This can find a title that is currently
    # displayed on the homepage even if the site's search template changes.
    try:
        r = await fetch(BASE + "/")
        all_items = parse_listing(r.text)

        words = [
            w.lower()
            for w in re.findall(r"[a-zA-Z0-9]+", query)
            if len(w) >= 2
        ]

        if words:
            filtered = [
                item for item in all_items
                if all(w in item["title"].lower() for w in words)
            ]
            if filtered:
                return filtered
    except Exception:
        pass

    return []


@app.get("/")
async def root():
    return {
        "status": "online",
        "version": "4.0",
        "base": BASE,
        "endpoints": [
            "/home",
            "/movies?page=1",
            "/tvshows?page=1",
            "/genre/{genre}?page=1",
            "/search?query=Stranger%20Things",
            "/detail?url=",
            "/debug",
        ],
    }


@app.get("/home")
async def home():
    r = await fetch(BASE + "/")
    return {
        "results": parse_listing(r.text)
    }


@app.get("/movies")
async def movies(page: int = Query(1, ge=1)):
    url = (
        f"{BASE}/movies/"
        if page == 1
        else f"{BASE}/movies/page/{page}/"
    )

    r = await fetch(url)
    soup = soup_of(r.text)

    return {
        "page": page,
        **pagination_info(soup),
        "results": parse_listing(r.text),
    }


@app.get("/tvshows")
async def tvshows(page: int = Query(1, ge=1)):
    url = (
        f"{BASE}/tvshows/"
        if page == 1
        else f"{BASE}/tvshows/page/{page}/"
    )

    r = await fetch(url)
    soup = soup_of(r.text)

    return {
        "page": page,
        **pagination_info(soup),
        "results": parse_listing(r.text),
    }


@app.get("/genre/{genre}")
async def genre(genre: str, page: int = Query(1, ge=1)):
    g = quote_plus(genre.strip())

    url = (
        f"{BASE}/genre/{g}/"
        if page == 1
        else f"{BASE}/genre/{g}/page/{page}/"
    )

    r = await fetch(url)
    soup = soup_of(r.text)

    return {
        "page": page,
        **pagination_info(soup),
        "results": parse_listing(r.text),
    }


@app.get("/search")
async def search(query: str = Query(..., min_length=1)):
    results = await search_source(query.strip())

    return {
        "query": query,
        "count": len(results),
        "results": results,
    }


@app.get("/detail")
async def detail(url: str):
    if not url.startswith("http"):
        url = absolute_url(url)

    if not url or not internal_url(url):
        raise HTTPException(
            400,
            "Only supported source-site detail URLs are allowed",
        )

    r = await fetch(url)
    soup = soup_of(r.text)

    title_el = soup.select_one(
        "h1, .entry-title, .title, .post-title"
    )

    poster_el = soup.select_one(
        ".poster img, .dtinfo img, .imdbwp img, "
        ".g-item img, article img, img"
    )

    desc_el = soup.select_one(
        ".wp-content p, .description, .sinopsis, "
        ".entry-content p, .content p"
    )

    if not desc_el:
        for p in soup.find_all("p"):
            txt = clean(p.get_text(" ", strip=True))
            if len(txt) > 60:
                desc_el = p
                break

    # Read season/episode labels as metadata only.
    seasons = []
    for text_node in soup.find_all(string=re.compile(r"Season\s+\d+", re.I)):
        text = clean(str(text_node))
        if text and text not in seasons:
            seasons.append(text)
        if len(seasons) >= 30:
            break

    return {
        "url": url,
        "title": clean(title_el.get_text(" ", strip=True))
        if title_el else None,
        "poster": image_url(poster_el),
        "description": clean(desc_el.get_text(" ", strip=True))
        if desc_el else None,
        "seasons": seasons,
        "type": content_type_from_url(url),
    }


@app.get("/debug")
async def debug(path: str = "/tvshows/"):
    if not path.startswith("/"):
        path = "/" + path

    r = await fetch(BASE + path)
    soup = soup_of(r.text)

    items = parse_listing(r.text)

    headings = [
        clean(h.get_text(" ", strip=True))[:100]
        for h in soup.find_all(["h1", "h2", "h3", "h4"])
    ][:30]

    return {
        "status": r.status_code,
        "base": BASE,
        "path": path,
        "final_url": str(r.url),
        "html_len": len(r.text),
        "cloudflare_challenge": (
            "just a moment" in r.text.lower()
            or "cf-chl-" in r.text.lower()
            or "challenge-platform" in r.text.lower()
        ),
        "parsed_count": len(items),
        "sample_results": items[:10],
        "headings": headings,
    }
