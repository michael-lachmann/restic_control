"""Generate the app icon: a folder with clock hands inside a counter-clockwise
"rewind" arrow, on a macOS-style rounded square.

    python resources/make_icon.py                     -> resources/icon.svg, icon.png, ResticControl.icns
    python resources/make_icon.py --center logo.png   -> same frame, your image in the middle
                                                         (instead of the folder)
Needs: cairosvg, Pillow.
"""
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
S, C = 1024, 512                      # canvas, centre


def pt(r, deg):
    a = math.radians(deg)             # math angle: counter-clockwise on screen
    return C + r * math.cos(a), C - r * math.sin(a)


def ring(r=318, start=-38, end=252, width=70, head=68):
    """Arc from *start* to *end* degrees going counter-clockwise, with an arrowhead."""
    sx, sy = pt(r, start)
    ex, ey = pt(r, end)
    large = 1 if (end - start) % 360 > 180 else 0
    arc = f"M {sx:.1f} {sy:.1f} A {r} {r} 0 {large} 0 {ex:.1f} {ey:.1f}"
    # arrowhead that follows the circle: base across the ring at *end*, tip further along it
    tip = pt(r, end + math.degrees(head * 1.25 / r))
    p1, p2 = pt(r + head, end), pt(r - head, end)
    arrow = f"M {tip[0]:.1f} {tip[1]:.1f} L {p1[0]:.1f} {p1[1]:.1f} L {p2[0]:.1f} {p2[1]:.1f} Z"
    return arc, arrow, width


def ticks(r_out=262, r_in=240, n=12):
    out = []
    for i in range(n):
        x1, y1 = pt(r_out, 90 - i * 360 / n)
        x2, y2 = pt(r_in if i % 3 else r_in - 14, 90 - i * 360 / n)
        out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"/>')
    return "\n      ".join(out)


SQUIRREL = """
  <!-- original squirrel motif: sitting, holding an acorn, a small stash at its feet -->
  <ellipse cx="535" cy="702" rx="150" ry="16" fill="#0B0D33" fill-opacity="0.30"/>
  <g filter="url(#soft)">
    <!-- tail: two stacked strokes for a fluffy two-tone curl -->
    <path d="M 478 650 C 360 640 330 490 372 404 C 404 338 492 318 522 372"
          fill="none" stroke="#8E3F1E" stroke-width="118" stroke-linecap="round"/>
    <path d="M 470 632 C 382 620 360 500 392 424 C 416 370 478 356 500 388"
          fill="none" stroke="#C4652F" stroke-width="58" stroke-linecap="round"/>
    <!-- body and belly -->
    <ellipse cx="545" cy="585" rx="90" ry="112" fill="#B4552A"/>
    <ellipse cx="570" cy="598" rx="54" ry="80" fill="#F4DFC2"/>
    <!-- feet -->
    <ellipse cx="518" cy="690" rx="40" ry="17" fill="#8E3F1E"/>
    <ellipse cx="590" cy="694" rx="40" ry="17" fill="#8E3F1E"/>
    <!-- ear (behind head) -->
    <path d="M 552 408 Q 546 352 584 334 Q 598 372 590 410 Z" fill="#8E3F1E"/>
    <path d="M 562 404 Q 560 368 580 356 Q 586 380 582 404 Z" fill="#E7A27A"/>
    <!-- head -->
    <circle cx="585" cy="455" r="72" fill="#B4552A"/>
    <ellipse cx="634" cy="474" rx="36" ry="29" fill="#F4DFC2"/>
    <circle cx="662" cy="464" r="10" fill="#3A2216"/>
    <circle cx="602" cy="438" r="12" fill="#23150E"/>
    <circle cx="606" cy="433" r="4.5" fill="#FFFFFF"/>
    <circle cx="612" cy="478" r="11" fill="#F08A7A" fill-opacity="0.45"/>
    <!-- acorn held against the chest -->
    <ellipse cx="615" cy="590" rx="27" ry="33" fill="#C98B4A"/>
    <path d="M 586 576 Q 588 546 615 544 Q 642 546 644 576 Z" fill="#6B4424"/>
    <path d="M 624 546 q 4 -7 12 -9" fill="none" stroke="#6B4424" stroke-width="7" stroke-linecap="round"/>
    <ellipse cx="590" cy="596" rx="17" ry="13" fill="#9E4722"/>
    <ellipse cx="640" cy="600" rx="17" ry="13" fill="#9E4722"/>
    <!-- the stash -->
    <g transform="translate(668 676) rotate(18)">
      <ellipse cx="0" cy="8" rx="17" ry="21" fill="#C98B4A"/>
      <path d="M -19 0 Q -18 -20 0 -21 Q 18 -20 19 0 Z" fill="#6B4424"/>
    </g>
    <g transform="translate(700 690) rotate(-14)">
      <ellipse cx="0" cy="7" rx="14" ry="17" fill="#B97B3E"/>
      <path d="M -15 0 Q -14 -16 0 -17 Q 14 -16 15 0 Z" fill="#5C391E"/>
    </g>
  </g>
"""


