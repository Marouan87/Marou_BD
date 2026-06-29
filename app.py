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
     - Page 28      : page reassurance
     - Page 29      : page marketing (3 univers + QR code)
     - Page 30      : pourquoi cette histoire pour [prenom]
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


def draw_reassurance(c, reassurance, titre_histoire, prenom):
    cx = PAGE / 2
    c.setFillColor(HexColor(C_CREME))
    c.rect(0, 0, PAGE, PAGE, fill=1, stroke=0)

    draw_tracked(c, "POUR TOI", F_BODY_B, 9, C_SURTITRE, cx, PAGE - 36*mm, 3.0)

    c.setFillColor(HexColor(C_BRUN))
    c.setFont(F_TITLE, 22)
    max_w = PAGE - 2 * (SAFE + 8*mm)
    tlines = wrap_text(c, titre_histoire or "", F_TITLE, 22, max_w)
    ty = PAGE - 50*mm
    for line in tlines:
        lw = c.stringWidth(line, F_TITLE, 22)
        c.drawString(cx - lw / 2, ty, line)
        ty -= 22 * 1.2

    c.setFillColor(HexColor(C_ORANGE))
    bar_w = 12 * mm
    c.roundRect(cx - bar_w / 2, ty - 4*mm, bar_w, 1.2*mm, 0.6*mm, fill=1, stroke=0)

    univers = (reassurance or {}).get("univers") or {}
    texte_r = univers.get("texte") or ""
    if texte_r:
        c.setFillColor(HexColor("#7C7064"))
        c.setFont(F_BODY, 13)
        para_w = PAGE - 2 * (SAFE + 12*mm)
        para_lines = wrap_text(c, texte_r.strip(), F_BODY, 13, para_w)
        py = ty - 12*mm
        for line in para_lines:
            lw = c.stringWidth(line, F_BODY, 13)
            c.drawString(cx - lw / 2, py, line)
            py -= 13 * 1.55

    if prenom:
        nom_txt = f"Une histoire creee pour {prenom}"
        c.setFillColor(HexColor(C_ORANGE))
        c.setFont(F_BODY_B, 11)
        nw = c.stringWidth(nom_txt, F_BODY_B, 11)
        c.drawString(cx - nw / 2, SAFE + 22*mm, nom_txt)

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


def draw_marketing(c, tmp_dir):
    cx = PAGE / 2
    c.setFillColor(HexColor(C_BRUN))
    c.rect(0, 0, PAGE, PAGE, fill=1, stroke=0)

    draw_tracked(c, "DECOUVREZ NOS HISTOIRES", F_BODY_B, 8, C_SURTITRE,
                 cx, PAGE - 28*mm, 2.5)
    c.setFillColor(HexColor(C_CREME))
    c.setFont(F_TITLE, 20)
    titre_w = c.stringWidth("Encore plus d'aventures !", F_TITLE, 20)
    c.drawString(cx - titre_w / 2, PAGE - 40*mm, "Encore plus d'aventures !")

    vignette_w = 48 * mm
    gap = 6 * mm
    total_w = 3 * vignette_w + 2 * gap
    start_x = cx - total_w / 2
    # Ancre haute des vignettes : on part du bas du titre et on descend
    y_top = PAGE - 50*mm

    for i, univers in enumerate(UNIVERS_MARKETING):
        x = start_x + i * (vignette_w + gap)
        if univers["url"]:
            try:
                r = requests.get(univers["url"], timeout=20)
                r.raise_for_status()
                book_img = _make_book_cover(r.content, out_w=300, out_h=380)
                # Fond sombre pour eviter les problemes de transparence
                bg = PILImage.new("RGB", book_img.size, (56, 48, 42))
                bg.paste(book_img, mask=book_img.split()[3])
                book_path = os.path.join(tmp_dir, f"mktg_{i}.jpg")
                bg.save(book_path, format="JPEG", quality=92)
                # Ratio exact de l'image generee, ancree par le haut
                img_w_px, img_h_px = book_img.size
                draw_w = vignette_w
                draw_h = draw_w * (img_h_px / img_w_px)
                y_img = y_top - draw_h
                c.drawImage(book_path,
                            x + (vignette_w - draw_w) / 2,
                            y_img,
                            width=draw_w,
                            height=draw_h,
                            preserveAspectRatio=False,
                            mask='auto')
            except Exception:
                draw_h = vignette_w * 1.25
                y_img = y_top - draw_h
                _draw_placeholder(c, x, y_img, vignette_w, draw_h)
        else:
            draw_h = vignette_w * 1.25
            y_img = y_top - draw_h
            _draw_placeholder(c, x, y_img, vignette_w, draw_h)

        c.setFillColor(HexColor(C_CREME))
        if univers["titre"]:
            tit = univers["titre"]
            font_size = 8
            c.setFont(F_BODY, font_size)
            tw = c.stringWidth(tit, F_BODY, font_size)
            if tw > vignette_w:
                font_size = 7
                c.setFont(F_BODY, font_size)
                tw = c.stringWidth(tit, F_BODY, font_size)
            c.drawString(x + vignette_w / 2 - tw / 2, y_img - 5*mm, tit)

    qr = qrcode.make(f"https://{BRAND_SITE}")
    qr_path = os.path.join(tmp_dir, "qr.png")
    qr.save(qr_path)
    qr_size = 22 * mm
    qr_x = cx - qr_size / 2
    qr_y = SAFE + 18*mm
    c.drawImage(qr_path, qr_x, qr_y, width=qr_size, height=qr_size)
    c.setFillColor(HexColor(C_CREME))
    c.setFont(F_BODY_B, 9)
    site_w = c.stringWidth(BRAND_SITE, F_BODY_B, 9)
    c.drawString(cx - site_w / 2, qr_y - 5*mm, BRAND_SITE)
    c.showPage()


