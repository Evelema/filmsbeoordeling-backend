import os
import re
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Filmsbeoordeling API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Optionele officiële API-sleutel (https://www.moviemeter.nl/api)
API_KEY = os.getenv("MOVIEMETER_API_KEY", "")
API_BASE = "https://www.moviemeter.nl/api/film"

# Interne Typesense zoek-API van moviemeter.nl (geen sleutel nodig)
TYPESENSE_URL = "https://www.moviemeter.nl/data/typesense/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
    "Referer": "https://www.moviemeter.nl/",
}


def parse_typesense_result(doc: dict) -> dict:
    """Zet een Typesense document om naar ons filmformaat."""
    # rating is op schaal van 0-5
    rating = None
    raw = doc.get("rating")
    if raw:
        try:
            rating = round(float(raw), 1)
        except (ValueError, TypeError):
            pass

    url_path = doc.get("url", "")
    full_url = f"https://www.moviemeter.nl{url_path}" if url_path.startswith("/") else url_path

    film_id_match = re.search(r"/film/(\d+)", url_path)
    film_id = film_id_match.group(1) if film_id_match else doc.get("id", "").replace("f_", "")

    return {
        "id": film_id,
        "title": doc.get("title", ""),
        "year": doc.get("year") or None,
        "rating": rating,
        "votes": doc.get("votes_count") or 0,
        "url": full_url,
        "poster": doc.get("cover", ""),
        "genres": doc.get("genres", ""),
        "duration": doc.get("duration") or None,
    }


async def typesense_search(query: str) -> list[dict]:
    """Doorzoek moviemeter.nl via hun interne Typesense API (geen sleutel nodig)."""
    async with httpx.AsyncClient(timeout=10, verify=False) as client:
        r = await client.get(
            TYPESENSE_URL,
            params={"q": query, "ty": "kale"},
            headers=HEADERS,
        )
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail="MovieMeter niet bereikbaar")

        data = r.json()
        films = []

        # hits is een lijst van lijsten
        hits_groups = data.get("hits", [])
        for group in hits_groups:
            if not isinstance(group, list):
                group = [group]
            for hit in group:
                doc = hit.get("document", {})
                # Alleen films (geen series, personen)
                if doc.get("type") not in ("film", None, ""):
                    continue
                # Sla personen over (hebben een profile_path maar geen cover)
                if doc.get("portret_image") and not doc.get("cover"):
                    continue
                films.append(parse_typesense_result(doc))
                if len(films) >= 5:
                    break
            if len(films) >= 5:
                break

        return films


async def api_search(query: str) -> list[dict]:
    """Officiële MovieMeter API (vereist API-sleutel)."""
    async with httpx.AsyncClient(timeout=10, verify=False) as client:
        r = await client.get(
            f"{API_BASE}/",
            params={"q": query, "api_key": API_KEY},
            headers=HEADERS,
        )
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail="MovieMeter API fout")

        results = r.json()
        if not isinstance(results, list):
            return []

        films = []
        for item in results[:5]:
            film_id = item.get("moviemeter_id") or item.get("id")
            if not film_id:
                continue
            detail = await client.get(
                f"{API_BASE}/{film_id}",
                params={"api_key": API_KEY},
                headers=HEADERS,
            )
            if detail.status_code == 200:
                d = detail.json()
                rating = d.get("average") or d.get("score")
                try:
                    rating = round(float(rating), 1) if rating else None
                except (ValueError, TypeError):
                    rating = None
                films.append({
                    "id": film_id,
                    "title": d.get("title", ""),
                    "year": d.get("year"),
                    "rating": rating,
                    "votes": d.get("votes_count", 0),
                    "url": d.get("url", f"https://www.moviemeter.nl/film/{film_id}"),
                    "poster": d.get("thumbnail") or d.get("poster", ""),
                    "genres": "",
                    "duration": d.get("duration"),
                })
        return films


@app.get("/search")
async def search(q: str = Query(..., min_length=1)):
    q = q.strip()
    # Gebruik de officiële API als er een sleutel is, anders Typesense
    if API_KEY:
        return {"results": await api_search(q)}
    return {"results": await typesense_search(q)}


@app.get("/health")
async def health():
    return {"status": "ok", "api_key_configured": bool(API_KEY)}
