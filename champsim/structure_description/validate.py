"""Is this a legal structure description? Computes nothing."""

from __future__ import annotations

import sys
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Union

import yaml

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from structure_description.new_words import IntRange, Place, SymbolOf, Words, symbol_lists

from structure_description.vocabulary import (
    CATEGORY_BOUND,
    TICK_WIDTH,
    AccessType,
    Comparison,
    Event,
    Extremum,
    FieldKind,
    FoldBit,
    InitValue,
    SampledSets,
    Scope,
    SearchSideEffect,
    SignatureHash,
    SignatureSource,
    Threshold,
    TieBreak,
    TrainOp,
    max_value,
    reject_bool,
    resolve_init,
    signed_range,
)

NAME = r"^[a-z][a-z0-9_]{0,30}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _no_bool(v):
    reject_bool(v)
    return v


def _words(info: Union[ValidationInfo, None]) -> Union[Words, None]:
    return (info.context or {}).get("words") if info is not None else None


def _take_declared(cls, data, info: ValidationInfo, place: Place):
    if not isinstance(data, dict):
        return data
    extra = [k for k in data if k not in cls.model_fields]
    if not extra:
        return data
    words = _words(info)
    here = words.at(place) if words else {}
    unknown = [k for k in extra if k not in here]
    if unknown:
        raise ValueError(f"{', '.join(map(repr, unknown))}: not a word of the grammar here, nor a "
                         f"declared {place.value} word")
    out = {k: v for k, v in data.items() if k in cls.model_fields}
    out["words"] = {**dict(out.get("words") or {}), **{k: data[k] or {} for k in extra}}
    return out


def Extendable(enum_cls: type[Enum]):
    return Annotated[Union[enum_cls, str], Field(union_mode="left_to_right")]


def _member_of(list_name: str):
    def check(v, info: ValidationInfo):
        for item in v if isinstance(v, list) else [v]:
            words = _words(info)
            if not isinstance(item, Enum) and (words is None or item not in words.members(list_name)):
                raise ValueError(f"{item!r} is not a {list_name} member: not in the vocabulary and not "
                                 f"a declared new word")
        return v
    return check


class _Field(Strict):
    name: str = Field(pattern=NAME)
    kind: FieldKind = FieldKind.COUNTER
    width: int = Field(ge=1, le=64)
    init: Union[InitValue, int] = InitValue.ZERO

    _nb = field_validator("init", mode="before")(_no_bool)

    @property
    def range(self) -> tuple[int, int]:
        if self.kind is FieldKind.SIGNED:
            return signed_range(self.width)
        return 0, max_value(self.width)

    @model_validator(mode="after")
    def _init_fits(self) -> _Field:
        _check_literal(self.init, self, f"{self.name}: init")
        return self


def _check_literal(value, target: _Field, where: str) -> None:
    if target.kind is FieldKind.SIGNED and not isinstance(value, int) and InitValue(value) is not InitValue.ZERO:
        raise ValueError(f"{where}: '{target.name}' is signed; write the literal, {value} has no meaning for it")
    resolved = resolve_init(value, target.width)
    lo, hi = target.range
    if not lo <= resolved <= hi:
        raise ValueError(
            f"{where}: writes {value!r} into '{target.name}', which is {target.width} bits and holds {lo}..{hi}"
        )


class StateField(_Field):
    scope: Scope


class SamplerField(_Field):
    pass


class ConstValue(Strict):
    const: Union[InitValue, int]
    _nb = field_validator("const", mode="before")(_no_bool)


class TickValue(Strict):
    tick: str


class CounterGatedValue(Strict):
    default: ConstValue
    counter: str
    every_n: int = Field(ge=2, le=1024)
    then: ConstValue


class SignatureValue(Strict):
    signature: str


class AddValue(Strict):
    add: int


class ReadValue(Strict):
    read: str


class TableValue(Strict):
    table: str
    index: str
    div: int = Field(default=1, ge=1)


class DeclaredValue(Strict):
    words: dict[str, dict[str, Any]] = Field(min_length=1, max_length=1)

    @model_validator(mode="before")
    @classmethod
    def _declared(cls, data, info: ValidationInfo):
        return _take_declared(cls, data, info, Place.VALUE)


ValueExpr = Annotated[
    Union[ConstValue, TickValue, CounterGatedValue, SignatureValue, AddValue, ReadValue, TableValue, DeclaredValue],
    Field(union_mode="left_to_right"),
]


