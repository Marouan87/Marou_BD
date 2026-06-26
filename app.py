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

# Polices de marque Piklo (Quicksand pour les titres, Nunito pour le corps).
# Si un fichier manque on retombe sur DejaVu pour ne pas casser la generation.
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
F_TITLE   = BRAND_FONTS["Quicksand-Bold"]   # titre couverture + mot "Piklo"
F_BODY    = BRAND_FONTS["Nunito"]           # surtitre, mentions
F_BODY_B  = BRAND_FONTS["Nunito-Bold"]
F_ITALIC  = BRAND_FONTS["Nunito-Italic"]

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
# ─── Marque Piklo (4e de couverture) ──────────────────────────────────────────
BRAND_NAME     = "Piklo"
BRAND_BASELINE = "Chaque histoire mérite son héros."
BRAND_SITE     = "studiopiklo.com"

# Tokens couleur (alignes sur la maquette Claude Design)
C_CREME    = "#FFF7EE"   # fond 4e de couv
C_ORANGE   = "#D2774B"   # orange chaud Piklo
C_ORANGE2  = "#E6AC63"   # haut du degrade du logo
C_BRUN     = "#38302A"   # brun texte
C_GRIS     = "#9A9AA8"   # mentions secondaires
C_SURTITRE = "#F0C99B"   # surtitre sur l'illustration (premiere de couv)


def draw_piklo_mark(c, x, y, box=11 * mm, gap=3.2 * mm, label_color="#FFF7EE",
                    label_size=23, with_label=True):
    """Logo Piklo : carre arrondi degrade orange + disque creme, suivi de 'Piklo'.
    (x, y) = coin bas-gauche du carre. Le degrade est approxime par un aplat
    orange (ReportLab ne fait pas de degrade lineaire simple sur un rect)."""
    # Carre arrondi
    c.setFillColor(HexColor(C_ORANGE))
    c.roundRect(x, y, box, box, box * 0.32, fill=1, stroke=0)
    # Disque creme centre
    c.setFillColor(HexColor(C_CREME))
    r = box * 0.17
    c.circle(x + box / 2, y + box / 2, r, fill=1, stroke=0)
    # Mot "Piklo"
    if with_label:
        c.setFillColor(HexColor(label_color))
        c.setFont(F_TITLE, label_size)
        c.drawString(x + box + gap, y + (box - label_size * 0.72) / 2, BRAND_NAME)


