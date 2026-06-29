"""
API Flask - Assemblage PDF livre illustre (version IMPRESSION Prodigi)
POST /generate-pdf  { "histoire_id": "uuid" }
Reponse : { "pdf_url": "https://..." } apres upload dans Supabase Storage

Format de sortie conforme aux specs Prodigi hardcover carre 21x21 cm :
- Pages SIMPLES carrees 210x210 mm (pas de double-page)
- Premiere page du PDF = couverture avant
- Derniere page du PDF = quatrieme de couverture
- Entre les deux : pour chaque scene, une page texte puis une page illustration
- 10 scenes => 1 + (10 x 2) + 1 = 22 pages (pair, sous le forfait 24 pages)
- Pas de fond perdu ni de traits de coupe (Prodigi les genere)
- Elements importants gardes a >= 10 mm des bords (zone de securite)
- Images preparees pour viser 300 dpi (≈ 2480 px sur 210 mm)
"""

import os
import io
import json
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

# --- Polices Unicode (accents francais) ---
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


def _register_brand_fonts():
    brand = {
        "Quicksand-Bold":  ("Quicksand-Bold.ttf",  "DejaVu-Bold"),
        "Nunito":          ("Nunito-Regular.ttf",  "DejaVu"),
        "Nunito-Bold":     ("Nunito-Bold.ttf",     "DejaVu-Bold"),
        "Nunito-Italic":   ("Nunito-Italic.ttf",   "DejaVu-Oblique"),
    }
    resolved = {}
    for name, (filename, fallback) in brand.items():
        path = _font_path(filename)
        if os.path.exists(path):
            pdfmetrics.registerFont(TTFont(name, path))
            resolved[name] = name
        else:
            resolved[name] = fallback
    return resolved

BRAND_FONTS = _register_brand_fonts()
F_TITLE   = BRAND_FONTS["Quicksand-Bold"]
F_BODY    = BRAND_FONTS["Nunito"]
F_BODY_B  = BRAND_FONTS["Nunito-Bold"]
F_ITALIC  = BRAND_FONTS["Nunito-Italic"]

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
API_SECRET   = os.environ.get("API_SECRET", "")

HEADERS = {
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "apikey": SUPABASE_KEY,
}

# --- Dimensions IMPRESSION (page simple carree Prodigi 21x21) ---
PAGE = 210 * mm          # cote de la page carree
SAFE = 10 * mm           # zone de securite Prodigi : rien d'important au-dela
TEXT_MARGIN_X = 22 * mm  # marge horizontale du bloc texte (> SAFE)

# Nombre de scenes attendu pour rester dans le forfait 24 pages.
NB_SCENES = 10

FONT_NAME = "DejaVu-Bold"
FONT_SIZE = 24

# Palette (fond, couleur du texte) - identique a la visionneuse
PALETTE = [
    ("#D2774B", "#FFFFFF"),
    ("#FFF7EE", "#38302A"),
    ("#F4E8D6", "#5A4632"),
    ("#F6E3DE", "#6A4138"),
    ("#E7EDE3", "#3C4A36"),
    ("#E4ECF2", "#33414E"),
]
PALETTE_DEFAUT = 0

# Resolution cible pour l'impression : 300 dpi sur 210 mm.
# 210 mm = 8.2677 in ; 8.2677 * 300 ≈ 2480 px.
TARGET_PX = 2480

# --- Supabase ---

def fetch_histoire(histoire_id):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/histoires",
        headers=HEADERS,
        params={"id": f"eq.{histoire_id}", "select": "id,titre,theme,contenu"},
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


def upload_pdf(pdf_bytes: bytes, histoire_id: str, palette_id: int) -> str:
    # Suffixe _print pour distinguer le PDF d'impression de tout autre fichier.
    filename = f"{histoire_id}_p{palette_id}_print.pdf"
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


def save_pdf_cache(histoire_id: str, contenu: dict, pdf_url: str, palette_id: int):
    new_contenu = dict(contenu or {})
    new_contenu["pdf_url"] = pdf_url
    new_contenu["pdf_palette_id"] = palette_id
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/histoires",
        headers={**HEADERS, "Content-Type": "application/json"},
        params={"id": f"eq.{histoire_id}"},
        data=json.dumps({"contenu": new_contenu}),
    )
    r.raise_for_status()

