import os
import re
import base64
import httpx
from fastapi import FastAPI, HTTPException, Query, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Filmsbeoordeling API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

API_KEY        = os.getenv("MOVIEMETER_API_KEY", "")
OCR_API_KEY    = os.getenv("OCR_SPACE_API_KEY", "K82783988588957")
API_BASE       = "https://www.moviemeter.nl/api/film"
TYPESENSE_URL  = "https://www.moviemeter.nl/data/typesense/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
    "Referer": "https://www.moviemeter.nl/",
}


# ── OCR ──────────────────────────────────────────────────────────────────────

@app.post("/ocr")
async def ocr_image(file: UploadFile = File(...)):
    """Stuur een foto naar OCR.space en geef herkende tekstregels terug."""
    image_data = await file.read()
    b64 = base64.b64encode(image_data).decode()
    mime = file.content_type or "image/jpeg"

    async with httpx.AsyncClient(timeout=30, verify=False) as client:
        r = await client.post(
            "https://api.ocr.space/parse/image",
            data={
                "apikey": OCR_API_KEY,
                "base64Image": f"data:{mime};base64,{b64}",
                "language": "eng",
                "isOverlayRequired": "true",
                "OCREngine": "2",          # Engine 2 is beter voor grafische tekst
                "scale": "true",
                "detectOrientation": "true",
            },
        )

    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="OCR service niet bereikbaar")

    data = r.json()
    if data.get("IsErroredOnProcessing"):
        raise HTTPException(status_code=400, detail=data.get("ErrorMessage", "OCR fout"))

    # Verzamel tekstregels gesorteerd op grootte (grotere tekst = waarschijnlijk titel)
    lines_with_height: list[tuple[float, str]] = []

    for parsed in data.get("ParsedResults", []):
        overlay = parsed.get("TextOverlay", {})
        for line in overlay.get("Lines", []):
            words = line.get("Words", [])
            if not words:
                continue
            # Gemiddelde woordhoogte als proxy voor lettergrootte
            avg_height = sum(w.get("Height", 0) for w in words) / len(words)
            text = " ".join(w.get("WordText", "") for w in words).strip()
            text = re.sub(r"[^a-zA-Z0-9À-ÿ\s:\-']", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > 1 and len(re.findall(r"[a-zA-ZÀ-ÿ]", text)) >= 2:
                lines_with_height.append((avg_height, text))

    # Sorteer op lettergrootte — grootste letters eerst (waarschijnlijk de titel)
    lines_with_height.sort(key=lambda x: x[0], reverse=True)
    lines = [text for _, text in lines_with_height]

    # Verwijder duplicaten, bewaar volgorde
    seen: set[str] = set()
    unique_lines: list[str] = []
    for l in lines:
        low = l.lower()
        if low not in seen:
            seen.add(low)
            unique_lines.append(l)

    return {"lines": unique_lines[:10]}


# ── Zoeken ────────────────────────────────────────────────────────────────────

def parse_typesense_result(doc: dict) -> dict:
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
    async with httpx.AsyncClient(timeout=10, verify=False) as client:
        r = await client.get(TYPESENSE_URL, params={"q": query, "ty": "kale"}, headers=HEADERS)
        if r.status_code != 200:
            raise HTTPException(status_code=502, detail="MovieMeter niet bereikbaar")

        data = r.json()
        films = []
        for group in data.get("hits", []):
            if not isinstance(group, list):
                group = [group]
            for hit in group:
                doc = hit.get("document", {})
                if doc.get("type") not in ("film", None, ""):
                    continue
                if doc.get("portret_image") and not doc.get("cover"):
                    continue
                films.append(parse_typesense_result(doc))
                if len(films) >= 5:
                    break
            if len(films) >= 5:
                break
        return films


@app.get("/search")
async def search(q: str = Query(..., min_length=1)):
    return {"results": await typesense_search(q)}


@app.get("/health")
async def health():
    return {"status": "ok", "ocr_configured": bool(OCR_API_KEY)}