class _Compare(Strict):
    cmp: Comparison
    threshold: Union[Threshold, int, None] = None
    _nb = field_validator("threshold", mode="before")(_no_bool)

    @model_validator(mode="after")
    def _threshold_matches_cmp(self) -> _Compare:
        if self.cmp is Comparison.IS_MAX and self.threshold != Threshold.MAX:
            raise ValueError(
                f"cmp: is_max compares against the counter's own maximum, so threshold must "
                f"be MAX, not {self.threshold!r}. As written the threshold is silently "
                f"ignored and the structure description reads as if it mattered."
            )
        if self.cmp is Comparison.ABSENT and self.threshold is not None:
            raise ValueError("cmp: absent asks whether the entry exists; it takes no threshold")
        if self.cmp is not Comparison.ABSENT and self.threshold is None:
            raise ValueError(f"cmp: {self.cmp.value} needs a threshold")
        return self


class CounterPredicate(_Compare):
    counter: str
    abs: bool = False


class TablePredicate(_Compare):
    table: str
    index: str


class TableExceedsVictim(Strict):
    table: str
    index: str
    div: int = Field(default=1, ge=1)


class Condition(Strict):
    type_in: Union[list[AccessType], None] = None
    type_not_in: Union[list[AccessType], None] = None
    counter: Union[CounterPredicate, None] = None
    table: Union[TablePredicate, None] = None
    table_exceeds_victim: Union[TableExceedsVictim, None] = None
    sampled_set: Union[bool, None] = None
    words: dict[str, dict[str, Any]] = {}

    @model_validator(mode="before")
    @classmethod
    def _declared(cls, data, info: ValidationInfo):
        return _take_declared(cls, data, info, Place.CONDITION)

    @model_validator(mode="after")
    def _no_contradiction(self) -> Condition:
        if self.type_in and self.type_not_in and set(self.type_in) & set(self.type_not_in):
            overlap = sorted(t.value for t in set(self.type_in) & set(self.type_not_in))
            raise ValueError(f"type_in and type_not_in overlap on {', '.join(overlap)}")
        return self

    @model_validator(mode="after")
    def _no_clause_that_inverts_itself(self) -> Condition:
        for name in ("type_in", "type_not_in"):
            value = getattr(self, name)
            if value is not None and len(value) == 0:
                raise ValueError(
                    f"{name} is present but empty. That reads as 'match nothing', but an "
                    f"empty clause lowers to no clause at all and the rule would fire on "
                    f"EVERY access -- the opposite. Omit {name} to match always, or name "
                    f"the access types you mean."
                )
        if self.sampled_set is False:
            raise ValueError(
                "sampled_set: false reads as 'only non-sampled sets', but a false flag "
                "lowers to no clause and the rule would fire on every set. Omit "
                "sampled_set to match all sets, or use true to match only sampled ones."
            )
        return self


class Train(Strict):
    table: str
    index: str
    op: TrainOp
    sample: Union[str, None] = None
    scale: Union[int, None] = Field(default=None, ge=1)
    min_diff: Union[int, None] = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _toward_has_a_sample(self) -> Train:
        extras = [k for k in ("sample", "scale", "min_diff") if getattr(self, k) is not None]
        if self.op is TrainOp.TOWARD and self.sample is None:
            raise ValueError("op: toward needs a sample to move toward")
        if self.op is not TrainOp.TOWARD and extras:
            raise ValueError(f"op: {self.op.value} moves by one; {', '.join(extras)} would be ignored")
        return self


class EachWay(Strict):
    when: Condition = Condition()
    except_touched: bool = False
    set: dict[str, ValueExpr] = Field(min_length=1)


class Rule(Strict):
    when: Condition = Condition()
    set: Union[dict[str, ValueExpr], None] = None
    use_duel: bool = False
    sample: Union[str, None] = None
    train: Union[Train, None] = None
    run: Union[str, None] = None
    each_way: Union[EachWay, None] = None
    free: bool = False
    stop: bool = False
    words: dict[str, dict[str, Any]] = {}

    @model_validator(mode="before")
    @classmethod
    def _declared(cls, data, info: ValidationInfo):
        return _take_declared(cls, data, info, Place.STEP)

    @model_validator(mode="after")
    def _one_action(self) -> Rule:
        actions = sum((bool(self.set), self.use_duel, self.sample is not None, self.train is not None,
                       self.run is not None, self.each_way is not None)) + len(self.words)
        if actions > 1 or (actions == 0 and not (self.stop or self.free)):
            raise ValueError(
                "a rule needs exactly one of `set`, `use_duel`, `sample`, `train`, `run`, "
                "`each_way` or a declared step word, or only `stop` / `free`"
            )
        return self


