#!/usr/bin/env python3
"""nginx_patch.py - find the right nginx server block and add/remove one include line.

Used by deploy.sh so the stove website can be added to a site that already has
its own routing. It never rewrites your config: it inserts exactly two lines

    # stove website (added by deploy.sh)
    include /etc/nginx/snippets/stove-cloud.conf;

before the closing brace of the matching HTTPS server block, after saving a
backup outside of nginx's include directories.

Commands (all print JSON unless noted):
  servers  [--ssl-only] [--names]          list server blocks nginx loads
                                           (--names: "main-name<TAB>other names" per block)
  plan     --domain D --prefix P --include FILE [--file F] [--allow-http]
  apply    --domain D --prefix P --include FILE --backup-dir DIR [--file F] [--allow-http] [--dry-run]
  remove   --include FILE --backup-dir DIR [--dry-run]
  restore  --manifest FILE
Common: --nginx BIN (default nginx), --nginx-arg=ARG (repeatable, e.g. --nginx-arg=-c --nginx-arg=/x.conf)

Standard library only, Python 3.7+.
"""

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

MARKER = "# stove website (added by deploy.sh)"
OLD_MARKERS = ("# mystove relay (added by deploy.sh)",)   # earlier versions


class ParseError(Exception):
    pass


# --------------------------------------------------------------------------
# nginx config parsing
# --------------------------------------------------------------------------
def tokenize(text):
    """Yield (kind, value, start, end); kind is word | str | { | } | ;  (mirrors ngx_conf_read_token)."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "#":
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c in "{};":
            yield c, c, i, i + 1
            i += 1
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                raise ParseError("unterminated quote")
            yield "str", text[i + 1:j], i, j + 1
            i = j + 1
        else:
            j = i
            while j < n:
                ch = text[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch in " \t\r\n;":
                    break
                if ch == "{" and not (j > i and text[j - 1] == "$"):
                    break
                j += 1
            yield "word", text[i:j], i, j
            i = j


class Node:
    __slots__ = ("name", "args", "start", "line", "children", "open", "close")

    def __init__(self, name, args, start, line):
        self.name, self.args, self.start, self.line = name, args, start, line
        self.children = None
        self.open = self.close = None


def parse(text):
    root = Node("", [], 0, 1)
    root.children = []
    stack, words, start = [root], [], None

    def line_of(pos):
        return text.count("\n", 0, pos) + 1

    for kind, value, s, e in tokenize(text):
        if kind in ("word", "str"):
            if not words:
                start = s
            words.append(value)
        elif kind == ";":
            if words:
                stack[-1].children.append(Node(words[0], words[1:], start, line_of(start)))
            words = []
        elif kind == "{":
            node = Node(words[0] if words else "", words[1:], start if words else s, line_of(start if words else s))
            node.children, node.open = [], s
            stack[-1].children.append(node)
            stack.append(node)
            words = []
        else:  # "}"
            if len(stack) == 1:
                raise ParseError(f"unexpected '}}' at line {line_of(s)}")
            stack.pop().close = s
            words = []
    if len(stack) != 1:
        raise ParseError("missing '}'")
    return root


def walk(node, parents=()):
    for child in node.children or ():
        yield child, parents
        if child.children is not None:
            yield from walk(child, parents + (child.name,))


# --------------------------------------------------------------------------
# What nginx actually loads
# --------------------------------------------------------------------------
def loaded_files(nginx="nginx", extra=()):
    try:
        out = subprocess.run([nginx, "-T", *extra], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True, timeout=60)
    except FileNotFoundError:
        raise SystemExit(f"nginx_patch: '{nginx}' not found")
    if out.returncode != 0:
        raise SystemExit("nginx_patch: 'nginx -T' failed - fix the current nginx config first:\n" + out.stderr)
    files = []
    for m in re.finditer(r"^# configuration file (.+):$", out.stdout, re.M):
        path = m.group(1)
        if path not in files:
            files.append(path)
    if not files:
        raise SystemExit("nginx_patch: could not read the config file list from 'nginx -T'")
    return files


def is_ssl(listens, children):
    for args in listens:
        if any(a in ("ssl", "quic", "http3") for a in args[1:]):
            return True
        if args and re.search(r"(^|:)443$", args[0]):
            return True
    return any(c.name == "ssl" and c.args[:1] == ["on"] for c in children)


def server_blocks(files):
    """All http server blocks: dicts with file, real path, line, names, ssl, includes, locations, node."""
    blocks, errors = [], []
    seen = set()
    for path in files:
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        try:
            with open(real, encoding="utf-8") as f:
                text = f.read()
            tree = parse(text)
        except (OSError, UnicodeDecodeError, ParseError) as e:
            errors.append({"file": path, "error": str(e)})
            continue
        for node, parents in walk(tree):
            if node.name != "server" or node.children is None or "stream" in parents or "mail" in parents:
                continue
            kids = node.children
            listens = [c.args for c in kids if c.name == "listen"]
            blocks.append({
                "file": path,
                "real": real,
                "line": node.line,
                "names": [a for c in kids if c.name == "server_name" for a in c.args],
                "listen": [" ".join(a) for a in listens],
                "ssl": is_ssl(listens, kids),
                "includes": [c.args[0] for c in kids if c.name == "include" and c.args],
                "locations": [c.args for c in kids if c.name == "location" and c.children is not None],
                "node": node,
            })
    return blocks, errors


def name_match(names, domain):
    d = domain.lower().rstrip(".")
    best = None
    for n in names:
        n = n.lower().rstrip(".")
        if n == d:
            return "exact"
        if (n.startswith(".") and (d == n[1:] or d.endswith(n))) or (n.startswith("*.") and d.endswith(n[1:])):
            best = "wildcard"
    return best


def location_conflicts(block, prefix):
    out = []
    for args in block["locations"]:
        if not args:
            continue
        path = args[-1]
        modifier = args[0] if len(args) > 1 else ""
        if modifier in ("~", "~*"):
            if prefix.strip("/").lower() in path.lower():
                out.append(" ".join(args))
        elif path.rstrip("/") == prefix or path.startswith(prefix + "/"):
            out.append(" ".join(args))
    return out


def plan(blocks, domain, prefix, include, only_file=None, allow_http=False):
    """Pick the server blocks to patch. Returns dict(targets, already, conflicts, error)."""
    cands = []
    for b in blocks:
        if only_file and os.path.realpath(only_file) != b["real"]:
            continue
        kind = name_match(b["names"], domain)
        if kind:
            cands.append((kind, b))
    result = {"targets": [], "already": [], "conflicts": [], "error": None, "http_only": False}
    if not cands:
        known = sorted({n for b in blocks for n in b["names"] if n not in ("_", "localhost", "")})
        result["error"] = (f"no nginx server block has server_name {domain}. "
                           f"Known names: {', '.join(known) or 'none'}")
        return result
    exact = [b for k, b in cands if k == "exact"]
    chosen = exact or [b for _, b in cands]
    ssl = [b for b in chosen if b["ssl"]]
    if not ssl:
        if not allow_http:
            result["http_only"] = True
            result["error"] = (f"{domain} has no HTTPS server block in nginx. Get a certificate first "
                               f"(e.g. certbot --nginx -d {domain}) or re-run with --allow-http (not recommended).")
            return result
        ssl = chosen
    for b in ssl:
        info = {"file": b["file"], "line": b["line"], "names": b["names"], "listen": b["listen"]}
        if include in b["includes"]:
            result["already"].append(info)
            continue
        conflicts = location_conflicts(b, prefix)
        if conflicts:
            result["conflicts"].append(dict(info, locations=conflicts))
        else:
            result["targets"].append(info)
    if result["conflicts"]:
        c = result["conflicts"][0]
        result["error"] = (f"{c['file']}:{c['line']} already has location(s) for {prefix}: "
                           f"{'; '.join(c['locations'])}. Remove them or pick another --path.")
    return result


# --------------------------------------------------------------------------
# Editing
# --------------------------------------------------------------------------
def _indent_of(text, node):
    if node.children:
        first = node.children[0].start
        line_start = text.rfind("\n", 0, first) + 1
        indent = text[line_start:first]
        if indent.strip() == "":
            return indent
    return "    "


def insert_include(text, nodes, include):
    """Insert the include line into each server node (offsets refer to `text`)."""
    for node in sorted(nodes, key=lambda n: n.close, reverse=True):
        indent = _indent_of(text, node)
        lines = f"{indent}{MARKER}\n{indent}include {include};\n"
        line_start = text.rfind("\n", 0, node.close) + 1
        if text[line_start:node.close].strip() == "":
            text = text[:line_start] + lines + text[line_start:]
        else:
            text = text[:node.close] + "\n" + lines + text[node.close:]
    return text


def remove_include(text, include):
    out = []
    for line in text.splitlines(keepends=True):
        if line.strip() == f"include {include};":
            if out and out[-1].strip() in (MARKER,) + OLD_MARKERS:
                out.pop()
            continue
        out.append(line)
    return "".join(out)


def write_file(path, text):
    """Replace path's content atomically, keeping its mode and owner."""
    st = os.stat(path)
    folder = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".stove-cloud-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, st.st_mode & 0o7777)
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            pass
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_all(edits):
    """Write {path: (old, new)}; if one write fails, put the already written files back."""
    written = []
    try:
        for path, (_old, new) in edits.items():
            write_file(path, new)
            written.append(path)
    except BaseException:
        for path in written:
            write_file(path, edits[path][0])
        raise


