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
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from PIL import Image as PILImage

app = Flask(__name__)

# ─── Polices Unicode (accents francais) ────────────────────────────────────────
# DejaVu Sans gere les accents, contrairement aux polices Helvetica integrees.
# On cherche d'abord la version embarquee dans le projet (dossier fonts/),
# avec repli sur le chemin systeme Linux.
_FONT_DIR = os.path.dirname(os.path.abspath(__file__))
_SYS_DIR = "/usr/share/fonts/truetype/dejavu"


def _font_path(filename):
    local = os.path.join(_FONT_DIR, filename)
    if os.path.exists(local):
        return local
    return os.path.join(_SYS_DIR, filename)


pdfmetrics.registerFont(TTFont("DejaVu", _font_path("DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("DejaVu-Bold", _font_path("DejaVuSans-Bold.ttf")))
pdfmetrics.registerFont(TTFont("DejaVu-Oblique", _font_path("DejaVuSans-Oblique.ttf")))

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

FONT_NAME = "DejaVu-Bold"
FONT_SIZE = 22

# Palette alignee sur la visionneuse Piklo : (fond, couleur du texte)
PALETTE = [
    ("#D2774B", "#FFFFFF"),  # Orange Piklo
    ("#FFF7EE", "#38302A"),  # Creme
    ("#F4E8D6", "#5A4632"),  # Sable
    ("#F6E3DE", "#6A4138"),  # Blush
    ("#E7EDE3", "#3C4A36"),  # Sauge
    ("#E4ECF2", "#33414E"),  # Ciel
]
PALETTE_DEFAUT = 1  # Creme par defaut

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


def fetch_heros_nom():
    """
    Recupere le prenom du personnage principal (role = 'enfant').
    Retourne None si aucun, pour que la couverture retombe sur la
    mention generique sans planter.
    """
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/personnages",
            headers=HEADERS,
            params={"role": "eq.enfant", "select": "nom", "limit": "1"},
        )
        r.raise_for_status()
        data = r.json()
        if data and data[0].get("nom"):
            return data[0]["nom"].capitalize()
    except Exception:
        pass
    return None


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


def draw_text_page(c, legende, bg_hex, fg_hex, x_offset=0):
    c.setFillColor(HexColor(bg_hex))
    c.rect(x_offset, 0, HALF_W, PAGE_H, fill=1, stroke=0)

    c.setFillColor(HexColor(fg_hex))
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


# Nom de travail provisoire de l'outil, a remplacer le jour de la commercialisation
TOOL_NAME = "MonHistoire"
TOOL_BASELINE = "Un livre unique, créé rien que pour votre enfant"


def draw_cover(c, titre, img_path, enfant_nom=None):
    """
    Double page de couverture en mode impression.
    Moitie gauche  : quatrieme de couverture (dos du livre)
    Moitie droite  : couverture avant, illustration carree + titre
    """
    # ── Moitie droite : couverture avant ──
    # Illustration carree qui remplit la demi-page droite (pas d'etirement)
    c.drawImage(img_path, HALF_W, 0, width=HALF_W, height=PAGE_H,
                preserveAspectRatio=True, anchor='c')

    # Bandeau titre semi-transparent en haut de la couverture avant
    bandeau_h = 38 * mm
    c.setFillColor(Color(1, 1, 1, alpha=0.85))
    c.rect(HALF_W, PAGE_H - bandeau_h, HALF_W, bandeau_h, fill=1, stroke=0)
    c.setFillColor(HexColor("#E67E00"))
    font_size = 30
    c.setFont(FONT_NAME, font_size)
    text_w = c.stringWidth(titre, FONT_NAME, font_size)
    c.drawString(HALF_W + (HALF_W - text_w) / 2,
                 PAGE_H - bandeau_h + 12 * mm, titre)

    # ── Moitie gauche : quatrieme de couverture ──
    # Fond uni doux
    c.setFillColor(HexColor("#FFF3E0"))
    c.rect(0, 0, HALF_W, PAGE_H, fill=1, stroke=0)

    # Quelques etoiles discretes pour rester dans l'univers du livre
    draw_stars(c, 0, HALF_W, PAGE_H, "#FFF3E0", n=8, seed=999)

    # Nom de l'outil, centre haut
    c.setFillColor(HexColor("#E67E00"))
    c.setFont(FONT_NAME, 34)
    name_w = c.stringWidth(TOOL_NAME, FONT_NAME, 34)
    c.drawString((HALF_W - name_w) / 2, PAGE_H - 45 * mm, TOOL_NAME)

    # Baseline
    c.setFillColor(HexColor("#5A5A6E"))
    c.setFont("DejaVu", 13)
    base_lines = wrap_text(c, TOOL_BASELINE, "DejaVu", 13, HALF_W - 50 * mm)
    by = PAGE_H - 58 * mm
    for line in base_lines:
        lw = c.stringWidth(line, "DejaVu", 13)
        c.drawString((HALF_W - lw) / 2, by, line)
        by -= 18

    # Mention de personnalisation au centre
    if enfant_nom:
        mention = f"Une histoire créée spécialement pour {enfant_nom}"
    else:
        mention = "Une histoire créée spécialement pour votre enfant"
    c.setFillColor(HexColor("#1A1A2E"))
    c.setFont("DejaVu-Oblique", 14)
    mlines = wrap_text(c, mention, "DejaVu-Oblique", 14, HALF_W - 50 * mm)
    my = PAGE_H / 2
    for line in mlines:
        lw = c.stringWidth(line, "DejaVu-Oblique", 14)
        c.drawString((HALF_W - lw) / 2, my, line)
        my -= 20

    # Pied : techno et annee
    c.setFillColor(HexColor("#9A9AA8"))
    c.setFont("DejaVu", 10)
    pied = "Histoire et illustrations générées par intelligence artificielle"
    pw = c.stringWidth(pied, "DejaVu", 10)
    c.drawString((HALF_W - pw) / 2, 30 * mm, pied)

    annee = "2026"
    aw = c.stringWidth(annee, "DejaVu", 10)
    c.drawString((HALF_W - aw) / 2, 22 * mm, annee)

    c.showPage()

# ─── Assemblage ───────────────────────────────────────────────────────────────

def assembler_pdf(histoire_id, palette_id=PALETTE_DEFAUT):
    histoire = fetch_histoire(histoire_id)
    pages = fetch_pages(histoire_id)
    titre = histoire.get("titre", "Mon livre")
    pages_ok = [p for p in pages if p.get("image_url")]

    if not pages_ok:
        raise ValueError("Aucune page avec image_url trouvée.")

    # Couleur unique choisie dans la visionneuse, appliquee a toutes les pages texte
    try:
        idx = int(palette_id)
    except (TypeError, ValueError):
        idx = PALETTE_DEFAUT
    if idx < 0 or idx >= len(PALETTE):
        idx = PALETTE_DEFAUT
    bg_hex, fg_hex = PALETTE[idx]

    with tempfile.TemporaryDirectory() as tmp:
        # Télécharger les images
        # On reconstruit l'URL réelle : les fichiers sont rangés dans
        # le bucket sous images/{histoire_id}/{page_id}.png
        img_paths = {}
        for page in pages_ok:
            pid = page["id"]
            path = os.path.join(tmp, f"{pid}.jpg")
            real_url = (
                f"{SUPABASE_URL}/storage/v1/object/public/"
                f"images/{histoire_id}/{pid}.png"
            )
            r = requests.get(real_url, timeout=30)
            r.raise_for_status()
            img = PILImage.open(io.BytesIO(r.content)).convert("RGB")
            img.save(path, format="JPEG", quality=95)
            img_paths[pid] = path

        # Générer le PDF en mémoire
        pdf_path = os.path.join(tmp, "livre.pdf")
        c = canvas.Canvas(pdf_path, pagesize=(PAGE_W, PAGE_H))

        # Couverture double page (4e de couv a gauche, couverture avant a droite)
        enfant_nom = fetch_heros_nom()
        draw_cover(c, titre, img_paths[pages_ok[0]["id"]], enfant_nom=enfant_nom)

        # Double pages : meme couleur de fond/texte sur toutes les pages
        for i, page in enumerate(pages_ok):
            draw_text_page(c, page.get("legende", ""), bg_hex, fg_hex, x_offset=0)
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
    palette_id = data.get("palette_id", PALETTE_DEFAUT)

    try:
        pdf_url = assembler_pdf(histoire_id, palette_id=palette_id)
        return jsonify({"pdf_url": pdf_url, "histoire_id": histoire_id})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
