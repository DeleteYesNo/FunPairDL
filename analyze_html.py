"""Analyze EroScripts HTML files to understand OP structure for parser design."""
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

class CookedExtractor(HTMLParser):
    """Extract the first .cooked div from the first .topic-post element."""

    def __init__(self):
        super().__init__()
        self.in_topic_post = False
        self.found_first_topic_post = False
        self.in_cooked = False
        self.found_first_cooked = False
        self.cooked_depth = 0
        self.cooked_html = []
        self.done = False
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        attrs_dict = dict(attrs)
        classes = attrs_dict.get('class', '')

        # Track entry into first topic-post
        if not self.found_first_topic_post and 'topic-post' in classes:
            self.in_topic_post = True
            self.found_first_topic_post = True

        # Track entry into first .cooked inside topic-post
        if self.in_topic_post and not self.found_first_cooked and 'cooked' in classes.split():
            self.in_cooked = True
            self.found_first_cooked = True
            self.cooked_depth = 0

        if self.in_cooked:
            self.cooked_depth += 1
            # Rebuild the tag
            attr_str = ''
            for k, v in attrs:
                if v is None:
                    attr_str += f' {k}'
                else:
                    attr_str += f' {k}="{v}"'
            self.cooked_html.append(f'<{tag}{attr_str}>')

    def handle_endtag(self, tag):
        if self.done:
            return
        if self.in_cooked:
            self.cooked_html.append(f'</{tag}>')
            self.cooked_depth -= 1
            if self.cooked_depth <= 0:
                self.in_cooked = False
                self.done = True

    def handle_data(self, data):
        if self.done:
            return
        if self.in_cooked:
            self.cooked_html.append(data)

    def handle_entityref(self, name):
        if self.in_cooked:
            self.cooked_html.append(f'&{name};')

    def handle_charref(self, name):
        if self.in_cooked:
            self.cooked_html.append(f'&#{name};')

    def get_cooked_html(self):
        return ''.join(self.cooked_html)


class CookedAnalyzer(HTMLParser):
    """Analyze the contents of a .cooked div."""

    def __init__(self):
        super().__init__()
        self.headings = []  # list of (tag, text)
        self.current_heading = None
        self.current_heading_text = []

        self.links = []  # list of (href, text, classes)
        self.current_link = None
        self.current_link_text = []
        self.current_link_classes = ''

        self.funscript_links = []
        self.video_host_links = []

        self.structure_elements = []  # track structural elements in order
        self.current_element_stack = []

        # Track details/summary
        self.in_details = False
        self.in_summary = False
        self.summary_text = []
        self.details_count = 0

        # Track tables
        self.in_table = False
        self.table_count = 0

        # Track all elements for structure analysis
        self.element_sequence = []

        # For large posts - track heading->content groupings
        self.sections = []  # list of {heading, links, funscripts, video_hosts}
        self.current_section = {'heading': None, 'heading_tag': None, 'links': [], 'funscripts': [], 'video_hosts': []}

    VIDEO_HOSTS = [
        'mega.nz', 'mega.co.nz',
        'pixeldrain.com',
        'gofile.io',
        'iwara.tv',
        'rule34video.com',
        'spankbang.com',
        'pornhub.com',
        'xhamster.com',
        'xvideos.com',
        'erome.com',
        'redgifs.com',
        'youtube.com', 'youtu.be',
        'vimeo.com',
        'dailymotion.com',
        'drive.google.com',
        'mediafire.com',
        'dropbox.com',
        'onedrive.live.com',
        '1drv.ms',
        'streamtape.com',
        'doodstream.com',
        'mixdrop.co',
    ]

    def _classify_link(self, href, text, classes):
        if not href:
            return

        href_lower = href.lower()
        text_lower = text.lower() if text else ''

        # Check funscript
        is_funscript = False
        if 'funscript-link-container' in classes:
            is_funscript = True
        elif href_lower.endswith('.funscript'):
            is_funscript = True
        elif '.funscript' in href_lower:
            is_funscript = True

        if is_funscript:
            self.funscript_links.append((href, text))
            self.current_section['funscripts'].append((href, text))
            return

        # Check video host
        for host in self.VIDEO_HOSTS:
            if host in href_lower:
                self.video_host_links.append((href, text, host))
                self.current_section['video_hosts'].append((href, text, host))
                return

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        classes = attrs_dict.get('class', '')

        if tag in ('h1', 'h2', 'h3', 'h4'):
            # Start new section
            if self.current_section['heading'] is not None or self.current_section['links'] or self.current_section['funscripts'] or self.current_section['video_hosts']:
                self.sections.append(self.current_section)
            self.current_heading = tag
            self.current_heading_text = []
            self.current_section = {'heading': None, 'heading_tag': tag, 'links': [], 'funscripts': [], 'video_hosts': []}
            self.element_sequence.append(('heading_start', tag))

        if tag == 'a':
            href = attrs_dict.get('href', '')
            self.current_link = href
            self.current_link_text = []
            self.current_link_classes = classes

        if tag == 'details':
            self.in_details = True
            self.details_count += 1
            self.element_sequence.append(('details_start', None))

        if tag == 'summary':
            self.in_summary = True
            self.summary_text = []

        if tag == 'table':
            self.in_table = True
            self.table_count += 1
            self.element_sequence.append(('table_start', None))

        if tag == 'hr':
            self.element_sequence.append(('hr', None))

        if tag == 'img':
            src = attrs_dict.get('src', '')
            alt = attrs_dict.get('alt', '')
            self.element_sequence.append(('img', alt or src[:60]))

    def handle_endtag(self, tag):
        if tag in ('h1', 'h2', 'h3', 'h4') and self.current_heading == tag:
            text = ''.join(self.current_heading_text).strip()
            self.headings.append((tag, text))
            self.current_section['heading'] = text
            self.current_heading = None
            self.element_sequence.append(('heading_end', f'{tag}: {text}'))

        if tag == 'a' and self.current_link is not None:
            text = ''.join(self.current_link_text).strip()
            self.links.append((self.current_link, text, self.current_link_classes))
            self.current_section['links'].append((self.current_link, text))
            self._classify_link(self.current_link, text, self.current_link_classes)
            self.current_link = None

        if tag == 'details':
            self.in_details = False
            self.element_sequence.append(('details_end', None))

        if tag == 'summary':
            self.in_summary = False
            text = ''.join(self.summary_text).strip()
            self.element_sequence.append(('summary', text))

        if tag == 'table':
            self.in_table = False

    def handle_data(self, data):
        if self.current_heading is not None:
            self.current_heading_text.append(data)
        if self.current_link is not None:
            self.current_link_text.append(data)
        if self.in_summary:
            self.summary_text.append(data)

    def finalize(self):
        # Save last section
        if self.current_section['heading'] is not None or self.current_section['links'] or self.current_section['funscripts'] or self.current_section['video_hosts']:
            self.sections.append(self.current_section)