def backup(paths, backup_dir):
    os.makedirs(backup_dir, mode=0o700, exist_ok=True)
    folder = tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=backup_dir)  # unique, mode 700
    entries = []
    for p in paths:
        dest = os.path.join(folder, p.strip("/").replace("/", "__"))
        shutil.copy2(p, dest)
        entries.append({"path": p, "backup": dest})
    manifest = os.path.join(folder, "manifest.json")
    with open(manifest, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
    return manifest, entries


def restore(manifest):
    with open(manifest, encoding="utf-8") as f:
        entries = json.load(f)
    for e in entries:
        with open(e["backup"], encoding="utf-8") as src:
            write_file(e["path"], src.read())
    return entries


def show_diff(old, new, path):
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), path, path + " (new)"))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="add/remove the stove website's include in nginx")
    ap.add_argument("command", choices=["servers", "plan", "apply", "remove", "restore"])
    ap.add_argument("--nginx", default="nginx")
    ap.add_argument("--nginx-arg", action="append", default=[])
    ap.add_argument("--domain")
    ap.add_argument("--prefix", default="/mystove")
    ap.add_argument("--include")
    ap.add_argument("--file", help="only consider server blocks in this file")
    ap.add_argument("--backup-dir", default="/var/backups/stove-cloud")
    ap.add_argument("--manifest")
    ap.add_argument("--allow-http", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ssl-only", action="store_true")
    ap.add_argument("--names", action="store_true", help="servers: print one server name per line")
    args = ap.parse_args(argv)

    if args.command == "restore":
        if not args.manifest:
            ap.error("restore needs --manifest")
        print(json.dumps({"restored": [e["path"] for e in restore(args.manifest)]}))
        return 0

    blocks, errors = server_blocks(loaded_files(args.nginx, args.nginx_arg))

    if args.command == "servers":
        shown = [b for b in blocks if b["ssl"] or not args.ssl_only]
        if args.names:
            # one line per server block: main name, TAB, other names
            seen = set()
            for b in shown:
                names = [n for n in b["names"] if n not in ("_", "localhost", "") and not n.startswith("~")]
                if names and names[0] not in seen:
                    seen.update(names)
                    print(names[0] + "\t" + " ".join(names[1:]))
        else:
            print(json.dumps({"servers": [{k: v for k, v in b.items() if k not in ("node", "real")}
                                          for b in shown], "errors": errors}, indent=2))
        return 0

    if not args.include:
        ap.error("--include is required")
    prefix = "/" + args.prefix.strip("/")

    if args.command == "remove":
        changed = {}
        for b in blocks:
            if args.include in b["includes"] and b["real"] not in changed:
                with open(b["real"], encoding="utf-8") as f:
                    old = f.read()
                changed[b["real"]] = (old, remove_include(old, args.include))
        if args.dry_run:
            for path, (old, new) in changed.items():
                sys.stdout.write(show_diff(old, new, path))
            return 0
        manifest = None
        if changed:
            manifest, _ = backup(list(changed), args.backup_dir)
            write_all(changed)
        print(json.dumps({"changed": list(changed), "manifest": manifest}))
        return 0

    if not args.domain:
        ap.error("--domain is required")
    result = plan(blocks, args.domain, prefix, args.include, args.file, args.allow_http)
    if args.command == "plan" or result["error"]:
        result["parse_errors"] = errors
        print(json.dumps(result, indent=2))
        return 1 if result["error"] else 0

    # apply
    per_file = {}
    for t in result["targets"]:
        b = next(b for b in blocks if b["file"] == t["file"] and b["line"] == t["line"])
        per_file.setdefault(b["real"], []).append(b["node"])
    edits = {}
    for path, nodes in per_file.items():
        with open(path, encoding="utf-8") as f:
            old = f.read()
        edits[path] = (old, insert_include(old, nodes, args.include))
    if args.dry_run:
        for path, (old, new) in edits.items():
            sys.stdout.write(show_diff(old, new, path))
        return 0
    manifest = None
    if edits:
        manifest, _ = backup(list(edits), args.backup_dir)
        print(f"nginx_patch: backup in {os.path.dirname(manifest)}", file=sys.stderr)
        write_all(edits)
    print(json.dumps({"changed": list(edits), "already": [a["file"] for a in result["already"]],
                      "manifest": manifest}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
