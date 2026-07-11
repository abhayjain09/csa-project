#!/usr/bin/env python3
"""Revert P2 (language-nav) — it pulls deep-nav into /privacy-center/en-us
locale pages. The normal topic-term match already follows the real doc page,
so this restores _nav_relevant to term-only. Run: python3 revert_p2.py agent.py
"""
import sys, subprocess
path = sys.argv[1] if len(sys.argv) > 1 else "agent.py"
src = open(path, encoding="utf-8").read()

patched = '''                        def _nav_relevant(u: str) -> bool:
                            path = unquote(urlparse(u).path).lower()
                            # Always follow language-chooser / locale links: a
                            # JS site routes to the file through these and their
                            # URL never mentions the query topic. Additive; still
                            # bounded by BROWSER_NAV_MAX_PAGES.
                            if _NAV_LANGUAGE_PATH_RE.search(path):
                                return True
                            if not _nav_terms:
                                return True
                            return any(t in path for t in _nav_terms)'''
original = '''                        def _nav_relevant(u: str) -> bool:
                            if not _nav_terms:
                                return True  # no terms -> don't over-filter
                            path = unquote(urlparse(u).path).lower()
                            return any(t in path for t in _nav_terms)'''

if patched not in src:
    if original in src:
        sys.exit("P2 already reverted — nothing to do.")
    sys.exit("ABORT: patched _nav_relevant not found; send me current agent.py.")
src = src.replace(patched, original)
open(path, "w", encoding="utf-8").write(src)
subprocess.run([sys.executable, "-m", "py_compile", path], check=True)
print("P2 reverted; COMPILES CLEAN")