UMBRELLA = """
  <!-- umbrella motif: one umbrella sheltering a folder from the rain -->
  <defs>
    <linearGradient id="canopy" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#FF7A6B"/>
      <stop offset="1" stop-color="#D9443B"/>
    </linearGradient>
    <clipPath id="canopyClip"><path d="M 312 472 Q 312 292 512 282 Q 712 292 712 472
             A 50 34 0 0 0 612 472 A 50 34 0 0 0 512 472
             A 50 34 0 0 0 412 472 A 50 34 0 0 0 312 472 Z"/></clipPath>
  </defs>
  <!-- raindrops kept off by the umbrella -->
  <g fill="#A9CFFF" fill-opacity="0.9">
    <path d="M 352 330 q 12 20 0 30 q -12 -10 0 -30 Z"/>
    <path d="M 300 420 q 12 20 0 30 q -12 -10 0 -30 Z"/>
    <path d="M 672 322 q 12 20 0 30 q -12 -10 0 -30 Z"/>
    <path d="M 726 412 q 12 20 0 30 q -12 -10 0 -30 Z"/>
  </g>
  <g filter="url(#soft)">
    <!-- shaft + handle (behind the folder, hook visible below it) -->
    <path d="M 512 300 V 690 q 0 34 -30 34 q -26 0 -28 -26" fill="none" stroke="#3A2A5C"
          stroke-width="16" stroke-linecap="round"/>
    <!-- canopy with scalloped edge -->
    <path d="M 312 472 Q 312 292 512 282 Q 712 292 712 472
             A 50 34 0 0 0 612 472 A 50 34 0 0 0 512 472
             A 50 34 0 0 0 412 472 A 50 34 0 0 0 312 472 Z" fill="url(#canopy)"/>
    <path d="M 512 282 Q 456 330 412 472 L 512 472 Z" fill="#FFFFFF" fill-opacity="0.16"
          clip-path="url(#canopyClip)"/>
    <path d="M 512 282 Q 590 330 612 472" fill="none" stroke="#B8352E" stroke-width="6"/>
    <path d="M 512 282 V 472" fill="none" stroke="#B8352E" stroke-width="6"/>
    <path d="M 512 282 Q 434 330 412 472" fill="none" stroke="#B8352E" stroke-width="6"/>
    <path d="M 512 282 V 256" stroke="#3A2A5C" stroke-width="12" stroke-linecap="round"/>
    <!-- sheltered folder -->
    <path d="M 404 528 q 0 -10 10 -10 h 64 q 9 0 14 7 l 11 14 h 105 q 10 0 10 10 v 12 h -214 Z"
          fill="url(#folderBack)"/>
    <rect x="404" y="542" width="214" height="128" rx="16" fill="url(#folderBack)"/>
    <rect x="404" y="558" width="214" height="112" rx="16" fill="url(#folderFront)"/>
  </g>
"""


