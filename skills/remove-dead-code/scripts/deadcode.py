#!/usr/bin/env python3
"""Find and remove commented-out code, debug statements and unused symbols, in any language.

Standard library only, Python 3.8+.

  deadcode.py scan   [PATH...] [--kind comments|debug|all] [--level high|medium] [--summary] [--json]
  deadcode.py show   SPEC...                      SPEC = file:START-END | file:LINE
  deadcode.py remove SPEC... | --from FILE | --level high [--kind K] [PATH...]  [--dry-run]
  deadcode.py unused [PATH...] [--root DIR] [--private-only]
  deadcode.py langs

Output is deliberately compact: one line per finding, so an agent can review a
whole repository without opening files.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from collections import Counter

# --------------------------------------------------------------------------
# Language table
# --------------------------------------------------------------------------

SH_HEREDOC = r"<<[-~]?(?P<hq>['\"]?)(?P<term>[A-Z_][A-Z0-9_]*)(?P=hq)"
PHP_HEREDOC = r"<<<[ \t]*(?P<hq>['\"]?)(?P<term>[A-Za-z_]\w*)(?P=hq)"

PY_DEFS = [r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)", r"^\s*class\s+([A-Za-z_]\w*)"]
JS_DEFS = [
    r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)",
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?"
    r"(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)",
    r"^\s*(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)",
]
PHP_DEFS = [
    r"^\s*(?:(?:public|private|protected|static|final|abstract)\s+)*function\s+&?\s*([A-Za-z_]\w*)",
    r"^\s*(?:(?:final|abstract|readonly)\s+)*(?:class|trait|interface|enum)\s+([A-Za-z_]\w*)",
]
GO_DEFS = [r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"]
RS_DEFS = [r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_]\w*)"]
RB_DEFS = [r"^\s*def\s+(?:self\.)?([A-Za-z_]\w*[?!]?)", r"^\s*class\s+([A-Z]\w*)"]
JAVA_DEFS = [
    r"^\s*(?:@\w+\s+)*(?:(?:public|private|protected|internal|static|final|abstract|virtual|override"
    r"|async|synchronized|sealed|partial)\s+)+[\w<>\[\],.?\s]*?\s([A-Za-z_]\w*)\s*\([^;]*$",
    r"^\s*(?:(?:public|private|protected|internal|static|final|abstract|sealed|partial|data|open)\s+)*"
    r"(?:class|interface|enum|record|struct|object)\s+([A-Za-z_]\w*)",
]
KT_DEFS = [r"^\s*(?:(?:private|internal|public|protected|override|suspend|inline|open)\s+)*fun\s+"
           r"(?:<[^>]*>\s*)?(?:[\w.]+\.)?([A-Za-z_]\w*)"] + JAVA_DEFS[1:]
SWIFT_DEFS = [r"^\s*(?:(?:private|fileprivate|internal|public|open|static|override|final|@objc)\s+)*"
              r"func\s+([A-Za-z_]\w*)"]
SCALA_DEFS = [r"^\s*(?:(?:private|protected|override|final)\s+)*def\s+([A-Za-z_]\w*)"]
LUA_DEFS = [r"^\s*(?:local\s+)?function\s+(?:[\w.]+[.:])?([A-Za-z_]\w*)"]
DART_DEFS = [r"^\s*(?:static\s+)?(?:[\w<>?,\s]+\s)?(_?[a-z]\w*)\s*\([^;]*\)\s*(?:async\s*)?\{"]


class Lang:
    def __init__(self, name, line=(), block=(), mls=(), quotes=('"', "'"), ml_quotes=False,
                 heredoc=None, attr=None, wordhash=False, defs=()):
        self.name = name
        self.line = tuple(line)
        self.openers = dict(block)
        self.mls = tuple(mls)
        self.quotes = tuple(quotes)
        self.ml_quotes = ml_quotes          # ordinary quotes may span lines
        self.attr = attr                    # e.g. PHP "#[" is an attribute, not a comment
        self.wordhash = wordhash            # "#" only starts a comment at a word boundary
        self.heredoc = heredoc
        self.defs = [re.compile(d) for d in defs]
        toks = sorted(set(self.line) | set(self.openers) | set(self.mls) | set(self.quotes),
                      key=len, reverse=True)
        alts = [re.escape(t) for t in toks]
        if heredoc:
            alts.insert(0, "(?P<hd>%s)" % heredoc)
        self.rx = re.compile("|".join(alts)) if alts else None


C_BLOCK = (("/*", "*/"),)
L = {
    "c": Lang("c", ("//",), C_BLOCK),
    "java": Lang("java", ("//",), C_BLOCK, mls=('"""',), defs=JAVA_DEFS),
    "groovy": Lang("groovy", ("//",), C_BLOCK, mls=('"""', "'''"), defs=JAVA_DEFS),
    "cs": Lang("cs", ("//",), C_BLOCK, mls=('"""',), defs=JAVA_DEFS),
    "js": Lang("js", ("//",), C_BLOCK, mls=("`",), defs=JS_DEFS),
    "go": Lang("go", ("//",), C_BLOCK, mls=("`",), defs=GO_DEFS),
    "rust": Lang("rust", ("//",), C_BLOCK, quotes=('"',), defs=RS_DEFS),
    "swift": Lang("swift", ("//",), C_BLOCK, mls=('"""',), quotes=('"',), defs=SWIFT_DEFS),
    "kotlin": Lang("kotlin", ("//",), C_BLOCK, mls=('"""',), defs=KT_DEFS),
    "scala": Lang("scala", ("//",), C_BLOCK, mls=('"""',), defs=SCALA_DEFS),
    "dart": Lang("dart", ("//",), C_BLOCK, mls=('"""', "'''"), defs=DART_DEFS),
    "zig": Lang("zig", ("//",), (), quotes=('"',)),
    "php": Lang("php", ("//", "#"), (("/*", "*/"), ("{{--", "--}}")), ml_quotes=True,
                heredoc=PHP_HEREDOC, attr="#[", defs=PHP_DEFS),
    "css": Lang("css", (), C_BLOCK),
    "scss": Lang("scss", ("//",), C_BLOCK),
    "python": Lang("python", ("#",), (), mls=('"""', "'''"), defs=PY_DEFS),
    "shell": Lang("shell", ("#",), (), ml_quotes=True, heredoc=SH_HEREDOC, wordhash=True),
    "ruby": Lang("ruby", ("#",), (("=begin", "=end"),), ml_quotes=True, heredoc=SH_HEREDOC, defs=RB_DEFS),
    "perl": Lang("perl", ("#",), (), ml_quotes=True, heredoc=SH_HEREDOC),
    "hash": Lang("hash", ("#",), ()),
    "julia": Lang("julia", ("#",), (("#=", "=#"),), mls=('"""',)),
    "elixir": Lang("elixir", ("#",), (), mls=('"""',)),
    "powershell": Lang("powershell", ("#",), (("<#", "#>"),), ml_quotes=True),
    "hcl": Lang("hcl", ("#", "//"), C_BLOCK),
    "coffee": Lang("coffee", ("#",), (("###", "###"),)),
    "sql": Lang("sql", ("--",), C_BLOCK, quotes=("'", '"'), ml_quotes=True),
    "lua": Lang("lua", ("--",), (("--[[", "]]"),), defs=LUA_DEFS),
    "haskell": Lang("haskell", ("--",), (("{-", "-}"),), quotes=('"',)),
    "ada": Lang("ada", ("--",), (), quotes=('"',)),
    "lisp": Lang("lisp", (";",), (("#|", "|#"),), quotes=('"',)),
    "erlang": Lang("erlang", ("%",), (), quotes=('"',)),
    "vb": Lang("vb", ("'",), (), quotes=('"',)),
    "asm": Lang("asm", (";",), (), quotes=('"',)),
    "html": Lang("html", (), (("<!--", "-->"),), quotes=()),
    "sfc": Lang("sfc", ("//",), (("/*", "*/"), ("<!--", "-->")), mls=("`",), defs=JS_DEFS),
    "twig": Lang("twig", (), (("{#", "#}"), ("<!--", "-->")), quotes=()),
    "hbs": Lang("hbs", (), (("{{!--", "--}}"), ("{{!", "}}"), ("<!--", "-->")), quotes=()),
    "erb": Lang("erb", (), (("<%#", "%>"), ("<!--", "-->")), quotes=()),
}