# --- Texte ---

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


def draw_text_page(c, legende, bg_hex, fg_hex):
    """Page simple carree : fond uni + legende centree, dans la zone de securite."""
    c.setFillColor(HexColor(bg_hex))
    c.rect(0, 0, PAGE, PAGE, fill=1, stroke=0)

    c.setFillColor(HexColor(fg_hex))
    c.setFont(FONT_NAME, FONT_SIZE)
    available_w = PAGE - 2 * TEXT_MARGIN_X
    lines = wrap_text(c, legende, FONT_NAME, FONT_SIZE, available_w)
    line_height = FONT_SIZE * 1.5
    total_h = len(lines) * line_height
    y_start = (PAGE + total_h) / 2 - line_height * 0.8
    for i, line in enumerate(lines):
        y = y_start - i * line_height
        text_w = c.stringWidth(line, FONT_NAME, FONT_SIZE)
        x = (PAGE - text_w) / 2
        c.drawString(x, y, line)
    c.showPage()


def draw_image_page(c, img_path):
    """Page simple carree : illustration plein cadre (l'image est deja carree)."""
    c.setFillColor(HexColor("#F5F5F5"))
    c.rect(0, 0, PAGE, PAGE, fill=1, stroke=0)
    c.drawImage(img_path, 0, 0, width=PAGE, height=PAGE, preserveAspectRatio=False)
    c.showPage()

# --- Marque Piklo ---
BRAND_NAME     = "Piklo"
BRAND_BASELINE = "Chaque histoire merite son heros."
BRAND_SITE     = "studiopiklo.com"

C_CREME    = "#FFF7EE"
C_ORANGE   = "#D2774B"
C_ORANGE2  = "#E6AC63"
C_BRUN     = "#38302A"
C_GRIS     = "#9A9AA8"
C_SURTITRE = "#F0C99B"


def draw_piklo_mark(c, x, y, box=11 * mm, gap=3.2 * mm, label_color="#FFF7EE",
                    label_size=23, with_label=True):
    c.setFillColor(HexColor(C_ORANGE))
    c.roundRect(x, y, box, box, box * 0.32, fill=1, stroke=0)
    c.setFillColor(HexColor(C_CREME))
    r = box * 0.17
    c.circle(x + box / 2, y + box / 2, r, fill=1, stroke=0)
    if with_label:
        c.setFillColor(HexColor(label_color))
        c.setFont(F_TITLE, label_size)
        c.drawString(x + box + gap, y + (box - label_size * 0.72) / 2, BRAND_NAME)


def draw_tracked(c, text, font, size, color, center_x, y, tracking):
    c.setFillColor(HexColor(color))
    c.setFont(font, size)
    widths = [c.stringWidth(ch, font, size) for ch in text]
    total = sum(widths) + tracking * (len(text) - 1)
    x = center_x - total / 2
    for ch, w in zip(text, widths):
        c.drawString(x, y, ch)
        x += w + tracking


