"""Evolve an LLC policy design with one of skydiscover's own search algorithms."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run_openevolve import (AI_STUDIO_BASE, VERTEX_REGION,
                            install_refreshing_auth, must_set, vertex_base,
                            vertex_credentials)

NATIVE = {
    "adaevolve": "adaptive multi-island evolution, intensity per island",
    "gepa_native": "guided evolution, acceptance gating and LLM merge",
    "openevolve_native": "MAP-Elites with islands: the controlled comparison",
    "evox": "co-evolves the search strategy beside the solutions",
    "topk": "keep the best k",
    "beam_search": "beam search",
    "best_of_n": "sample n, keep the best",
}


def database_fields(search: str, args) -> dict:
    if search == "adaevolve":
        return {"fitness_key": "combined_score",
                "higher_is_better": {"combined_score": True},
                "population_size": args.population, "num_islands": args.islands,
                "migration_interval": args.migration_interval}
    if search == "gepa_native":
        return {"population_size": args.population, "acceptance_gating": True}
    if search == "openevolve_native":
        return {"population_size": args.population, "num_islands": args.islands,
                "migration_interval": args.migration_interval}
    return {}


def install_marker_extraction(search: str) -> None:
    import importlib

    from skydiscover.utils import code_utils
    from candidate import MARKER, STRUCTURE_DESCRIPTION

    opening = MARKER.format(STRUCTURE_DESCRIPTION)
    original = code_utils.parse_full_rewrite

    import yaml

    words_marker = MARKER.format("NEW_WORDS")

    def from_markers(llm_response: str, language=None):
        start = llm_response.rfind(opening)
        if start == -1:
            return original(llm_response, language)
        text = llm_response[start:]
        fence = text.rfind("```")
        if fence != -1:
            text = text[:fence]
        cut = text.find(words_marker)
        if cut != -1:
            head = text[:cut + len(words_marker)]
            lines = text[cut + len(words_marker):].splitlines()
            while lines:
                try:
                    parsed = yaml.safe_load("\n".join(lines))
                except yaml.YAMLError:
                    lines.pop()
                    continue
                if parsed is None or isinstance(parsed, list):
                    break
                lines.pop()
            return (head + "\n" + "\n".join(lines)).strip()
        return text.strip()

    code_utils.parse_full_rewrite = from_markers
    patched = ["skydiscover.utils.code_utils"]
    for name in ("skydiscover.search.default_discovery_controller",
                 "skydiscover.search.adaevolve.controller",
                 "skydiscover.search.gepa_native.controller",
                 "skydiscover.search.evox.controller"):
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        if getattr(module, "parse_full_rewrite", None) is not None:
            module.parse_full_rewrite = from_markers
            patched.append(name)
    mine = f"skydiscover.search.{search}.controller"
    assert mine in patched or search not in ("adaevolve", "gepa_native"), \
        f"{search} has its own parse_full_rewrite and it was not patched: {patched}"
    print(f"candidate taken from its markers in: "
          f"{', '.join(p.rsplit('.', 2)[-2] + '.' + p.rsplit('.', 1)[-1] for p in patched)}")


def guided_system(system: str, fixed: str, path: Path, rebuild) -> str:
    if system != fixed:
        return system
    return rebuild(guidance=path.read_text() if path.exists() else "")


def install_guidance_native(path: Path, fixed: str) -> None:
    import evolver_prompt
    from skydiscover.search.default_discovery_controller import DiscoveryController

    original = DiscoveryController._call_llm

    async def with_guidance(self, system_message: str, user_message: str, **kwargs):
        return await original(
            self, guided_system(system_message, fixed, path, evolver_prompt.instructions),
            user_message, **kwargs)

    DiscoveryController._call_llm = with_guidance


def set_llm(config, proposers, api_base: str, api_key: str, args) -> None:
    from skydiscover.config import LLMModelConfig

    must_set(config.llm, "api_base", api_base)
    must_set(config.llm, "temperature", args.temperature)
    must_set(config.llm, "max_tokens", args.max_tokens)
    must_set(config.llm, "timeout", args.llm_timeout)
    must_set(config.llm, "retries", args.llm_retries)
    models = [LLMModelConfig(name=n, weight=w, api_key=api_key, api_base=api_base,
                             temperature=args.temperature, max_tokens=args.max_tokens,
                             timeout=args.llm_timeout, retries=args.llm_retries)
              for n, w in proposers]
    must_set(config.llm, "models", models)
    must_set(config.llm, "evaluator_models", list(models))
    must_set(config.llm, "guide_models", list(models))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--search", required=True, choices=sorted(NATIVE),
                    help="; ".join(f"{k}: {v}" for k, v in sorted(NATIVE.items())))
    ap.add_argument("--iterations", type=int, default=100000)
    ap.add_argument("--seed", default="mockingjay")
    ap.add_argument("--model", default="gemini-2.5-pro")
    ap.add_argument("--ensemble", help="model=weight,model=weight; one drawn per proposal")
    ap.add_argument("--vertex", action="store_true",
                    help="call Gemini through Vertex (bills the project's credit)")
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--max-tokens", type=int, default=32_000)
    ap.add_argument("--llm-timeout", type=int, default=600)
    ap.add_argument("--llm-retries", type=int, default=2)
    ap.add_argument("--population", type=int, default=40)
    ap.add_argument("--islands", type=int, default=2)
    ap.add_argument("--migration-interval", type=int, default=50)
    ap.add_argument("--parallel", type=int, default=2,
                    help="candidates evaluated at once")
    ap.add_argument("--eval-timeout", type=int, default=10800,
                    help="seconds for one candidate: a build plus a simulation per trace")
    ap.add_argument("--traces", help="comma-separated stems; the training set by default")
    ap.add_argument("--warmup", type=int)
    ap.add_argument("--sim", type=int)
    ap.add_argument("--max-change", type=int,
                    help="canonical lines a proposal may change")
    ap.add_argument("--guidance", type=Path,
                    help="a file whose text goes into every proposal's prompt, read at "
                         "each one (guidance_writer.py rewrites it beside the search). "
                         "Absent, the prompt is the fixed one")
    ap.add_argument("--run-dir", type=Path,
                    help="where records.jsonl and the candidates go; <out>/yaml_run by default")
    ap.add_argument("--out", type=Path, default=Path.home() / "oe_out")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    os.environ["A3_RUN_DIR"] = str(args.run_dir or args.out / "yaml_run")
    os.environ["A3_RESULT"] = "skydiscover"
    for flag, var in (("traces", "A3_TRACES"), ("warmup", "A3_WARMUP"), ("sim", "A3_SIM"),
                      ("max_change", "A3_MAX_CHANGE")):
        if getattr(args, flag):
            os.environ[var] = str(getattr(args, flag))

    import evolver_prompt
    from chia_llc_loop import EXPLORE, absent

    group = args.traces.split(",") if args.traces else EXPLORE
    missing = absent(group)
    if missing:
        print(f"{len(missing)} trace(s) not on this machine: {', '.join(missing)}")
        return 1

    instructions = evolver_prompt.instructions()
    if args.max_change:
        assert f"at most {args.max_change} canonical lines" in instructions, \
            "the prompt does not carry the step limit"

    proposers = [(args.model, 1.0)]
    if args.ensemble:
        proposers = []
        for item in args.ensemble.split(","):
            name, _, weight = item.strip().partition("=")
            assert name and weight, f"--ensemble item {item!r}: write model=weight"
            proposers.append((name, float(weight)))
        assert all(w > 0 for _, w in proposers) and len({n for n, _ in proposers}) == len(proposers), \
            "--ensemble: weights must be positive and model names distinct"

    if args.vertex:
        api_key, project = vertex_credentials()
        api_base = vertex_base(project)
        proposers = [(n if "/" in n else f"google/{n}", w) for n, w in proposers]
        expect = {"temperature": args.temperature, "seed": None}
        install_refreshing_auth(args.out / "llm_calls.jsonl", expect)
        print(f"LLM: {', '.join(f'{n} (weight {w:g})' for n, w in proposers)} via Vertex "
              f"({project}, {VERTEX_REGION}); usage bills that project's credit, "
              f"token refreshed per request")
    else:
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            print("set GEMINI_API_KEY, or pass --vertex to use the GCP credit")
            return 1
        api_base = AI_STUDIO_BASE
        print(f"LLM: {', '.join(f'{n} (weight {w:g})' for n, w in proposers)} via AI Studio")

    program = args.out / "seed_candidate.txt"
    program.write_text(evolver_prompt.packed_seed(args.seed))
    from design_evaluator import NEW_WORDS, STRUCTURE_DESCRIPTION, split_candidate
    parts = split_candidate(program.read_text())
    assert parts.get(STRUCTURE_DESCRIPTION, "").strip() and NEW_WORDS in parts, \
        f"the seed candidate does not split into its two sections: {sorted(parts)}"
    evaluator_path = HERE / "design_evaluator.py"
    assert evaluator_path.exists(), evaluator_path
    print(f"seed written to {program} ({program.stat().st_size} B); "
          f"evaluator {evaluator_path.name}")

    install_marker_extraction(args.search)
    if args.guidance:
        import evolver_prompt as _ep
        install_guidance_native(args.guidance, _ep.instructions())
        print(f"guidance: every proposal's prompt carries {args.guidance} as it stands at that moment")

    from skydiscover.api import run_discovery
    from skydiscover.config import Config, LLMModelConfig

    config = Config()
    must_set(config, "max_iterations", args.iterations)
    must_set(config, "file_suffix", ".txt")
    must_set(config, "system_prompt_override", instructions)
    must_set(config, "max_solution_length", 200_000)

    must_set(config.search, "type", args.search)
    must_set(config.evaluator, "timeout", args.eval_timeout)
    must_set(config, "diff_based_generation", False)
    must_set(config.evaluator, "cascade_evaluation", False)
    must_set(config, "max_parallel_iterations", args.parallel)

    set_llm(config, proposers, api_base, api_key, args)

    from skydiscover.config import _DB_CONFIG_BY_TYPE
    db_cls = _DB_CONFIG_BY_TYPE.get(args.search)
    if db_cls is not None and not isinstance(config.search.database, db_cls):
        config.search.database = db_cls()
    for field, value in database_fields(args.search, args).items():
        must_set(config.search.database, field, value)

    print(f"effective config: search {config.search.type} "
          f"({NATIVE[args.search]}), iterations {config.max_iterations}, "
          f"evaluator timeout {config.evaluator.timeout}s, "
          f"parallel {config.max_parallel_iterations}, "
          f"diff_based_generation {config.diff_based_generation}, "
          f"cascade {config.evaluator.cascade_evaluation}, "
          f"temperature {config.llm.models[0].temperature}, "
          f"max_tokens {config.llm.models[0].max_tokens}, "
          f"llm timeout {config.llm.models[0].timeout}s, "
          f"retries {config.llm.models[0].retries}, "
          f"proposers {[(m.name, m.weight) for m in config.llm.models]}, "
          f"database {type(config.search.database).__name__}"
          f"{ {k: getattr(config.search.database, k) for k in database_fields(args.search, args)} }")
    print(f"running {args.search} for {args.iterations} iterations")

    result = run_discovery(
        evaluator=str(evaluator_path),
        initial_program=str(program),
        config=config,
        search=args.search,
        iterations=args.iterations,
        output_dir=str(args.out),
        system_prompt=instructions,
        cleanup=False,
    )
    print("\n=== result ===")
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