EXT = {}
for lang, exts in {
    "c": "c h cc cpp cxx c++ hpp hh hxx ino m mm cu cuh sol proto v",
    "java": "java", "groovy": "groovy gradle jenkinsfile", "cs": "cs",
    "js": "js jsx mjs cjs ts tsx mts cts", "go": "go", "rust": "rs", "swift": "swift",
    "kotlin": "kt kts", "scala": "scala sc", "dart": "dart", "zig": "zig",
    "php": "php phtml php3 php4 php5 php7 php8 inc", "css": "css", "scss": "scss less sass styl",
    "python": "py pyw pyi", "shell": "sh bash zsh ksh fish", "ruby": "rb rake gemspec",
    "perl": "pl pm", "hash": "r yaml yml toml nim cr mk cmake tf dockerfile conf",
    "julia": "jl", "elixir": "ex exs", "powershell": "ps1 psm1", "hcl": "hcl tfvars",
    "coffee": "coffee", "sql": "sql", "lua": "lua", "haskell": "hs elm purs",
    "ada": "adb ads vhd vhdl", "lisp": "lisp el clj cljs cljc edn scm rkt",
    "erlang": "erl hrl", "vb": "vb vbs bas", "asm": "asm s",
    "html": "html htm xhtml xml", "sfc": "vue svelte astro",
    "twig": "twig jinja jinja2 j2 njk", "hbs": "hbs handlebars mustache", "erb": "erb",
}.items():
    for e in exts.split():
        EXT[e] = L[lang]
NAMES = {"dockerfile": L["hash"], "makefile": L["hash"], "gnumakefile": L["hash"],
         "cmakelists.txt": L["hash"], "rakefile": L["ruby"], "gemfile": L["ruby"],
         "jenkinsfile": L["groovy"], "vagrantfile": L["ruby"]}


def lang_for(path):
    base = os.path.basename(path).lower()
    if base in NAMES:
        return NAMES[base]
    if base.endswith(".blade.php"):
        return L["php"]
    return EXT.get(base.rsplit(".", 1)[-1]) if "." in base else None


# --------------------------------------------------------------------------
# File discovery
# --------------------------------------------------------------------------

DEFAULT_EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "vendor", "bower_components", "jspm_packages",
    "dist", "build", "target", "coverage", ".next", ".nuxt", ".svelte-kit", ".output",
    "__pycache__", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache", ".gradle",
    "Pods", "Carthage", "DerivedData", "third_party", ".terraform", ".dart_tool",
}
DEFAULT_EXCLUDE_FILES = ["*.min.*", "*.bundle.js", "*.map", "*.pb.go", "*_pb2.py",
                         "*.generated.*", "*.g.dart", "*.freezed.dart", "*.lock"]
GENERATED_RE = re.compile(r"@generated|DO NOT EDIT|auto-?generated", re.I)


def git_files(d):
    try:
        out = subprocess.run(["git", "-C", d, "ls-files", "-z", "-co", "--exclude-standard"],
                             capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return [os.path.join(d, f) for f in out.decode("utf-8", "surrogateescape").split("\0") if f]


def walk_files(d, excl_dirs):
    for root, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if x not in excl_dirs]
        for f in files:
            yield os.path.join(root, f)


def list_files(paths, args, code_only=True):
    excl_dirs = set() if args.no_default_excludes else DEFAULT_EXCLUDE_DIRS
    excl_files = ([] if args.no_default_excludes else DEFAULT_EXCLUDE_FILES) + (args.exclude or [])
    seen, out = set(), []
    for p in paths or ["."]:
        if os.path.isfile(p):
            cands = [p]
        else:
            cands = git_files(p)
            if cands is None:
                cands = walk_files(p, excl_dirs)
        for f in cands:
            f = os.path.normpath(f)
            if f in seen or not os.path.isfile(f):
                continue
            rel = os.path.relpath(f)
            parts = rel.replace("\\", "/").split("/")
            if any(x in excl_dirs for x in parts[:-1]):
                continue
            if any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(parts[-1], g) for g in excl_files):
                continue
            if code_only and lang_for(f) is None:
                continue
            seen.add(f)
            out.append(f)
    return sorted(out)


def read_text(path, limit=2_000_000):
    try:
        if os.path.getsize(path) > limit:
            return None
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", "surrogateescape")


def looks_generated(text):
    head = text[:2048]
    if GENERATED_RE.search(head):
        return True
    n = text.count("\n") + 1
    return len(text) / n > 300  # minified


# --------------------------------------------------------------------------
# Lexer: which lines are comment-only, and where block comments sit
# --------------------------------------------------------------------------