def analyze_file(filepath):
    print(f"\n{'='*80}")
    print(f"FILE: {Path(filepath).name}")
    print(f"{'='*80}")

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            html = f.read()
    except UnicodeDecodeError:
        with open(filepath, 'r', encoding='latin-1') as f:
            html = f.read()

    print(f"  File size: {len(html):,} bytes")

    # Step 1: Extract first .cooked div
    extractor = CookedExtractor()
    try:
        extractor.feed(html)
    except Exception as e:
        print(f"  ERROR extracting cooked: {e}")
        return

    cooked_html = extractor.get_cooked_html()
    if not cooked_html:
        print("  WARNING: No .cooked div found!")
        return

    print(f"  Cooked div size: {len(cooked_html):,} chars")

    # Step 2: Analyze the cooked content
    analyzer = CookedAnalyzer()
    try:
        analyzer.feed(cooked_html)
    except Exception as e:
        print(f"  ERROR analyzing cooked: {e}")
        return
    analyzer.finalize()

    # Report headings
    print(f"\n  HEADINGS ({len(analyzer.headings)}):")
    if analyzer.headings:
        for tag, text in analyzer.headings:
            print(f"    <{tag}> {text}")
    else:
        print(f"    (none)")

    # Report video host links
    print(f"\n  VIDEO HOST LINKS ({len(analyzer.video_host_links)}):")
    # Group by host
    hosts = {}
    for href, text, host in analyzer.video_host_links:
        hosts.setdefault(host, []).append((href, text))
    for host, links in hosts.items():
        print(f"    {host}: {len(links)} links")
        for href, text in links[:5]:
            display_text = text[:60] if text else '(no text)'
            display_href = href[:80]
            print(f"      - [{display_text}] -> {display_href}")
        if len(links) > 5:
            print(f"      ... and {len(links)-5} more")

    # Report funscript links
    print(f"\n  FUNSCRIPT LINKS ({len(analyzer.funscript_links)}):")
    for href, text in analyzer.funscript_links[:10]:
        display_text = text[:60] if text else '(no text)'
        display_href = href[:80]
        print(f"    - [{display_text}] -> {display_href}")
    if len(analyzer.funscript_links) > 10:
        print(f"    ... and {len(analyzer.funscript_links)-10} more")

    # Report structural elements
    print(f"\n  STRUCTURAL ELEMENTS:")
    print(f"    <details>/<summary> blocks: {analyzer.details_count}")
    print(f"    <table> elements: {analyzer.table_count}")
    hr_count = sum(1 for e in analyzer.element_sequence if e[0] == 'hr')
    print(f"    <hr> separators: {hr_count}")
    img_count = sum(1 for e in analyzer.element_sequence if e[0] == 'img')
    print(f"    <img> elements: {img_count}")

    # Report layout description
    print(f"\n  LAYOUT DESCRIPTION:")
    if analyzer.details_count > 0:
        print(f"    Uses <details>/<summary> for collapsible sections")
        summaries = [e[1] for e in analyzer.element_sequence if e[0] == 'summary']
        for s in summaries[:10]:
            print(f"      Summary: \"{s}\"")
        if len(summaries) > 10:
            print(f"      ... and {len(summaries)-10} more summaries")
    if analyzer.table_count > 0:
        print(f"    Uses tables for organization")
    if hr_count > 0:
        print(f"    Uses <hr> to separate sections")
    if analyzer.headings and not analyzer.details_count and not analyzer.table_count:
        print(f"    Organized by headings (flat structure with heading dividers)")
    if not analyzer.headings and not analyzer.details_count and not analyzer.table_count and hr_count == 0:
        print(f"    Flat list layout (no structural dividers)")

    # Report sections (heading -> content groupings)
    print(f"\n  SECTIONS ANALYSIS ({len(analyzer.sections)} sections):")
    for i, sec in enumerate(analyzer.sections):
        heading = sec['heading'] or '(no heading / preamble)'
        htag = sec['heading_tag'] or ''
        n_links = len(sec['links'])
        n_fs = len(sec['funscripts'])
        n_vh = len(sec['video_hosts'])
        print(f"    Section {i+1}: <{htag}> \"{heading}\"")
        print(f"      Links: {n_links} total, {n_vh} video hosts, {n_fs} funscripts")
        # Show video hosts in this section
        if sec['video_hosts']:
            vh_hosts = set(h for _,_,h in sec['video_hosts'])
            print(f"      Video hosts: {', '.join(vh_hosts)}")
        if n_fs > 0:
            for href, text in sec['funscripts'][:3]:
                print(f"      Funscript: {text[:50]}")
            if n_fs > 3:
                print(f"      ... and {n_fs-3} more funscripts")

    # Total links
    print(f"\n  TOTAL LINKS: {len(analyzer.links)}")

    return {
        'name': Path(filepath).name,
        'headings': len(analyzer.headings),
        'video_hosts': len(analyzer.video_host_links),
        'funscripts': len(analyzer.funscript_links),
        'details': analyzer.details_count,
        'tables': analyzer.table_count,
        'hrs': hr_count,
        'sections': len(analyzer.sections),
        'layout': 'details' if analyzer.details_count > 0 else 'headings' if analyzer.headings else 'flat',
    }


