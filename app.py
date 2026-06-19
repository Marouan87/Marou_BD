"""
API Flask - Assemblage PDF livre illustré
POST /generate-pdf  { "histoire_id": "uuid" }
Réponse : { "pdf_url": "https://..." } après upload dans Supabase Storage
"""

import os
import io
import math
import random
import requests
import tempfile
from flask import Flask, request, jsonify
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, Color
from PIL import Image as PILImage

app = Flask(__name__)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
API_SECRET   = os.environ.get("API_SECRET", "")   # optionnel, pour sécuriser l'endpoint

HEADERS = {
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "apikey": SUPABASE_KEY,
}

# ─── Dimensions ───────────────────────────────────────────────────────────────
PAGE_W  = 360 * mm
PAGE_H  = 180 * mm
HALF_W  = PAGE_W / 2
TEXT_MARGIN_X = 30 * mm

FONT_NAME = "Helvetica-Bold"
FONT_SIZE = 22

PALETTE = [
    "#FFF3E0", "#FFF8E1", "#F1F8E9", "#E8F5E9",
    "#FFF9C4", "#E3F2FD", "#F8F9FA", "#FCE4EC",
    "#EDE7F6", "#E0F7FA", "#FBE9E7", "#E8EAF6",
]

# ─── Supabase ─────────────────────────────────────────────────────────────────

def fetch_histoire(histoire_id):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/histoires",
        headers=HEADERS,
        params={"id": f"eq.{histoire_id}", "select": "id,titre,theme"},
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        raise ValueError(f"Histoire {histoire_id} introuvable")
    return data[0]


def fetch_pages(histoire_id):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/pages",
        headers=HEADERS,
        params={
            "histoire_id": f"eq.{histoire_id}",
            "select": "id,numero,legende,image_url,statut",
            "order": "numero.asc",
        },
    )
    r.raise_for_status()
    return r.json()


def upload_pdf(pdf_bytes: bytes, histoire_id: str) -> str:
    filename = f"{histoire_id}.pdf"
    r = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/pdfs/{filename}",
        headers={
            **HEADERS,
            "Content-Type": "application/pdf",
            "x-upsert": "true",
        },
        data=pdf_bytes,
    )
    r.raise_for_status()
    return f"{SUPABASE_URL}/storage/v1/object/public/pdfs/{filename}"

# ─── Dessin ───────────────────────────────────────────────────────────────────

def draw_star(c, cx, cy, r):
    points = []
    for i in range(10):
        angle = math.pi / 2 + i * math.pi / 5
        radius = r if i % 2 == 0 else r * 0.45
        points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    p = c.beginPath()
    p.moveTo(*points[0])
    for pt in points[1:]:
        p.lineTo(*pt)
    p.close()
    c.drawPath(p, fill=1, stroke=0)


def draw_stars(c, x_offset, width, height, color_hex, n=10, seed=0):
    rng = random.Random(seed)
    base = HexColor(color_hex)
    star_color = Color(max(0, base.red - 0.12), max(0, base.green - 0.12), max(0, base.blue - 0.12))
    c.setFillColor(star_color)
    for _ in range(n):
        sx = x_offset + rng.uniform(15 * mm, width - 15 * mm)
        sy = rng.uniform(15 * mm, height - 15 * mm)
        size = rng.uniform(3 * mm, 6 * mm)
        draw_star(c, sx, sy, size)


