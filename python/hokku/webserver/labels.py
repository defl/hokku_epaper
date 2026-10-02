"""Labels: free-form tags on pictures, and the per-screen filter that reads them.

A label is nothing more than a short string a user typed. There is no label
registry: the vocabulary is whatever the pictures currently carry, so a label
disappears the moment the last picture drops it and nothing has to be kept in
sync. Pictures and screens store the same normalised form, produced here, so
the filter is a plain set intersection.
"""

from __future__ import annotations

MAX_LABEL_LENGTH = 40
MAX_LABELS = 32


class LabelError(ValueError):
    """A label list that cannot be stored — the message is user-facing."""


def parse_labels(raw: object, *, field: str = "labels") -> tuple[str, ...]:
    """Normalise a user-supplied label list.

    Accepts a JSON list of strings. Each label is stripped and its inner
    whitespace collapsed; blanks are rejected, duplicates dropped, and the
    result sorted so two lists with the same labels compare equal.
    """
    if not isinstance(raw, list):
        raise LabelError(f"{field} must be a list of strings")
    if len(raw) > MAX_LABELS:
        raise LabelError(f"{field}: at most {MAX_LABELS} labels")
    out: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise LabelError(f"{field} must be a list of strings")
        label = " ".join(item.split())
        if not label:
            raise LabelError(f"{field}: a label cannot be blank")
        if len(label) > MAX_LABEL_LENGTH:
            raise LabelError(
                f"{field}: {label[:20]!r}… is longer than {MAX_LABEL_LENGTH} characters"
            )
        out.add(label)
    return tuple(sorted(out))


def matches_labels(have: tuple[str, ...], wanted: frozenset[str]) -> bool:
    """A picture is eligible when it carries any of the wanted labels.

    An empty filter matches everything — that is the pre-labels behaviour and
    what an unconfigured screen gets.
    """
    return not wanted or not wanted.isdisjoint(have)