def find_close(t, start, delim):
    """Index just past the unescaped `delim` at or after `start`, or -1."""
    j = start
    while True:
        k = t.find(delim, j)
        if k < 0:
            return -1
        bs = 0
        while k - bs - 1 >= start and t[k - bs - 1] == "\\":
            bs += 1
        if bs % 2 == 0:
            return k + len(delim)
        j = k + 1


def analyze(text, lang):
    """Return (lines, line_comments, blocks) or None if the file cannot be parsed safely.

    line_comments: {lineno: (marker, content)} for lines whose only content is a line comment.
    blocks: dicts {start, end, open, close, col, solo}; solo = nothing but the comment on its lines.
    normal: line numbers that begin outside any string, heredoc or comment.
    """
    lines = text.splitlines()
    linec, blocks = {}, []
    rx = lang.rx
    state = None  # ("b", closer, blk) | ("s", delim) | ("h", term)
    normal = set()  # lines that start outside any string/comment
    for i, t in enumerate(lines):
        pos, prior = 0, False
        if state is None:
            normal.add(i)
        if state:
            kind = state[0]
            if kind == "h":
                term = state[1]
                if t.strip() == term or (lang.name == "php" and re.match(r"\s*%s\b" % re.escape(term), t)):
                    state = None
                continue
            if kind == "s":
                k = find_close(t, 0, state[1])
                if k < 0:
                    continue
                pos, state, prior = k, None, True
            else:
                blk = state[2]
                k = t.find(state[1])
                if k < 0:
                    continue
                blk["end"] = i
                pos = k + len(state[1])
                if t[pos:].strip():
                    blk["solo"] = False
                state, prior = None, True
        if rx is None:
            continue
        pending = None
        while True:
            m = rx.search(t, pos)
            if not m:
                break
            tok, p = m.group(0), m.start()
            lead_blank = not prior and not t[:p].strip()
            if lang.heredoc and m.group("hd"):
                pending, pos, prior = m.group("term"), m.end(), True
                continue
            if tok in lang.line:
                if lang.attr and t.startswith(lang.attr, p):
                    pos, prior = p + len(lang.attr), True
                    continue
                if lang.wordhash and p > 0 and not t[p - 1].isspace():
                    pos, prior = p + 1, True
                    continue
                if lead_blank:
                    linec[i] = (tok, t[p + len(tok):])
                break
            if tok in lang.openers:
                closer = lang.openers[tok]
                blk = {"start": i, "end": None, "open": tok, "close": closer, "col": p, "solo": lead_blank}
                blocks.append(blk)
                k = t.find(closer, p + len(tok))
                if k < 0:
                    state = ("b", closer, blk)
                    break
                blk["end"] = i
                pos = k + len(closer)
                if t[pos:].strip():
                    blk["solo"] = False
                prior = True
                continue
            if tok in lang.mls:
                k = find_close(t, p + len(tok), tok)
                if k < 0:
                    state = ("s", tok)
                    break
                pos, prior = k, True
                continue
            k = find_close(t, p + 1, tok)  # ordinary quote
            if k < 0:
                if lang.ml_quotes:
                    state = ("s", tok)
                break
            pos, prior = k, True
        if pending and state is None:
            state = ("h", pending)
    if state:
        return None
    return lines, linec, blocks, normal


# --------------------------------------------------------------------------
# Classifier: is a piece of comment text code or prose?
# --------------------------------------------------------------------------

KEEP_RE = re.compile(r"""(?ix)
    \b(?-i:TODO|FIXME|HACK|XXX|NOTE|BUG|NB|SAFETY|WARNING)\b
  | eslint | tslint | prettier-ignore | stylelint | jshint | istanbul\s | \bc8\s | biome-ignore | deno-
  | phpcs | phpstan | psalm- | @codeCoverageIgnore | noqa | type:\s*ignore | pyright: | mypy: | pylint:
  | fmt:\s*(?:off|on|skip) | isort: | pragma | nolint | clang-format | clang-tidy | rubocop:
  | frozen_string_literal | -\*- | \bvim?: | coding[:=] | @ts- | @flow | @jsx | @license | @preserve
  | spdx | copyright | \blicen[cs]e\b | \(c\)\s*\d | \bgo:(?:build|generate|embed|noinline|linkname) | \+build
  | swiftlint | ktlint | @suppress | \#?\b(?:end)?region\b | <reference | sourceMappingURL
  | webpackChunkName | @vite-ignore | dead-code:\s*keep | @keep\b | @lang\b | language=
  | ^\s*@(?:param|returns?|var|throws?|see|since|author|deprecated|example|link|inheritdoc
          |property|method|package|api|internal|todo|type|typedef|template|override)\b
""")
SEPARATOR_RE = re.compile(r"([=*#~_+<>/\\-])\1{2,}.*|.*([=*#~_+<>/\\-])\2{4,}")
WORD_RE = re.compile(r"[(\"'`]?[^\W\d_]+(?:['’-][^\W\d_]+)*[.,:;!?)\"'`]*")
KW = (r"if|else|elif|elsif|elseif|for|foreach|while|do|switch|case|return|break|continue|function"
      r"|func|fn|def|class|interface|trait|struct|enum|impl|var|let|const|val|export|require"
      r"|require_once|include|include_once|use|using|namespace|package|module|public|private"
      r"|protected|internal|static|final|abstract|override|async|await|yield|try|catch|except"
      r"|finally|throw|raise|rescue|begin|end|unless|new|echo|print|println|printf|puts|console"
      r"|self|this|super|local|defer|lambda|assert|pass|del|global|typedef|void|int|char|bool"
      r"|float|double|long|string|auto|mut|pub|match|fi|done|esac|dd|dump|var_dump|print_r|die|exit")
