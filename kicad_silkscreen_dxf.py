# Extracts component reference/value silkscreen text from a KiCad .kicad_pcb file
# and exports it as real vector glyph outlines in a DXF file - independent of any
# Gerber file/pipeline (Gerber has no text semantics to recover it from).

import re
import math
from dataclasses import dataclass

import ezdxf
from ezdxf.addons import text2path
from ezdxf.fonts import fonts
from ezdxf.math import Matrix44
from ezdxf import path as ezpath

SILK_LAYERS = {"F.SilkS", "B.SilkS"}
DXF_LAYER_MAP = {"F.SilkS": "F_SilkS", "B.SilkS": "B_SilkS"}

_TOKEN_RE = re.compile(r'"(?:\\.|[^"\\])*"|\(|\)|[^\s()]+')
_PLACEHOLDER_RE = re.compile(r'REF[\*\?]+')


def tokenize(text):
    return _TOKEN_RE.findall(text)


def parse_sexpr(text):
    """Parses a KiCad S-expression file into nested lists of string atoms
    (quotes stripped, escapes resolved). Returns the single root list."""
    tokens = tokenize(text)
    pos = 0

    def _parse():
        nonlocal pos
        assert tokens[pos] == '('
        pos += 1
        node = []
        while tokens[pos] != ')':
            if tokens[pos] == '(':
                node.append(_parse())
            else:
                tok = tokens[pos]
                if tok.startswith('"'):
                    tok = tok[1:-1].replace('\\"', '"').replace('\\\\', '\\')
                node.append(tok)
                pos += 1
        pos += 1  # consume ')'
        return node

    return _parse()


@dataclass
class SilkText:
    text: str
    x_mm: float
    y_mm: float
    rotation_deg: float
    layer: str
    height_mm: float
    mirror: bool


def _rotate(x, y, deg):
    t = math.radians(deg)
    c, s = math.cos(t), math.sin(t)
    return x * c - y * s, x * s + y * c


def _find(node, key):
    for child in node:
        if isinstance(child, list) and child and child[0] == key:
            return child
    return None


def _is_placeholder(s):
    return bool(_PLACEHOLDER_RE.fullmatch(s.strip()))


def _is_hidden(node, effects):
    if "hide" in node:
        return True
    if effects is not None:
        hide_node = _find(effects, "hide")
        if hide_node and len(hide_node) > 1 and hide_node[1] == "yes":
            return True
    return False


def _walk(node, fx=0.0, fy=0.0, frot=0.0, out=None):
    """Recursively walks the parsed S-expression tree, tracking the enclosing
    footprint's absolute position/rotation, and collects reference/value text
    items found on fp_text/property nodes."""
    if out is None:
        out = []
    if not (isinstance(node, list) and node):
        return out

    head = node[0]

    if head in ("footprint", "module") and len(node) > 1:
        nfx, nfy, nfrot = fx, fy, frot
        at = _find(node, "at")
        if at:
            nfx, nfy = float(at[1]), float(at[2])
            nfrot = float(at[3]) if len(at) > 3 else 0.0
        for child in node:
            _walk(child, nfx, nfy, nfrot, out)
        return out

    if head in ("fp_text", "property") and len(node) >= 3:
        kind = node[1]
        if kind in ("reference", "value", "Reference", "Value"):
            text = node[2]
            at = _find(node, "at")
            layer_node = _find(node, "layer")
            effects = _find(node, "effects")
            if at and layer_node and not _is_hidden(node, effects) and not _is_placeholder(text):
                lx, ly = float(at[1]), float(at[2])
                lrot = float(at[3]) if len(at) > 3 else 0.0
                rx, ry = _rotate(lx, ly, frot)
                abs_x, abs_y = fx + rx, fy + ry
                layer = layer_node[1]

                height_mm = 1.0
                if effects:
                    font_node = _find(effects, "font")
                    if font_node:
                        size_node = _find(font_node, "size")
                        if size_node:
                            height_mm = float(size_node[1])

                mirror = False
                if effects:
                    justify = _find(effects, "justify")
                    if justify and "mirror" in justify:
                        mirror = True

                out.append(SilkText(text, abs_x, abs_y, lrot, layer, height_mm, mirror))
        return out  # don't descend further into a text node

    for child in node:
        _walk(child, fx, fy, frot, out)
    return out


def extract_silkscreen_texts(pcb_path):
    """Parses a .kicad_pcb file and returns the reference/value text items that
    live on the front or back silkscreen layer (F.SilkS/B.SilkS), skipping
    hidden text and unresolved library placeholders (e.g. "REF**")."""
    with open(pcb_path, encoding="utf-8") as f:
        root = parse_sexpr(f.read())
    items = _walk(root)
    return [t for t in items if t.layer in SILK_LAYERS]


def export_silkscreen_to_dxf(pcb_path, dxf_path):
    """Renders the extracted silkscreen text as real vector glyph outlines
    (not a font-name TEXT reference) into a DXF file, front and back
    silkscreen each on their own DXF layer. Returns (n_front, n_back)."""
    items = extract_silkscreen_texts(pcb_path)

    doc = ezdxf.new(setup=True)
    doc.units = ezdxf.units.MM
    msp = doc.modelspace()
    doc.layers.add("F_SilkS", color=1)
    doc.layers.add("B_SilkS", color=5)

    font = fonts.FontFace()  # default sans-serif; ezdxf resolves an installed font per-OS

    n_front = n_back = 0
    for item in items:
        # KiCad's Y axis points down; DXF/math convention points up.
        dxf_x = item.x_mm
        dxf_y = -item.y_mm
        dxf_rot = (-item.rotation_deg) % 360

        m = Matrix44()
        if item.mirror:
            m = m @ Matrix44.scale(-1, 1, 1)
        m = m @ Matrix44.z_rotate(math.radians(dxf_rot))
        m = m @ Matrix44.translate(dxf_x, dxf_y, 0)

        paths = text2path.make_paths_from_str(item.text, font, size=item.height_mm, m=m)
        dxf_layer = DXF_LAYER_MAP[item.layer]
        ezpath.render_splines_and_polylines(msp, paths, dxfattribs={"layer": dxf_layer})

        if item.layer == "F.SilkS":
            n_front += 1
        else:
            n_back += 1

    doc.saveas(dxf_path)
    return n_front, n_back
