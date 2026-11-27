#!/usr/bin/env python3
"""Build frontend/js/i18n/<lang>.js from strings.json (English, as written in the JS source) and
<lang>.json (translations). Usage: python3 tools/i18n/build.py de fr es nl"""
import json, os, sys
S = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(S, "..", "..", "frontend", "js", "i18n")
import re
keys = json.load(open(f"{S}/strings.json"))
# keys were read from JavaScript source: turn escapes (\\n, \\\\, \\") into the characters the browser shows
unesc = lambda x: re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), x)
os.makedirs(OUT, exist_ok=True)
for lang in sys.argv[1:]:
    tr = json.load(open(f"{S}/{lang}.json"))
    missing = [k for k in keys if k not in tr or not str(tr[k]).strip()]
    ws = [k for k in keys if k in tr and (len(k) - len(k.lstrip()), len(k) - len(k.rstrip())) != (len(tr[k]) - len(tr[k].lstrip()), len(tr[k]) - len(tr[k].rstrip()))]
    table = {unesc(k): unesc(tr[k]) for k in keys if k in tr and tr[k] != k}
    with open(f"{OUT}/{lang}.js", "w") as f:
        f.write(f"// Generated from the English UI strings (see i18n.js). {len(table)} entries.\n")
        f.write(f"i18nAdd({json.dumps(lang)}, {json.dumps(table, ensure_ascii=False, indent=0)});\n")
    print(lang, "entries:", len(table), "missing:", len(missing), "whitespace mismatches:", len(ws), (ws[:3] if ws else ""))