def svg(folder=True, motif="umbrella"):
    arc, arrow, width = ring()
    # folder geometry (centred slightly low, like a document sitting in the clock)
    fx, fy, fw, fh = 330, 395, 364, 262
    tab = f"M {fx} {fy + 14} q 0 -14 14 -14 h 96 q 12 0 20 10 l 16 20 h 204 q 14 0 14 14 v 20 h -364 Z"
    hx, hy = C, fy + 165                          # clock centre on the folder front
    hour_end = (hx + 62 * math.cos(math.radians(150)), hy - 62 * math.sin(math.radians(150)))
    min_end = (hx + 84 * math.cos(math.radians(90)), hy - 84 * math.sin(math.radians(90)))
    folder_svg = f"""  <!-- folder -->
  <g filter="url(#soft)">
    <path d="{tab}" fill="url(#folderBack)"/>
    <rect x="{fx}" y="{fy + 40}" width="{fw}" height="{fh - 40}" rx="22" fill="url(#folderBack)"/>
    <rect x="{fx}" y="{fy + 62}" width="{fw}" height="{fh - 62}" rx="22" fill="url(#folderFront)"/>
  </g>

  <!-- clock hands on the folder front -->
  <g stroke="#23286E" stroke-linecap="round">
    <line x1="{hx}" y1="{hy}" x2="{hour_end[0]:.1f}" y2="{hour_end[1]:.1f}" stroke-width="22"/>
    <line x1="{hx}" y1="{hy}" x2="{min_end[0]:.1f}" y2="{min_end[1]:.1f}" stroke-width="16"/>
  </g>
  <circle cx="{hx}" cy="{hy}" r="17" fill="#F2861E"/>"""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{S}" height="{S}" viewBox="0 0 {S} {S}">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#4A4FC4"/>
      <stop offset="1" stop-color="#1C1F5C"/>
    </linearGradient>
    <radialGradient id="glow" cx="0.5" cy="0.12" r="0.75">
      <stop offset="0" stop-color="#FFFFFF" stop-opacity="0.22"/>
      <stop offset="1" stop-color="#FFFFFF" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="amber" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#FFD66B"/>
      <stop offset="1" stop-color="#F2861E"/>
    </linearGradient>
    <linearGradient id="folderBack" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#9FC0F2"/>
      <stop offset="1" stop-color="#6F97DA"/>
    </linearGradient>
    <linearGradient id="folderFront" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#F4F8FF"/>
      <stop offset="1" stop-color="#C9DBF7"/>
    </linearGradient>
    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="10" stdDeviation="14" flood-color="#000" flood-opacity="0.35"/>
    </filter>
    <filter id="soft" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="6" stdDeviation="8" flood-color="#0B0D33" flood-opacity="0.45"/>
    </filter>
  </defs>

  <!-- rounded-square body (macOS icon grid: 824 px body, 100 px margin) -->
  <g filter="url(#shadow)">
    <rect x="100" y="100" width="824" height="824" rx="185" fill="url(#bg)"/>
  </g>
  <rect x="100" y="100" width="824" height="824" rx="185" fill="url(#glow)"/>

  <!-- faint clock ticks between arrow and folder -->
  <g stroke="#FFFFFF" stroke-opacity="0.28" stroke-width="12" stroke-linecap="round">
      {ticks()}
  </g>

  <!-- counter-clockwise rewind arrow -->
  <g filter="url(#soft)">
    <path d="{arc}" fill="none" stroke="url(#amber)" stroke-width="{width}" stroke-linecap="round"/>
    <path d="{arrow}" fill="url(#amber)" stroke="url(#amber)" stroke-width="14" stroke-linejoin="round"/>
  </g>

  {({"squirrel": SQUIRREL, "umbrella": UMBRELLA}.get(motif, folder_svg)) if folder else CENTER_DISC}
</svg>
"""


# light disc behind a user-supplied centre image, for contrast on the dark background
CENTER_R = 228
CENTER_DISC = (f'<circle cx="{C}" cy="{C}" r="{CENTER_R}" fill="#F4F8FF" fill-opacity="0.94" '
               f'filter="url(#soft)"/>')


def render_png(svg_path, png_path):
    """SVG -> 1024 px PNG with whatever renderer is available.

    cairosvg needs the cairo C library (brew install cairo, or conda install cairo);
    rsvg-convert comes with `brew install librsvg`.  Ready-made icons for every motif
    are in resources/icons/ if neither is installed.
    """
    import shutil
    import subprocess
    try:
        import cairosvg
        cairosvg.svg2png(url=svg_path, write_to=png_path, output_width=S, output_height=S)
        return
    except (ImportError, OSError) as e:          # OSError: cairo library not found
        reason = str(e).splitlines()[0]
    exe = shutil.which("rsvg-convert") or next(
        (p for p in ("/opt/homebrew/bin/rsvg-convert", "/usr/local/bin/rsvg-convert")
         if os.path.exists(p)), None)
    if exe:
        subprocess.run([exe, "-w", str(S), "-h", str(S), "-o", png_path, svg_path], check=True)
        return
    raise SystemExit(
        f"No SVG renderer available ({reason}).\n"
        "Either install one:  brew install librsvg   (or: brew install cairo)\n"
        "or use a ready-made icon, e.g.:\n"
        "  cp resources/icons/umbrella.icns resources/ResticControl.icns")


def main():
    import argparse
    from PIL import Image
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--motif", choices=("folder", "squirrel", "umbrella"), default="umbrella",
                    help="what to draw in the middle (default: umbrella)")
    ap.add_argument("--center", metavar="IMAGE",
                    help="image (PNG/JPEG, ideally transparent) to put in the middle instead of the folder")
    ap.add_argument("--no-disc", action="store_true", help="no light disc behind the centre image")
    args = ap.parse_args()

    svg_path = os.path.join(HERE, "icon.svg")
    png_path = os.path.join(HERE, "icon.png")
    icns_path = os.path.join(HERE, "ResticControl.icns")
    source = svg(folder=args.center is None, motif=args.motif)
    if args.center and args.no_disc:
        source = source.replace(CENTER_DISC, "")
    with open(svg_path, "w") as f:
        f.write(source)
    render_png(svg_path, png_path)
    img = Image.open(png_path).convert("RGBA")
    if args.center:
        pic = Image.open(args.center).convert("RGBA")
        box = int(CENTER_R * 2 * 0.80)                 # fit inside the disc with a margin
        pic.thumbnail((box, box), Image.LANCZOS)
        img.alpha_composite(pic, (C - pic.width // 2, C - pic.height // 2))
        img.save(png_path)
    img.save(icns_path, format="ICNS")          # Pillow writes all sizes 16…1024
    print("wrote", svg_path, png_path, icns_path)


if __name__ == "__main__":
    main()
