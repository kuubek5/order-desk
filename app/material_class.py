"""Visual classification of the Матеріал/Колір chip in the queue (screen 1).

Цирконій (моно/емо/тисячний/числові коди типу "800") stays the default chip
style — user explicitly asked not to touch it. ПММА (multi-color plastic)
and титан get a distinct highlight so an operator scanning the queue can
tell the material family at a glance without reading the text.
"""


def material_color_css_class(material_color: str | None) -> str:
    if not material_color:
        return ""
    text = material_color.strip().lower()
    if "титан" in text:
        return "chip-titan"
    if "пмма" in text:
        return "chip-pmma"
    return ""


# Category → (badge symbol, css class). Symbols are the element/polymer marks
# operators recognise; the css class carries the material's signature colour
# (see the .matbadge palette in base.css). Colours agreed with Roman:
# Zr ice-blue, PMMA amber, Ti emerald, SLM steel, Wax rose.
_MATERIAL_BADGES = {
    "Цирконій": ("Zr", "mat-zr"),
    "ПММА": ("PMMA", "mat-pmma"),
    "Титан": ("Ti", "mat-ti"),
    "СЛМ": ("SLM", "mat-slm"),
    "Віск": ("Wax", "mat-wax"),
}


def material_badge(order) -> dict | None:
    """Compact material badge for an order, or None when no badge should show.

    - resolved production material → its symbol + signature colour;
    - unresolved (material_id NULL) → a muted "?" so it reads as "needs a rule";
    - the non-production "Не матеріал" bucket → None (stage/part rows carry no
      material badge).
    """
    material = getattr(order, "material", None)
    if material is None:
        # Only flag "?" when there IS colour text that stayed unresolved; an
        # order with no material text at all just shows no badge.
        if not (getattr(order, "material_color", None) or "").strip():
            return None
        return {"symbol": "?", "cls": "mat-unknown", "title": "матеріал не визначено"}
    if material.name == "Не матеріал":
        return None
    symbol, cls = _MATERIAL_BADGES.get(material.name, (material.name[:4], "mat-other"))
    return {"symbol": symbol, "cls": cls, "title": material.name}
