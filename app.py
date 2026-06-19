"""
Assemblage PDF livre illustré - Format double page
Page gauche : fond coloré + texte centré + étoiles décoratives
Page droite : illustration carrée

Entrée  : données histoire + pages depuis Supabase
Sortie  : PDF paysage, une page par double page + couverture
"""

import requests
import random
import math
import io
import os
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white, Color
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from PIL import Image as PILImage

# ─── Configuration Supabase ───────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://nrqvexaulphzjtjkczje.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "VOTRE_SERVICE_ROLE_KEY")

HEADERS = {
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "apikey": SUPABASE_KEY,
}

# ─── Dimensions ───────────────────────────────────────────────────────────────
# Double page paysage : 36 x 18 cm (deux pages carrées côte à côte)
PAGE_W = 360 * mm
PAGE_H = 180 * mm
HALF_W = PAGE_W / 2  # largeur d'une demi-page = 180 mm

# Marges texte sur la page gauche
TEXT_MARGIN_X = 30 * mm
TEXT_MARGIN_Y = 40 * mm

# ─── Palette de couleurs (une par page, cycle) ────────────────────────────────
PALETTE = [
    "#FFF3E0",  # pêche clair
    "#FFF8E1",  # jaune doux
    "#F1F8E9",  # vert menthe
    "#E8F5E9",  # vert clair
    "#FFF9C4",  # jaune pâle
    "#E3F2FD",  # bleu ciel
    "#F8F9FA",  # gris très clair
    "#FCE4EC",  # rose poudré
    "#EDE7F6",  # lavande
    "#E0F7FA",  # turquoise clair
    "#FBE9E7",  # saumon
    "#E8EAF6",  # indigo pâle
]

# ─── Fontes ───────────────────────────────────────────────────────────────────
# On utilise Helvetica-Bold (built-in, disponible partout)
FONT_NAME = "Helvetica-Bold"
FONT_SIZE = 22


# ─── Helpers ──────────────────────────────────────────────────────────────────

def fetch_histoire(histoire_id: str) -> dict:
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


def fetch_pages(histoire_id: str) -> list:
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


def download_image(url: str) -> PILImage.Image:
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return PILImage.open(io.BytesIO(r.content)).convert("RGB")


def pil_to_temp(img: PILImage.Image, path: str):
    img.save(path, format="JPEG", quality=95)


def draw_stars(c: canvas.Canvas, x_offset: float, width: float, height: float,
               color_hex: str, n: int = 8, seed: int = 0):
    """Dessine des étoiles décoratives sur la page gauche."""
    rng = random.Random(seed)
    base_color = HexColor(color_hex)
    # Étoiles légèrement plus sombres que le fond
    star_color = Color(
        max(0, base_color.red - 0.12),
        max(0, base_color.green - 0.12),
        max(0, base_color.blue - 0.12),
        1,
    )
    c.setFillColor(star_color)
    c.setStrokeColor(star_color)

    for _ in range(n):
        sx = x_offset + rng.uniform(15 * mm, width - 15 * mm)
        sy = rng.uniform(15 * mm, height - 15 * mm)
        size = rng.uniform(3 * mm, 6 * mm)
        draw_star(c, sx, sy, size)


def draw_star(c: canvas.Canvas, cx: float, cy: float, r: float):
    """Dessine une étoile à 5 branches."""
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


def draw_text_page(c: canvas.Canvas, legende: str, color_hex: str,
                   page_index: int, x_offset: float = 0):
    """Dessine la page gauche : fond coloré, étoiles, texte centré."""
    # Fond coloré
    c.setFillColor(HexColor(color_hex))
    c.rect(x_offset, 0, HALF_W, PAGE_H, fill=1, stroke=0)

    # Étoiles décoratives
    draw_stars(c, x_offset, HALF_W, PAGE_H, color_hex, n=10, seed=page_index)

    # Texte centré
    c.setFillColor(HexColor("#1A1A2E"))
    c.setFont(FONT_NAME, FONT_SIZE)

    # Découpe le texte en lignes selon la largeur disponible
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


