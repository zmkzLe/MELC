from __future__ import annotations

import fcntl
import inspect
import json
import os
from enum import Enum
from pathlib import Path
from typing import Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from structure_description import vocabulary

NAME = r"^[a-z][a-z0-9_]{0,30}$"
MIN_MEANING_WORDS = 8


class Place(str, Enum):
    CONDITION = "condition"
    VALUE = "value"
    STEP = "step"
    EXTREMUM = "Extremum"
    TIE_BREAK = "TieBreak"
    SEARCH_SIDE_EFFECT = "SearchSideEffect"
    SIGNATURE_SOURCE = "SignatureSource"
    FOLD_BIT = "FoldBit"
    SIGNATURE_HASH = "SignatureHash"


LIST_PLACES = {p for p in Place if p.value[0].isupper()}
NAME_TYPES = ("state", "table", "signature", "sampler")


class IntRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    int: tuple[int, int]


class SymbolOf(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    symbol: str


ParamType = Union[str, IntRange, SymbolOf]


class NewWord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=NAME)
    place: Place
    params: dict[str, ParamType] = {}
    means: str

    @field_validator("params")
    @classmethod
    def _param_types(cls, params: dict) -> dict:
        import re
        for name, t in params.items():
            if not re.fullmatch(NAME, name):
                raise ValueError(f"parameter name {name!r} does not match {NAME}")
            if isinstance(t, str) and t not in NAME_TYPES + ("access_types",):
                raise ValueError(f"parameter '{name}': type {t!r} is not one of {', '.join(NAME_TYPES)}, "
                                 f"access_types, {{int: [lo, hi]}} or {{symbol: <list>}}")
            if isinstance(t, IntRange) and t.int[0] > t.int[1]:
                raise ValueError(f"parameter '{name}': int range {list(t.int)} is empty")
            if isinstance(t, SymbolOf) and t.symbol not in symbol_lists():
                raise ValueError(f"parameter '{name}': {t.symbol!r} is not a symbol list")
        return params

    def definition(self) -> dict:
        return self.model_dump(mode="json")


def symbol_lists() -> dict[str, type[Enum]]:
    return {n: c for n, c in inspect.getmembers(vocabulary, inspect.isclass)
            if issubclass(c, Enum) and c.__module__ == vocabulary.__name__}


def existing_words() -> set[str]:
    from structure_description import validate
    from pydantic import BaseModel as _BM
    words = {m.value for e in symbol_lists().values() for m in e}
    for _, cls in inspect.getmembers(validate, inspect.isclass):
        if issubclass(cls, _BM) and cls.__module__ == validate.__name__:
            words |= set(cls.model_fields)
    return {w for w in words if isinstance(w, str)} | {"age"}


class Words:
    def __init__(self, words: list[NewWord]):
        self.words = {w.name: w for w in words}

    def at(self, place: Place) -> dict[str, NewWord]:
        return {n: w for n, w in self.words.items() if w.place is place}

    def members(self, list_name: str) -> set[str]:
        return {n for n, w in self.words.items() if w.place.value == list_name}


def parse(text: str) -> list[NewWord]:
    data = yaml.safe_load(text) if text and text.strip() else None
    if data is None:
        return []
    if not isinstance(data, list):
        raise ValueError("the new words must be a YAML list of words")
    return [NewWord.model_validate(d) for d in data]


def check(words: list[NewWord], registry: Union[str, Path, None] = None) -> Words:
    names = [w.name for w in words]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(f"word(s) declared twice: {', '.join(dupes)}")
    clash = sorted(set(names) & existing_words())
    if clash:
        raise ValueError(f"{', '.join(clash)}: already a word of the fixed vocabulary, whose meanings "
                         f"never change; declare a new name instead")
    for w in words:
        if len(w.means.split()) < MIN_MEANING_WORDS:
            raise ValueError(f"'{w.name}': a meaning of {len(w.means.split())} words cannot say exactly "
                             f"what the code does; write at least {MIN_MEANING_WORDS}")
        if w.place in LIST_PLACES and w.params:
            raise ValueError(f"'{w.name}': a member of {w.place.value} takes no parameters")
    if registry is not None:
        _register(words, Path(registry))
    return Words(words)


def _register(words: list[NewWord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with open(path) as fh:
            known = {d["name"]: d for d in map(json.loads, filter(str.strip, fh))}
        changed = sorted(w.name for w in words if w.name in known and known[w.name] != w.definition())
        if changed:
            raise ValueError(f"{', '.join(changed)}: already declared in this run with another definition; "
                             f"a word is never changed, so declare a new name")
        with open(path, "a") as fh:
            for w in words:
                if w.name not in known:
                    fh.write(json.dumps(w.definition(), sort_keys=True) + "\n")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
