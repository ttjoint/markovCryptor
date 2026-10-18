"""Corpus-driven punctuation layer.

The steganographic payload lives only in the hanzi sequence. Punctuation and
line breaks are added afterwards by :class:`PunctuationModel`, which samples
them from statistics learned on the training corpus using an independent random
source. Decoding simply discards every non-hanzi character, so this layer can
never corrupt the payload.

Each gap between two adjacent hanzi ``a`` and ``b`` holds a *gap token*: the
exact punctuation string found there in the corpus (``""``, ``"，"``, ``"。\\n"``,
``"：“"``, ``"？”"`` ...). Rendering decides per gap

1. whether a boundary occurs, from the pair-level rate P(boundary | a, b)
   (backed off to the left/right character rates) combined with the corpus
   clause-length distribution, and
2. which token, from P(token | a) x P(token | b) / P(token), adjusted by the
   sentence-length distribution and a bracket state machine that keeps quotes
   and brackets balanced and only allows line breaks after a sentence end.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
import math
import random
import re
import secrets
from typing import Any, Mapping, Optional

from .progress import REPORT_EVERY, ProgressFn, report

HANZI_PATTERN = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
OPEN_TO_CLOSE = {"“": "”", "‘": "’", "（": "）", "《": "》", "〈": "〉", "【": "】", "「": "」", "『": "』"}
CLOSE_TO_OPEN = {close: open_ for open_, close in OPEN_TO_CLOSE.items()}
# Title marks enclose a bare name: inside them the only allowed gap token closes them.
ATOMIC_OPENERS = set("《〈")
TERMINAL_MARKS = set("。！？…")
PUNCTUATION = set("，。！？；：、—…·") | set(OPEN_TO_CLOSE) | set(CLOSE_TO_OPEN)
_HALFWIDTH = str.maketrans({",": "，", "!": "！", "?": "？", ";": "；", ":": "：", "(": "（", ")": "）"})

MAX_TOKENS = 256
MAX_TOKEN_LENGTH = 8
CLAUSE_CAP = 80
SENTENCE_CAP = 16
MAX_BRACKET_DEPTH = 2
_PAIR_PRIOR = 3.0  # pseudo-count pulling P(boundary | a, b) toward the backoff estimate
_CHAR_PRIOR = 10.0  # pseudo-count pulling per-character boundary rates toward the global rate
_TOKEN_PRIOR = 5.0  # pseudo-count pulling per-character token distributions toward the global one
_CANDIDATE_TOP = 48
_DEFAULT_SPAN = 12.0


def is_hanzi(char: str) -> bool:
    code = ord(char)
    return 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF


def extract_hanzi(text: str) -> str:
    return "".join(HANZI_PATTERN.findall(text))


def _ends_sentence(text: str) -> bool:
    stripped = text.rstrip("".join(CLOSE_TO_OPEN))
    return bool(stripped) and stripped[-1] in TERMINAL_MARKS


def _is_terminal(token: str) -> bool:
    return "\n" in token or any(char in TERMINAL_MARKS for char in token)


def normalize_gap(raw: str) -> str:
    """Reduce the raw text between two hanzi to punctuation plus paragraph breaks.

    Non-punctuation (Latin letters, digits, spaces) is dropped. A line break is
    kept only when it follows a sentence end, so headings and hard-wrapped lines
    in the corpus do not teach the model to break lines mid-sentence.
    """
    result = ""
    for index, part in enumerate(re.split(r"[\r\n]+", raw.translate(_HALFWIDTH))):
        if index and _ends_sentence(result):
            result += "\n"
        result += "".join(char for char in part if char in PUNCTUATION)
    return result


def segment_corpus(text: str) -> tuple[str, dict[int, str]]:
    """Split text into its hanzi stream and the gap tokens between hanzi.

    ``gaps[i]`` is the normalized punctuation between hanzi ``i`` and ``i + 1``;
    empty gaps are omitted. Text before the first hanzi is ignored.
    """
    runs: list[str] = []
    gaps: dict[int, str] = {}
    length = 0
    position = 0
    for match in HANZI_PATTERN.finditer(text):
        if runs and match.start() > position:
            gap = normalize_gap(text[position:match.start()])
            if gap:
                gaps[length - 1] = gap
        run = match.group()
        runs.append(run)
        length += len(run)
        position = match.end()
    return "".join(runs), gaps


def filter_vocabulary(hanzi: str, gaps: Mapping[int, str], keep: set[str]) -> tuple[str, dict[int, str]]:
    """Drop out-of-vocabulary hanzi, merging their gaps into the preceding kept one."""
    drop = set(hanzi) - keep
    if not drop:
        return hanzi, dict(gaps)
    pattern = re.compile("[" + "".join(re.escape(char) for char in sorted(drop)) + "]")
    removed = [match.start() for match in pattern.finditer(hanzi)]
    kept = hanzi.translate({ord(char): None for char in drop})
    merged: dict[int, str] = {}
    for index in sorted(gaps):
        # Kept hanzi up to and including ``index``, minus one: the gap's new left neighbour.
        position = index - bisect_right(removed, index)
        if position < 0:
            continue
        gap = gaps[index]
        previous = merged.get(position, "")
        # Concatenate only when brackets/paragraphs must survive; otherwise
        # keep the stronger single mark so "，" + "、" does not become "，、".
        if any(char in OPEN_TO_CLOSE or char in CLOSE_TO_OPEN or char == "\n" for char in previous + gap):
            merged[position] = previous + gap
        elif not previous or _is_terminal(gap) or not _is_terminal(previous):
            merged[position] = gap
    return kept, merged


def _apply_brackets(token: str, stack: tuple[tuple[str, int], ...], start: int) -> tuple[tuple[str, int], ...] | None:
    """Return the bracket stack after ``token``, or None if the token is not allowed here."""
    current = list(stack)
    if current and current[-1][0] in ATOMIC_OPENERS and not token.startswith(OPEN_TO_CLOSE[current[-1][0]]):
        return None
    for char in token:
        if char in OPEN_TO_CLOSE:
            # Chinese nests different marks (“…‘…’…”), never the same one twice.
            if len(current) >= MAX_BRACKET_DEPTH or any(opener == char for opener, _ in current):
                return None
            current.append((char, start))
        elif char in CLOSE_TO_OPEN:
            if not current or current[-1][0] != CLOSE_TO_OPEN[char]:
                return None
            current.pop()
        elif char == "\n" and current:
            return None  # paragraphs never break inside quotes or brackets
    return tuple(current)


def _odds(probability: float) -> float:
    probability = min(max(probability, 1e-6), 1 - 1e-6)
    return probability / (1 - probability)


def _hazards(histogram: list[int]) -> list[float]:
    """Smoothed P(stop at n | reached n) for n = 0..cap; the overflow bucket is capped."""
    hazards = [0.0] * len(histogram)
    remaining = sum(histogram)
    for index, count in enumerate(histogram):
        hazards[index] = (count + 0.5) / (remaining + 1.0)
        remaining -= count
    if len(hazards) > 2:
        hazards[-1] = max(hazards[-2], 0.5)
    return hazards


def _sample(rng: random.Random, items: list[Any], weights: list[float]) -> Any | None:
    total = sum(weights)
    if total <= 0:
        return None
    threshold = rng.random() * total
    for item, weight in zip(items, weights):
        threshold -= weight
        if threshold < 0:
            return item
    return items[-1]


class PunctuationModel:
    """Learned punctuation statistics plus the renderer that applies them."""

    def __init__(
        self,
        tokens: list[str],
        boundary_pairs: dict[str, dict[str, int]],
        left: dict[str, dict[str, int]],
        right: dict[str, dict[str, int]],
        clause_lengths: list[int],
        sentence_clauses: list[int],
        bracket_spans: dict[str, list[int]],
        total_pairs: int,
    ) -> None:
        self.tokens = tuple(tokens)
        self._ids = {token: index for index, token in enumerate(self.tokens)}
        self.boundary_pairs = {a: {b: int(k) for b, k in row.items()} for a, row in boundary_pairs.items()}
        self.left = {a: {self._ids[t]: int(c) for t, c in row.items() if t in self._ids} for a, row in left.items()}
        self.right = {b: {self._ids[t]: int(c) for t, c in row.items() if t in self._ids} for b, row in right.items()}
        self.clause_lengths = [int(value) for value in clause_lengths]
        self.sentence_clauses = [int(value) for value in sentence_clauses]
        self.bracket_spans = {opener: [int(value) for value in span] for opener, span in bracket_spans.items()}
        self.total_pairs = int(total_pairs)
        self._prepared_for: int | None = None

    # ------------------------------------------------------------- training

    @classmethod
    def train(cls, hanzi: str, gaps: Mapping[int, str]) -> "PunctuationModel":
        raw = Counter(gaps.values())
        tokens = [token for token, _ in raw.most_common() if len(token) <= MAX_TOKEN_LENGTH][:MAX_TOKENS]
        known = set(tokens)

        def canonical(token: str) -> str | None:
            if token in known:
                return token
            fallback = "。\n" if "\n" in token else "。" if _is_terminal(token) else "，"
            return fallback if fallback in known else None

        boundary_pairs: dict[str, Counter[str]] = defaultdict(Counter)
        left: dict[str, Counter[str]] = defaultdict(Counter)
        right: dict[str, Counter[str]] = defaultdict(Counter)
        clause_lengths = [0] * (CLAUSE_CAP + 1)
        sentence_clauses = [0] * (SENTENCE_CAP + 1)
        spans: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        stack: list[tuple[str, int]] = []
        previous = -1
        clauses = 0
        last = len(hanzi) - 1
        for position in sorted(gaps):
            if position >= last:
                continue
            token = canonical(gaps[position])
            if token is None:
                continue
            a, b = hanzi[position], hanzi[position + 1]
            boundary_pairs[a][b] += 1
            left[a][token] += 1
            right[b][token] += 1
            clause_lengths[min(position - previous, CLAUSE_CAP)] += 1
            previous = position
            clauses += 1
            if _is_terminal(token):
                sentence_clauses[min(clauses, SENTENCE_CAP)] += 1
                clauses = 0
            for char in token:
                if char == "\n":
                    stack.clear()  # a paragraph break resynchronizes unbalanced corpus text
                elif char in OPEN_TO_CLOSE:
                    stack.append((char, position + 1))
                elif char in CLOSE_TO_OPEN:
                    if stack and stack[-1][0] == CLOSE_TO_OPEN[char]:
                        opener, start = stack.pop()
                        spans[opener][0] += 1
                        spans[opener][1] += position + 1 - start
                    else:
                        stack.clear()
            if len(stack) > MAX_BRACKET_DEPTH:
                stack.clear()
        return cls(
            tokens=tokens,
            boundary_pairs={a: dict(row) for a, row in boundary_pairs.items()},
            left={a: dict(row) for a, row in left.items()},
            right={b: dict(row) for b, row in right.items()},
            clause_lengths=clause_lengths,
            sentence_clauses=sentence_clauses,
            bracket_spans=dict(spans),
            total_pairs=max(len(hanzi) - 1, 0),
        )

    # -------------------------------------------------------- serialization

    def to_dict(self) -> dict[str, Any]:
        return {
            "tokens": list(self.tokens),
            "boundary_pairs": self.boundary_pairs,
            "left": {a: {self.tokens[t]: c for t, c in row.items()} for a, row in self.left.items()},
            "right": {b: {self.tokens[t]: c for t, c in row.items()} for b, row in self.right.items()},
            "clause_lengths": self.clause_lengths,
            "sentence_clauses": self.sentence_clauses,
            "bracket_spans": self.bracket_spans,
            "total_pairs": self.total_pairs,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PunctuationModel":
        return cls(
            tokens=list(payload["tokens"]),
            boundary_pairs=payload["boundary_pairs"],
            left=payload["left"],
            right=payload["right"],
            clause_lengths=list(payload["clause_lengths"]),
            sentence_clauses=list(payload["sentence_clauses"]),
            bracket_spans=payload.get("bracket_spans", {}),
            total_pairs=int(payload["total_pairs"]),
        )

    # ------------------------------------------------------------ summaries

    @property
    def boundary_count(self) -> int:
        return sum(self.clause_lengths)

    def token_counts(self) -> list[tuple[str, int]]:
        """Corpus occurrences of each punctuation token, most frequent first."""
        totals: Counter[str] = Counter()
        for row in self.left.values():
            for token_id, count in row.items():
                totals[self.tokens[token_id]] += count
        return totals.most_common()

    def summary(self) -> dict[str, float | int]:
        boundaries = self.boundary_count
        sentences = sum(self.sentence_clauses)
        paragraphs = sum(
            count for row in self.left.values() for token_id, count in row.items() if "\n" in self.tokens[token_id]
        )
        return {
            "token_kinds": len(self.tokens),
            "boundaries": boundaries,
            "mean_clause_length": (
                sum(length * count for length, count in enumerate(self.clause_lengths)) / boundaries
                if boundaries
                else 0.0
            ),
            "mean_sentence_clauses": boundaries / sentences if sentences else 0.0,
            "mean_paragraph_sentences": sentences / paragraphs if paragraphs else 0.0,
        }

    # ------------------------------------------------------------- rendering

    def _prepare(self, transitions: Mapping[str, Mapping[str, int]]) -> None:
        if self._prepared_for == id(transitions):
            return
        self._left_pairs: dict[str, int] = {}
        self._right_pairs: Counter[str] = Counter()
        for context, row in transitions.items():
            if context.startswith("<"):
                continue
            self._left_pairs[context] = sum(row.values())
            self._right_pairs.update(row)
        self._left_boundaries = {a: sum(row.values()) for a, row in self.boundary_pairs.items()}
        self._right_boundaries: Counter[str] = Counter()
        for row in self.boundary_pairs.values():
            self._right_boundaries.update(row)
        boundaries = self.boundary_count
        self._rate = boundaries / self.total_pairs if self.total_pairs else 0.0
        totals = [0] * len(self.tokens)
        for row in self.left.values():
            for token_id, count in row.items():
                totals[token_id] += count
        denominator = sum(totals) + len(self.tokens)
        self._global = [(count + 1) / denominator for count in totals]
        self._terminal = [_is_terminal(token) for token in self.tokens]
        terminal_share = sum(c for c, t in zip(totals, self._terminal) if t) / max(sum(totals), 1)
        self._terminal_odds = _odds(terminal_share)
        self._top = sorted(range(len(self.tokens)), key=lambda i: -totals[i])[:_CANDIDATE_TOP]
        self._clause_hazard = _hazards(self.clause_lengths)
        self._sentence_hazard = _hazards(self.sentence_clauses)
        self._spans = {
            opener: (span[1] / span[0]) if span[0] else _DEFAULT_SPAN for opener, span in self.bracket_spans.items()
        }
        self._transitions = transitions
        self._prepared_for = id(transitions)

    def _char_rate(self, boundaries: int, pairs: int) -> float:
        return (boundaries + _CHAR_PRIOR * self._rate) / (pairs + _CHAR_PRIOR)

    def _boundary_probability(self, a: str, b: str, clause_length: int, pressure: float) -> float:
        rate_odds = _odds(self._rate)
        left = self._char_rate(self._left_boundaries.get(a, 0), self._left_pairs.get(a, 0))
        right = self._char_rate(self._right_boundaries[b], self._right_pairs[b])
        backoff_odds = _odds(left) * _odds(right) / rate_odds
        backoff = backoff_odds / (1 + backoff_odds)
        pairs = self._transitions.get(a, {}).get(b, 0)
        hits = self.boundary_pairs.get(a, {}).get(b, 0)
        pair = (hits + _PAIR_PRIOR * backoff) / (pairs + _PAIR_PRIOR)
        hazard = self._clause_hazard[min(clause_length, CLAUSE_CAP)]
        odds = _odds(pair) * _odds(hazard) / rate_odds * math.exp(min(pressure, 20.0))
        return odds / (1 + odds)

    def _pressure(self, stack: tuple[tuple[str, int], ...], position: int) -> float:
        """How overdue the innermost open quote/bracket is, relative to the corpus mean span."""
        if not stack:
            return 0.0
        opener, start = stack[-1]
        mean = self._spans.get(opener, _DEFAULT_SPAN)
        return 2.0 * max(0.0, (position - start) / max(mean, 1.0) - 1.0)

    def _token_weight(self, token_id: int, a: str, b: str) -> float:
        prior = _TOKEN_PRIOR * self._global[token_id]
        left = self.left.get(a, {}).get(token_id, 0) + prior
        right = self.right.get(b, {}).get(token_id, 0) + prior
        return left * right / self._global[token_id]

    def _choose_token(
        self,
        a: str,
        b: str,
        stack: tuple[tuple[str, int], ...],
        clause_index: int,
        pressure: float,
        position: int,
        rng: random.Random,
    ) -> tuple[str, tuple[tuple[str, int], ...]] | None:
        candidates = set(self._top) | set(self.left.get(a, ())) | set(self.right.get(b, ()))
        hazard = self._sentence_hazard[min(clause_index, SENTENCE_CAP)]
        sentence_factor = _odds(hazard) / self._terminal_odds
        options: list[tuple[str, tuple[tuple[str, int], ...]]] = []
        weights: list[float] = []
        for token_id in candidates:
            token = self.tokens[token_id]
            after = _apply_brackets(token, stack, position)
            if after is None:
                continue
            weight = self._token_weight(token_id, a, b)
            if self._terminal[token_id]:
                weight *= sentence_factor
            if stack:
                closes = any(char in CLOSE_TO_OPEN for char in token)
                # An overdue quote/bracket favours closing and suppresses everything else.
                weight *= math.exp(min(pressure, 20.0) * (1 if closes else -1))
            if len(after) > len(stack) and stack:
                weight *= 0.2
            options.append((token, after))
            weights.append(weight)
        return _sample(rng, options, weights)

    def _opening(self, b: str, rng: random.Random) -> tuple[str, tuple[tuple[str, int], ...]]:
        """Paragraph-initial marks (e.g. an opening quote) for the first hanzi."""
        options: list[tuple[str, tuple[tuple[str, int], ...]]] = []
        weights: list[float] = []
        for token_id, token in enumerate(self.tokens):
            if "\n" not in token:
                continue
            tail = token.rsplit("\n", 1)[1]
            after = _apply_brackets(tail, (), 0)
            if after is None:
                continue
            prior = _TOKEN_PRIOR * self._global[token_id]
            options.append((tail, after))
            weights.append(self.right.get(b, {}).get(token_id, 0) + prior)
        return _sample(rng, options, weights) or ("", ())

    def _closing(self, a: str, stack: tuple[tuple[str, int], ...], rng: random.Random) -> str:
        """Sentence-final marks for the last hanzi, closing whatever is still open."""
        options: list[tuple[str, tuple[tuple[str, int], ...]]] = []
        weights: list[float] = []
        for token_id, token in enumerate(self.tokens):
            head = token.split("\n", 1)[0]
            if not _ends_sentence(head):
                continue
            after = _apply_brackets(head, stack, 0)
            if after is None:
                continue
            prior = _TOKEN_PRIOR * self._global[token_id]
            options.append((head, after))
            weights.append(self.left.get(a, {}).get(token_id, 0) + prior)
        head, after = _sample(rng, options, weights) or ("。", stack)
        return head + "".join(OPEN_TO_CLOSE[opener] for opener, _ in reversed(after))

    def render(
        self,
        hanzi: str,
        transitions: Mapping[str, Mapping[str, int]],
        rng: random.Random | None = None,
        progress: Optional[ProgressFn] = None,
    ) -> str:
        """Insert punctuation and paragraph breaks between the hanzi of ``hanzi``."""
        if not hanzi or not self.tokens:
            return hanzi
        # Punctuation carries no data, so a fast PRNG seeded from the OS is enough.
        rng = rng or random.Random(secrets.randbits(128))
        self._prepare(transitions)
        opening, stack = self._opening(hanzi[0], rng)
        output = [opening]
        clause_length = 0
        clauses = 0
        last = len(hanzi) - 1
        for position, a in enumerate(hanzi):
            if position % REPORT_EVERY == 0:
                report(progress, position / len(hanzi), "添加标点")
            output.append(a)
            clause_length += 1
            if position == last:
                break
            b = hanzi[position + 1]
            pressure = self._pressure(stack, position + 1)
            if rng.random() >= self._boundary_probability(a, b, clause_length, pressure):
                continue
            chosen = self._choose_token(a, b, stack, clauses + 1, pressure, position + 1, rng)
            if chosen is None:
                continue
            token, stack = chosen
            output.append(token)
            clause_length = 0
            clauses = 0 if _is_terminal(token) else clauses + 1
        output.append(self._closing(hanzi[-1], stack, rng))
        return "".join(output)
