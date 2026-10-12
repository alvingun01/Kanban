"""Minimal YAML subset for card frontmatter.

Deliberately not a general YAML implementation. The card schema is fixed and
bounded, so this handles exactly that shape: nested mappings, block and inline
lists, quoted and plain scalars, null, bool, int, float. Staying stdlib-only
means ``kanban init`` works in a fresh project with no install step, and every
git worktree can read the board without a shared dependency.

Two properties matter as much as correctness:

* **Deterministic emission.** Stable key order and stable quoting, so a card's
  git diff shows real changes only.
* **Unknown keys round-trip untouched.** A fixed-key-order emitter is exactly
  the kind of code that silently eats fields it does not recognise, which would
  let an older tool destroy data written by a newer one. Ordering is the
  caller's job (see ``model.normalize``); this module only preserves.
"""

import re

__all__ = ["YamliteError", "parse_document", "format_document", "parse_mapping"]


class YamliteError(ValueError):
    """Raised when frontmatter is malformed."""


_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")
_RESERVED_WORDS = frozenset(("null", "~", "true", "false", "yes", "no", "on", "off"))
# Characters that change a plain scalar's meaning when they lead it.
_LEADING_SPECIALS = "-?:,[]{}#&*!|>'\"%@`"
_INLINE_WIDTH = 88


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def parse_document(text):
    """Split ``text`` into (frontmatter dict, body str).

    A document with no leading ``---`` is treated as all body, so a
    hand-written note never raises.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        raise YamliteError("unterminated frontmatter: no closing '---'")

    data = parse_mapping("\n".join(lines[1:end]))
    body = "\n".join(lines[end + 1:])
    return data, body.lstrip("\n")


def parse_mapping(text):
    """Parse a YAML-subset mapping from ``text``."""
    tokens = _tokenize(text.splitlines())
    if not tokens:
        return {}
    value, index = _parse_map(tokens, 0, tokens[0][0])
    if index != len(tokens):
        raise YamliteError("could not parse line: %r" % (tokens[index][1],))
    return value


def _tokenize(lines):
    """Drop blanks and comments, returning [(indent, text)]."""
    out = []
    for raw in lines:
        if not raw.strip():
            continue
        decommented = _strip_comment(raw)
        if not decommented.strip():
            continue
        indent = len(decommented) - len(decommented.lstrip(" "))
        out.append((indent, decommented.strip()))
    return out


def _strip_comment(line):
    """Remove a trailing ``#`` comment, ignoring ``#`` inside quotes."""
    out = []
    quote = None
    i = 0
    while i < len(line):
        char = line[i]
        if quote:
            if char == "\\" and quote == '"' and i + 1 < len(line):
                out.append(char)
                out.append(line[i + 1])
                i += 2
                continue
            if char == quote:
                quote = None
            out.append(char)
        elif char in "\"'":
            quote = char
            out.append(char)
        elif char == "#" and (not out or out[-1] in " \t"):
            break
        else:
            out.append(char)
        i += 1
    return "".join(out).rstrip()


def _parse_block(tokens, index, indent):
    text = tokens[index][1]
    if text == "-" or text.startswith("- "):
        return _parse_list(tokens, index, indent)
    return _parse_map(tokens, index, indent)


def _parse_map(tokens, index, indent):
    result = {}
    while index < len(tokens):
        line_indent, text = tokens[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise YamliteError("unexpected indent at %r" % (text,))
        if text == "-" or text.startswith("- "):
            break
        if ":" not in text:
            raise YamliteError("expected 'key: value', got %r" % (text,))

        key, _, rest = text.partition(":")
        key = key.strip()
        if not key:
            raise YamliteError("empty key in %r" % (text,))
        rest = rest.strip()
        index += 1

        if rest:
            result[key] = _scalar(rest)
            continue

        # No inline value: either a nested block, a block list, or null.
        if index < len(tokens) and tokens[index][0] > indent:
            child, index = _parse_block(tokens, index, tokens[index][0])
            result[key] = child
        elif (
            index < len(tokens)
            and tokens[index][0] == indent
            and (tokens[index][1] == "-" or tokens[index][1].startswith("- "))
        ):
            child, index = _parse_list(tokens, index, indent)
            result[key] = child
        else:
            result[key] = None
    return result, index


def _parse_list(tokens, index, indent):
    items = []
    while index < len(tokens):
        line_indent, text = tokens[index]
        if line_indent != indent:
            break
        if not (text == "-" or text.startswith("- ")):
            break

        item = text[1:].strip()
        index += 1
        if item:
            items.append(_scalar(item))
        elif index < len(tokens) and tokens[index][0] > indent:
            child, index = _parse_block(tokens, index, tokens[index][0])
            items.append(child)
        else:
            items.append(None)
    return items, index


def _scalar(raw):
    """Convert one scalar token to a Python value."""
    text = raw.strip()
    if not text:
        return None

    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return _unescape(text[1:-1])
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1].replace("''", "'")
    if text.startswith("[") and text.endswith("]"):
        return [_scalar(part) for part in _split_inline(text[1:-1])]
    if text.startswith("{") and text.endswith("}"):
        result = {}
        for part in _split_inline(text[1:-1]):
            key, _, value = part.partition(":")
            result[key.strip()] = _scalar(value)
        return result

    lowered = text.lower()
    if lowered in ("null", "~"):
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False

    if _INT_RE.match(text):
        # A leading zero is significant: card id "042" must not become 42.
        digits = text[1:] if text[0] == "-" else text
        if len(digits) > 1 and digits[0] == "0":
            return text
        return int(text)
    if _FLOAT_RE.match(text):
        return float(text)
    return text