RX = {
    "kw": re.compile(r"(?:%s)\b" % KW),
    "construct": re.compile(r"(?:if|elif|else|for|while|foreach|switch|catch|except|with|def|class|try"
                            r"|finally|function|func|fn)\b.*[:{]$|(?:if|for|while|foreach|switch|catch"
                            r"|elseif|elsif)\s*\("),
    "import": re.compile(r"import\s+[\w.]+(?:\s+as\s+\w+)?(?:\s*,\s*[\w.]+(?:\s+as\s+\w+)?)*;?$"
                         r"|from\s+[\w.]+\s+import\s+[\w., ()*]+$|import\s.*\bfrom\s+['\"]"
                         r"|import\s+['\"]|#\s*(?:include|define|ifn?def|endif|undef)\b"
                         r"|(?:use|using)\s+[\w\\.:]+(?:\s+as\s+\w+)?;$"),
    "assign": re.compile(r"(?:(?:var|let|const|val|my|our|local|auto|final|static|[A-Za-z_][\w<>\[\]?]*)\s+)?"
                         r"[$@]?[A-Za-z_][\w.$\[\]'\"\->:]*\s*(?:[-+*/%.|&^:]|\?\?|<<|>>)?=(?![=>])\s*\S"),
    "call": re.compile(r"[A-Za-z_$][\w$]*(?:(?:\.|->|::)[\w$]+)*\s*\([^()]*(?:\([^()]*\)[^()]*)*\)"),
    "ops": re.compile(r"->|::|\$[A-Za-z_{]|===|!==|==|!=|&&|\|\||\+\+|\+=|-=|\?\?|\w\.\w+\(|\w\[[^\]]*\]"),
    "tag": re.compile(r"</?[A-Za-z][\w:.-]*(?:\s[^<>]*)?/?>"),
    "deco": re.compile(r"@[A-Za-z_][\w.]*(?:\(.*\))?$"),
    "sql": re.compile(r"(?:SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|WHERE|JOIN|ORDER BY|GROUP BY)\b"),
    "close_only": re.compile(r"[)\]}]+[;,)]*$"),
}
BLANK, KEEP, CODE, WEAK, PROSE = "B", "K", "C", "W", "P"


def score_text(s):
    s = s.strip()
    if not s:
        return BLANK, 0
    if KEEP_RE.search(s) or (len(s) >= 3 and SEPARATOR_RE.fullmatch(s)):
        return KEEP, 0
    sc, exempt = 0, False
    if s[-1] in ";{":
        sc += 2
    elif RX["close_only"].fullmatch(s):
        sc += 3
        exempt = True
    elif s[-1] in ")]}" or s.endswith(("),", "],", "},", "(", "[", "=>", "->", "&&", "||")):
        sc += 1
    if RX["kw"].match(s):
        sc += 1
    words = s.split()
    if RX["construct"].match(s):
        sc += 2
        exempt = exempt or len(words) <= 4
    if RX["import"].match(s):
        sc += 4
        exempt = True
    if RX["assign"].match(s):
        sc += 3
        exempt = True
    if RX["call"].search(s):
        sc += 1
    sc += min(len(RX["ops"].findall(s)), 2)
    if RX["tag"].match(s):
        sc += 3
        exempt = True
    if RX["deco"].fullmatch(s):
        sc += 2
    if RX["sql"].match(s):
        sc += 2
        exempt = True
    if not exempt and len(words) >= 3 and sum(1 for w in words if WORD_RE.fullmatch(w)) / len(words) >= 0.75:
        sc -= 3
    if s.endswith(".") and not s.endswith("..") and " " in s:
        sc -= 2
    if sc >= 2:
        return CODE, sc
    if sc == 1:
        return WEAK, sc
    return PROSE, sc


def is_doc_line(marker, content, lineno):
    c = content[:1]
    if marker == "//":
        return c in ("/", "!")
    if marker == "#":
        return c == "!" or c == "#" or (lineno == 0 and c == "!")
    if marker == "--":
        return c in ("-", "|", "^")
    if marker == "'":
        return c == "'"
    return False


def level_of(scores):
    n = len(scores)
    if not n:
        return None
    if max(scores) >= 5 or (n >= 2 and sum(scores) / n >= 3):
        return "high"
    return "medium"


def line_findings(linec):
    out = []
    keys = sorted(linec)
    runs, run = [], []
    for i in keys:
        if run and (i != run[-1] + 1 or linec[run[-1]][0] != linec[i][0]):
            runs.append(run)
            run = []
        run.append(i)
    if run:
        runs.append(run)
    for run in runs:
        kinds, scores = [], []
        for i in run:
            mk, content = linec[i]
            if is_doc_line(mk, content, i):
                k, s = KEEP, 0
            else:
                k, s = score_text(content)
            kinds.append(k)
            scores.append(s)
        # weak lines touching code are code (e.g. "return x" inside a commented function)
        for rng in (range(1, len(kinds)), range(len(kinds) - 2, -1, -1)):
            for j in rng:
                nb = j - 1 if rng.step == 1 else j + 1
                if kinds[j] == WEAK and kinds[nb] == CODE:
                    kinds[j] = CODE
        j = 0
        while j < len(run):
            if kinds[j] != CODE:
                j += 1
                continue
            s = j
            while j < len(run) and kinds[j] in (CODE, BLANK):
                j += 1
            e = j - 1
            while kinds[e] == BLANK:
                e -= 1
            sc = [scores[x] for x in range(s, e + 1) if kinds[x] == CODE]
            out.append({
                "start": run[s] + 1, "end": run[e] + 1, "kind": "line", "level": level_of(sc),
                "ctx": s > 0 and kinds[s - 1] == PROSE,
                "preview": linec[run[s]][0] + linec[run[s]][1].rstrip(),
            })
    return out


def block_contents(lines, b):
    res = []
    for i in range(b["start"], b["end"] + 1):
        t = lines[i]
        if i == b["start"]:
            t = t[b["col"] + len(b["open"]):]
        if i == b["end"]:
            t = t[:t.find(b["close"])] if b["close"] in t else t
        if i != b["start"]:
            t = re.sub(r"^\s*\*(?!/)\s?", "", t)
        res.append(t)
    return res


def block_findings(lines, blocks):
    out = []
    for b in blocks:
        if not b["solo"] or b["end"] is None:
            continue
        cont = block_contents(lines, b)
        first = cont[0]
        if b["open"] == "/*" and first[:1] in ("*", "!") and not first.startswith("*/"):
            continue  # doc block / preserved comment
        if b["open"] == "<!--" and first.strip()[:1] in ("[", "#"):
            continue  # conditional comment / SSI
        kinds = [score_text(c) for c in cont]
        if any(k == KEEP for k, _ in kinds):
            continue
        code = [s for k, s in kinds if k == CODE]
        weak = sum(1 for k, _ in kinds if k == WEAK)
        nonblank = sum(1 for k, _ in kinds if k != BLANK)
        if not code or (len(code) + weak * 0.5) / nonblank < 0.7:
            continue
        lvl = level_of(code)
        if len(code) < nonblank:
            lvl = "medium"
        prev = next((c.strip() for c in cont if c.strip()), "")
        out.append({"start": b["start"] + 1, "end": b["end"] + 1, "kind": "block", "level": lvl,
                    "ctx": False, "preview": b["open"] + " " + prev})
    return out