class VictimSelector(Strict):
    key: str
    extremum: Extendable(Extremum)
    tie_break: Extendable(TieBreak) = TieBreak.FIRST
    on_search: Extendable(SearchSideEffect) = SearchSideEffect.NONE
    bypass_when: Union[list[Condition], None] = None

    _ex = field_validator("extremum")(_member_of("Extremum"))
    _tb = field_validator("tie_break")(_member_of("TieBreak"))
    _os = field_validator("on_search")(_member_of("SearchSideEffect"))


class SamplerVictim(Strict):
    key: str
    extremum: Extendable(Extremum)
    tie_break: Extendable(TieBreak) = TieBreak.FIRST
    prefer_free: bool = False

    _ex = field_validator("extremum")(_member_of("Extremum"))
    _tb = field_validator("tie_break")(_member_of("TieBreak"))


class DuelArm(Strict):
    set: dict[str, ValueExpr]


class Duel(Strict):
    selector: str
    arm_a: DuelArm
    arm_b: DuelArm
    leader_a_category: int = 1
    leader_b_category: int = 0
    leader_a_delta: int = Field(default=1, ge=-1, le=1)
    leader_b_delta: int = Field(default=-1, ge=-1, le=1)
    follower_uses_b_when: CounterPredicate


class Signature(Strict):
    name: str = Field(pattern=NAME)
    source: Extendable(SignatureSource)
    fold: list[Extendable(FoldBit)] = []
    hash: Extendable(SignatureHash) = SignatureHash.NONE
    low_bits: int = Field(ge=1, le=64)
    modulo: Union[int, None] = Field(default=None, ge=2, le=1 << 20)

    _src = field_validator("source")(_member_of("SignatureSource"))
    _fold = field_validator("fold")(_member_of("FoldBit"))
    _hash = field_validator("hash")(_member_of("SignatureHash"))

    @model_validator(mode="after")
    def _fold_once(self) -> Signature:
        if len(set(self.fold)) != len(self.fold):
            raise ValueError(f"signature '{self.name}' folds a bit twice")
        return self

    @property
    def values(self) -> int:
        return self.modulo if self.modulo is not None else 1 << self.low_bits


class Table(_Field):
    scope: Scope
    entries: int = Field(ge=2, le=1 << 20)
    unset: bool = False
    max: Union[int, None] = Field(default=None, ge=1)

    @property
    def range(self) -> tuple[int, int]:
        return 0, self.max if self.max is not None else max_value(self.width)

    @model_validator(mode="after")
    def _table_shape(self) -> Table:
        if self.scope not in (Scope.PER_CPU, Scope.GLOBAL):
            raise ValueError(
                f"table '{self.name}' is scope {self.scope.value}; a table is indexed by a "
                f"signature, so it is per_cpu or global"
            )
        if self.kind is FieldKind.SIGNED:
            raise ValueError(f"table '{self.name}' is signed; a table holds unsigned counters")
        if self.max is not None and self.max > max_value(self.width):
            raise ValueError(f"table '{self.name}': max {self.max} does not fit in {self.width} bits")
        if self.unset and resolve_init(self.init, self.width) != 0:
            raise ValueError(f"table '{self.name}' is unset, so its entries have no initial value; drop init")
        return self


class MirrorSets(Strict):
    mirror: int = Field(ge=1)

    @model_validator(mode="after")
    def _power_of_two(self) -> MirrorSets:
        if self.mirror & (self.mirror - 1):
            raise ValueError(f"mirror: {self.mirror} is not a power of two")
        return self


class Age(Strict):
    clock: str
    stamp: str


class Sampler(Strict):
    name: str = Field(pattern=NAME)
    scope: Scope
    sets: Union[SampledSets, MirrorSets]
    extra_index_bits: int = Field(default=0, ge=0, le=16)
    ways: int = Field(ge=1, le=64)
    tag_bits: int = Field(ge=1, le=64)
    fields: list[SamplerField] = Field(min_length=1)
    age: Union[Age, None] = None
    victim: SamplerVictim
    on_match: list[Rule] = []
    sweep: list[Rule] = []
    on_replace: list[Rule] = []
    install: dict[str, ValueExpr] = Field(min_length=1)
    after: list[Rule] = []

    @model_validator(mode="after")
    def _indexed_scope(self) -> Sampler:
        if self.scope not in (Scope.PER_CPU, Scope.GLOBAL):
            raise ValueError(f"sampler '{self.name}' is scope {self.scope.value}; it is per_cpu or global")
        return self


