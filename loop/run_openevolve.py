"""Evolve an LLC replacement policy with OpenEvolve (CHIA hackathon)."""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from candidate import INSTRUCTIONS, packed_seed

AI_STUDIO_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"

VERTEX_REGION = os.environ.get("A3_VERTEX_REGION", "us-central1")


def vertex_base(project: str) -> str:
    host = "aiplatform.googleapis.com" if VERTEX_REGION == "global" else f"{VERTEX_REGION}-aiplatform.googleapis.com"
    return f"https://{host}/v1/projects/{project}/locations/{VERTEX_REGION}/endpoints/openapi"


def must_set(obj, field: str, value) -> None:
    if not hasattr(obj, field):
        raise AttributeError(
            f"{type(obj).__name__} has no field {field!r}; it was renamed or "
            f"never existed, and assigning it would silently do nothing")
    setattr(obj, field, value)


SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def vertex_credentials() -> tuple[str, str]:
    import google.auth
    import google.auth.transport.requests

    creds, project = google.auth.default(scopes=SCOPES)
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token, os.environ.get("GOOGLE_CLOUD_PROJECT") or project


def log_usage(path: Path, req, resp, seconds: float) -> None:
    try:
        body = json.loads(req.content or b"{}")
        usage = (resp.json() if resp.status_code == 200 else {}).get("usage") or {}
        system = next((m.get("content") or "" for m in body.get("messages") or [] if m.get("role") == "system"), "")
        line = {"time": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "model": body.get("model"), "temperature": body.get("temperature"), "seed": body.get("seed"),
                "status": resp.status_code, "seconds": round(seconds, 1), "system_chars": len(system),
                "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                "total_tokens": usage.get("total_tokens")}
        with open(path, "a") as fh:
            fh.write(json.dumps(line) + "\n")
    except Exception:
        pass


def check_request(req, expect: dict) -> None:
    body = json.loads(req.content or b"{}")
    sent = {"temperature": body.get("temperature"), "seed": body.get("seed")}
    if sent != expect:
        raise RuntimeError(f"the completion request carries {sent}, not the configured {expect}")


def install_refreshing_auth(usage_log: Path | None = None, expect: dict | None = None) -> None:
    import google.auth
    import google.auth.transport.requests
    import httpx
    import openai

    creds, _ = google.auth.default(scopes=SCOPES)
    request = google.auth.transport.requests.Request()

    class _Refreshing(httpx.Auth):
        requires_request_body = usage_log is not None or expect is not None
        requires_response_body = usage_log is not None

        def auth_flow(self, req):
            if not creds.valid:
                creds.refresh(request)
            req.headers["Authorization"] = f"Bearer {creds.token}"
            if expect is not None:
                check_request(req, expect)
            t0 = time.time()
            resp = yield req
            if usage_log is not None:
                log_usage(usage_log, req, resp, time.time() - t0)

    for cls, client in ((openai.OpenAI, httpx.Client),
                        (openai.AsyncOpenAI, httpx.AsyncClient)):
        original = cls.__init__

        def patched(self, *a, _orig=original, _client=client, **kw):
            kw.setdefault("http_client", _client(auth=_Refreshing(), timeout=600.0))
            _orig(self, *a, **kw)

        cls.__init__ = patched


def install_guidance(path: Path, fixed: str) -> None:
    import evolver_prompt
    from openevolve.prompt.sampler import PromptSampler

    original = PromptSampler.build_prompt

    def with_guidance(self, *a, **kw):
        out = original(self, *a, **kw)
        if self.system_template_override is None:
            assert out["system"] == fixed, "the system message being replaced is not the fixed prompt"
            out["system"] = evolver_prompt.instructions(guidance=path.read_text() if path.exists() else "")
        return out

    PromptSampler.build_prompt = with_guidance


def install_prompt_log(out: Path) -> None:
    import fcntl
    import hashlib
    from openevolve.prompt.sampler import PromptSampler

    (out / "prompts").mkdir(parents=True, exist_ok=True)
    original = PromptSampler.build_prompt
    warned: list[bool] = []

    def logged(self, *a, **kw):
        result = original(self, *a, **kw)
        if self.system_template_override is not None:
            return result
        try:
            system, user = result.get("system", ""), result.get("user", "")
            sha = hashlib.sha256(system.encode()).hexdigest()[:12]
            keep = out / "prompts" / f"system_{sha}.txt"
            if not keep.exists():
                tmp = keep.with_name(f".{keep.name}.{os.getpid()}")
                tmp.write_text(system)
                os.replace(tmp, keep)
            arts = kw.get("program_artifacts") or {}
            metrics = kw.get("program_metrics") or {}
            from openevolve import process_parallel
            wc = getattr(process_parallel, "_worker_config", None)
            line = {"time": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "pid": os.getpid(), "round": kw.get("evolution_round"),
                    "parent_sha": hashlib.sha256((kw.get("parent_program") or "").encode()).hexdigest()[:12],
                    "parent_score": metrics.get("combined_score"), "parent_status": metrics.get("status"),
                    "artifacts": sorted(arts), "artifacts_chars": sum(len(v) for v in arts.values()
                                                                      if isinstance(v, (str, bytes))),
                    "snapshot_cap": wc.database.max_snapshot_artifacts if wc is not None else "no worker config",
                    "system_sha": sha, "system_chars": len(system), "user_chars": len(user), "user": user}
            with open(out / "prompts.jsonl", "a") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                fh.write(json.dumps(line, default=str) + "\n")
                fcntl.flock(fh, fcntl.LOCK_UN)
        except Exception as exc:
            if not warned:
                warned.append(True)
                print(f"prompt log failed: {exc!r}", file=sys.stderr)
        return result

    PromptSampler.build_prompt = logged


def install_migrant_artifacts() -> None:
    from openevolve.database import ProgramDatabase

    original = ProgramDatabase.add

    def add(self, program, *a, **kw):
        source = self.programs.get(program.parent_id) if program.metadata.get("migrant") else None
        if source is not None and program.artifacts_json is None and program.artifact_dir is None:
            program.artifacts_json, program.artifact_dir = source.artifacts_json, source.artifact_dir
        return original(self, program, *a, **kw)

    ProgramDatabase.add = add


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--iterations", type=int, default=2)
    ap.add_argument("--seed", default="mockingjay")
    ap.add_argument("--model", default="gemini-2.5-flash")
    ap.add_argument("--ensemble", default=None,
                    help="several proposers with weights, e.g. gemini-2.5-pro=0.7,gemini-2.5-flash=0.3: "
                         "OpenEvolve draws one per proposal by weight, and llm_calls.jsonl names it. "
                         "Overrides --model")
    ap.add_argument("--vertex", action="store_true",
                    help="call Gemini through Vertex (bills the GCP credit) "
                         "instead of an AI Studio key")
    ap.add_argument("--max-tokens", type=int, default=32_000,
                    help="reply ceiling; a candidate is about 6 k tokens")
    ap.add_argument("--llm-timeout", type=int, default=300,
                    help="seconds allowed for one completion")
    ap.add_argument("--llm-retries", type=int, default=1,
                    help="retries of a failed completion, each billed; OpenEvolve and "
                         "the OpenAI client under it each retry this many times, so "
                         "the worst case is (N+1)^2 attempts: 4 here, 16 at "
                         "OpenEvolve's own default of 3, 1 at 0")
    ap.add_argument("--eval-timeout", type=int, default=7200,
                    help="seconds allowed per candidate evaluation (a build plus "
                         "the training traces: the seed took 55 min alone on 16 "
                         "cores, longer when candidates overlap)")
    ap.add_argument("--islands", type=int, default=None,
                    help="OpenEvolve database islands (its default 5): each is seeded with the seed and samples "
                         "parents from itself, so few islands compound faster in a short run")
    ap.add_argument("--migration-interval", type=int, default=None,
                    help="generations between migrations across islands (OpenEvolve's default 50)")
    ap.add_argument("--parallel", type=int, default=2,
                    help="candidates evaluated at once; a second one fills the "
                         "cores the first leaves idle while mcf finishes")
    ap.add_argument("--out", type=Path, default=Path.home() / "oe_out")
    ap.add_argument("--evaluator", choices=("ae", "yaml"), default="ae",
                    help="ae: the candidate is YAML + C++ (ae_evaluator.py); yaml: the candidate "
                         "is YAML + new words and Pro writes the C++ (design_evaluator.py)")
    ap.add_argument("--traces", default="",
                    help="yaml evaluator only: comma-separated trace stems in place of the 17 training traces")
    ap.add_argument("--warmup", type=int, default=None, help="yaml evaluator only: warm-up instructions (default 20M)")
    ap.add_argument("--sim", type=int, default=None, help="yaml evaluator only: measured instructions (default 50M)")
    ap.add_argument("--run-dir", type=Path, default=None,
                    help="yaml evaluator only: its store, records and candidate folders (default <out>/yaml_run)")
    ap.add_argument("--temperature", type=float, default=None,
                    help="sampling temperature sent with every completion; unset leaves the endpoint's default, "
                         "under which two workers given the same parent proposed byte-identical candidates")
    ap.add_argument("--max-change", type=int, default=0,
                    help="yaml evaluator only: canonical lines one candidate may change against its closest "
                         "earlier design (the ablation's limited arm); 0, the default, is ArchAgent's scope, "
                         "no limit. A guard against runaway designs (three times the seed) is always on.")
    ap.add_argument("--guidance", type=Path, default=None,
                    help="yaml evaluator only: a file whose text goes into the prompt of every proposal, read "
                         "at each one (guidance_writer.py rewrites it beside the search: the adaptive arm). "
                         "Absent or empty, the prompt is the fixed one")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    from chia_llc_loop import EXPLORE, absent
    if args.evaluator == "yaml":
        import evolver_prompt
        os.environ["A3_RUN_DIR"] = str(args.run_dir or args.out / "yaml_run")
        for flag, var in (("traces", "A3_TRACES"), ("warmup", "A3_WARMUP"), ("sim", "A3_SIM"),
                          ("max_change", "A3_MAX_CHANGE")):
            if getattr(args, flag):
                os.environ[var] = str(getattr(args, flag))
        instructions, seed_of, evaluator_path = evolver_prompt.instructions(), evolver_prompt.packed_seed, HERE / "design_evaluator.py"
        if args.max_change:
            assert f"at most {args.max_change} canonical lines" in instructions, "the prompt does not carry the step limit"
        if args.guidance:
            install_guidance(args.guidance, instructions)
            print(f"guidance: every proposal's prompt carries {args.guidance} as it stands at that moment")
        install_prompt_log(args.out)
        print(f"prompts: every proposal's prompt goes to {args.out / 'prompts.jsonl'}")
        install_migrant_artifacts()
        print("migrants: a program copied to another island keeps its feedback text")
        group = args.traces.split(",") if args.traces else EXPLORE
    else:
        assert args.guidance is None, "--guidance is the yaml evaluator's"
        instructions, seed_of, evaluator_path, group = INSTRUCTIONS, packed_seed, HERE / "ae_evaluator.py", EXPLORE
    missing = absent(group)
    if missing:
        print(f"{len(missing)} trace(s) not on this machine: {', '.join(missing)}")
        return 1

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
        expect = {"temperature": args.temperature, "seed": None} if args.temperature is not None else None
        install_refreshing_auth(args.out / "llm_calls.jsonl", expect)
        print(f"LLM: {', '.join(f'{n} (weight {w:g})' for n, w in proposers)} via Vertex ({project}); "
              f"usage bills that project's credit, token refreshed per request")
    else:
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            print("set GEMINI_API_KEY, or pass --vertex to use the GCP credit")
            return 1
        api_base = AI_STUDIO_BASE
        print(f"LLM: {', '.join(f'{n} (weight {w:g})' for n, w in proposers)} via the AI Studio endpoint")

    program = args.out / "seed_candidate.txt"
    program.write_text(seed_of(args.seed))
    assert evaluator_path.exists(), evaluator_path
    if args.evaluator == "yaml":
        from design_evaluator import NEW_WORDS, STRUCTURE_DESCRIPTION, split_candidate
        parts = split_candidate(program.read_text())
        assert parts.get(STRUCTURE_DESCRIPTION, "").strip() and NEW_WORDS in parts, \
            f"the seed candidate does not split into its two sections: {sorted(parts)}"
    print(f"seed written to {program} ({program.stat().st_size} B); evaluator {evaluator_path.name}")

    from skydiscover.config import Config
    from skydiscover.extras.external import openevolve_backend
    from openevolve.config import Config as OEConfig, LLMModelConfig as OEModel

    oe = OEConfig()
    must_set(oe, "max_iterations", args.iterations)
    must_set(oe, "max_code_length", 200_000)
    must_set(oe, "diff_based_evolution", False)
    must_set(oe.evaluator, "timeout", args.eval_timeout)
    must_set(oe.evaluator, "parallel_evaluations", args.parallel)
    if args.islands is not None:
        must_set(oe.database, "num_islands", args.islands)
    if args.migration_interval is not None:
        must_set(oe.database, "migration_interval", args.migration_interval)
    if args.evaluator == "yaml":
        must_set(oe.database, "max_snapshot_artifacts", None)
        assert oe.database.max_snapshot_artifacts is None
    oe.llm.api_base = api_base
    must_set(oe.llm, "max_tokens", args.max_tokens)
    must_set(oe.llm, "timeout", args.llm_timeout)
    must_set(oe.llm, "retries", args.llm_retries)
    oe.llm.models = [OEModel(name=n, weight=w, api_key=api_key, api_base=api_base) for n, w in proposers]
    oe.llm.evaluator_models = list(oe.llm.models)
    oe.llm.update_model_params({"max_tokens": args.max_tokens,
                                "timeout": args.llm_timeout,
                                "retries": args.llm_retries,
                                "retry_delay": oe.llm.retry_delay,
                                **({"temperature": args.temperature} if args.temperature is not None else {})})
    if args.temperature is not None:
        must_set(oe.database, "random_seed", oe.database.random_seed or oe.random_seed)
        must_set(oe, "random_seed", None)
        must_set(oe.llm, "random_seed", None)
        oe.llm.update_model_params({"random_seed": None}, overwrite=True)
        for m in oe.llm.models + oe.llm.evaluator_models:
            assert m.temperature == args.temperature and m.random_seed is None, \
                f"{m.name}: temperature {m.temperature}, seed {m.random_seed}"
        assert oe.random_seed is None and oe.llm.random_seed is None and oe.database.random_seed is not None
    for m in oe.llm.models:
        assert m.max_tokens == args.max_tokens and m.timeout == args.llm_timeout and m.retries == args.llm_retries \
            and m.retry_delay is not None, f"{m.name}: caps not applied"
    assert [(m.name, m.weight) for m in oe.llm.models] == proposers
    oe.prompt.system_message = instructions

    config = Config()
    config.file_suffix = ".txt"
    config.max_iterations = args.iterations
    config.max_solution_length = 200_000
    setattr(config, "system_prompt_override", instructions)
    setattr(config, "external_config", oe)

    print(f"effective config: iterations {oe.max_iterations}, "
          f"diff_based_evolution {oe.diff_based_evolution}, "
          f"evaluator timeout {oe.evaluator.timeout}s, "
          f"max_code_length {oe.max_code_length}, "
          f"max_tokens {oe.llm.models[0].max_tokens}, "
          f"llm timeout {oe.llm.models[0].timeout}s, "
          f"llm retries {oe.llm.models[0].retries} "
          f"(delay {oe.llm.models[0].retry_delay}s), "
          f"temperature {oe.llm.models[0].temperature}, "
          f"model seed {oe.llm.models[0].random_seed} (run seed {oe.random_seed}, "
          f"database seed {oe.database.random_seed}), "
          f"parallel {oe.evaluator.parallel_evaluations}, "
          f"islands {oe.database.num_islands} (migration every {oe.database.migration_interval}), "
          f"snapshot artifacts cap {oe.database.max_snapshot_artifacts}, "
          f"proposers {[(m.name, m.weight) for m in oe.llm.models]}, "
          f"vertex region {VERTEX_REGION}")
    print(f"running OpenEvolve for {args.iterations} iterations")
    result = await openevolve_backend.run(
        program_path=str(program),
        evaluator_path=str(evaluator_path),
        config_obj=config,
        iterations=args.iterations,
        output_dir=str(args.out),
    )
    print("\n=== result ===")
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