# --------------------------------------------------------------------------
# Debug statements: live code such as console.log / var_dump / dbg!
# --------------------------------------------------------------------------

H, M = "high", "medium"
JS_CONSOLE = (r"console\.(?:log|debug|trace|dir|dirxml|table|count|countReset|time|timeEnd|timeLog"
              r"|group|groupCollapsed|groupEnd|assert|profile|profileEnd)")
# (form, pattern, level): form "call" = NAME(...) with balanced parens, "bare" = whole
# statement, "word" = NAME args (no parens, Ruby/Elixir style)
_JS = [("call", JS_CONSOLE, H), ("bare", r"debugger", H), ("call", r"console\.info", M)]
DEBUG_SPECS = {
    "js": _JS, "sfc": _JS,
    "php": [("call", r"(?:var_dump|dd|ddd|dump|debug_zval_dump|debug_print_backtrace|xdebug_break"
                     r"|xdebug_var_dump|ray|\\?Kint::dump)", H),
            # print_r/var_export double as lazy response output in some apps
            ("call", r"(?:echo\s+)?(?:print_r|var_export)|\$this->output->enable_profiler", M)] + _JS,
    "html": _JS,
    "python": [("call", r"breakpoint|(?:i?pdb|pudb)\.set_trace|pdb\.pm|__import__\((?:''|\"\")\)\.set_trace", H),
               ("call", r"print|pprint(?:\.pprint)?|ic", M)],
    "ruby": [("bare", r"binding\.(?:pry|irb|break)|byebug|debugger", H),
             ("call", r"(?:puts|pp?|print|ap)", M),
             ("word", r"(?:puts|pp?|print|ap)\s+(?![-+*/%|&^]?=)\S.*", M)],
    "go": [("call", r"spew\.(?:Dump|Printf|Fdump)|litter\.Dump|pretty\.Println", H),
           ("call", r"fmt\.Print(?:ln|f)?|println|print", M)],
    "rust": [("call", r"dbg!", H), ("call", r"(?:e?println|e?print)!", M)],
    "java": [("call", r"System\.(?:out|err)\.print(?:ln|f)?|\w+\.printStackTrace", M)],
    "groovy": [("call", r"System\.(?:out|err)\.print(?:ln|f)?|\w+\.printStackTrace|println", M)],
    "kotlin": [("call", r"println|print|System\.(?:out|err)\.println|\w+\.printStackTrace", M)],
    "scala": [("call", r"println|print|\w+\.printStackTrace", M)],
    "cs": [("call", r"(?:System\.Diagnostics\.)?Debugger\.Break", H),
           ("call", r"Console\.Write(?:Line)?|Debug\.(?:WriteLine|Write|Print|Log)", M)],
    "swift": [("call", r"print|debugPrint|dump|NSLog", M)],
    "dart": [("call", r"print|debugPrint", M)],
    "lua": [("call", r"print", M)],
    "elixir": [("call", r"(?:\|>\s*)?(?:IO\.inspect|dbg)|IEx\.pry", H),
               ("word", r"IO\.inspect\s+\S.*", H), ("bare", r"require\s+IEx", H)],
    "perl": [("call", r"(?:print\s+(?:STDERR\s+)?|warn\s+)?Dumper", H)],
    "shell": [("bare", r"set\s+[-+]x", M)],
}
# Statements allowed on the same line as a debug call: (pattern, forces_medium)
COMPANIONS = {
    "php": [(r"(?:die|exit)(?:\s*\(.*\))?", True), (r"echo\s*(?:\"\"|'')(?:\s*\.\s*(?:\"\"|''|PHP_EOL))*", False)],
    "python": [(r"import\s+(?:pdb|ipdb|pudb|pprint)|from\s+(?:pprint|icecream)\s+import\s+[\w, ]+", False)],
}
BRACE_LANGS = {"js", "sfc", "php", "html", "go", "rust", "java", "groovy", "kotlin", "scala", "cs", "swift", "dart", "c"}
CTRL_RE = re.compile(r"(?:\}\s*)?(?:if|else\s+if|elseif|for|foreach|while|with)\b.*\)$|(?:\}\s*)?else$")
HELPER_BODY_RE = re.compile(r"(?:echo\s*(?:\"\"|'')|die|exit)(?:\s*\(.*\))?\s*;?|return\s*;?|[{}]")
FUNC_RE = re.compile(  # a *named* function/method header; anonymous callbacks do not count
    r"\bfunction\s+&?\w+\s*\(|\bdef\s+\w+|\bfunc\s+(?:\([^)]*\)\s*)?\w+\s*\(|\bfn\s+\w+"
    r"|^(?:(?:public|private|protected|static|async|final|override|internal)\s+)*"
    r"(?!(?:if|for|while|switch|catch|foreach|elseif|return|function)\b)[A-Za-z_$][\w$]*\s*\([^)]*\)"
    r"\s*(?::\s*[\w?\\<>]+\s*)?\{?$")
DANGLING = ("&&", "||", "?", "=", "+", "-", "*", "/", ".", "(", "[", ",", "=>", "->", "??", "and", "or")
_debug_cache = {}


def _debug_rx(lang):
    if lang.name not in _debug_cache:
        specs = [(form, re.compile(p), lvl) for form, p, lvl in DEBUG_SPECS.get(lang.name, [])]
        comps = [(re.compile(p), f) for p, f in COMPANIONS.get(lang.name, [])]
        starts = [p for _, p, _ in DEBUG_SPECS.get(lang.name, [])] + [p for p, _ in COMPANIONS.get(lang.name, [])]
        pre = re.compile(r"\s*(?:%s)" % "|".join(starts)) if starts else None
        q = list(lang.quotes) + [d for d in lang.mls if len(d) == 1]
        strs = re.compile("|".join(r"%s(?:\\.|[^%s\\])*%s" % (re.escape(c), re.escape(c), re.escape(c)) for c in q)) if q else None
        _debug_cache[lang.name] = (specs, comps, pre, strs)
    return _debug_cache[lang.name]


def _code_part(t, lang, strs):
    """Line with string contents blanked and any trailing line comment cut."""
    if strs:
        t = strs.sub(lambda m: m.group(0)[0] * 2, t)
    cut = len(t)
    for mk in lang.line:
        j = t.find(mk)
        while j >= 0:
            if (lang.attr and t.startswith(lang.attr, j)) or (lang.wordhash and j > 0 and not t[j - 1].isspace()):
                j = t.find(mk, j + 1)
                continue
            cut = min(cut, j)
            break
    return t[:cut].rstrip()