FILES = [
    r"G:/Download/HTML_eroscript/BlobCG's Entire VR Portfolio Multi-Axis Scripted, 40 VR Videos [SR6 + Twist Axes] - Scripts _ Free Scripts - EroScripts.html",
    r"G:/Download/HTML_eroscript/NoodleDude Ultimate Collection - All Scripts & Upscaled Videos - Scripts _ Free Scripts - EroScripts.html",
    r"G:/Download/HTML_eroscript/（multi-axis）Iwara - 背面駅弁 - Scripts _ Free Scripts - EroScripts.html",
    r"G:/Download/HTML_eroscript/[Giddora] Fluorite – ass - Scripts _ Free Scripts - EroScripts.html",
    r"G:/Download/HTML_eroscript/Drop-it-hmv - Scripts _ Free Scripts - EroScripts.html",
    r"G:/Download/HTML_eroscript/Qingyi and Robot - Scripts _ Free Scripts - EroScripts.html",
    r"G:/Download/HTML_eroscript/[Nintendawg] The Overwatch HMV (Requested) - Scripts _ Free Scripts - EroScripts.html",
    r"G:/Download/HTML_eroscript/(CS-FREE-0118)(Crisisbeat)Wednesday turning very hot(Multi-Axis) - Scripts _ Free Scripts - EroScripts.html",
    r"G:/Download/HTML_eroscript/[cos][吸酱]推特福利姬cos申鹤口交 - Scripts _ Free Scripts - EroScripts.html",
    r"G:/Download/HTML_eroscript/[ふぇり] 発情しすぎてちんぐり生ハメ交尾しまくるホロメン - Scripts _ Free Scripts - EroScripts.html",
]

results = []
for f in FILES:
    p = Path(f)
    if not p.exists():
        print(f"\nFILE NOT FOUND: {f}")
        continue
    r = analyze_file(f)
    if r:
        results.append(r)

# Summary table
print(f"\n\n{'='*120}")
print(f"SUMMARY TABLE")
print(f"{'='*120}")
print(f"{'File':<65} {'Headings':>8} {'VidHosts':>8} {'FunScr':>6} {'Details':>7} {'Tables':>6} {'HRs':>4} {'Sections':>8} {'Layout':>10}")
print(f"{'-'*65} {'-'*8} {'-'*8} {'-'*6} {'-'*7} {'-'*6} {'-'*4} {'-'*8} {'-'*10}")
for r in results:
    name = r['name'][:63]
    print(f"{name:<65} {r['headings']:>8} {r['video_hosts']:>8} {r['funscripts']:>6} {r['details']:>7} {r['tables']:>6} {r['hrs']:>4} {r['sections']:>8} {r['layout']:>10}")
