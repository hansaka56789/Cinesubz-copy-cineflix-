import re
from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

BASE = "https://cinesubz.net"
IMPERSONATE = "chrome124"   # Chrome 124 browser fingerprint

app = FastAPI(title="CineSubz Unofficial API", version="2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# httpx වෙනුවට curl_cffi — Cloudflare TLS fingerprint check bypass
session = AsyncSession(impersonate=IMPERSONATE, timeout=25)

async def fetch(url: str, **kwargs) -> str:
    r = await session.get(url, headers={"Accept-Language": "en-US,en;q=0.9,si;q=0.8"}, **kwargs)
    if r.status_code == 403:
        raise HTTPException(502, "Cloudflare block — retry කරන්න හෝ proxy එකක් අවශ්‍යයි")
    r.raise_for_status()
    return r.text

def soup_of(html):
    return BeautifulSoup(html, "lxml")

def parse_listing(html: str) -> list:
    s = soup_of(html)
    items, seen = [], set()
    for h in s.find_all(["h2", "h3"]):
        a = h.find("a") or h.find_parent("a")
        if not a or not a.get("href"):
            continue
        url = a["href"]
        if "/movies/" not in url and "/tvshows/" not in url:
            continue
        if url in seen:
            continue
        seen.add(url)
        container = h.find_parent("div") or h.find_parent("article")
        img = container.select_one("img") if container else None
        rating = None
        if container:
            r_el = container.find(string=re.compile(r"★|\d\.\d"))
            if r_el:
                rating = r_el.strip()
        items.append({
            "title": h.get_text(strip=True),
            "url": url,
            "poster": (img.get("src") or img.get("data-src")) if img else None,
            "rating": rating,
        })
    return items

def get_pagination(s):
    nums = []
    for p in s.select("a[href]"):
        m = re.search(r"/page/(\d+)", p.get("href", "") or "")
        if m and int(m.group(1)) < 5000:
            nums.append(int(m.group(1)))
    return {"last_page": max(nums) if nums else 1}

@app.get("/debug")
async def debug(path: str = "/movies/"):
    try:
        html = await fetch(BASE + path)
    except HTTPException:
        raise
    s = soup_of(html)
    classes = sorted({c for tag in s.find_all(True) for c in (tag.get("class") or [])})
    h2h3 = [h.get_text(strip=True)[:60] for h in s.find_all(["h2", "h3"])][:10]
    cf_block = "Just a moment" in html or "challenge" in html.lower()[:2000]
    return {"status": 200, "html_len": len(html), "cloudflare_block": cf_block,
            "classes": classes[:60], "headings": h2h3}

@app.get("/")
async def index():
    return {"version": "2.0 (curl_cffi)", "endpoints": ["/home", "/movies?page=", "/tvshows?page=", "/genre/{g}", "/search?query=", "/detail?url=", "/download?url=", "/debug"]}

@app.get("/home")
async def home():
    return {"results": parse_listing(await fetch(BASE))}

@app.get("/movies")
async def movies(page: int = 1):
    url = f"{BASE}/movies/" if page == 1 else f"{BASE}/movies/page/{page}/"
    html = await fetch(url)
    return {"page": page, **get_pagination(soup_of(html)), "results": parse_listing(html)}

@app.get("/tvshows")
async def tvshows(page: int = 1):
    url = f"{BASE}/tvshows/" if page == 1 else f"{BASE}/tvshows/page/{page}/"
    html = await fetch(url)
    return {"page": page, **get_pagination(soup_of(html)), "results": parse_listing(html)}

@app.get("/genre/{genre}")
async def genre(genre: str, page: int = 1):
    url = f"{BASE}/genre/{genre}/" if page == 1 else f"{BASE}/genre/{genre}/page/{page}/"
    return {"page": page, "results": parse_listing(await fetch(url))}

@app.get("/search")
async def search(query: str = Query(..., min_length=1)):
    html = await session.get(f"{BASE}/", params={"s": query})  # params support
    return {"query": query, "results": parse_listing(html.text)}

@app.get("/detail")
async def detail(url: str):
    if not url.startswith("http"):
        url = BASE + "/" + url.lstrip("/")
    s = soup_of(await fetch(url))
    title = s.select_one("h1")
    poster = s.select_one(".poster img, .dtinfo img, .imdbwp img") or s.select_one("img")
    desc = s.select_one(".wp-content p, .description, .sinopsis")
    if not desc:
        for p in s.find_all("p"):
            if len(p.get_text(strip=True)) > 60:
                desc = p
                break
    sub_links = [{"text": a.get_text(strip=True), "url": a["href"]}
                 for a in s.select("a[href]")
                 if a.get("href") and (".srt" in a["href"] or "download" in a["href"].lower())]
    return {
        "url": url,
        "title": title.get_text(strip=True) if title else None,
        "poster": (poster.get("src") or poster.get("data-src")) if poster else None,
        "description": desc.get_text(" ", strip=True) if desc else None,
        "subtitle_links": sub_links,
    }

@app.get("/download")
async def download(url: str):
    if not url.startswith("http"):
        url = BASE + "/" + url.lstrip("/")
    r = await session.get(url)
    r.raise_for_status()
    ct = r.headers.get("content-type", "application/octet-stream")
    fname = url.split("/")[-1].split("?")[0] or "subtitle.srt"
    return StreamingResponse(iter([r.content]), media_type=ct,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