def _depth(s):
    return sum(s.count(c) for c in "([{") - sum(s.count(c) for c in ")]}")


def _split_top(s):
    parts, d, cur = [], 0, []
    for ch in s:
        d += ch in "([{"
        d -= ch in ")]}"
        if ch == ";" and d == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _is_call(piece, rx):
    m = rx.match(piece)
    if not m:
        return False
    k = m.end()
    while k < len(piece) and piece[k].isspace():
        k += 1
    if k >= len(piece) or piece[k] != "(":
        return False
    d = 0
    for j in range(k, len(piece)):
        d += piece[j] == "("
        d -= piece[j] == ")"
        if d == 0:
            return j == len(piece) - 1
    return False


def _classify_stmt(stmt, specs, comps):
    levels, halt = [], False
    for piece in _split_top(stmt):
        hit = None
        for form, rx, lvl in specs:
            if (form == "call" and _is_call(piece, rx)) or (form != "call" and rx.fullmatch(piece)):
                hit = lvl
                break
        if hit:
            levels.append(hit)
            continue
        for rx, forces in comps:
            if rx.fullmatch(piece):
                halt = halt or forces
                break
        else:
            return None
    if not levels:
        return None
    return M if halt or M in levels else H


def _indent(t):
    return len(t) - len(t.lstrip())


def debug_findings(lines, linec, normal, lang):
    specs, comps, pre, strs = _debug_rx(lang)
    if not pre:
        return []
    n, cands, i = len(lines), [], 0
    code = {}

    def cp(k):
        if k not in code:
            code[k] = _code_part(lines[k], lang, strs)
        return code[k]

    while i < n:
        if i not in normal or i in linec or not pre.match(lines[i]) and not pre.match(cp(i)):
            i += 1
            continue
        j, stmt = i, cp(i).strip()
        while _depth(stmt) > 0 and j + 1 < n and j - i < 30 and (j + 1) in normal:
            j += 1
            stmt += " " + cp(j).strip()
        if _depth(stmt) != 0 or (j + 1 < n and (j + 1) not in normal):
            i = j + 1
            continue
        lvl = _classify_stmt(stmt, specs, comps)
        if lvl:
            cands.append([i, j, lvl, lines[i].strip()])
        i = j + 1
    taken = {k for s, e, _, _ in cands for k in range(s, e + 1)}

    def neighbour(k, step):
        while 0 <= k < n:
            if k not in taken and k not in linec and lines[k].strip():
                return k
            k += step
        return None

    def helper_body(s, e):
        """True when the statement sits in a function whose body is nothing but debug output."""
        if lang.name == "python":
            k = neighbour(s - 1, -1)
            if k is None or not re.match(r"\s*(?:async\s+)?def\s", lines[k]):
                return False
            ind, j = _indent(lines[s]), k + 1
            while j < n and (not lines[j].strip() or _indent(lines[j]) >= ind):
                if lines[j].strip() and j not in taken and j not in linec and not HELPER_BODY_RE.fullmatch(cp(j).strip()):
                    return False
                j += 1
            return True
        if lang.name not in BRACE_LANGS:
            return False
        d, k = 0, s - 1
        while k >= 0 and s - k < 200:
            c = cp(k)
            d += c.count("}") - c.count("{")
            if d < 0:
                break
            if k not in taken and k not in linec and c.strip() and not HELPER_BODY_RE.fullmatch(c.strip()):
                return False
            k -= 1
        if k < 0 or d >= 0:
            return False
        head = cp(k).strip()
        if head == "{" and k > 0:
            head = cp(k - 1).strip()
        if not FUNC_RE.search(head):
            return False
        d, j = 0, s
        while j < n and j - s < 200:
            c = cp(j)
            d += c.count("{") - c.count("}")
            if d < 0:
                return True
            if j not in taken and j not in linec and c.strip() and not HELPER_BODY_RE.fullmatch(c.strip()):
                return False
            j += 1
        return False

    out = []
    for s, e, lvl, preview in cands:
        why = "debug-helper" if helper_body(s, e) else None
        p = neighbour(s - 1, -1)
        nx = neighbour(e + 1, 1)
        pc = cp(p).strip() if p is not None else ""
        nc = cp(nx).strip() if nx is not None else ""
        if why:
            pass
        elif lang.name in BRACE_LANGS:
            if CTRL_RE.fullmatch(pc):
                why = "braceless-body"
            elif pc.endswith(DANGLING):
                why = "inside-expression"
        elif lang.name == "python":
            if pc.endswith(":") and (nx is None or _indent(lines[nx]) < _indent(lines[s])):
                why = "only-statement"
            elif pc.endswith(("\\", ",", "(", "[")):
                why = "inside-expression"
        elif lang.name == "shell":
            if re.search(r"(?:\bthen|\bdo|\belse|\{)$", pc) and re.match(r"(?:fi|done|esac|elif|else|\})\b", nc):
                why = "only-statement"
        elif lang.name in ("ruby", "elixir"):
            if re.match(r"end\b", nc):
                lvl = M  # last expression is the return value
        out.append({"start": s + 1, "end": e + 1, "kind": "debug", "level": "unsafe" if why else lvl,
                    "why": why, "ctx": False, "preview": preview})
    return out


KINDS = ("comments", "debug", "all")


def scan_file(path, lang=None, kind="comments"):
    """Return (findings, status) where status is None or a skip reason."""
    lang = lang or lang_for(path)
    text = read_text(path)
    if text is None:
        return [], "unreadable"
    if looks_generated(text):
        return [], "generated"
    res = analyze(text, lang)
    if res is None:
        return [], "parse"
    lines, linec, blocks, normal = res
    f = []
    if kind in ("comments", "all"):
        f += line_findings(linec) + block_findings(lines, blocks)
    if kind in ("debug", "all"):
        f += debug_findings(lines, linec, normal, lang)
    f.sort(key=lambda x: x["start"])
    return f, None


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

LEVELS = {"high": 2, "medium": 1, "unsafe": 1}


