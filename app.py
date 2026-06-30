"""
API Flask - Assemblage PDF livre illustre (version IMPRESSION Gelato)
POST /generate-pdf  { "histoire_id": "uuid" }
Reponse : { "pdf_url": "...", "cover_url": "..." }

Format Gelato hardcover carre 20x20 cm, 30 pages interieures :
- Deux fichiers PDF distincts :
  1. PDF interieur  : 31 pages de 200x200 mm
     - Page 1       : endpaper blanc (non imprimable)
     - Page 2       : faux-titre
     - Page 3       : dedicace (message parent ou phrase generique)
     - Pages 4-27   : histoire 12 scenes (texte + illustration)
     - Page 28      : pourquoi c'est parfait pour [prenom] (sur mesure)
     - Page 29      : page marketing (3 univers + QR code)
     - Page 30      : ce que cette histoire apporte (benefices)
     - Page 31      : endpaper blanc (non imprimable)
  2. PDF couverture : wraparound 458x246 mm
"""

import os
import io
import json
import requests
import tempfile
import qrcode
from flask import Flask, request, jsonify
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, Color
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from PIL import Image as PILImage, ImageDraw as PILDraw, ImageFilter as PILFilter

app = Flask(__name__)

# ── Polices ──────────────────────────────────────────────────────────────────
_FONT_DIR = os.path.dirname(os.path.abspath(__file__))
_SYS_DIR  = "/usr/share/fonts/truetype/dejavu"

def _font_path(filename):
    local = os.path.join(_FONT_DIR, filename)
    if os.path.exists(local):
        return local
    return os.path.join(_SYS_DIR, filename)