def draw_front_cover(c, titre, img_path):
    """Premiere page du PDF : couverture avant, page simple carree.
    Illustration plein cadre + scrims + logo + titre en bas."""
    cx = PAGE / 2

    c.drawImage(img_path, 0, 0, width=PAGE, height=PAGE,
                preserveAspectRatio=True, anchor='c')

    # Scrim sombre en bas (lisibilite du titre)
    scrim_h = 95 * mm
    bands = 90
    bh = scrim_h / bands
    for i in range(bands):
        frac = i / (bands - 1)
        alpha = 0.86 * (1 - frac) ** 1.6
        c.setFillColor(Color(28 / 255, 17 / 255, 9 / 255, alpha=alpha))
        c.rect(0, i * bh, PAGE, bh + 1.2, fill=1, stroke=0)

    # Scrim leger en haut (pour le logo)
    top_h = 40 * mm
    tbands = 50
    tbh = top_h / tbands
    for i in range(tbands):
        frac = i / (tbands - 1)
        alpha = 0.42 * (1 - frac) ** 1.6
        c.setFillColor(Color(34 / 255, 22 / 255, 12 / 255, alpha=alpha))
        c.rect(0, PAGE - (i + 1) * tbh, PAGE, tbh + 1.2, fill=1, stroke=0)

    # Logo Piklo (haut gauche, dans la zone de securite)
    draw_piklo_mark(c, SAFE + 2 * mm, PAGE - 22 * mm,
                    label_color=C_CREME, label_size=21)

    # Surtitre
    draw_tracked(c, "UNE HISTOIRE PERSONNALISEE", F_BODY_B, 9, C_SURTITRE,
                 cx, 60 * mm, 2.4)

    # Titre principal, centre, 2 lignes max, dans la zone de securite
    title_size = 38
    max_w = PAGE - 2 * (SAFE + 8 * mm)
    tlines = wrap_text(c, titre, F_TITLE, title_size, max_w)
    while len(tlines) > 2 and title_size > 24:
        title_size -= 2
        tlines = wrap_text(c, titre, F_TITLE, title_size, max_w)
    c.setFillColor(HexColor(C_CREME))
    c.setFont(F_TITLE, title_size)
    line_h = title_size * 1.12
    ty = 40 * mm + (len(tlines) - 1) * line_h
    for line in tlines:
        lw = c.stringWidth(line, F_TITLE, title_size)
        c.drawString(cx - lw / 2, ty, line)
        ty -= line_h

    # Trait orange sous le titre
    bar_w = 18 * mm
    c.setFillColor(HexColor(C_ORANGE))
    c.roundRect(cx - bar_w / 2, 31 * mm, bar_w, 1.4 * mm, 0.7 * mm, fill=1, stroke=0)

    c.showPage()


def draw_back_cover(c, enfant_nom=None):
    """Derniere page du PDF : quatrieme de couverture, page simple carree."""
    cx = PAGE / 2

    c.setFillColor(HexColor(C_CREME))
    c.rect(0, 0, PAGE, PAGE, fill=1, stroke=0)

    # Logo + "Piklo" en haut, centre
    box = 12 * mm
    label_size = 26
    label_w = c.stringWidth(BRAND_NAME, F_TITLE, label_size)
    gap = 3.5 * mm
    group_w = box + gap + label_w
    gx = cx - group_w / 2
    gy = PAGE - 48 * mm
    draw_piklo_mark(c, gx, gy, box=box, gap=gap,
                    label_color=C_ORANGE, label_size=label_size)

    # Baseline
    c.setFillColor(HexColor(C_BRUN))
    c.setFont(F_BODY, 13)
    bw = c.stringWidth(BRAND_BASELINE, F_BODY, 13)
    c.drawString(cx - bw / 2, PAGE - 60 * mm, BRAND_BASELINE)

    # Mention personnalisee, centree
    prenom = (enfant_nom or "votre enfant").strip()
    intro = "Une histoire imaginee pour "
    size = 15
    iw = c.stringWidth(intro, F_ITALIC, size)
    pw = c.stringWidth(prenom, F_ITALIC, size)
    total = iw + pw
    mx = cx - total / 2
    my = PAGE / 2
    c.setFont(F_ITALIC, size)
    c.setFillColor(HexColor(C_BRUN))
    c.drawString(mx, my, intro)
    c.setFillColor(HexColor(C_ORANGE))
    c.drawString(mx + iw, my, prenom)

    # CTA site
    cta_intro = "Creez la votre sur"
    c.setFont(F_BODY, 12)
    ci_w = c.stringWidth(cta_intro, F_BODY, 12)
    site_w = c.stringWidth(BRAND_SITE, F_BODY_B, 12)
    cta_total = ci_w + 2 * mm + site_w
    cta_x = cx - cta_total / 2
    cta_y = 50 * mm
    c.setFillColor(HexColor(C_GRIS))
    c.setFont(F_BODY, 12)
    c.drawString(cta_x, cta_y, cta_intro)
    c.setFillColor(HexColor(C_ORANGE))
    c.setFont(F_BODY_B, 12)
    c.drawString(cta_x + ci_w + 2 * mm, cta_y, BRAND_SITE)

    # Mentions legales (au-dessus de la zone de securite basse)
    c.setFillColor(HexColor(C_GRIS))
    c.setFont(F_BODY, 9)
    legal_lines = [
        "Histoire et illustrations generees par intelligence artificielle.",
        f"(c) 2026 Piklo - {BRAND_SITE}. Tous droits reserves.",
    ]
    ly = SAFE + 8 * mm
    for line in reversed(legal_lines):
        lw = c.stringWidth(line, F_BODY, 9)
        c.drawString(cx - lw / 2, ly, line)
        ly += 13

    c.showPage()