def cmd_scan(args):
    files = list_files(args.paths, args)
    minlvl = LEVELS[args.level]
    report, skipped, parse_files = [], Counter(), []
    for f in files:
        found, why = scan_file(f, kind=args.kind)
        if why:
            if why != "unreadable":
                skipped[why] += 1
                if why == "parse":
                    parse_files.append(os.path.relpath(f))
            continue
        found = [x for x in found if LEVELS[x["level"]] >= minlvl]
        if found:
            report.append((os.path.relpath(f), found))
    if args.json:
        json.dump([{"file": f, **x} for f, xs in report for x in xs], sys.stdout, indent=1)
        print()
        return 0
    nh = sum(1 for _, xs in report for x in xs if x["level"] == "high")
    nm = sum(1 for _, xs in report for x in xs if x["level"] == "medium")
    nu = sum(1 for _, xs in report for x in xs if x["level"] == "unsafe")
    nl = sum(x["end"] - x["start"] + 1 for _, xs in report for x in xs)
    pv = args.preview
    if args.summary:
        for f, xs in sorted(report, key=lambda r: -sum(x["end"] - x["start"] + 1 for x in r[1])):
            print("%s H%d M%d%s %dL" % (f, sum(x["level"] == "high" for x in xs),
                                         sum(x["level"] == "medium" for x in xs),
                                         " !%d" % sum(x["level"] == "unsafe" for x in xs)
                                         if any(x["level"] == "unsafe" for x in xs) else "",
                                         sum(x["end"] - x["start"] + 1 for x in xs)))
        report_lines = []
    else:
        report_lines = report
    for f, xs in report_lines:
        print(f)
        for x in xs:
            p = re.sub(r"\s+", " ", x["preview"]).strip()
            if len(p) > pv:
                p = p[:pv - 1] + "…"
            rng = "%d-%d" % (x["start"], x["end"]) if x["end"] != x["start"] else str(x["start"])
            tag = {"high": "H", "medium": "M"}.get(x["level"]) or "!" + x["why"]
            print("  %s %s%s %dL %s" % (rng, tag, "^" if x["ctx"] else "", x["end"] - x["start"] + 1, p))
    sk = ", ".join("%d %s" % (v, k) for k, v in skipped.items())
    print("# %d files, %d findings (%d H, %d M%s), %d lines%s" % (
        len(files), nh + nm + nu, nh, nm, ", %d ! unsafe: fix by hand" % nu if nu else "", nl,
        "; skipped: " + sk if sk else ""))
    if parse_files:
        print("# unparsed (review by hand if needed): " + " ".join(parse_files[:20])
              + (" …" if len(parse_files) > 20 else ""))
    return 0


def parse_spec(spec):
    path, _, rng = spec.rpartition(":")
    if not path or not re.fullmatch(r"\d+(-\d+)?", rng):
        raise ValueError("bad spec %r (want file:START-END)" % spec)
    a, _, b = rng.partition("-")
    a, b = int(a), int(b or a)
    if b < a:
        a, b = b, a
    return path, a, b


def read_specs(args):
    specs = list(args.specs or [])
    if getattr(args, "from_file", None):
        src = sys.stdin if args.from_file == "-" else open(args.from_file, encoding="utf-8")
        specs += [ln.split()[0] for ln in src if ln.strip() and not ln.startswith("#")]
    out = {}
    for s in specs:
        p, a, b = parse_spec(s)
        out.setdefault(os.path.normpath(p), []).append((a, b))
    return out


def cmd_show(args):
    for path, ranges in read_specs(args).items():
        text = read_text(path)
        if text is None:
            print("! %s: unreadable" % path)
            continue
        lines = text.splitlines()
        for a, b in sorted(ranges):
            print("== %s:%d-%d" % (path, a, b))
            lo, hi = max(1, a - args.context), min(len(lines), b + args.context)
            for n in range(lo, hi + 1):
                mark = "|" if a <= n <= b else " "
                print("%5d%s%s" % (n, mark, lines[n - 1]))
    return 0


def validate(lines, linec, blocks, a, b, debug=()):
    """Error string if lines a..b (1-based) are not entirely removable comment/debug lines."""
    if a < 1 or b > len(lines):
        return "out of range (file has %d lines)" % len(lines)
    inblock = {}
    for blk in blocks:
        if blk["end"] is None:
            continue
        for i in range(blk["start"], blk["end"] + 1):
            inblock[i] = blk
    dbg = {}
    for x in debug:
        for i in range(x["start"] - 1, x["end"]):
            dbg[i] = x
    for i in range(a - 1, b):
        x = dbg.get(i)
        if x is not None:
            if x["level"] == "unsafe":
                return "line %d: debug statement in unsafe context (%s), edit by hand" % (i + 1, x["why"])
            if x["start"] < a or x["end"] > b:
                return "range cuts debug statement %d-%d" % (x["start"], x["end"])
            continue
        blk = inblock.get(i)
        if blk is not None:
            if not blk["solo"]:
                return "line %d shares a line with code" % (i + 1)
            if blk["start"] < a - 1 or blk["end"] > b - 1:
                return "range cuts block comment %d-%d" % (blk["start"] + 1, blk["end"] + 1)
        elif i not in linec and lines[i].strip():
            return "line %d is neither a comment nor a debug statement" % (i + 1)
    return None


def remove_ranges(path, ranges, dry):
    lang = lang_for(path)
    if lang is None:
        return 0, ["%s: unsupported file type" % path]
    with open(path, "rb") as fh:
        raw = fh.read().decode("utf-8", "surrogateescape")
    res = analyze(raw, lang)
    if res is None:
        return 0, ["%s: cannot parse safely, skipped" % path]
    lines, linec, blocks, normal = res
    debug = debug_findings(lines, linec, normal, lang)
    errs, good = [], []
    for a, b in sorted(set(ranges)):
        e = validate(lines, linec, blocks, a, b, debug)
        if e:
            errs.append("%s:%d-%d: %s" % (path, a, b, e))
        elif good and a <= good[-1][1] + 1:
            good[-1] = (good[-1][0], max(b, good[-1][1]))
        else:
            good.append((a, b))
    if not good:
        return 0, errs
    keep = raw.splitlines(keepends=True)
    removed = 0
    for a, b in reversed(good):
        del keep[a - 1:b]
        removed += b - a + 1
        s = a - 1  # first line after the cut, 0-based
        if 0 < s < len(keep) and not keep[s - 1].strip() and not keep[s].strip():
            del keep[s]
            removed += 1
        elif s == len(keep) and s > 0 and not keep[s - 1].strip() and s - 1 > 0:
            del keep[s - 1]
            removed += 1
    if not dry:
        with open(path, "wb") as fh:
            fh.write("".join(keep).encode("utf-8", "surrogateescape"))
    return removed, errs


