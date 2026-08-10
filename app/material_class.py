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