pdfmetrics.registerFont(TTFont("DejaVu",         _font_path("DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("DejaVu-Bold",    _font_path("DejaVuSans-Bold.ttf")))
pdfmetrics.registerFont(TTFont("DejaVu-Oblique", _font_path("DejaVuSans-Oblique.ttf")))

def _register_brand_fonts():
    brand = {
        "Quicksand-Bold": ("Quicksand-Bold.ttf",  "DejaVu-Bold"),
        "Nunito":         ("Nunito-Regular.ttf",   "DejaVu"),
        "Nunito-Bold":    ("Nunito-Bold.ttf",      "DejaVu-Bold"),
        "Nunito-Italic":  ("Nunito-Italic.ttf",    "DejaVu-Oblique"),
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
F_TITLE  = BRAND_FONTS["Quicksand-Bold"]
F_BODY   = BRAND_FONTS["Nunito"]
F_BODY_B = BRAND_FONTS["Nunito-Bold"]
F_ITALIC = BRAND_FONTS["Nunito-Italic"]

# ── Config ───────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
API_SECRET   = os.environ.get("API_SECRET", "")

HEADERS = {
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "apikey": SUPABASE_KEY,
}

# ── Dimensions ───────────────────────────────────────────────────────────────
PAGE_MM       = 200
PAGE          = PAGE_MM * mm
SAFE          = 10 * mm
TEXT_MARGIN_X = 20 * mm

# Wraparound couverture (API Gelato cover-dimensions, pageCount=32)
WRAP_W = 458.0 * mm
WRAP_H = 246.0 * mm

CONTENT_BACK_X_MM  = 20.0
CONTENT_BACK_W_MM  = 198.0
CONTENT_BACK_H_MM  = 206.0
CONTENT_BACK_Y_MM  = 20.0
SPINE_X_MM         = 226.0
SPINE_W_MM         = 6.0
CONTENT_FRONT_X_MM = 240.0
CONTENT_FRONT_W_MM = 198.0
CONTENT_FRONT_H_MM = 206.0
CONTENT_FRONT_Y_MM = 20.0

NB_SCENES = 12
TARGET_PX = 2362

TEXT_FONT = F_BODY
TEXT_SIZE = 22

PALETTE = [
    ("#D2774B", "#FFFFFF"),
    ("#FFF7EE", "#38302A"),
    ("#F4E8D6", "#5A4632"),
    ("#F6E3DE", "#6A4138"),
    ("#E7EDE3", "#3C4A36"),
    ("#E4ECF2", "#33414E"),
]
PALETTE_DEFAUT = 0

# ── Assets statiques page marketing ──────────────────────────────────────────
UNIVERS_MARKETING = [
    {
        "titre": "Lana offre sa tetine a la fee",
        "url": "https://nrqvexaulphzjtjkczje.supabase.co/storage/v1/object/public/images/Lana_Cov.png",
    },
    {
        "titre": "Nael arrive au Maroc",
        "url": "https://nrqvexaulphzjtjkczje.supabase.co/storage/v1/object/public/images/Nael_Cov.png",
    },
    {
        "titre": "Noah a une petite soeur !",
        "url": "https://nrqvexaulphzjtjkczje.supabase.co/storage/v1/object/public/images/Noah_Couv.png",
    },
]

BRAND_SITE = "studiopiklo.com"
BRAND_NAME = "Piklo"
C_CREME    = "#FFF7EE"
C_ORANGE   = "#D2774B"
C_BRUN     = "#38302A"
C_GRIS     = "#9A9AA8"
C_SURTITRE = "#F0C99B"

DEDICACE_DEFAUT = (
    "Cette histoire a ete creee specialement pour toi. "
    "Que tu la lises mille fois, elle sera toujours la tienne."
)

# ── Supabase ─────────────────────────────────────────────────────────────────

def fetch_histoire(histoire_id):
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/histoires",
        headers=HEADERS,
        params={
            "id": f"eq.{histoire_id}",
            "select": "id,titre,theme,contenu,reassurance,personnage_principal,dedicace",
        },
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


def upload_pdf(pdf_bytes, filename):
    r = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/pdfs/{filename}",
        headers={**HEADERS, "Content-Type": "application/pdf", "x-upsert": "true"},
        data=pdf_bytes,
    )
    r.raise_for_status()
    return f"{SUPABASE_URL}/storage/v1/object/public/pdfs/{filename}"


def save_pdf_cache(histoire_id, contenu, pdf_url, cover_url, palette_id):
    new_contenu = dict(contenu or {})
    new_contenu["pdf_url"]        = pdf_url
    new_contenu["cover_url"]      = cover_url
    new_contenu["pdf_palette_id"] = palette_id
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/histoires",
        headers={**HEADERS, "Content-Type": "application/json"},
        params={"id": f"eq.{histoire_id}"},
        data=json.dumps({"contenu": new_contenu}),
    )
    r.raise_for_status()

# ── Helpers ───────────────────────────────────────────────────────────────────

def _prenom_affiche(valeur):
    if not valeur:
        return None
    valeur = valeur.strip()
    if not valeur:
        return None
    out, debut_mot = [], True
    for ch in valeur:
        if debut_mot and ch.isalpha():
            out.append(ch.upper())
            debut_mot = False
        else:
            out.append(ch)
        if ch in (" ", "-"):
            debut_mot = True
    return "".join(out)


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


def draw_tracked(c, text, font, size, color, center_x, y, tracking):
    c.setFillColor(HexColor(color))
    c.setFont(font, size)
    widths = [c.stringWidth(ch, font, size) for ch in text]
    total = sum(widths) + tracking * (len(text) - 1)
    x = center_x - total / 2
    for ch, w in zip(text, widths):
        c.drawString(x, y, ch)
        x += w + tracking


def draw_piklo_mark(c, x, y, box=11*mm, gap=3.2*mm,
                    label_color=C_CREME, label_size=23, with_label=True):
    c.setFillColor(HexColor(C_ORANGE))
    c.roundRect(x, y, box, box, box * 0.32, fill=1, stroke=0)
    c.setFillColor(HexColor(C_CREME))
    c.circle(x + box / 2, y + box / 2, box * 0.17, fill=1, stroke=0)
    if with_label:
        c.setFillColor(HexColor(label_color))
        c.setFont(F_TITLE, label_size)
        c.drawString(x + box + gap, y + (box - label_size * 0.72) / 2, BRAND_NAME)


def draw_check(c, cx, cy, r, color=C_ORANGE):
    """Pastille ronde avec une coche blanche, centree sur (cx, cy)."""
    c.setFillColor(HexColor(color))
    c.circle(cx, cy, r, fill=1, stroke=0)
    c.setStrokeColor(HexColor(C_CREME))
    c.setLineWidth(r * 0.32)
    c.setLineCap(1)
    c.setLineJoin(1)
    p = c.beginPath()
    p.moveTo(cx - r * 0.42, cy + r * 0.02)
    p.lineTo(cx - r * 0.10, cy - r * 0.34)
    p.lineTo(cx + r * 0.46, cy + r * 0.34)
    c.drawPath(p, stroke=1, fill=0)


def _prepare_image(content, path):
    img = PILImage.open(io.BytesIO(content)).convert("RGB")
    w, h = img.size
    if w != h:
        cote = min(w, h)
        left = (w - cote) // 2
        top  = (h - cote) // 2
        img  = img.crop((left, top, left + cote, top + cote))
    if img.size[0] < TARGET_PX:
        img = img.resize((TARGET_PX, TARGET_PX), PILImage.LANCZOS)
    img.save(path, format="JPEG", quality=95, dpi=(300, 300))


def _download_image(url, path):
    print(f"Downloading: {url}", flush=True)
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    _prepare_image(r.content, path)


def _draw_placeholder(c, x, y, w, h, color=C_ORANGE):
    c.setFillColor(HexColor(color))
    c.rect(x, y, w, h, fill=1, stroke=0)

# ── Pages interieures ─────────────────────────────────────────────────────────

def draw_endpaper(c):
    c.setFillColor(HexColor(C_CREME))
    c.rect(0, 0, PAGE, PAGE, fill=1, stroke=0)
    c.showPage()


def draw_faux_titre(c, titre):
    cx = PAGE / 2
    c.setFillColor(HexColor(C_CREME))
    c.rect(0, 0, PAGE, PAGE, fill=1, stroke=0)

    bar_w = 14 * mm
    c.setFillColor(HexColor(C_ORANGE))
    c.roundRect(cx - bar_w / 2, PAGE - 52*mm, bar_w, 1.2*mm, 0.6*mm, fill=1, stroke=0)

    box = 9 * mm
    label_size = 18
    label_w = c.stringWidth(BRAND_NAME, F_TITLE, label_size)
    gap = 3 * mm
    group_w = box + gap + label_w
    draw_piklo_mark(c, cx - group_w / 2, PAGE - 46*mm,
                    box=box, gap=gap, label_color=C_ORANGE, label_size=label_size)

    title_size = 28
    max_w = PAGE - 2 * (SAFE + 10*mm)
    tlines = wrap_text(c, titre, F_TITLE, title_size, max_w)
    while len(tlines) > 3 and title_size > 18:
        title_size -= 2
        tlines = wrap_text(c, titre, F_TITLE, title_size, max_w)
    c.setFillColor(HexColor(C_BRUN))
    c.setFont(F_TITLE, title_size)
    line_h = title_size * 1.2
    ty = PAGE / 2 + ((len(tlines) - 1) * line_h) / 2
    for line in tlines:
        lw = c.stringWidth(line, F_TITLE, title_size)
        c.drawString(cx - lw / 2, ty, line)
        ty -= line_h

    c.setFillColor(HexColor(C_ORANGE))
    c.roundRect(cx - bar_w / 2, SAFE + 18*mm, bar_w, 1.2*mm, 0.6*mm, fill=1, stroke=0)
    c.setFillColor(HexColor(C_GRIS))
    c.setFont(F_BODY, 9)
    site_w = c.stringWidth(BRAND_SITE, F_BODY, 9)
    c.drawString(cx - site_w / 2, SAFE + 12*mm, BRAND_SITE)
    c.showPage()


def draw_dedicace(c, dedicace_texte, prenom=None):
    cx = PAGE / 2
    c.setFillColor(HexColor(C_CREME))
    c.rect(0, 0, PAGE, PAGE, fill=1, stroke=0)

    texte = dedicace_texte.strip() if dedicace_texte else DEDICACE_DEFAUT

    c.setFillColor(HexColor(C_ORANGE))
    c.setFont(F_TITLE, 48)
    c.drawString(SAFE + 4*mm, PAGE - 50*mm, "\u201c")

    c.setFillColor(HexColor(C_BRUN))
    c.setFont(F_ITALIC, 15)
    available_w = PAGE - 2 * (SAFE + 10*mm)
    lines = wrap_text(c, texte, F_ITALIC, 15, available_w)
    line_h = 15 * 1.6
    total_h = len(lines) * line_h
    y = PAGE / 2 + total_h / 2
    for line in lines:
        lw = c.stringWidth(line, F_ITALIC, 15)
        c.drawString(cx - lw / 2, y, line)
        y -= line_h

    if prenom:
        c.setFillColor(HexColor(C_ORANGE))
        c.setFont(F_BODY_B, 12)
        pw = c.stringWidth(f"-- {prenom}", F_BODY_B, 12)
        c.drawString(cx - pw / 2, y - 8*mm, f"-- {prenom}")

    c.setFillColor(HexColor(C_ORANGE))
    c.setFont(F_TITLE, 48)
    c.drawString(PAGE - SAFE - 20*mm, SAFE + 28*mm, "\u201d")
    c.showPage()


def draw_text_page(c, legende, bg_hex, fg_hex):
    c.setFillColor(HexColor(bg_hex))
    c.rect(0, 0, PAGE, PAGE, fill=1, stroke=0)
    c.setFillColor(HexColor(fg_hex))
    c.setFont(TEXT_FONT, TEXT_SIZE)
    available_w = PAGE - 2 * TEXT_MARGIN_X
    lines = wrap_text(c, legende or "", TEXT_FONT, TEXT_SIZE, available_w)
    if not lines:
        c.showPage()
        return
    line_height = TEXT_SIZE * 1.8
    cap = TEXT_SIZE * 0.70
    visual_h = (len(lines) - 1) * line_height + cap
    first_baseline = PAGE / 2 + visual_h / 2 - cap
    for i, line in enumerate(lines):
        y = first_baseline - i * line_height
        text_w = c.stringWidth(line, TEXT_FONT, TEXT_SIZE)
        x = (PAGE - text_w) / 2
        c.drawString(x, y, line)
    c.showPage()


def draw_image_page(c, img_path):
    c.setFillColor(HexColor("#F5F5F5"))
    c.rect(0, 0, PAGE, PAGE, fill=1, stroke=0)
    c.drawImage(img_path, 0, 0, width=PAGE, height=PAGE, preserveAspectRatio=False)
    c.showPage()


def draw_pourquoi_sur_mesure(c, reassurance, prenom):
    """Page 28 : 'Sur mesure / Pourquoi c'est parfait pour [prenom] ?'
    Liste les raisons de reassurance.pourquoi, chacune avec une coche."""
    cx = PAGE / 2
    c.setFillColor(HexColor(C_CREME))
    c.rect(0, 0, PAGE, PAGE, fill=1, stroke=0)

    # Surtitre
    draw_tracked(c, "SUR MESURE", F_BODY_B, 9, C_SURTITRE, cx, PAGE - 32*mm, 3.0)

    # Titre
    prenom_txt = prenom or "votre enfant"
    titre_q = f"Pourquoi c'est parfait pour {prenom_txt} ?"
    t_size = 22
    max_w = PAGE - 2 * (SAFE + 8*mm)
    t_lines = wrap_text(c, titre_q, F_TITLE, t_size, max_w)
    while len(t_lines) > 2 and t_size > 16:
        t_size -= 2
        t_lines = wrap_text(c, titre_q, F_TITLE, t_size, max_w)
    c.setFillColor(HexColor(C_BRUN))
    c.setFont(F_TITLE, t_size)
    ty = PAGE - 46*mm
    for line in t_lines:
        lw = c.stringWidth(line, F_TITLE, t_size)
        c.drawString(cx - lw / 2, ty, line)
        ty -= t_size * 1.2

    # Trait orange
    bar_w = 12 * mm
    c.setFillColor(HexColor(C_ORANGE))
    c.roundRect(cx - bar_w / 2, ty - 2*mm, bar_w, 1.2*mm, 0.6*mm, fill=1, stroke=0)

    # Liste des raisons
    raisons = (reassurance or {}).get("pourquoi") or []
    if not isinstance(raisons, list):
        raisons = []
    raisons = [str(x).strip() for x in raisons if str(x).strip()]

    if raisons:
        # Marges du bloc liste
        list_left  = SAFE + 14*mm
        list_right = PAGE - SAFE - 12*mm
        check_r    = 3.0*mm
        text_x     = list_left + check_r * 2 + 4*mm
        text_w     = list_right - text_x
        line_h     = 13 * 1.5
        item_gap   = 7*mm
        body_size  = 13

        # Hauteur totale pour centrer verticalement le bloc dans l'espace dispo
        total_h = 0
        wrapped = []
        for r in raisons:
            wl = wrap_text(c, r, F_BODY, body_size, text_w)
            wrapped.append(wl)
            total_h += len(wl) * line_h + item_gap
        total_h -= item_gap

        zone_top = ty - 14*mm
        zone_bot = SAFE + 18*mm
        zone_h = zone_top - zone_bot
        y = zone_top - max(0, (zone_h - total_h) / 2)

        for wl in wrapped:
            block_h = len(wl) * line_h
            # Coche alignee sur la premiere ligne
            check_cy = y - body_size * 0.30
            draw_check(c, list_left + check_r, check_cy, check_r)
            # Texte
            c.setFillColor(HexColor("#5A5048"))
            c.setFont(F_BODY, body_size)
            ly = y
            for wline in wl:
                c.drawString(text_x, ly, wline)
                ly -= line_h
            y -= block_h + item_gap

    c.showPage()


def _make_book_cover(img_content, out_w=300, out_h=380):
    SPINE_COLOR = (210, 119, 75)
    SPINE_W = int(out_w * 0.06)
    pad = 16

    src = PILImage.open(io.BytesIO(img_content)).convert("RGBA")
    canvas_img = PILImage.new("RGBA", (out_w + pad, out_h + pad), (0, 0, 0, 0))

    shadow = PILImage.new("RGBA", (out_w - SPINE_W + 4, out_h - 8), (0, 0, 0, 70))
    shadow = shadow.filter(PILFilter.GaussianBlur(6))
    canvas_img.paste(shadow, (SPINE_W + 6, 10), shadow)

    spine = PILImage.new("RGBA", (SPINE_W, out_h - 8), (*SPINE_COLOR, 255))
    draw_spine = PILDraw.Draw(spine)
    for x in range(SPINE_W):
        factor = x / SPINE_W
        col = tuple(max(0, int(ch * (0.7 + 0.3 * factor))) for ch in SPINE_COLOR)
        draw_spine.line([(x, 0), (x, out_h - 8)], fill=(*col, 255))
    canvas_img.paste(spine, (0, 4), spine)

    page_w = out_w - SPINE_W
    page_h = out_h - 8
    page_img = src.resize((page_w, page_h), PILImage.LANCZOS)

    vignette = PILImage.new("RGBA", (page_w, page_h), (0, 0, 0, 0))
    vdraw = PILDraw.Draw(vignette)
    for i in range(10):
        alpha = int(25 * (1 - i / 10))
        vdraw.rectangle([i, i, page_w - i - 1, page_h - i - 1],
                        outline=(0, 0, 0, alpha))
    page_combined = PILImage.alpha_composite(page_img, vignette)
    canvas_img.paste(page_combined, (SPINE_W, 4), page_combined)

    reflet_h = int(page_h * 0.12)
    reflet = PILImage.new("RGBA", (page_w, reflet_h), (0, 0, 0, 0))
    rdraw = PILDraw.Draw(reflet)
    for y in range(reflet_h):
        alpha = int(20 * (1 - y / reflet_h))
        rdraw.line([(0, y), (page_w, y)], fill=(255, 255, 255, alpha))
    canvas_img.paste(reflet, (SPINE_W, 4), reflet)

    return canvas_img


def _vignette_arrondie(img_content, target_px=600, radius_ratio=0.07,
                       shadow_blur=18, shadow_alpha=90, shadow_offset=10):
    """Prend une couverture finie et renvoie une image RGBA avec coins
    arrondis et ombre portee douce, prete a etre posee sur le fond brun."""
    src = PILImage.open(io.BytesIO(img_content)).convert("RGB")
    w, h = src.size
    # Normaliser la largeur a target_px en gardant le ratio
    new_w = target_px
    new_h = int(round(h * (target_px / w)))
    src = src.resize((new_w, new_h), PILImage.LANCZOS).convert("RGBA")

    radius = int(min(new_w, new_h) * radius_ratio)

    # Masque arrondi
    mask = PILImage.new("L", (new_w, new_h), 0)
    mdraw = PILDraw.Draw(mask)
    mdraw.rounded_rectangle([0, 0, new_w - 1, new_h - 1], radius=radius, fill=255)
    src.putalpha(mask)

    # Canvas avec marge pour l'ombre
    pad = shadow_blur * 2 + shadow_offset
    canvas_img = PILImage.new("RGBA", (new_w + pad * 2, new_h + pad * 2), (0, 0, 0, 0))

    # Ombre : silhouette arrondie floutee
    shadow = PILImage.new("RGBA", canvas_img.size, (0, 0, 0, 0))
    sdraw = PILDraw.Draw(shadow)
    sx0 = pad + shadow_offset // 2
    sy0 = pad + shadow_offset
    sdraw.rounded_rectangle(
        [sx0, sy0, sx0 + new_w, sy0 + new_h],
        radius=radius, fill=(20, 12, 6, shadow_alpha)
    )
    shadow = shadow.filter(PILFilter.GaussianBlur(shadow_blur))
    canvas_img = PILImage.alpha_composite(canvas_img, shadow)

    # Image par-dessus
    canvas_img.alpha_composite(src, (pad, pad))
    return canvas_img, pad, new_w, new_h


def draw_marketing(c, tmp_dir):
    cx = PAGE / 2
    c.setFillColor(HexColor(C_BRUN))
    c.rect(0, 0, PAGE, PAGE, fill=1, stroke=0)

    # ── En-tete ────────────────────────────────────────────────────────────
    draw_tracked(c, "DECOUVREZ NOS HISTOIRES", F_BODY_B, 8, C_SURTITRE,
                 cx, PAGE - 28*mm, 2.5)
    c.setFillColor(HexColor(C_CREME))
    c.setFont(F_TITLE, 22)
    titre_w = c.stringWidth("Encore plus d'aventures !", F_TITLE, 22)
    c.drawString(cx - titre_w / 2, PAGE - 42*mm, "Encore plus d'aventures !")

    # ── Trois vignettes arrondies avec ombre ──────────────────────────────────
    vignette_w = 52 * mm
    gap = 5 * mm
    total_w = 3 * vignette_w + 2 * gap
    start_x = cx - total_w / 2
    y_top = PAGE - 58*mm   # haut des vignettes

    for i, univers in enumerate(UNIVERS_MARKETING):
        x = start_x + i * (vignette_w + gap)
        if univers["url"]:
            try:
                r = requests.get(univers["url"], timeout=20)
                r.raise_for_status()
                vig, pad, vw_px, vh_px = _vignette_arrondie(r.content)
                book_path = os.path.join(tmp_dir, f"mktg_{i}.png")
                vig.save(book_path, format="PNG")
                # Echelle : la largeur visible de l'image = vignette_w
                scale = vignette_w / vw_px
                full_w = vig.size[0] * scale
                full_h = vig.size[1] * scale
                pad_pt = pad * scale
                # On positionne pour que le coin haut-gauche de l'IMAGE
                # (hors padding ombre) tombe a (x, y_top - hauteur image)
                draw_h_img = vh_px * scale
                img_x = x - pad_pt
                img_y = (y_top - draw_h_img) - pad_pt
                c.drawImage(book_path, img_x, img_y,
                            width=full_w, height=full_h,
                            preserveAspectRatio=False, mask='auto')
            except Exception:
                draw_h = vignette_w
                _draw_placeholder(c, x, y_top - draw_h, vignette_w, draw_h)
        else:
            draw_h = vignette_w
            _draw_placeholder(c, x, y_top - draw_h, vignette_w, draw_h)

    # ── Encart CTA en bas ─────────────────────────────────────────────────────
    card_x = SAFE + 2*mm
    card_w = PAGE - 2 * (SAFE + 2*mm)
    card_h = 56*mm
    card_y = SAFE + 8*mm
    card_r = 6*mm

    c.setFillColor(HexColor(C_CREME))
    c.roundRect(card_x, card_y, card_w, card_h, card_r, fill=1, stroke=0)

    # QR a droite, dans un petit cadre
    qr = qrcode.make(f"https://{BRAND_SITE}")
    qr_path = os.path.join(tmp_dir, "qr.png")
    qr.save(qr_path)
    qr_size = 30*mm
    qr_pad = 4*mm
    qr_box = qr_size + 2 * qr_pad
    qr_box_x = card_x + card_w - qr_box - 10*mm
    qr_box_y = card_y + (card_h - qr_box) / 2
    c.setFillColor(HexColor("#FFFFFF"))
    c.setStrokeColor(HexColor("#E7D9C6"))
    c.setLineWidth(0.8)
    c.roundRect(qr_box_x, qr_box_y, qr_box, qr_box, 3*mm, fill=1, stroke=1)
    c.drawImage(qr_path, qr_box_x + qr_pad, qr_box_y + qr_pad,
                width=qr_size, height=qr_size)

    # Texte a gauche
    txt_x = card_x + 12*mm
    # Surtitre aligne a gauche (tracking manuel depuis txt_x)
    c.setFillColor(HexColor(C_ORANGE))
    c.setFont(F_BODY_B, 8)
    _sx = txt_x
    for ch in "OFFRIR UNE HISTOIRE":
        c.drawString(_sx, card_y + card_h - 16*mm, ch)
        _sx += c.stringWidth(ch, F_BODY_B, 8) + 1.6

    c.setFillColor(HexColor(C_BRUN))
    titre_cta = "Et si la prochaine aventure etait la sienne ?"
    cta_size = 16
    max_cta_w = (qr_box_x - 6*mm) - txt_x
    cta_lines = wrap_text(c, titre_cta, F_TITLE, cta_size, max_cta_w)
    while len(cta_lines) > 2 and cta_size > 12:
        cta_size -= 1
        cta_lines = wrap_text(c, titre_cta, F_TITLE, cta_size, max_cta_w)
    c.setFont(F_TITLE, cta_size)
    cy = card_y + card_h - 24*mm
    for line in cta_lines:
        c.drawString(txt_x, cy, line)
        cy -= cta_size * 1.2

    c.setFillColor(HexColor("#7C7064"))
    c.setFont(F_BODY, 10)
    sub = "Scannez le code pour creer un livre ou votre enfant devient le heros."
    sub_lines = wrap_text(c, sub, F_BODY, 10, max_cta_w)
    cy -= 2*mm
    for line in sub_lines:
        c.drawString(txt_x, cy, line)
        cy -= 10 * 1.4

    c.setFillColor(HexColor(C_ORANGE))
    c.setFont(F_BODY_B, 10)
    c.drawString(qr_box_x + (qr_box - c.stringWidth(BRAND_SITE, F_BODY_B, 10)) / 2,
                 qr_box_y - 6*mm, BRAND_SITE)

    c.showPage()


def draw_benefices(c, reassurance, prenom):
    """Page 30 : 'Ce que cette histoire apporte' - 3 benefices (titre + texte)."""
    cx = PAGE / 2
    c.setFillColor(HexColor(C_CREME))
    c.rect(0, 0, PAGE, PAGE, fill=1, stroke=0)

    # Surtitre
    draw_tracked(c, "LES BIENFAITS DE CETTE HISTOIRE", F_BODY_B, 8, C_SURTITRE,
                 cx, PAGE - 30*mm, 2.2)

    # Titre
    titre_b = "Ce que cette histoire apporte"
    t_size = 22
    max_w = PAGE - 2 * (SAFE + 8*mm)
    t_lines = wrap_text(c, titre_b, F_TITLE, t_size, max_w)
    while len(t_lines) > 2 and t_size > 16:
        t_size -= 2
        t_lines = wrap_text(c, titre_b, F_TITLE, t_size, max_w)
    c.setFillColor(HexColor(C_BRUN))
    c.setFont(F_TITLE, t_size)
    ty = PAGE - 44*mm
    for line in t_lines:
        lw = c.stringWidth(line, F_TITLE, t_size)
        c.drawString(cx - lw / 2, ty, line)
        ty -= t_size * 1.2

    # Trait orange
    bar_w = 12 * mm
    c.setFillColor(HexColor(C_ORANGE))
    c.roundRect(cx - bar_w / 2, ty - 2*mm, bar_w, 1.2*mm, 0.6*mm, fill=1, stroke=0)

    # Liste des benefices
    benefices = (reassurance or {}).get("benefices") or []
    if not isinstance(benefices, list):
        benefices = []

    bloc_left  = SAFE + 12*mm
    bloc_right = PAGE - SAFE - 12*mm
    text_w     = bloc_right - bloc_left
    titre_size = 14
    body_size  = 12
    body_lh    = body_size * 1.5
    item_gap   = 9*mm

    # Mesure pour centrage vertical
    wrapped = []
    total_h = 0
    for b in benefices:
        b_titre = (b.get("titre") or "").strip()
        b_texte = (b.get("texte") or "").strip()
        tl = wrap_text(c, b_texte, F_BODY, body_size, text_w)
        wrapped.append((b_titre, tl))
        total_h += titre_size * 1.3 + len(tl) * body_lh + item_gap
    total_h -= item_gap

    zone_top = ty - 12*mm
    zone_bot = SAFE + 16*mm
    zone_h = zone_top - zone_bot
    y = zone_top - max(0, (zone_h - total_h) / 2)

    for b_titre, tl in wrapped:
        if b_titre:
            c.setFillColor(HexColor(C_ORANGE))
            c.setFont(F_TITLE, titre_size)
            c.drawString(bloc_left, y, b_titre)
            y -= titre_size * 1.3
        c.setFillColor(HexColor("#5A5048"))
        c.setFont(F_BODY, body_size)
        for wline in tl:
            c.drawString(bloc_left, y, wline)
            y -= body_lh
        y -= item_gap

    # Footer
    foot_top = SAFE + 24*mm
    c.setStrokeColor(HexColor("#EADFD0"))
    c.setLineWidth(0.7)
    c.line(SAFE + 4*mm, foot_top, PAGE - SAFE - 4*mm, foot_top)
    foot_x = SAFE + 4*mm
    c.setFillColor(HexColor(C_ORANGE))
    c.setFont(F_BODY_B, 11)
    c.drawString(foot_x, foot_top - 8*mm, BRAND_SITE)
    c.setFillColor(HexColor(C_GRIS))
    c.setFont(F_BODY, 8.5)
    c.drawString(foot_x, foot_top - 14*mm,
                 "\u00c9dition personnalis\u00e9e \u00b7 Imprim\u00e9 en France \u00b7 2026")
    c.setFont(F_BODY, 8)
    c.drawString(foot_x, foot_top - 19*mm,
                 "Histoire et illustrations g\u00e9n\u00e9r\u00e9es par intelligence artificielle.")
    c.showPage()


# ── Wraparound couverture ─────────────────────────────────────────────────────

def draw_wraparound(c, titre, img_path_front, histoire=None, prenom=None):
    # Fond creme
    c.setFillColor(HexColor(C_CREME))
    c.rect(0, 0, WRAP_W, WRAP_H, fill=1, stroke=0)

    fx  = CONTENT_FRONT_X_MM * mm
    fy  = (246.0 - CONTENT_FRONT_Y_MM - CONTENT_FRONT_H_MM) * mm
    fw  = CONTENT_FRONT_W_MM * mm
    fh  = CONTENT_FRONT_H_MM * mm
    fcx = fx + fw / 2

    bx  = CONTENT_BACK_X_MM * mm
    by  = (246.0 - CONTENT_BACK_Y_MM - CONTENT_BACK_H_MM) * mm
    bw  = CONTENT_BACK_W_MM * mm
    bh  = CONTENT_BACK_H_MM * mm
    bcx = bx + bw / 2

    sx  = SPINE_X_MM * mm
    sy  = by
    sw  = SPINE_W_MM * mm
    sh  = bh

    # ── Couverture avant ──────────────────────────────────────────────────────
    if img_path_front and os.path.exists(img_path_front):
        c.drawImage(img_path_front, fx, fy, width=fw, height=fh,
                    preserveAspectRatio=False)
    else:
        _draw_placeholder(c, fx, fy, fw, fh)

    scrim_h = fh * 0.46
    bands = 60
    bh_band = scrim_h / bands
    for i in range(bands):
        frac = i / (bands - 1)
        alpha = 0.86 * (1 - frac) ** 1.6
        c.setFillColor(Color(28/255, 17/255, 9/255, alpha=alpha))
        c.rect(fx, fy + i * bh_band, fw, bh_band + 1.2, fill=1, stroke=0)

    top_h = fh * 0.18
    tbands = 30
    tbh = top_h / tbands
    for i in range(tbands):
        frac = i / (tbands - 1)
        alpha = 0.42 * (1 - frac) ** 1.6
        c.setFillColor(Color(34/255, 22/255, 12/255, alpha=alpha))
        c.rect(fx, fy + fh - (i+1) * tbh, fw, tbh + 1.2, fill=1, stroke=0)

    safe_f = SAFE * 0.6
    draw_piklo_mark(c, fx + safe_f, fy + fh - 18*mm,
                    box=8*mm, gap=2.5*mm, label_color=C_CREME, label_size=16)

    draw_tracked(c, "UNE HISTOIRE PERSONNALISEE", F_BODY_B, 7, C_SURTITRE,
                 fcx, fy + 26*mm, 2.0)

    title_size = 28
    max_w = fw - 2 * safe_f
    tlines = wrap_text(c, titre, F_TITLE, title_size, max_w)
    while len(tlines) > 2 and title_size > 18:
        title_size -= 2
        tlines = wrap_text(c, titre, F_TITLE, title_size, max_w)
    c.setFillColor(HexColor(C_CREME))
    c.setFont(F_TITLE, title_size)
    line_h = title_size * 1.12
    ty = fy + 18*mm + (len(tlines) - 1) * line_h
    for line in tlines:
        lw = c.stringWidth(line, F_TITLE, title_size)
        c.drawString(fcx - lw / 2, ty, line)
        ty -= line_h

    bar_w = 12 * mm
    c.setFillColor(HexColor(C_ORANGE))
    c.roundRect(fcx - bar_w/2, fy + 11*mm, bar_w, 1.0*mm, 0.5*mm, fill=1, stroke=0)

    # ── Quatrieme de couverture ───────────────────────────────────────────────
    c.setFillColor(HexColor(C_CREME))
    c.rect(bx, by, bw, bh, fill=1, stroke=0)

    box_b = 13 * mm
    lsize_b = 27
    lw_b = c.stringWidth(BRAND_NAME, F_TITLE, lsize_b)
    gap_b = 4 * mm
    gw_b = box_b + gap_b + lw_b
    draw_piklo_mark(c, bcx - gw_b / 2, by + bh - 34*mm,
                    box=box_b, gap=gap_b, label_color=C_ORANGE, label_size=lsize_b)

    draw_tracked(c, "L'HISTOIRE", F_BODY_B, 10, C_SURTITRE, bcx, by + bh - 52*mm, 3.0)

    reassurance = (histoire.get("reassurance") or {}) if histoire else {}
    univers = reassurance.get("univers") or {}
    accroche = (univers.get("titre") or "Une histoire rien qu'\u00e0 lui").strip()
    acc_size = 28
    max_w_b = bw - 2 * (SAFE + 6*mm)
    acc_lines = wrap_text(c, accroche, F_TITLE, acc_size, max_w_b)
    while len(acc_lines) > 2 and acc_size > 18:
        acc_size -= 2
        acc_lines = wrap_text(c, accroche, F_TITLE, acc_size, max_w_b)
    c.setFillColor(HexColor(C_BRUN))
    c.setFont(F_TITLE, acc_size)
    ay = by + bh - 68*mm
    for line in acc_lines:
        lw = c.stringWidth(line, F_TITLE, acc_size)
        c.drawString(bcx - lw / 2, ay, line)
        ay -= acc_size * 1.16

    univers_texte = univers.get("texte")
    if univers_texte:
        c.setFillColor(HexColor("#7C7064"))
        c.setFont(F_BODY, 13)
        para_lines = wrap_text(c, univers_texte.strip(), F_BODY, 13, max_w_b)
        py = ay - 5*mm
        for line in para_lines:
            lw = c.stringWidth(line, F_BODY, 13)
            c.drawString(bcx - lw / 2, py, line)
            py -= 13 * 1.55
    else:
        py = ay

    if prenom:
        mention = "Une histoire cr\u00e9\u00e9e sp\u00e9cialement pour "
        msize = 12
        mi_w = c.stringWidth(mention, F_BODY_B, msize)
        mp_w = c.stringWidth(prenom, F_BODY_B, msize)
        dot_gap = 7 * mm
        total_w = mi_w + mp_w
        block_y = py - 12*mm
        start_x = bcx - total_w / 2
        c.setFillColor(HexColor(C_ORANGE))
        c.circle(start_x - dot_gap, block_y + msize * 0.32, 1.6, fill=1, stroke=0)
        c.setFillColor(HexColor("#8A7E70"))
        c.setFont(F_BODY_B, msize)
        c.drawString(start_x, block_y, mention)
        c.setFillColor(HexColor(C_ORANGE))
        c.drawString(start_x + mi_w, block_y, prenom)
        c.circle(start_x + total_w + dot_gap, block_y + msize * 0.32, 1.6, fill=1, stroke=0)

    foot_top_b = by + 26*mm
    c.setStrokeColor(HexColor("#EADFD0"))
    c.setLineWidth(0.7)
    c.line(bx + 6*mm, foot_top_b, bx + bw - 6*mm, foot_top_b)
    c.setFillColor(HexColor(C_ORANGE))
    c.setFont(F_BODY_B, 13)
    c.drawString(bx + 6*mm, foot_top_b - 9*mm, BRAND_SITE)
    c.setFillColor(HexColor(C_GRIS))
    c.setFont(F_BODY, 9)
    c.drawString(bx + 6*mm, foot_top_b - 15*mm,
                 "\u00c9dition personnalis\u00e9e \u00b7 Imprim\u00e9 en France \u00b7 2026")
    c.setFont(F_BODY, 8.5)
    c.drawString(bx + 6*mm, foot_top_b - 20*mm,
                 "Illustrations g\u00e9n\u00e9r\u00e9es par intelligence artificielle.")

    c.setFillColor(HexColor(C_ORANGE))
    c.rect(sx, sy, sw, sh, fill=1, stroke=0)

    c.saveState()
    c.translate(sx + sw/2, sy + sh/2)
    c.rotate(90)
    c.setFillColor(HexColor(C_CREME))
    c.setFont(F_TITLE, 5)
    spine_txt = f"Piklo  -  {titre}"
    stw = c.stringWidth(spine_txt, F_TITLE, 5)
    c.drawString(-stw/2, -1.5, spine_txt)
    c.restoreState()

    c.showPage()

# ── Assemblage ────────────────────────────────────────────────────────────────

def assembler_pdf_gelato(histoire_id, palette_id=PALETTE_DEFAUT, histoire=None):
    if histoire is None:
        histoire = fetch_histoire(histoire_id)
    pages    = fetch_pages(histoire_id)
    titre    = histoire.get("titre", "Mon livre")
    pages_ok = [p for p in pages if p.get("image_url")][:NB_SCENES]

    if not pages_ok:
        raise ValueError("Aucune page avec image_url trouvee.")

    try:
        idx = int(palette_id)
    except (TypeError, ValueError):
        idx = PALETTE_DEFAUT
    if idx < 0 or idx >= len(PALETTE):
        idx = PALETTE_DEFAUT
    bg_hex, fg_hex = PALETTE[idx]

    prenom      = _prenom_affiche(histoire.get("personnage_principal"))
    dedicace    = histoire.get("dedicace")
    reassurance = histoire.get("reassurance") or {}

    with tempfile.TemporaryDirectory() as tmp:
        img_paths = {}
        for page in pages_ok:
            pid  = page["id"]
            path = os.path.join(tmp, f"{pid}.jpg")
            real_url = (
                f"{SUPABASE_URL}/storage/v1/object/public/"
                f"images/{histoire_id}/{pid}.png"
            )
            _download_image(real_url, path)
            img_paths[pid] = path

        # PDF interieur
        interior_path = os.path.join(tmp, "interior.pdf")
        c = canvas.Canvas(interior_path, pagesize=(PAGE, PAGE))

        draw_endpaper(c)                                         # p.1
        draw_faux_titre(c, titre)                                # p.2
        draw_dedicace(c, dedicace, prenom)                       # p.3
        for page in pages_ok:                                    # p.4-27
            draw_text_page(c, page.get("legende", ""), bg_hex, fg_hex)
            draw_image_page(c, img_paths[page["id"]])
        draw_pourquoi_sur_mesure(c, reassurance, prenom)         # p.28
        draw_marketing(c, tmp)                                   # p.29
        draw_benefices(c, reassurance, prenom)                   # p.30
        draw_endpaper(c)                                         # p.31

        c.save()

        # PDF couverture wraparound
        cover_path = os.path.join(tmp, "cover.pdf")
        cc = canvas.Canvas(cover_path, pagesize=(WRAP_W, WRAP_H))
        draw_wraparound(cc, titre, img_paths[pages_ok[0]["id"]],
                        histoire=histoire, prenom=prenom)
        cc.save()

        with open(interior_path, "rb") as f:
            interior_bytes = f.read()
        with open(cover_path, "rb") as f:
            cover_bytes = f.read()

        pdf_url   = upload_pdf(interior_bytes,
                               f"{histoire_id}_p{idx}_interior.pdf")
        cover_url = upload_pdf(cover_bytes,
                               f"{histoire_id}_p{idx}_cover.pdf")

    save_pdf_cache(histoire_id,
                   histoire.get("contenu") or {},
                   pdf_url, cover_url, idx)
    return pdf_url, cover_url, idx

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/generate-pdf", methods=["POST"])
def generate_pdf():
    if API_SECRET:
        if request.headers.get("X-API-Secret", "") != API_SECRET:
            return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data or "histoire_id" not in data:
        return jsonify({"error": "histoire_id requis"}), 400

    histoire_id = data["histoire_id"]
    palette_id  = data.get("palette_id", PALETTE_DEFAUT)
    force       = bool(data.get("force", False))

    try:
        wanted = int(palette_id)
    except (TypeError, ValueError):
        wanted = PALETTE_DEFAUT
    if wanted < 0 or wanted >= len(PALETTE):
        wanted = PALETTE_DEFAUT

    try:
        histoire = fetch_histoire(histoire_id)
        contenu  = histoire.get("contenu") or {}
        cached_pdf   = contenu.get("pdf_url")
        cached_cover = contenu.get("cover_url")
        cached_pal   = contenu.get("pdf_palette_id")

        if not force and cached_pdf and cached_cover and cached_pal == wanted:
            return jsonify({
                "pdf_url":     cached_pdf,
                "cover_url":   cached_cover,
                "histoire_id": histoire_id,
                "cached":      True,
            })

        pdf_url, cover_url, used_palette = assembler_pdf_gelato(
            histoire_id, palette_id=wanted, histoire=histoire
        )
        return jsonify({
            "pdf_url":     pdf_url,
            "cover_url":   cover_url,
            "histoire_id": histoire_id,
            "cached":      False,
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