def cmd_remove(args):
    if args.level:
        if args.specs:
            print("error: give either SPECs or --level with paths", file=sys.stderr)
            return 2
        minlvl = LEVELS[args.level]
        plan = {}
        for f in list_files(args.paths, args):
            found, why = scan_file(f, kind=args.kind)
            for x in found:
                if x["level"] != "unsafe" and LEVELS[x["level"]] >= minlvl:
                    plan.setdefault(f, []).append((x["start"], x["end"]))
    else:
        if args.paths:
            args.specs = (args.specs or []) + args.paths
        plan = read_specs(args)
    total, files, allerr = 0, 0, []
    for path, ranges in sorted(plan.items()):
        n, errs = remove_ranges(path, ranges, args.dry_run)
        allerr += errs
        if n:
            files += 1
            total += n
            print("%s -%d" % (os.path.relpath(path), n))
    for e in allerr:
        print("! " + e)
    print("# %s %d lines in %d files%s" % ("would remove" if args.dry_run else "removed", total, files,
                                          "; %d rejected" % len(allerr) if allerr else ""))
    return 1 if allerr and not total else 0


IGNORE_NAMES = {
    "main", "init", "index", "constructor", "render", "setup", "setUp", "tearDown", "setUpBeforeClass",
    "tearDownAfterClass", "handle", "up", "down", "run", "boot", "register", "invoke", "toString",
    "equals", "hashCode", "compareTo", "ngOnInit", "ngOnDestroy", "ngOnChanges", "ngAfterViewInit",
    "componentDidMount", "componentWillUnmount", "componentDidUpdate", "shouldComponentUpdate",
    "getDerivedStateFromProps", "mounted", "created", "beforeDestroy", "unmounted", "data", "setup",
    "viewDidLoad", "applicationDidFinishLaunching", "onCreate", "onDestroy", "Main", "ServeHTTP",
    "Error", "String", "MarshalJSON", "UnmarshalJSON", "drop", "fmt", "new", "default", "from", "into",
}
IGNORE_RE = re.compile(r"^(?:__\w+__|__\w+|test\w*|Test\w*|Benchmark\w*|Example\w*|Fuzz\w*|_?on[A-Z]\w*)$")
TOKEN_RE = re.compile(rb"[A-Za-z_$][\w$]*")


def cmd_unused(args):
    targets = list_files(args.paths, args)
    root = args.root
    if not root:
        try:
            root = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                                  check=True, text=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            root = "."
    counts = Counter()
    for f in list_files([root], args, code_only=False):
        try:
            if os.path.getsize(f) > 2_000_000:
                continue
            with open(f, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        counts.update(TOKEN_RE.findall(data))
    rows = []
    for f in targets:
        lang = lang_for(f)
        if not lang or not lang.defs:
            continue
        text = read_text(f)
        if text is None or looks_generated(text):
            continue
        res = analyze(text, lang)
        if res is None:
            continue
        lines, linec, blocks, _ = res
        commented = set(linec)
        for b in blocks:
            if b["end"] is not None:
                commented.update(range(b["start"], b["end"] + 1))
        prev = ""
        for i, t in enumerate(lines):
            st = t.strip()
            if i in commented or not st:
                continue
            for rx in lang.defs:
                m = rx.match(t)
                if not m:
                    continue
                name = m.group(1).rstrip("?!")
                if name in IGNORE_NAMES or IGNORE_RE.match(name):
                    break
                if prev.startswith(("@", "#[")) or (prev.startswith("[") and prev.endswith("]")):
                    break  # annotated/decorated: usually wired up by a framework
                if counts[name.encode()] > 1:
                    break
                if re.search(r"\b(?:private|fileprivate)\b", t) or (name.startswith("_") and not name.startswith("__")):
                    vis = "private"
                elif lang.name == "go":
                    vis = "export" if name[0].isupper() else "private"
                elif lang.name == "rust":
                    vis = "export" if re.match(r"\s*pub\b", t) else "private"
                elif re.match(r"\s*export\b", t):
                    vis = "export"
                else:
                    vis = "public"
                if not args.private_only or vis == "private":
                    rows.append((vis != "private", os.path.relpath(f), i + 1, name, vis))
                break
            prev = st
    rows.sort()
    for _, f, n, name, vis in rows:
        print("%s:%d %s [%s]" % (f, n, name, vis))
    print("# %d candidates in %d files (referenced nowhere else under %s); verify before deleting"
          % (len(rows), len({r[1] for r in rows}), os.path.relpath(root)))
    return 0


def cmd_langs(_args):
    by = {}
    for e, lang in EXT.items():
        by.setdefault(lang.name, []).append(e)
    for name, exts in sorted(by.items()):
        lang = L[name]
        print("%-10s %-22s %s" % (name, " ".join(lang.line + tuple("%s %s" % kv for kv in lang.openers.items())),
                                  " ".join(sorted(exts))))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--exclude", action="append", metavar="GLOB", help="extra path/file glob to skip")
        p.add_argument("--no-default-excludes", action="store_true",
                       help="also scan vendor/, node_modules/, dist/, minified files, ...")

    p = sub.add_parser("scan", help="list commented-out code")
    p.add_argument("paths", nargs="*")
    p.add_argument("--level", choices=("high", "medium"), default="medium")
    p.add_argument("--json", action="store_true")
    p.add_argument("--summary", action="store_true", help="one line per file: counts only")
    p.add_argument("--kind", choices=KINDS, default="comments",
                   help="comments = commented-out code (default), debug = live debug statements, all")
    p.add_argument("--preview", type=int, default=72, help="preview width (default 72)")
    common(p)
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("show", help="print line ranges")
    p.add_argument("specs", nargs="*")
    p.add_argument("--from", dest="from_file", metavar="FILE", help="read specs from FILE ('-' = stdin)")
    p.add_argument("-C", "--context", type=int, default=0, help="extra lines around each range")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("remove", help="delete comment-only line ranges (validated)")
    p.add_argument("paths", nargs="*", help="SPECs, or paths when --level is given")
    p.add_argument("--from", dest="from_file", metavar="FILE", help="read specs from FILE ('-' = stdin)")
    p.add_argument("--level", choices=("high", "medium"), help="remove every scan finding at or above this level")
    p.add_argument("--kind", choices=KINDS, default="comments", help="finding kind for --level")
    p.add_argument("--dry-run", action="store_true")
    common(p)
    p.set_defaults(fn=cmd_remove, specs=None)

    p = sub.add_parser("unused", help="list functions/classes referenced nowhere else")
    p.add_argument("paths", nargs="*")
    p.add_argument("--root", help="corpus for reference counting (default: git top level)")
    p.add_argument("--private-only", action="store_true")
    common(p)
    p.set_defaults(fn=cmd_unused)

    p = sub.add_parser("langs", help="list supported languages")
    p.set_defaults(fn=cmd_langs)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except ValueError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
