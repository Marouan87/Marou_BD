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
        params={"id": f"eq.{histoire_id}", "select": "id,titre,theme,contenu,reassurance"},
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
BRAND_BASELINE = "Chaque histoire m\u00e9rite son h\u00e9ros."
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


def draw_back_cover(c, enfant_nom=None, univers_titre=None, univers_texte=None):
    """Derniere page du PDF : quatrieme de couverture riche, page simple carree.
    Structure facon editeur : logo, surtitre, accroche (titre d'univers),
    paragraphe de presentation (texte d'univers), mention personnalisee
    encadree de points, puis pied (site, edition, mention IA)."""
    cx = PAGE / 2

    c.setFillColor(HexColor(C_CREME))
    c.rect(0, 0, PAGE, PAGE, fill=1, stroke=0)

    # ── Logo + "Piklo" en haut, centre ──
    box = 13 * mm
    label_size = 27
    label_w = c.stringWidth(BRAND_NAME, F_TITLE, label_size)
    gap = 4 * mm
    group_w = box + gap + label_w
    gx = cx - group_w / 2
    gy = PAGE - 34 * mm
    draw_piklo_mark(c, gx, gy, box=box, gap=gap,
                    label_color=C_ORANGE, label_size=label_size)

    # ── Surtitre "L'HISTOIRE" en interlettrage dore ──
    draw_tracked(c, "L'HISTOIRE", F_BODY_B, 10, C_SURTITRE, cx, PAGE - 62 * mm, 3.0)

    # ── Accroche : titre d'univers, en gros, avec prenom en orange si present ──
    prenom = (enfant_nom or "votre enfant").strip()
    accroche = (univers_titre or "Une histoire rien qu'\u00e0 lui").strip()
    acc_size = 30
    max_w = PAGE - 2 * (SAFE + 12 * mm)
    acc_lines = wrap_text(c, accroche, F_TITLE, acc_size, max_w)
    while len(acc_lines) > 2 and acc_size > 20:
        acc_size -= 2
        acc_lines = wrap_text(c, accroche, F_TITLE, acc_size, max_w)
    c.setFillColor(HexColor(C_BRUN))
    c.setFont(F_TITLE, acc_size)
    ay = PAGE - 78 * mm
    line_h = acc_size * 1.16
    for line in acc_lines:
        lw = c.stringWidth(line, F_TITLE, acc_size)
        c.drawString(cx - lw / 2, ay, line)
        ay -= line_h

    # ── Paragraphe de presentation : texte d'univers ──
    if univers_texte:
        c.setFillColor(HexColor("#7C7064"))
        para_size = 13
        para_w = PAGE - 2 * (SAFE + 14 * mm)
        para_lines = wrap_text(c, univers_texte.strip(), F_BODY, para_size, para_w)
        py = ay - 6 * mm
        pl_h = para_size * 1.55
        for line in para_lines:
            lw = c.stringWidth(line, F_BODY, para_size)
            c.drawString(cx - lw / 2, py, line)
            py -= pl_h
    else:
        py = ay

    # ── Mention personnalisee encadree de points orange ──
    mention = "Une histoire cr\u00e9\u00e9e sp\u00e9cialement pour "
    msize = 12
    mi_w = c.stringWidth(mention, F_BODY_B, msize)
    mp_w = c.stringWidth(prenom, F_BODY_B, msize)
    dot_gap = 8 * mm
    total_w = mi_w + mp_w
    block_y = py - 16 * mm
    start_x = cx - total_w / 2
    # point gauche
    c.setFillColor(HexColor(C_ORANGE))
    c.circle(start_x - dot_gap, block_y + msize * 0.32, 1.6, fill=1, stroke=0)
    # texte (intro brun doux + prenom orange)
    c.setFillColor(HexColor("#8A7E70"))
    c.setFont(F_BODY_B, msize)
    c.drawString(start_x, block_y, mention)
    c.setFillColor(HexColor(C_ORANGE))
    c.drawString(start_x + mi_w, block_y, prenom)
    # point droit
    c.circle(start_x + total_w + dot_gap, block_y + msize * 0.32, 1.6, fill=1, stroke=0)

    # ── Filet separateur au-dessus du pied ──
    foot_top = SAFE + 26 * mm
    c.setStrokeColor(HexColor("#EADFD0"))
    c.setLineWidth(0.7)
    c.line(SAFE + 6 * mm, foot_top, PAGE - SAFE - 6 * mm, foot_top)

    # ── Pied : site (gras orange) a gauche, mentions en dessous ──
    foot_x = SAFE + 6 * mm
    c.setFillColor(HexColor(C_ORANGE))
    c.setFont(F_BODY_B, 13)
    c.drawString(foot_x, foot_top - 9 * mm, BRAND_SITE)

    c.setFillColor(HexColor(C_GRIS))
    c.setFont(F_BODY, 9.5)
    c.drawString(foot_x, foot_top - 15 * mm, "\u00c9dition personnalis\u00e9e \u00b7 Imprim\u00e9 en France \u00b7 2026")
    c.setFont(F_BODY, 9)
    c.drawString(foot_x, foot_top - 20 * mm, "Histoire et illustrations g\u00e9n\u00e9r\u00e9es par intelligence artificielle.")

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

        # 3) Quatrieme de couverture (page simple) avec resume d'univers
        reassurance = histoire.get("reassurance") or {}
        univers = reassurance.get("univers") or {}
        draw_back_cover(
            c,
            enfant_nom=enfant_nom,
            univers_titre=univers.get("titre"),
            univers_texte=univers.get("texte"),
        )

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