def wrap_text(c, text, font, size, max_width):
    words = text.split()
    lines, current = [], ""
    for word in words:
        test = (current + " " + word).strip()
        if c.stringWidth(test, font, size) <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def draw_text_page(c, legende, color_hex, page_index, x_offset=0):
    c.setFillColor(HexColor(color_hex))
    c.rect(x_offset, 0, HALF_W, PAGE_H, fill=1, stroke=0)
    draw_stars(c, x_offset, HALF_W, PAGE_H, color_hex, n=10, seed=page_index)

    c.setFillColor(HexColor("#1A1A2E"))
    c.setFont(FONT_NAME, FONT_SIZE)
    available_w = HALF_W - 2 * TEXT_MARGIN_X
    lines = wrap_text(c, legende, FONT_NAME, FONT_SIZE, available_w)
    line_height = FONT_SIZE * 1.5
    total_h = len(lines) * line_height
    y_start = (PAGE_H + total_h) / 2 - line_height * 0.8
    for i, line in enumerate(lines):
        y = y_start - i * line_height
        text_w = c.stringWidth(line, FONT_NAME, FONT_SIZE)
        x = x_offset + (HALF_W - text_w) / 2
        c.drawString(x, y, line)


def draw_image_page(c, img_path, x_offset=0):
    c.setFillColor(HexColor("#F5F5F5"))
    c.rect(x_offset, 0, HALF_W, PAGE_H, fill=1, stroke=0)
    c.drawImage(img_path, x_offset, 0, width=HALF_W, height=PAGE_H, preserveAspectRatio=False)


def draw_cover(c, titre, img_path):
    c.drawImage(img_path, 0, 0, width=PAGE_W, height=PAGE_H, preserveAspectRatio=False)
    bandeau_h = 45 * mm
    c.setFillColor(Color(1, 1, 1, alpha=0.82))
    c.rect(0, PAGE_H - bandeau_h, PAGE_W, bandeau_h, fill=1, stroke=0)
    c.setFillColor(HexColor("#E67E00"))
    font_size = 36
    c.setFont(FONT_NAME, font_size)
    text_w = c.stringWidth(titre, FONT_NAME, font_size)
    c.drawString((PAGE_W - text_w) / 2, PAGE_H - bandeau_h + 10 * mm, titre)
    c.showPage()

# ─── Assemblage ───────────────────────────────────────────────────────────────

def assembler_pdf(histoire_id):
    histoire = fetch_histoire(histoire_id)
    pages = fetch_pages(histoire_id)
    titre = histoire.get("titre", "Mon livre")
    pages_ok = [p for p in pages if p.get("image_url")]

    if not pages_ok:
        raise ValueError("Aucune page avec image_url trouvée.")

    with tempfile.TemporaryDirectory() as tmp:
        # Télécharger les images
        img_paths = {}
        for page in pages_ok:
            pid = page["id"]
            path = os.path.join(tmp, f"{pid}.jpg")
            r = requests.get(page["image_url"], timeout=30)
            r.raise_for_status()
            img = PILImage.open(io.BytesIO(r.content)).convert("RGB")
            img.save(path, format="JPEG", quality=95)
            img_paths[pid] = path

        # Générer le PDF en mémoire
        pdf_path = os.path.join(tmp, "livre.pdf")
        c = canvas.Canvas(pdf_path, pagesize=(PAGE_W, PAGE_H))

        # Couverture
        draw_cover(c, titre, img_paths[pages_ok[0]["id"]])

        # Double pages
        for i, page in enumerate(pages_ok):
            color = PALETTE[i % len(PALETTE)]
            draw_text_page(c, page.get("legende", ""), color, page_index=i, x_offset=0)
            draw_image_page(c, img_paths[page["id"]], x_offset=HALF_W)
            c.showPage()

        c.save()

        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

    # Upload dans Supabase Storage
    pdf_url = upload_pdf(pdf_bytes, histoire_id)
    return pdf_url

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/generate-pdf", methods=["POST"])
def generate_pdf():
    # Vérification secret optionnelle
    if API_SECRET:
        token = request.headers.get("X-API-Secret", "")
        if token != API_SECRET:
            return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data or "histoire_id" not in data:
        return jsonify({"error": "histoire_id requis"}), 400

    histoire_id = data["histoire_id"]

    try:
        pdf_url = assembler_pdf(histoire_id)
        return jsonify({"pdf_url": pdf_url, "histoire_id": histoire_id})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
