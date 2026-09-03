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
EDGE_CUTS_LAYER = "Edge.Cuts"
EDGE_CUTS_DXF_LAYER = "Edge_Cuts"

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


def _parse_pcb_file(pcb_path):
    with open(pcb_path, encoding="utf-8") as f:
        return parse_sexpr(f.read())


def extract_silkscreen_texts(pcb_path):
    """Parses a .kicad_pcb file and returns the reference/value text items that
    live on the front or back silkscreen layer (F.SilkS/B.SilkS), skipping
    hidden text and unresolved library placeholders (e.g. "REF**")."""
    root = _parse_pcb_file(pcb_path)
    items = _walk(root)
    return [t for t in items if t.layer in SILK_LAYERS]


def _rotate_kicad(x, y, cx, cy, deg):
    """Rotates (x,y) around (cx,cy) by deg degrees, standard CCW-positive
    convention applied directly to the raw stored KiCad coordinate values."""
    dx, dy = x - cx, y - cy
    rx, ry = _rotate(dx, dy, deg)
    return cx + rx, cy + ry


def _to_dxf_point(x_mm, y_mm):
    # KiCad's Y axis points down; DXF/math convention points up.
    return (x_mm, -y_mm)


def _circle_from_3p(p1, p2, p3):
    ax, ay = p1
    bx, by = p2
    cx, cy = p3
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return None
    ux = ((ax**2 + ay**2) * (by - cy) + (bx**2 + by**2) * (cy - ay) + (cx**2 + cy**2) * (ay - by)) / d
    uy = ((ax**2 + ay**2) * (cx - bx) + (bx**2 + by**2) * (ax - cx) + (cx**2 + cy**2) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    return (ux, uy), r


def _arc_from_3_dxf_points(start, mid, end):
    """Given 3 points already in DXF space (start/mid/end along the desired arc),
    returns (center, radius, start_angle_deg, end_angle_deg) such that drawing a
    counter-clockwise arc from start_angle to end_angle passes through mid - this
    correctly picks the major/minor arc and direction regardless of how the
    source data was originally encoded."""
    circle = _circle_from_3p(start, mid, end)
    if circle is None:
        return None
    (cx, cy), r = circle

    def _angle_of(p):
        return math.degrees(math.atan2(p[1] - cy, p[0] - cx)) % 360

    a_start, a_mid, a_end = _angle_of(start), _angle_of(mid), _angle_of(end)

    def _ccw_span_contains(a0, a1, am):
        span = (a1 - a0) % 360
        rel = (am - a0) % 360
        return rel <= span

    if _ccw_span_contains(a_start, a_end, a_mid):
        return (cx, cy), r, a_start, a_end
    return (cx, cy), r, a_end, a_start


def _edge_arc_shape(node):
    start = _find(node, "start")
    end = _find(node, "end")
    mid = _find(node, "mid")
    angle = _find(node, "angle")

    if mid is not None:
        # KiCad 6+ three-point arc: start/mid/end are all points on the arc.
        sx, sy = float(start[1]), float(start[2])
        mx, my = float(mid[1]), float(mid[2])
        ex, ey = float(end[1]), float(end[2])
    elif angle is not None:
        # Legacy arc: "start" is actually the CENTER, "end" is the arc's start
        # point on the circumference, "angle" is the CCW sweep in degrees.
        cx, cy = float(start[1]), float(start[2])
        sx0, sy0 = float(end[1]), float(end[2])
        sweep = float(angle[1])
        sx, sy = sx0, sy0
        mx, my = _rotate_kicad(sx0, sy0, cx, cy, sweep / 2)
        ex, ey = _rotate_kicad(sx0, sy0, cx, cy, sweep)
    else:
        return None

    return _arc_from_3_dxf_points(
        _to_dxf_point(sx, sy), _to_dxf_point(mx, my), _to_dxf_point(ex, ey)
    )


def extract_edge_cuts(pcb_path):
    """Parses a .kicad_pcb file and returns the board-outline (Edge.Cuts) graphic
    primitives as a list of normalized DXF-space shapes:
    ("line", (x1,y1), (x2,y2))
    ("arc", (cx,cy), radius, start_angle_deg, end_angle_deg)
    ("circle", (cx,cy), radius)
    ("polyline", [(x,y), ...])
    Only considers top-level board graphics (gr_line/gr_arc/gr_circle/gr_rect/
    gr_poly), which is how KiCad represents the board edge in every real file
    seen so far - not footprint-relative graphics."""
    root = _parse_pcb_file(pcb_path)
    shapes = []
    for node in root:
        if not (isinstance(node, list) and node):
            continue
        head = node[0]
        if head not in ("gr_line", "gr_arc", "gr_circle", "gr_rect", "gr_poly"):
            continue
        layer_node = _find(node, "layer")
        if not layer_node or layer_node[1] != EDGE_CUTS_LAYER:
            continue

        if head == "gr_line":
            start = _find(node, "start")
            end = _find(node, "end")
            if start and end:
                p1 = _to_dxf_point(float(start[1]), float(start[2]))
                p2 = _to_dxf_point(float(end[1]), float(end[2]))
                shapes.append(("line", p1, p2))

        elif head == "gr_arc":
            arc = _edge_arc_shape(node)
            if arc:
                center, r, a0, a1 = arc
                shapes.append(("arc", center, r, a0, a1))

        elif head == "gr_circle":
            center_node = _find(node, "center")
            end_node = _find(node, "end")
            if center_node and end_node:
                cx, cy = float(center_node[1]), float(center_node[2])
                ex, ey = float(end_node[1]), float(end_node[2])
                r = math.hypot(ex - cx, ey - cy)
                shapes.append(("circle", _to_dxf_point(cx, cy), r))

        elif head == "gr_rect":
            start = _find(node, "start")
            end = _find(node, "end")
            if start and end:
                x1, y1 = float(start[1]), float(start[2])
                x2, y2 = float(end[1]), float(end[2])
                pts = [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]
                shapes.append(("polyline", [_to_dxf_point(x, y) for x, y in pts]))

        elif head == "gr_poly":
            pts_node = _find(node, "pts")
            if pts_node:
                pts = [(float(p[1]), float(p[2])) for p in pts_node if isinstance(p, list) and p and p[0] == "xy"]
                if pts:
                    pts.append(pts[0])
                    shapes.append(("polyline", [_to_dxf_point(x, y) for x, y in pts]))

    return shapes


def _draw_edge_shape(msp, shape):
    kind = shape[0]
    attribs = {"layer": EDGE_CUTS_DXF_LAYER}
    if kind == "line":
        _, p1, p2 = shape
        msp.add_line(p1, p2, dxfattribs=attribs)
    elif kind == "arc":
        _, center, r, a0, a1 = shape
        msp.add_arc(center=center, radius=r, start_angle=a0, end_angle=a1, dxfattribs=attribs)
    elif kind == "circle":
        _, center, r = shape
        msp.add_circle(center=center, radius=r, dxfattribs=attribs)
    elif kind == "polyline":
        _, pts = shape
        msp.add_lwpolyline(pts, dxfattribs=attribs)


def export_silkscreen_to_dxf(pcb_path, dxf_path):
    """Renders the extracted silkscreen text as real vector glyph outlines
    (not a font-name TEXT reference) plus the board outline (Edge.Cuts) into a
    DXF file, front/back silkscreen and the outline each on their own DXF
    layer (F_SilkS, B_SilkS, Edge_Cuts). Returns (n_front, n_back, n_edges)."""
    root = _parse_pcb_file(pcb_path)
    all_texts = _walk(root)
    items = [t for t in all_texts if t.layer in SILK_LAYERS]
    edge_shapes = extract_edge_cuts(pcb_path)

    doc = ezdxf.new(setup=True)
    doc.units = ezdxf.units.MM
    msp = doc.modelspace()
    doc.layers.add("F_SilkS", color=1)
    doc.layers.add("B_SilkS", color=5)
    doc.layers.add(EDGE_CUTS_DXF_LAYER, color=3)

    font = fonts.FontFace()  # default sans-serif; ezdxf resolves an installed font per-OS

    n_front = n_back = 0
    for item in items:
        dxf_x, dxf_y = _to_dxf_point(item.x_mm, item.y_mm)
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

    for shape in edge_shapes:
        _draw_edge_shape(msp, shape)

    doc.saveas(dxf_path)
    return n_front, n_back, len(edge_shapes)