class StructureDescription(Strict):
    name: str = Field(pattern=NAME)
    doc: Union[str, None] = None

    state: list[StateField] = Field(min_length=1)
    victim: VictimSelector

    duel: Union[Duel, None] = None
    signatures: list[Signature] = []
    tables: list[Table] = []
    samplers: list[Sampler] = []
    procedures: dict[str, list[Rule]] = {}

    on_access: list[Rule] = []
    on_hit: list[Rule] = []
    on_fill: list[Rule] = []

    @model_validator(mode="after")
    def _referential_integrity(self, info: ValidationInfo) -> StructureDescription:
        by_name = {f.name: f for f in self.state}
        if len(by_name) != len(self.state):
            seen: set[str] = set()
            dupes = sorted({f.name for f in self.state if f.name in seen or seen.add(f.name)})
            raise ValueError(f"duplicate state field name(s): {', '.join(dupes)}")

        self._check_names()
        self._check_victim(by_name)
        self._check_rules(by_name)
        self._check_duel(by_name)
        self._check_samplers(by_name)
        self._check_declared(_words(info))
        return self


    def _names_in_scope(self, where: str) -> dict[str, set[str]]:
        names = {"state": {f.name for f in self.state}, "table": {t.name for t in self.tables},
                 "signature": {s.name for s in self.signatures}, "sampler": {s.name for s in self.samplers}}
        inside = __import__("re").match(r"samplers\[(\d+)\]\.(\w+)", where)
        if inside is None:
            return names
        sampler, section = self.samplers[int(inside.group(1))], inside.group(2)
        outer = {f.name for f in self.state if f.scope is not Scope.PER_LINE}
        if section == "after":
            return {**names, "state": outer}
        entry = {f.name for f in sampler.fields}
        return {**names, "state": outer | entry | ({"age"} if sampler.age is not None else set()),
                "signature": names["signature"] | entry}

    def _check_declared(self, words: Union[Words, None]) -> None:
        places = {Condition: Place.CONDITION, Rule: Place.STEP, DeclaredValue: Place.VALUE}
        for where, node in _walk(self, ""):
            place = places.get(type(node))
            if not place or not node.words:
                continue
            here = self._names_in_scope(where)
            for word, args in node.words.items():
                declared = words.at(place).get(word) if words else None
                if declared is None:
                    raise ValueError(f"{where}: '{word}' is not a declared {place.value} word")
                if set(args) != set(declared.params):
                    raise ValueError(f"{where}: '{word}' takes {sorted(declared.params) or 'no parameters'}, "
                                     f"got {sorted(args) or 'none'}")
                for p, t in declared.params.items():
                    _check_argument(f"{where}: '{word}' {p}", args[p], t, here, words)


    def _check_names(self) -> None:
        top = [x.name for group in (self.state, self.signatures, self.tables, self.samplers) for x in group]
        top += list(self.procedures)
        dupes = sorted({n for n in top if top.count(n) > 1})
        if dupes:
            raise ValueError(
                f"name(s) {', '.join(dupes)} declared twice across state, signatures, tables, "
                f"samplers and procedures; rules refer to all of them by name"
            )
        for s in self.samplers:
            own = [f.name for f in s.fields] + (["age"] if s.age is not None else [])
            clash = sorted({n for n in own if own.count(n) > 1} | (set(own) & set(top)))
            if clash:
                raise ValueError(
                    f"sampler '{s.name}': field name(s) {', '.join(clash)} declared twice or "
                    f"shadowing a top-level name; its rules could not tell them apart"
                )
        for name in self.procedures:
            if not __import__("re").fullmatch(NAME, name):
                raise ValueError(f"procedure name {name!r} does not match {NAME}")


    def _check_selector(self, sel, key: _Field, where: str) -> None:
        if sel.extremum is Extremum.MAX_MAGNITUDE and key.kind is not FieldKind.SIGNED:
            raise ValueError(
                f"{where}: max_magnitude compares |value|, which needs a signed key; "
                f"'{key.name}' is {key.kind.value}"
            )
        if sel.tie_break is TieBreak.PREFER_NEGATIVE and sel.extremum is not Extremum.MAX_MAGNITUDE:
            raise ValueError(
                f"{where}: prefer_negative breaks ties between values of equal magnitude and "
                f"opposite sign, which only a max_magnitude search produces"
            )

    def _check_victim(self, by_name: dict[str, StateField]) -> None:
        key = by_name.get(self.victim.key)
        if key is None:
            raise ValueError(f"victim.key '{self.victim.key}' is not a state field")
        if key.scope is not Scope.PER_LINE:
            raise ValueError(
                f"victim.key '{key.name}' must be per_line, got {key.scope.value}. The victim "
                f"search compares ways within one set; a field that is not per-line has one "
                f"value for the whole set and cannot distinguish them."
            )
        if self.victim.on_search is SearchSideEffect.AGE_TO_MAX and self.victim.extremum is not Extremum.MAX:
            raise ValueError(
                "age_to_max requires extremum: max. It adds (MAX - found) to every way, which "
                "only makes sense when the victim is the largest value."
            )
        self._check_selector(self.victim, key, "victim")

        if self.victim.bypass_when is not None:
            if not self.victim.bypass_when:
                raise ValueError("victim.bypass_when is present but empty; drop it to never bypass")
            for i, cond in enumerate(self.victim.bypass_when):
                where = f"victim.bypass_when[{i}]"
                if cond.type_in and AccessType.WRITE in cond.type_in:
                    raise ValueError(f"{where}: a WRITE never bypasses (ChampSim asserts it), so this clause is dead")
                self._check_condition(cond, where, by_name, {}, bypass=True)


    def _check_value(self, v, where: str, target: _Field, by_name: dict[str, StateField],
                     readable: dict[str, _Field], fields: dict[str, SamplerField]) -> None:
        if isinstance(v, ConstValue):
            _check_literal(v.const, target, where)

        elif isinstance(v, TickValue):
            clock = by_name.get(v.tick)
            if clock is None or clock.scope is not Scope.GLOBAL:
                raise ValueError(f"{where}: tick source '{v.tick}' must be a global state field")
            if clock.width != TICK_WIDTH:
                raise ValueError(
                    f"{where}: tick counter '{clock.name}' is {clock.width} bits and must be "
                    f"{TICK_WIDTH}. A narrower counter wraps mid-run, stamps start repeating, "
                    f"and the victim search reads a freshly-wrapped small stamp as older than "
                    f"an old large one -- LRU would evict its most recently used lines while "
                    f"still looking healthy."
                )
            if target.width < clock.width:
                raise ValueError(
                    f"{where}: '{target.name}' is {target.width} bits but stores stamps from "
                    f"'{clock.name}', which is {clock.width} bits. Stamps would be truncated "
                    f"on arrival and the victim search would misorder lines."
                )

        elif isinstance(v, CounterGatedValue):
            ctr = by_name.get(v.counter)
            if ctr is None:
                raise ValueError(f"{where}: counter '{v.counter}' is not a state field")
            if max_value(ctr.width) < v.every_n:
                raise ValueError(
                    f"{where}: the gate fires every {v.every_n}, but counter '{ctr.name}' is "
                    f"{ctr.width} bits and only reaches {max_value(ctr.width)}. It would wrap "
                    f"past the trigger and never match, silently disabling the gate -- BRRIP "
                    f"would lose its bimodality while still looking fine."
                )
            _check_literal(v.default.const, target, where)
            _check_literal(v.then.const, target, where)

        elif isinstance(v, SignatureValue):
            sig = next((s for s in self.signatures if s.name == v.signature), None)
            if sig is None:
                raise ValueError(f"{where}: signature '{v.signature}' is not declared")
            if target.range[1] < sig.values - 1:
                raise ValueError(
                    f"{where}: '{target.name}' is {target.width} bits but signature "
                    f"'{sig.name}' reaches {sig.values - 1}. The stored signature would be "
                    f"truncated and train the wrong table entry."
                )

        elif isinstance(v, AddValue):
            lo, hi = target.range
            if v.add == 0 or not lo <= v.add <= hi:
                raise ValueError(
                    f"{where}: add {v.add} to '{target.name}', which holds {lo}..{hi}; a step "
                    f"of 0 or one outside the range is not a step"
                )

        elif isinstance(v, ReadValue):
            src = readable.get(v.read)
            if src is None:
                raise ValueError(
                    f"{where}: read source '{v.read}' must be a per_set, per_cpu or global state "
                    f"field" + (" or a field of this sampler" if fields else "")
                )
            if (src.kind is FieldKind.SIGNED) != (target.kind is FieldKind.SIGNED) or src.width > target.width:
                raise ValueError(
                    f"{where}: '{target.name}' ({target.kind.value}, {target.width} bits) cannot hold "
                    f"every value of '{src.name}' ({src.kind.value}, {src.width} bits)"
                )

        elif isinstance(v, TableValue):
            t = self._check_table_use(v.table, v.index, where, fields)
            if t.range[1] // v.div > target.range[1]:
                raise ValueError(
                    f"{where}: '{t.name}' / {v.div} reaches {t.range[1] // v.div}, which "
                    f"'{target.name}' ({target.width} bits, {target.range[0]}..{target.range[1]}) cannot hold"
                )

    def _check_predicate(self, p: CounterPredicate, where: str, by_name: dict[str, _Field]) -> None:
        if p.cmp is Comparison.ABSENT:
            raise ValueError(f"{where}: absent applies to a table entry; a counter always has a value")
        ctr = by_name.get(p.counter)
        if ctr is None:
            raise ValueError(f"{where}: unknown counter '{p.counter}' in condition")
        signed = ctr.kind is FieldKind.SIGNED
        if p.abs and not signed:
            raise ValueError(f"{where}: abs compares |value|, and '{ctr.name}' is {ctr.kind.value}, never negative")
        if signed and not isinstance(p.threshold, int) and Threshold(p.threshold) is not Threshold.ZERO:
            raise ValueError(f"{where}: '{ctr.name}' is signed; write the threshold as a literal")
        lo, hi = ctr.range
        if p.abs:
            lo, hi = 0, -lo if -lo > hi else hi
        if isinstance(p.threshold, int) and not lo <= p.threshold <= hi:
            raise ValueError(
                f"{where}: threshold {p.threshold!r} is outside '{ctr.name}''s range ({lo}..{hi}). "
                f"An EQ or LT test on it can never fire and a GT test fires always -- a rule "
                f"that is never a rule."
            )

    def _check_table_use(self, table: str, index: str, where: str, fields: dict[str, SamplerField]) -> Table:
        t = next((t for t in self.tables if t.name == table), None)
        if t is None:
            raise ValueError(f"{where}: table '{table}' is not declared")
        sig = next((s for s in self.signatures if s.name == index), None)
        if sig is not None:
            values = sig.values
        elif index in fields:
            values = 1 << fields[index].width
        else:
            raise ValueError(
                f"{where}: index '{index}' is not a declared signature"
                + (" or a field of this sampler" if fields else "")
            )
        if values > t.entries:
            raise ValueError(
                f"{where}: index '{index}' takes {values} values but table '{t.name}' has "
                f"{t.entries} entries. The excess indices read and train memory past the table."
            )
        return t

    def _check_table_predicate(self, p: TablePredicate, where: str, fields: dict[str, SamplerField]) -> None:
        t = self._check_table_use(p.table, p.index, where, fields)
        if p.cmp is Comparison.ABSENT and not t.unset:
            raise ValueError(f"{where}: table '{t.name}' has no absent entries; declare unset: true or test a value")
        if isinstance(p.threshold, int) and not 0 <= p.threshold <= t.range[1]:
            raise ValueError(
                f"{where}: threshold {p.threshold!r} is outside table '{t.name}''s range (0..{t.range[1]})"
            )

    def _check_train(self, tr: Train, where: str, fields: dict[str, SamplerField], readable: dict[str, _Field]) -> None:
        self._check_table_use(tr.table, tr.index, where, fields)
        if tr.sample is not None and tr.sample not in readable:
            raise ValueError(
                f"{where}: sample '{tr.sample}' must be a per_set, per_cpu or global state field"
                + (", a field of this sampler or its age" if fields else "")
            )

    def _check_condition(self, cond: Condition, where: str, by_name: dict[str, StateField],
                         fields: dict[str, SamplerField], readable: Union[dict, None] = None, bypass: bool = False) -> None:
        if cond.counter is not None:
            self._check_predicate(cond.counter, where, readable if readable is not None else by_name)
        if cond.table is not None:
            self._check_table_predicate(cond.table, where, fields)
        if cond.table_exceeds_victim is not None:
            if not bypass:
                raise ValueError(f"{where}: table_exceeds_victim compares with the victim search; it belongs in victim.bypass_when")
            self._check_table_use(cond.table_exceeds_victim.table, cond.table_exceeds_victim.index, where, fields)


    def _check_rules(self, by_name: dict[str, StateField]) -> None:
        outer = {n: f for n, f in by_name.items() if f.scope is not Scope.PER_LINE}
        used: set[str] = set()
        for event in Event:
            self._check_rule_list(getattr(self, event.value), event, event.value, by_name, outer, used)
        unused = sorted(set(self.procedures) - used)
        if unused:
            raise ValueError(f"procedure(s) {', '.join(unused)} declared but never run by any event")

    def _check_rule_list(self, rules: list[Rule], event: Event, prefix: str, by_name: dict[str, StateField],
                         outer: dict[str, StateField], used: set[str], in_procedure: bool = False) -> None:
        samplers = {s.name for s in self.samplers}
        for i, rule in enumerate(rules):
            where = f"{prefix}[{i}]"

            if rule.free:
                raise ValueError(f"{where}: free invalidates a sampler entry; it belongs in a sampler's on_match or sweep")
            if rule.use_duel and self.duel is None:
                raise ValueError(f"{where}: use_duel, but no duel block is declared")
            if rule.sample is not None and rule.sample not in samplers:
                raise ValueError(f"{where}: sampler '{rule.sample}' is not declared")
            if rule.run is not None:
                if in_procedure:
                    raise ValueError(f"{where}: a procedure may not run another procedure")
                if rule.run not in self.procedures:
                    raise ValueError(f"{where}: procedure '{rule.run}' is not declared")
                used.add(rule.run)
                self._check_rule_list(self.procedures[rule.run], event, f"{where} -> procedures.{rule.run}",
                                      by_name, outer, used, in_procedure=True)

            self._check_condition(rule.when, where, by_name, {})
            if rule.train is not None:
                self._check_train(rule.train, where, {}, outer)

            if rule.each_way is not None:
                self._check_condition(rule.each_way.when, where, by_name, {})
                for fname, value in rule.each_way.set.items():
                    f = by_name.get(fname)
                    if f is None:
                        raise ValueError(f"{where}: unknown target field '{fname}'")
                    if f.scope is not Scope.PER_LINE:
                        raise ValueError(f"{where}: each_way writes every way of the set; '{fname}' is {f.scope.value}, not per_line")
                    self._check_value(value, where, f, by_name, outer, {})

            targets = dict(rule.set or {})
            if rule.use_duel and self.duel is not None:
                for arm in (self.duel.arm_a, self.duel.arm_b):
                    targets.update(arm.set)

            for fname, value in targets.items():
                if fname not in by_name:
                    raise ValueError(f"{where}: unknown target field '{fname}'")
                self._reject_per_line_on_access(event, where, by_name[fname])
                if fname in (rule.set or {}):
                    self._check_value(value, where, by_name[fname], by_name, outer, {})

    def _reject_per_line_on_access(self, event: Event, where: str, target: StateField) -> None:
        if event is Event.ON_ACCESS and target.scope is Scope.PER_LINE:
            raise ValueError(
                f"{where}: writes per_line field '{target.name}' on ON_ACCESS. That event fires "
                f"on misses, where way == NUM_WAY and the write lands on the next set instead "
                f"of throwing. Put way-indexed writes in on_hit or on_fill."
            )


    def _check_duel(self, by_name: dict[str, StateField]) -> None:
        if self.duel is None:
            return

        referenced = any(
            r.use_duel for e in Event for r in getattr(self, e.value)
        ) or any(r.use_duel for rules in self.procedures.values() for r in rules)
        if not referenced:
            raise ValueError("duel block declared but never referenced by any rule")

        sel = by_name.get(self.duel.selector)
        if sel is None:
            raise ValueError(f"duel.selector '{self.duel.selector}' is not a state field")
        if sel.kind is not FieldKind.SATURATING:
            raise ValueError(
                f"duel.selector '{sel.name}' is kind {sel.kind.value}, must be saturating. The "
                f"selector is what keeps the duel stable: a wrapping counter rolls past the "
                f"threshold and both arms flip. DRRIP uses fwcounter<10> precisely because it "
                f"clamps at its extremes."
            )
        if sel.scope is not Scope.PER_CPU:
            raise ValueError(
                f"duel.selector '{sel.name}' is scope {sel.scope.value}, must be per_cpu. A duel "
                f"arbitrates between leader sets via a counter updated on fills."
            )

        if self.duel.leader_a_category == self.duel.leader_b_category:
            raise ValueError(
                f"both leader groups claim set category {self.duel.leader_a_category}. One arm "
                f"would be unreachable."
            )
        for label in ("leader_a_category", "leader_b_category"):
            cat = getattr(self.duel, label)
            if not 0 <= cat < CATEGORY_BOUND:
                raise ValueError(
                    f"duel.{label} is {cat}, must be in 0..{CATEGORY_BOUND - 1}. "
                    f"get_set_sample_category returns values below the set sample rate, which "
                    f"is at most {CATEGORY_BOUND}; a category no set can produce is a dead arm."
                )

        self._check_predicate(self.duel.follower_uses_b_when, "duel.follower_uses_b_when", by_name)

        outer = {n: f for n, f in by_name.items() if f.scope is not Scope.PER_LINE}
        for label in ("arm_a", "arm_b"):
            arm: DuelArm = getattr(self.duel, label)
            for fname, value in arm.set.items():
                if fname not in by_name:
                    raise ValueError(f"duel.{label}: unknown target field '{fname}'")
                self._check_value(value, f"duel.{label}", by_name[fname], by_name, outer, {})


    def _check_samplers(self, by_name: dict[str, StateField]) -> None:
        for s in self.samplers:
            fields = {f.name: f for f in s.fields}
            outer = {n: f for n, f in by_name.items() if f.scope is not Scope.PER_LINE}
            readable: dict[str, _Field] = {**outer, **fields}
            name = f"sampler '{s.name}'"

            if s.age is not None:
                clock, stamp = outer.get(s.age.clock), fields.get(s.age.stamp)
                if clock is None:
                    raise ValueError(f"{name}: age.clock '{s.age.clock}' must be a per_set, per_cpu or global state field")
                if stamp is None:
                    raise ValueError(f"{name}: age.stamp '{s.age.stamp}' must be a field of the sampler")
                if clock.width != stamp.width or clock.kind is FieldKind.SIGNED or stamp.kind is FieldKind.SIGNED:
                    raise ValueError(
                        f"{name}: age is a modular difference, so clock and stamp must be unsigned "
                        f"and one width; got {clock.width} and {stamp.width} bits"
                    )
                readable["age"] = SamplerField(name="age", width=stamp.width)

            key = readable.get(s.victim.key) if s.victim.key in fields or s.victim.key == "age" else None
            if key is None:
                raise ValueError(f"{name}: victim.key '{s.victim.key}' is not a field of the sampler or its age")
            self._check_selector(s.victim, key, f"{name} victim")

            for event in ("on_match", "sweep", "on_replace", "after"):
                per_entry = event != "after"
                ctx = readable if per_entry else outer
                ctx_fields = fields if per_entry else {}
                for i, rule in enumerate(getattr(s, event)):
                    where = f"{name} {event}[{i}]"
                    if rule.use_duel or rule.sample is not None or rule.run is not None or rule.each_way is not None:
                        raise ValueError(f"{where}: a sampler rule can only set, train, free or stop")
                    if rule.free and event in ("on_replace", "after"):
                        raise ValueError(f"{where}: free applies to an entry that stays; use it in on_match or sweep")
                    self._check_condition(rule.when, where, by_name, ctx_fields, readable=ctx)
                    if rule.train is not None:
                        self._check_train(rule.train, where, ctx_fields, ctx)
                    for fname, value in (rule.set or {}).items():
                        target = ctx.get(fname)
                        if target is None or fname == "age":
                            raise ValueError(
                                f"{where}: '{fname}' is not a field of the sampler or a per_set, "
                                f"per_cpu or global state field" + ("" if per_entry else " (after has no entry)")
                            )
                        self._check_value(value, where, target, by_name, ctx, ctx_fields)

            for fname, value in s.install.items():
                if fname not in fields:
                    raise ValueError(f"{name} install: '{fname}' is not a field of the sampler")
                self._check_value(value, f"{name} install", fields[fname], by_name, readable, fields)
            missing = sorted(set(fields) - set(s.install))
            if missing:
                raise ValueError(f"{name} install: leaves {', '.join(missing)} holding the evicted entry's value")

    @property
    def guards_update_on_hit(self) -> bool:
        return not self.on_access