def _split_inline(text):
    """Split a flow collection's body on top-level commas."""
    if not text.strip():
        return []
    parts = []
    buf = []
    depth = 0
    quote = None
    for char in text:
        if quote:
            buf.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
            buf.append(char)
        elif char in "[{":
            depth += 1
            buf.append(char)
        elif char in "]}":
            depth -= 1
            buf.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    parts.append("".join(buf))
    return [part.strip() for part in parts]


def _unescape(text):
    out = []
    i = 0
    simple = {
        "n": "\n",
        "t": "\t",
        "r": "\r",
        '"': '"',
        "\\": "\\",
        "/": "/",
        "0": "\0",
    }
    while i < len(text):
        char = text[i]
        if char == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt in simple:
                out.append(simple[nxt])
                i += 2
                continue
            if nxt == "u" and i + 6 <= len(text):
                try:
                    out.append(chr(int(text[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
        out.append(char)
        i += 1
    return "".join(out)


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def format_document(data, body=""):
    """Render frontmatter + body. Keys emit in ``data`` insertion order."""
    lines = ["---"]
    lines.extend(_emit_map(data, 0))
    lines.append("---")
    text = "\n".join(lines) + "\n"
    if body and body.strip():
        text += "\n" + body.rstrip("\n") + "\n"
    return text


def _emit_map(data, indent):
    pad = " " * indent
    out = []
    for key, value in data.items():
        out.extend(_emit_pair(pad, key, value, indent))
    return out


def _emit_pair(pad, key, value, indent):
    if isinstance(value, dict):
        if not value:
            return ["%s%s: {}" % (pad, key)]
        return ["%s%s:" % (pad, key)] + _emit_map(value, indent + 2)

    if isinstance(value, (list, tuple)):
        if not value:
            return ["%s%s: []" % (pad, key)]
        inline = _try_inline(value)
        if inline is not None and indent + len(key) + 2 + len(inline) <= _INLINE_WIDTH:
            return ["%s%s: %s" % (pad, key, inline)]
        out = ["%s%s:" % (pad, key)]
        for item in value:
            if isinstance(item, dict):
                out.append("%s  -" % pad)
                out.extend(_emit_map(item, indent + 4))
            elif isinstance(item, (list, tuple)):
                out.append("%s  - %s" % (pad, _try_inline(item) or "[]"))
            else:
                out.append("%s  - %s" % (pad, _emit_scalar(item)))
        return out

    return ["%s%s: %s" % (pad, key, _emit_scalar(value))]


def _try_inline(items):
    """Render a flat scalar list inline, or None if it has nested values."""
    if any(isinstance(item, (dict, list, tuple)) for item in items):
        return None
    return "[" + ", ".join(_emit_scalar(item) for item in items) + "]"


def _emit_scalar(value):
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    text = str(value)
    if _needs_quote(text):
        escaped = (
            text.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\t", "\\t")
        )
        return '"' + escaped + '"'
    return text


def _needs_quote(text):
    """Quote only when a plain scalar would parse back as something else."""
    if text == "":
        return True
    if text != text.strip():
        return True
    if text.lower() in _RESERVED_WORDS:
        return True
    # Anything that looks numeric must be quoted, so ids like "042" survive as
    # strings and real numbers survive as numbers.
    if _INT_RE.match(text) or _FLOAT_RE.match(text):
        return True
    if text[0] in _LEADING_SPECIALS:
        return True
    if ": " in text or text.endswith(":"):
        return True
    if " #" in text:
        return True
    if "\n" in text or "\t" in text:
        return True
    return False