def wrap_text(c: canvas.Canvas, text: str, font: str, size: float,
              max_width: float) -> list:
    """Coupe le texte en lignes qui tiennent dans max_width."""
    words = text.split()
    lines = []
    current = ""
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


def draw_image_page(c: canvas.Canvas, img_path: str, x_offset: float = 0):
    """Dessine la page droite : image carrée qui remplit la demi-page."""
    c.setFillColor(HexColor("#F5F5F5"))
    c.rect(x_offset, 0, HALF_W, PAGE_H, fill=1, stroke=0)
    c.drawImage(img_path, x_offset, 0, width=HALF_W, height=PAGE_H,
                preserveAspectRatio=False)


def draw_cover(c: canvas.Canvas, titre: str, img_path: str):
    """
    Couverture : image pleine page avec bandeau titre en haut.
    Format identique aux doubles pages (paysage 36x18) mais
    l'image occupe toute la largeur.
    """
    # Image pleine page
    c.drawImage(img_path, 0, 0, width=PAGE_W, height=PAGE_H,
                preserveAspectRatio=False)

    # Bandeau semi-transparent en haut
    c.setFillColor(Color(1, 1, 1, alpha=0.82))
    bandeau_h = 45 * mm
    c.rect(0, PAGE_H - bandeau_h, PAGE_W, bandeau_h, fill=1, stroke=0)

    # Titre
    c.setFillColor(HexColor("#E67E00"))
    font_size = 36
    c.setFont(FONT_NAME, font_size)
    text_w = c.stringWidth(titre, FONT_NAME, font_size)
    c.drawString((PAGE_W - text_w) / 2, PAGE_H - bandeau_h + 10 * mm, titre)

    c.showPage()


# ─── Assemblage principal ─────────────────────────────────────────────────────

def assembler_pdf(histoire_id: str, output_path: str):
    print(f"Récupération de l'histoire {histoire_id}...")
    histoire = fetch_histoire(histoire_id)
    pages = fetch_pages(histoire_id)

    titre = histoire.get("titre", "Mon livre")
    print(f"Titre : {titre} — {len(pages)} pages")

    # Filtrer les pages qui ont une image
    pages_ok = [p for p in pages if p.get("image_url")]
    if not pages_ok:
        raise ValueError("Aucune page avec image_url trouvée.")

    print(f"{len(pages_ok)} pages avec image disponibles.")

    # Télécharger toutes les images
    temp_dir = "/tmp/bd_images"
    os.makedirs(temp_dir, exist_ok=True)

    img_paths = {}
    for page in pages_ok:
        pid = page["id"]
        temp_path = f"{temp_dir}/{pid}.jpg"
        print(f"  Téléchargement image page {page['numero']}...")
        img = download_image(page["image_url"])
        pil_to_temp(img, temp_path)
        img_paths[pid] = temp_path

    # Génération PDF
    c = canvas.Canvas(output_path, pagesize=(PAGE_W, PAGE_H))

    # ── Couverture (page 1 = image de couverture séparée ou première page) ──
    cover_page = pages_ok[0]
    draw_cover(c, titre, img_paths[cover_page["id"]])

    # ── Double pages (une page PDF par double page) ──
    for i, page in enumerate(pages_ok):
        color = PALETTE[i % len(PALETTE)]
        legende = page.get("legende", "")
        img_path = img_paths[page["id"]]

        # Page gauche : texte
        draw_text_page(c, legende, color, page_index=i, x_offset=0)

        # Page droite : image
        draw_image_page(c, img_path, x_offset=HALF_W)

        c.showPage()
        print(f"  Page {page['numero']} assemblée.")

    c.save()
    print(f"\nPDF généré : {output_path}")


# ─── Point d'entrée ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage : python assembler_pdf.py <histoire_id> [output.pdf]")
        print("Variables d'environnement : SUPABASE_URL, SUPABASE_KEY")
        sys.exit(1)

    histoire_id = sys.argv[1]
    output = sys.argv[2] if len(sys.argv) > 2 else f"livre_{histoire_id[:8]}.pdf"

    assembler_pdf(histoire_id, output)
