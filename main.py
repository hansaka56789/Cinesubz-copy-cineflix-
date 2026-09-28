import re
from urllib.parse import urljoin, quote

from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

BASE = "https://cinesubz.co"
IMPERSONATE = "chrome124"

app = FastAPI(title="CineSubz Catalog API", version="3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

session = AsyncSession(impersonate=IMPERSONATE, timeout=25)


def abs_url(url):
    return urljoin(BASE + "/", url) if url else None


def soup(html):
    return BeautifulSoup(html, "lxml")


async def fetch(url, **kwargs):
    try:
        r = await session.get(
            url,
            headers={
                "Accept-Language": "en-US,en;q=0.9,si;q=0.8",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            },
            **kwargs,
        )
    except Exception as e:
        raise HTTPException(502, f"Upstream request failed: {e}")

    if r.status_code == 403:
        raise HTTPException(502, "Upstream returned HTTP 403")
    if r.status_code >= 400:
        raise HTTPException(502, f"Upstream returned HTTP {r.status_code}")
    return r.text


def image_url(img):
    if not img:
        return None
    for attr in ("data-src", "data-lazy-src", "data-original", "src"):
        value = img.get(attr)
        if value and not value.startswith("data:"):
            return abs_url(value)
    srcset = img.get("srcset") or img.get("data-srcset")
    if srcset:
        value = srcset.split(",")[-1].strip().split()[0]
        return abs_url(value)
    return None


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def internal(url):
    return bool(url and url.startswith(BASE))


def valid_content_url(url):
    if not internal(url):
        return False
    u = url.lower()
    if "/movies/" in u or "/tvshows/" in u:
        return True
    blocked = ("/page/", "/category/", "/genre/", "/tag/", "/author/", "/feed/", "/search/")
    return not any(x in u for x in blocked)


def rating(container):
    if not container:
        return None
    text = container.get_text(" ", strip=True)
    for pattern in (
        r"(?:IMDb|IMDB|Rating)\s*[:\-]?\s*(\d+(?:\.\d+)?)",
        r"â˜…\s*(\d+(?:\.\d+)?)",
        r"\b(10(?:\.0)?|[0-9](?:\.[0-9])?)\s*/\s*10\b",
    ):
        m = re.search(pattern, text, flags=re.I)
        if m:
            return m.group(1)
    return None


def parse_listing(html):
    s = soup(html)
    candidates = []

    containers = s.select(
        "article, .item, .item.movies, .item.tvshows, .post, "
        ".result-item, .movie, .tvshow"
    )
    for box in containers:
        for a in box.select("a[href]"):
            href = abs_url(a.get("href"))
            if valid_content_url(href):
                candidates.append((a, box))
                break

    if not candidates:
        for h in s.find_all(["h1", "h2", "h3", "h4"]):
            a = h.find("a", href=True) or h.find_parent("a", href=True)
            if a:
                href = abs_url(a.get("href"))
                if valid_content_url(href):
                    candidates.append((a, h.parent))

    if not candidates:
        for a in s.select("a[href]"):
            href = abs_url(a.get("href"))
            title = clean(a.get_text(" ", strip=True))
            if valid_content_url(href) and len(title) >= 2:
                candidates.append((a, a.parent))

    results, seen = [], set()
    for a, box in candidates:
        href = abs_url(a.get("href"))
        if not href or href in seen:
            continue

        title = clean(a.get_text(" ", strip=True))
        if len(title) < 2 and box:
            h = box.find(["h1", "h2", "h3", "h4"])
            title = clean(h.get_text(" ", strip=True)) if h else ""
        if len(title) < 2:
            continue

        img = box.select_one("img") if box else None
        results.append({
            "title": title,
            "url": href,
            "poster": image_url(img),
            "rating": rating(box),
        })
        seen.add(href)

    return results


def pagination(s):
    nums = []
    for a in s.select("a[href]"):
        href = a.get("href", "") or ""
        for pattern in (r"/page/(\d+)", r"[?&]paged=(\d+)", r"[?&]page=(\d+)"):
            m = re.search(pattern, href)
            if m and 1 <= int(m.group(1)) < 5000:
                nums.append(int(m.group(1)))
    return {"last_page": max(nums) if nums else 1}


@app.get("/")
async def index():
    return {
        "version": "3.0",
        "status": "online",
        "endpoints": [
            "/home", "/movies?page=1", "/tvshows?page=1",
            "/genre/{genre}?page=1", "/search?query=", "/detail?url=", "/debug"
        ],
    }


@app.get("/home")
async def home():
    return {"results": parse_listing(await fetch(BASE + "/"))}


@app.get("/movies")
async def movies(page: int = Query(1, ge=1)):
    url = BASE + "/movies/" if page == 1 else f"{BASE}/movies/page/{page}/"
    html = await fetch(url)
    s = soup(html)
    return {"page": page, **pagination(s), "results": parse_listing(html)}


@app.get("/tvshows")
async def tvshows(page: int = Query(1, ge=1)):
    url = BASE + "/tvshows/" if page == 1 else f"{BASE}/tvshows/page/{page}/"
    html = await fetch(url)
    s = soup(html)
    return {"page": page, **pagination(s), "results": parse_listing(html)}


@app.get("/genre/{genre}")
async def genre(genre: str, page: int = Query(1, ge=1)):
    g = quote(genre.strip(), safe="")
    url = f"{BASE}/genre/{g}/" if page == 1 else f"{BASE}/genre/{g}/page/{page}/"
    html = await fetch(url)
    s = soup(html)
    return {"page": page, **pagination(s), "results": parse_listing(html)}


@app.get("/search")
async def search(query: str = Query(..., min_length=1)):
    html = await fetch(BASE + "/", params={"s": query})
    return {"query": query, "results": parse_listing(html)}


@app.get("/detail")
async def detail(url: str):
    url = url if url.startswith("http") else abs_url(url)
    if not internal(url):
        raise HTTPException(400, "Only supported internal detail URLs are allowed")

    s = soup(await fetch(url))
    title = s.select_one("h1, .entry-title, .title, .post-title")
    poster = s.select_one(".poster img, .dtinfo img, .imdbwp img, article img, img")
    desc = s.select_one(".wp-content p, .description, .sinopsis, .entry-content p, .content p")
    if not desc:
        for p in s.find_all("p"):
            if len(clean(p.get_text(" ", strip=True))) > 60:
                desc = p
                break

    return {
        "url": url,
        "title": clean(title.get_text(" ", strip=True)) if title else None,
        "poster": image_url(poster),
        "description": clean(desc.get_text(" ", strip=True)) if desc else None,
    }


@app.get("/debug")
async def debug(path: str = "/movies/"):
    if not path.startswith("/"):
        path = "/" + path
    html = await fetch(BASE + path)
    s = soup(html)
    headings = [clean(h.get_text(" ", strip=True))[:100] for h in s.find_all(["h1", "h2", "h3", "h4"])][:20]
    links = []
    for a in s.select("a[href]"):
        href = abs_url(a.get("href"))
        if internal(href):
            links.append({"text": clean(a.get_text(" ", strip=True))[:80], "url": href})
        if len(links) >= 30:
            break
    low = html.lower()
    return {
        "status": 200,
        "base": BASE,
        "requested_path": path,
        "html_len": len(html),
        "cloudflare_block": "just a moment" in low or "cf-chl-" in low or "challenge-platform" in low,
        "headings": headings,
        "sample_links": links,
}
