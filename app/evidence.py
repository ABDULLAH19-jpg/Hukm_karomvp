"""Check that the customer, not the model, picked an option.

The model must quote the customer's words for each option group it fills. We verify the quote was
really said, and that it points at the chosen choice more than at any other choice in the group.
"""
import re

from app.errors import DomainError
from app.menu import MenuItem

# Roman Urdu and plain-English words for the common size/variant names in the menu.
SYNONYMS = {"chota": "small", "chhota": "small", "choti": "small", "chhoti": "small",
            "darmiyana": "medium", "darmiyani": "medium",
            "bara": "large", "bada": "large", "badi": "large", "bari": "large", "big": "large",
            "normal": "regular"}
# A bare "first one" or "haan" answers a list the customer was just read, so it counts as a pick.
ORDINALS_AND_YES = {"first", "second", "third", "fourth", "1st", "2nd", "3rd", "pehli", "pehla", "pehle",
                    "dusri", "dusra", "doosri", "doosra", "tisri", "tisra", "yes", "haan", "han", "ji", "jee",
                    "ok", "okay", "theek", "bilkul", "sure", "wali", "wala", "wahi"}


def tokens(text: str) -> list[str]:
    return [SYNONYMS.get(t, t) for t in re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text.lower())]


_NUMBER_WORDS = {1: {"1", "one", "first", "1st", "pehla", "pehli", "pehle", "aik", "ek"},
                 2: {"2", "two", "second", "2nd", "dusra", "dusri", "doosra", "doosri", "do"},
                 3: {"3", "three", "third", "3rd", "teesra", "teesri", "tisra", "tisri", "teen"}}


def check_place_pick(labels: list[str], number: int, said: str) -> None:
    """When several places were offered, "yes" does not say which. The customer must name the number, or
    words that only the chosen place contains. Raises DomainError."""
    if len(labels) < 2:
        return  # a single place was read back, a plain yes confirms it
    spoken = set(re.findall(r"[a-z0-9]+", said.lower()))
    if spoken & _NUMBER_WORDS.get(number, {str(number)}):
        return
    own = set(tokens(labels[number - 1]))
    others = set().union(*(set(tokens(l)) for i, l in enumerate(labels) if i != number - 1))
    if spoken & (own - others - {"lahore", "pakistan", "district", "urban"}):
        return
    raise DomainError("place_not_chosen", "The customer has not said which place is theirs. Ask them to pick by number: "
                      + "; ".join(f"{i + 1}) {l}" for i, l in enumerate(labels)))


def check_customer_choices(item: MenuItem, selections: list[tuple[int, list[int], str]], heard: list[str]) -> None:
    """selections: (group_id, choice_ids, customer_words). Raises DomainError if the model guessed."""
    said = " ".join(" ".join(re.findall(r"[a-z0-9.]+", h.lower())) for h in heard)
    groups = {g.id: g for g in item.groups}
    for group_id, choice_ids, words in selections:
        group = groups.get(group_id)
        if group is None or len(group.choices) == 1:
            continue  # unknown groups are rejected by the cart rules, a single choice leaves nothing to pick
        ask = DomainError("option_not_chosen",
                          f"The customer has not chosen '{group.name}' for {item.name}. Ask them which one: "
                          + ", ".join(sorted({c.name for c in group.choices})[:8]))
        quote = " ".join(re.findall(r"[a-z0-9.]+", words.lower()))
        if not quote or quote not in said:
            raise ask  # no quote, or words the customer never said
        spoken = set(tokens(quote))
        if spoken <= ORDINALS_AND_YES:
            continue
        counts: dict[str, int] = {}
        per_choice = {c.id: set(tokens(c.name)) for c in group.choices}
        for toks in per_choice.values():
            for t in toks:
                counts[t] = counts.get(t, 0) + 1
        distinct = {cid: {t for t in toks if counts[t] < len(group.choices)} for cid, toks in per_choice.items()}
        score = {cid: len(spoken & toks) for cid, toks in distinct.items()}
        # Customers often type compound menu names without spaces ("reddragon") or add a space
        # inside a name ("cow boy classic"). Accept that only when the compact spelling uniquely
        # identifies the chosen option.
        compact_spoken = "".join(tokens(quote))
        for choice in group.choices:
            compact_choice = "".join(tokens(choice.name))
            if compact_spoken == compact_choice:
                score[choice.id] = max(score.get(choice.id, 0), len(per_choice[choice.id]))
        best = max(score.values())
        for cid in choice_ids:
            if score.get(cid, 0) == 0 or score[cid] < best:
                raise ask


# A plain affirmation of the quoted total. This step only runs after the quote was read back, so a
# bare "haan", "theek" or "confirm" is enough — the point is that it must be the customer's own yes.
# Deliberately excludes ambiguous verbs ("place", "book", "karo") that could match "place order".
CONFIRM_WORDS = {"yes", "yeah", "yep", "yup", "haan", "han", "jee", "ji", "ok", "okay", "theek", "thik",
                 "bilkul", "sure", "confirm", "confirmed", "done", "perfect", "great", "sahi", "zabardast",
                 "hojaye", "hohaye"}


def check_confirm(said: str) -> None:
    """The customer must affirm the quoted total in their own words. Raises DomainError otherwise."""
    if set(tokens(said)) & CONFIRM_WORDS:
        return
    raise DomainError("confirmation_needed",
                      "The customer has not confirmed the quote. Read the items and total back and wait for their yes.")