def draw_cover(c, titre, img_path, enfant_nom=None):
    """
    Double page de couverture en mode impression.
    Moitie gauche  : quatrieme de couverture (page de garde)
    Moitie droite  : premiere de couverture, illustration plein cadre + titre en bas
    """
    # ══ Moitie droite : PREMIERE DE COUVERTURE (maquette plein cadre) ══
    # Illustration plein cadre sur toute la demi-page droite
    c.drawImage(img_path, HALF_W, 0, width=HALF_W, height=PAGE_H,
                preserveAspectRatio=True, anchor='c')

    # Scrim sombre en bas (lisibilite du titre). Approxime par bandes empilees
    # de transparence croissante, du bas (opaque) vers le haut (transparent).
    scrim_h = 95 * mm
    bands = 90
    bh = scrim_h / bands
    for i in range(bands):
        frac = i / (bands - 1)               # 0 en bas, 1 en haut
        alpha = 0.86 * (1 - frac) ** 1.6
        c.setFillColor(Color(28 / 255, 17 / 255, 9 / 255, alpha=alpha))
        c.rect(HALF_W, i * bh, HALF_W, bh + 1.2, fill=1, stroke=0)

    # Scrim leger en haut (pour le logo)
    top_h = 40 * mm
    tbands = 50
    tbh = top_h / tbands
    for i in range(tbands):
        frac = i / (tbands - 1)
        alpha = 0.42 * (1 - frac) ** 1.6
        c.setFillColor(Color(34 / 255, 22 / 255, 12 / 255, alpha=alpha))
        c.rect(HALF_W, PAGE_H - (i + 1) * tbh, HALF_W, tbh + 1.2, fill=1, stroke=0)

    # Logo Piklo (haut gauche de la couverture)
    draw_piklo_mark(c, HALF_W + 12 * mm, PAGE_H - 22 * mm,
                    label_color=C_CREME, label_size=21)

    # Bloc titre, centre en bas
    cx = HALF_W + HALF_W / 2          # centre horizontal de la demi-page droite
    # Surtitre
    surtitre = "UNE HISTOIRE PERSONNALISÉE"
    c.setFillColor(HexColor(C_SURTITRE))
    c.setFont(F_BODY_B, 9)
    # interlettrage manuel pour l'effet "letter-spacing"
    def draw_tracked(text, font, size, color, center_x, y, tracking):
        c.setFillColor(HexColor(color))
        c.setFont(font, size)
        widths = [c.stringWidth(ch, font, size) for ch in text]
        total = sum(widths) + tracking * (len(text) - 1)
        x = center_x - total / 2
        for ch, w in zip(text, widths):
            c.drawString(x, y, ch)
            x += w + tracking
    draw_tracked(surtitre, F_BODY_B, 9, C_SURTITRE, cx, 60 * mm, 2.4)

    # Titre principal (Quicksand), centre, peut tenir sur 2 lignes
    title_size = 38
    tlines = wrap_text(c, titre, F_TITLE, title_size, HALF_W - 30 * mm)
    while len(tlines) > 2 and title_size > 24:
        title_size -= 2
        tlines = wrap_text(c, titre, F_TITLE, title_size, HALF_W - 30 * mm)
    c.setFillColor(HexColor(C_CREME))
    c.setFont(F_TITLE, title_size)
    line_h = title_size * 1.12
    ty = 40 * mm + (len(tlines) - 1) * line_h
    for line in tlines:
        lw = c.stringWidth(line, F_TITLE, title_size)
        c.drawString(cx - lw / 2, ty, line)
        ty -= line_h

    # Petit trait orange sous le titre
    bar_w = 18 * mm
    c.setFillColor(HexColor(C_ORANGE))
    c.roundRect(cx - bar_w / 2, 31 * mm, bar_w, 1.4 * mm, 0.7 * mm,
                fill=1, stroke=0)

    # ══ Moitie gauche : QUATRIEME DE COUVERTURE (page de garde) ══
    c.setFillColor(HexColor(C_CREME))
    c.rect(0, 0, HALF_W, PAGE_H, fill=1, stroke=0)

    lcx = HALF_W / 2   # centre horizontal de la demi-page gauche

    # Logo Piklo + "Piklo" en haut, centre
    box = 12 * mm
    label_size = 26
    label_w = c.stringWidth(BRAND_NAME, F_TITLE, label_size)
    gap = 3.5 * mm
    group_w = box + gap + label_w
    gx = lcx - group_w / 2
    gy = PAGE_H - 40 * mm
    draw_piklo_mark(c, gx, gy, box=box, gap=gap,
                    label_color=C_ORANGE, label_size=label_size)

    # Baseline sous le logo
    c.setFillColor(HexColor(C_BRUN))
    c.setFont(F_BODY, 13)
    bw = c.stringWidth(BRAND_BASELINE, F_BODY, 13)
    c.drawString(lcx - bw / 2, PAGE_H - 52 * mm, BRAND_BASELINE)

    # Mention de personnalisation au centre, prenom en orange entre deux traits.
    # Forme : "Une histoire imaginée pour  — Nael —"
    prenom = (enfant_nom or "votre enfant").strip()
    intro = "Une histoire imaginée pour"
    c.setFillColor(HexColor(C_BRUN))
    c.setFont(F_ITALIC, 14)
    iw = c.stringWidth(intro, F_ITALIC, 14)
    my = PAGE_H / 2 + 6 * mm
    c.drawString(lcx - iw / 2, my, intro)

    # Ligne prenom : tiret — prenom(orange) — tiret
    name_size = 20
    name_w = c.stringWidth(prenom, F_TITLE, name_size)
    dash = "—"
    c.setFont(F_TITLE, name_size)
    dash_w = c.stringWidth(dash, F_TITLE, name_size)
    dgap = 5 * mm
    ny = my - 12 * mm
    total = dash_w + dgap + name_w + dgap + dash_w
    nx = lcx - total / 2
    c.setFillColor(HexColor(C_ORANGE))
    c.drawString(nx, ny, dash)
    c.drawString(nx + dash_w + dgap, ny, prenom)
    c.drawString(nx + dash_w + dgap + name_w + dgap, ny, dash)

    # CTA : site en orange gras, centre
    cta_intro = "Créez la vôtre sur"
    c.setFillColor(HexColor(C_GRIS))
    c.setFont(F_BODY, 12)
    ci_w = c.stringWidth(cta_intro, F_BODY, 12)
    c.setFont(F_BODY_B, 12)
    site_w = c.stringWidth(BRAND_SITE, F_BODY_B, 12)
    cta_total = ci_w + 2 * mm + site_w
    cta_x = lcx - cta_total / 2
    cta_y = 38 * mm
    c.setFillColor(HexColor(C_GRIS))
    c.setFont(F_BODY, 12)
    c.drawString(cta_x, cta_y, cta_intro)
    c.setFillColor(HexColor(C_ORANGE))
    c.setFont(F_BODY_B, 12)
    c.drawString(cta_x + ci_w + 2 * mm, cta_y, BRAND_SITE)

    # Mentions legales completes (pied)
    c.setFillColor(HexColor(C_GRIS))
    c.setFont(F_BODY, 9)
    legal_lines = [
        "Histoire et illustrations générées par intelligence artificielle.",
        f"© 2026 Piklo — {BRAND_SITE}. Tous droits réservés.",
    ]
    ly = 26 * mm
    for line in legal_lines:
        lw = c.stringWidth(line, F_BODY, 9)
        c.drawString(lcx - lw / 2, ly, line)
        ly -= 13

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