def draw_pourquoi_cette_histoire(c, prenom=None, univers_titre=None, univers_texte=None):
    """Page 30 : pourquoi cette histoire est parfaite pour [prenom].
    Angle benefice parent, distinct de la page reassurance."""
    cx = PAGE / 2
    c.setFillColor(HexColor(C_CREME))
    c.rect(0, 0, PAGE, PAGE, fill=1, stroke=0)

    # Surtitre
    prenom_txt = prenom or "votre enfant"
    surtitre = f"POURQUOI CETTE HISTOIRE POUR {prenom_txt.upper()} ?"
    draw_tracked(c, surtitre, F_BODY_B, 8, C_SURTITRE, cx, PAGE - 30*mm, 2.0)

    # Accroche
    accroche = (univers_titre or "Une histoire con\u00e7ue pour lui").strip()
    acc_size = 26
    max_w = PAGE - 2 * (SAFE + 10*mm)
    acc_lines = wrap_text(c, accroche, F_TITLE, acc_size, max_w)
    while len(acc_lines) > 2 and acc_size > 18:
        acc_size -= 2
        acc_lines = wrap_text(c, accroche, F_TITLE, acc_size, max_w)
    c.setFillColor(HexColor(C_BRUN))
    c.setFont(F_TITLE, acc_size)
    ay = PAGE - 46*mm
    for line in acc_lines:
        lw = c.stringWidth(line, F_TITLE, acc_size)
        c.drawString(cx - lw / 2, ay, line)
        ay -= acc_size * 1.16

    # Trait orange
    bar_w = 12 * mm
    c.setFillColor(HexColor(C_ORANGE))
    c.roundRect(cx - bar_w / 2, ay - 4*mm, bar_w, 1.2*mm, 0.6*mm, fill=1, stroke=0)
    ay -= 10*mm

    # Paragraphe benefice
    if univers_texte:
        texte_benefice = univers_texte.strip()
    else:
        texte_benefice = (
            f"Cette histoire a ete con\u00e7ue pour accompagner {prenom_txt} "
            "dans une \u00e9tape importante, avec douceur et bienveillance. "
            "Chaque page a \u00e9t\u00e9 pens\u00e9e pour qu'il s'y reconnaisse "
            "et se sente compris."
        )

    c.setFillColor(HexColor("#7C7064"))
    c.setFont(F_BODY, 13)
    para_w = PAGE - 2 * (SAFE + 12*mm)
    para_lines = wrap_text(c, texte_benefice, F_BODY, 13, para_w)
    py = ay - 2*mm
    for line in para_lines:
        lw = c.stringWidth(line, F_BODY, 13)
        c.drawString(cx - lw / 2, py, line)
        py -= 13 * 1.6

    # Bloc "3 bonnes raisons"
    raisons = [
        "\u2022  Une histoire \u00e0 son pr\u00e9nom, dans son univers",
        "\u2022  Des illustrations cr\u00e9\u00e9es rien que pour lui",
        "\u2022  Un souvenir unique \u00e0 garder toute la vie",
    ]
    py -= 6*mm
    c.setFillColor(HexColor(C_BRUN))
    c.setFont(F_BODY_B, 12)
    for raison in raisons:
        lw = c.stringWidth(raison, F_BODY_B, 12)
        c.drawString(cx - lw / 2, py, raison)
        py -= 12 * 1.7

    # Mention personnalisee
    if prenom:
        mention = "Cr\u00e9\u00e9 sp\u00e9cialement pour "
        msize = 12
        mi_w = c.stringWidth(mention, F_BODY_B, msize)
        mp_w = c.stringWidth(prenom, F_BODY_B, msize)
        dot_gap = 7 * mm
        total_w = mi_w + mp_w
        block_y = py - 10*mm
        start_x = cx - total_w / 2
        c.setFillColor(HexColor(C_ORANGE))
        c.circle(start_x - dot_gap, block_y + msize * 0.32, 1.4, fill=1, stroke=0)
        c.setFillColor(HexColor("#8A7E70"))
        c.setFont(F_BODY_B, msize)
        c.drawString(start_x, block_y, mention)
        c.setFillColor(HexColor(C_ORANGE))
        c.drawString(start_x + mi_w, block_y, prenom)
        c.circle(start_x + total_w + dot_gap, block_y + msize * 0.32, 1.4, fill=1, stroke=0)

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

    # Zones en points ReportLab
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

    # Scrim bas
    scrim_h = fh * 0.46
    bands = 60
    bh_band = scrim_h / bands
    for i in range(bands):
        frac = i / (bands - 1)
        alpha = 0.86 * (1 - frac) ** 1.6
        c.setFillColor(Color(28/255, 17/255, 9/255, alpha=alpha))
        c.rect(fx, fy + i * bh_band, fw, bh_band + 1.2, fill=1, stroke=0)

    # Scrim haut
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

    # Logo
    box_b = 13 * mm
    lsize_b = 27
    lw_b = c.stringWidth(BRAND_NAME, F_TITLE, lsize_b)
    gap_b = 4 * mm
    gw_b = box_b + gap_b + lw_b
    draw_piklo_mark(c, bcx - gw_b / 2, by + bh - 34*mm,
                    box=box_b, gap=gap_b, label_color=C_ORANGE, label_size=lsize_b)

    # Surtitre
    draw_tracked(c, "L'HISTOIRE", F_BODY_B, 10, C_SURTITRE, bcx, by + bh - 52*mm, 3.0)

    # Accroche univers
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

    # Paragraphe univers
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

    # Mention personnalisee
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

    # Footer
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

    # ── Tranche ───────────────────────────────────────────────────────────────
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
    univers     = reassurance.get("univers") or {}

    with tempfile.TemporaryDirectory() as tmp:
        img_paths = {}
        for page in pages_ok:
            pid  = page["id"]
            path = os.path.join(tmp, f"{pid}.jpg")
            real_url = (
                f"{SUPABASE_URL}/storage/v1/object/public/"
                f"images/{histoire_id}/{pid}"
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
        draw_reassurance(c, reassurance, titre, prenom)          # p.28
        draw_marketing(c, tmp)                                   # p.29
        draw_pourquoi_cette_histoire(                            # p.30
            c,
            prenom=prenom,
            univers_titre=univers.get("titre"),
            univers_texte=univers.get("texte"),
        )
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