class _NoDuplicateKeys(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                mark = key_node.start_mark
                raise ValueError(
                    f"duplicate key {key!r} at line {mark.line + 1}, column {mark.column + 1}. "
                    f"yaml would silently keep the last one and the structure description "
                    f"would not mean what it appears to say."
                )
            seen.add(key)
        return super().construct_mapping(node, deep)


def _walk(obj, path: str):
    if isinstance(obj, BaseModel):
        yield path or "description", obj
        for k in type(obj).model_fields:
            yield from _walk(getattr(obj, k), f"{path}.{k}" if path else k)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")


def _check_argument(where: str, value, typ, names: dict[str, set[str]], words: Words) -> None:
    if isinstance(typ, str) and typ in names:
        if value not in names[typ]:
            raise ValueError(f"{where}: {value!r} is not a declared {typ}")
    elif typ == "access_types":
        allowed = {t.value for t in AccessType}
        if not isinstance(value, list) or not value or not set(value) <= allowed:
            raise ValueError(f"{where}: {value!r} is not a non-empty list of {', '.join(sorted(allowed))}")
    elif isinstance(typ, IntRange):
        lo, hi = typ.int
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise ValueError(f"{where}: {value!r} is not an integer in {lo}..{hi}")
    elif isinstance(typ, SymbolOf):
        members = {m.value for m in symbol_lists()[typ.symbol]} | words.members(typ.symbol)
        if value not in members:
            raise ValueError(f"{where}: {value!r} is not a {typ.symbol} member")


def load(path: str, words: Union[Words, None] = None) -> StructureDescription:
    with open(path) as fh:
        return parse(fh.read(), words)


def parse(text: str, words: Union[Words, None] = None) -> StructureDescription:
    return StructureDescription.model_validate(yaml.load(text, Loader=_NoDuplicateKeys), context={"words": words})


if __name__ == "__main__":
    import argparse

    from structure_description import new_words

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--words", help="the candidate's new words, a YAML list")
    args = ap.parse_args()
    declared = new_words.check(new_words.parse(Path(args.words).read_text())) if args.words else None
    for p in args.paths:
        g = load(p, declared)
        print(f"ok  {p}  ->  {g.name}")