# --- Assemblage ---

def _prepare_image(content: bytes, path: str):
    """Charge l'image, la met en RGB, la recadre en carre si besoin, et
    l'upscale vers TARGET_PX (300 dpi sur 210 mm) si elle est plus petite."""
    img = PILImage.open(io.BytesIO(content)).convert("RGB")
    w, h = img.size
    if w != h:
        cote = min(w, h)
        left = (w - cote) // 2
        top = (h - cote) // 2
        img = img.crop((left, top, left + cote, top + cote))
    if img.size[0] < TARGET_PX:
        img = img.resize((TARGET_PX, TARGET_PX), PILImage.LANCZOS)
    img.save(path, format="JPEG", quality=95, dpi=(300, 300))


def assembler_pdf(histoire_id, palette_id=PALETTE_DEFAUT, histoire=None):
    if histoire is None:
        histoire = fetch_histoire(histoire_id)
    pages = fetch_pages(histoire_id)
    titre = histoire.get("titre", "Mon livre")
    pages_ok = [p for p in pages if p.get("image_url")]

    if not pages_ok:
        raise ValueError("Aucune page avec image_url trouvee.")

    # On borne a NB_SCENES (10) pour rester dans le forfait Prodigi 24 pages.
    pages_ok = pages_ok[:NB_SCENES]

    try:
        idx = int(palette_id)
    except (TypeError, ValueError):
        idx = PALETTE_DEFAUT
    if idx < 0 or idx >= len(PALETTE):
        idx = PALETTE_DEFAUT
    bg_hex, fg_hex = PALETTE[idx]

    with tempfile.TemporaryDirectory() as tmp:
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
            _prepare_image(r.content, path)
            img_paths[pid] = path

        pdf_path = os.path.join(tmp, "livre.pdf")
        c = canvas.Canvas(pdf_path, pagesize=(PAGE, PAGE))

        # 1) Couverture avant (page simple) : illustration de la 1re scene
        enfant_nom = fetch_heros_nom()
        draw_front_cover(c, titre, img_paths[pages_ok[0]["id"]])

        # 2) Pour chaque scene : page texte puis page illustration
        for page in pages_ok:
            draw_text_page(c, page.get("legende", ""), bg_hex, fg_hex)
            draw_image_page(c, img_paths[page["id"]])

        # 3) Quatrieme de couverture (page simple)
        draw_back_cover(c, enfant_nom=enfant_nom)

        c.save()

        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

    pdf_url = upload_pdf(pdf_bytes, histoire_id, idx)
    return pdf_url, idx

# --- Routes ---

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/generate-pdf", methods=["POST"])
def generate_pdf():
    if API_SECRET:
        token = request.headers.get("X-API-Secret", "")
        if token != API_SECRET:
            return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data or "histoire_id" not in data:
        return jsonify({"error": "histoire_id requis"}), 400

    histoire_id = data["histoire_id"]
    palette_id = data.get("palette_id", PALETTE_DEFAUT)
    force = bool(data.get("force", False))

    try:
        wanted = int(palette_id)
    except (TypeError, ValueError):
        wanted = PALETTE_DEFAUT
    if wanted < 0 or wanted >= len(PALETTE):
        wanted = PALETTE_DEFAUT

    try:
        histoire = fetch_histoire(histoire_id)
        contenu = histoire.get("contenu") or {}
        cached_url = contenu.get("pdf_url")
        cached_palette = contenu.get("pdf_palette_id")

        if not force and cached_url and cached_palette == wanted:
            return jsonify({
                "pdf_url": cached_url,
                "histoire_id": histoire_id,
                "cached": True,
            })

        pdf_url, used_palette = assembler_pdf(
            histoire_id, palette_id=wanted, histoire=histoire
        )
        save_pdf_cache(histoire_id, contenu, pdf_url, used_palette)
        return jsonify({
            "pdf_url": pdf_url,
            "histoire_id": histoire_id,
            "cached": False,
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
