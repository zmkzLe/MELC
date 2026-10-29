from __future__ import annotations

import copy
import fcntl
import glob
import hashlib
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

DEFAULT_BASE_CONFIG = "dpc4/1C.fullBW.nopref.mockingjay.json"
CACHE_LEVELS = ("L1I", "L1D", "L2C", "LLC", "ITLB", "DTLB", "STLB")
SOURCE_SUFFIXES = (".cc", ".h")
MODULE_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
BUILD_LOCK = ".chia_build.lock"
BUILD_STAMP = ".chia_build.stamp"
SOURCE_ID = "chia-source-id:"
RUN_BINARY_PREFIX = "chia_run_"
BUILD_ERROR = re.compile(r"(error[:\s]|undefined reference|fatal error|note:)", re.IGNORECASE)

try:
    import ray
    from chia.base.ChiaFunction import ChiaFunction
    from chia.simulators.champsim import ChampSimNode

    _HAS_CHIA = True
except ImportError:
    _HAS_CHIA = False


@dataclass
class BuildResult:
    binary: bytes
    module_name: str
    executable_name: str
    champsim_root: str
    base_rev: str
    config: dict = field(default_factory=dict)
    success: bool = False
    returncode: int = -1
    build_duration_s: float = 0.0
    stdout_tail: str = ""
    build_diagnostics: str = ""
    source_id: str = ""
    cleaned: bool = False
    stale_retry: bool = False


@dataclass
class RunResult:
    ipc: float
    instructions: int
    cycles: int
    stats: list = field(default_factory=list)
    command: list = field(default_factory=list)
    json_path: str = ""
    opt_log_path: str = ""
    opt_log_bytes: int = 0
    success: bool = False
    returncode: int = -1
    wall_s: float = 0.0
    stdout_tail: str = ""
    timed_out: bool = False


def check_module_name(name: str) -> str:
    if not MODULE_NAME.fullmatch(name):
        raise ValueError(f"module name {name!r} must match [a-zA-Z_][a-zA-Z0-9_]*")
    return name


def check_sources(sources: dict) -> dict:
    if not sources:
        raise ValueError("a replacement module needs at least one source file")
    for name in sources:
        if name != os.path.basename(name) or name.startswith("."):
            raise ValueError(f"{name!r} must be a plain file name, not a path")
        if not name.endswith(SOURCE_SUFFIXES):
            raise ValueError(f"{name!r} must end with one of {SOURCE_SUFFIXES}")
    return sources


def champsim_config(base: dict, module_name: str, *, cache_level: str = "LLC",
                    executable_name: str | None = None) -> dict:
    if cache_level not in CACHE_LEVELS:
        raise ValueError(f"cache level {cache_level!r} must be one of {CACHE_LEVELS}")
    for key in ("executable_name", "num_cores", "block_size", cache_level):
        if key not in base:
            raise ValueError(f"the base config does not define {key!r}; without it the "
                             "run falls back to ChampSim's default machine")
    for key in ("sets", "ways"):
        if key not in base[cache_level]:
            raise ValueError(f"the base config does not give {cache_level} a {key!r}")
    config = copy.deepcopy(base)
    config[cache_level]["replacement"] = module_name
    config["executable_name"] = executable_name or f"chia_{module_name}"
    return config


def build_command(config_path: str, jobs: int | None = None, clean: bool = True) -> str:
    return (("make clean && " if clean else "")
            + f"python3 ./config.sh {shlex.quote(config_path)} "
            f"&& make -j{jobs if jobs else '$(nproc)'}")


def source_id(sources: dict) -> str:
    h = hashlib.sha256()
    for name in sorted(sources):
        h.update(name.encode() + b"\0" + sources[name].encode() + b"\0")
    return h.hexdigest()[:16]


_SOURCE_ID_LINE = re.compile(r'^\[\[gnu::used\]\] static const char chia_source_id_\d+\[\] = "'
                             + re.escape(SOURCE_ID) + r'[0-9a-f]+";\n?', re.M)


def without_source_id(sources: dict) -> dict:
    return {name: _SOURCE_ID_LINE.sub("", text) if name.endswith(".cc") else text
            for name, text in sources.items()}


def with_source_id(sources: dict, sid: str) -> dict:
    out = dict(sources)
    for i, name in enumerate(sorted(n for n in sources if n.endswith(".cc"))):
        text = sources[name]
        out[name] = (text + ("" if text.endswith("\n") or not text else "\n")
                     + f'[[gnu::used]] static const char chia_source_id_{i}[] = "{SOURCE_ID}{sid}";\n')
    return out


def build_stamp(config: dict, base_rev: str) -> str:
    return hashlib.sha256((json.dumps(config, sort_keys=True) + "\0" + base_rev).encode()).hexdigest()


def generated_state(champsim_root: str) -> str:
    h = hashlib.sha256()
    paths = [os.path.join(champsim_root, "_configuration.mk")]
    paths += sorted(glob.glob(os.path.join(champsim_root, ".csconfig", "*.inc")))
    for path in paths:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            data = b"\0missing"
        h.update(os.path.relpath(path, champsim_root).encode() + b"\0" + data + b"\0")
    return h.hexdigest()


def run_command(binary: str, trace: str, *, warmup_instructions: int, simulation_instructions: int,
                json_path: str, opt_log: str | None = None, opt_sets: int | None = None,
                opt_cache: str | None = None) -> list:
    if opt_log is None and (opt_sets is not None or opt_cache is not None):
        raise ValueError("--opt-sets and --opt-cache do nothing without opt_log")
    cmd = [binary,
           "--warmup-instructions", str(warmup_instructions),
           "--simulation-instructions", str(simulation_instructions),
           "--json", json_path]
    if opt_log is not None:
        cmd += ["--opt-log", opt_log]
        if opt_cache is not None:
            cmd += ["--opt-cache", opt_cache]
        if opt_sets is not None:
            cmd += ["--opt-sets", str(opt_sets)]
    cmd.append(trace)
    return cmd


def sim_phase(stats: list) -> dict:
    if not stats:
        raise ValueError("empty JSON output")
    for entry in stats:
        if entry.get("name") == "Simulation":
            return entry
    return stats[-1]


def core_totals(stats: list) -> dict:
    cores = sim_phase(stats)["roi"]["cores"]
    if not cores:
        raise ValueError("JSON output has no cores")
    instructions, cycles = int(cores[0]["instructions"]), int(cores[0]["cycles"])
    return {"instructions": instructions, "cycles": cycles,
            "ipc": instructions / cycles if cycles else 0.0}


def _run_logged(cmd, cwd, timeout_s: int) -> tuple:
    t0 = time.time()
    proc = subprocess.Popen(cmd, cwd=cwd, shell=isinstance(cmd, str), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
        return proc.returncode, stdout, stderr, False, time.time() - t0
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return -1, stdout, stderr, True, time.time() - t0


def _diagnostics(stdout: str, stderr: str, max_bytes: int = 3000) -> str:
    lines = (stdout + "\n" + stderr).splitlines()
    flagged = [line for line in lines if BUILD_ERROR.search(line)]
    return "\n".join(flagged[-60:] or lines[-30:])[-max_bytes:]


def _resolve_trace(trace: str) -> str:
    if os.path.isfile(trace):
        return trace
    try:
        from chia.simulators.champsim import _resolve_trace as chia_resolve
    except ImportError:
        raise FileNotFoundError(f"trace not found: {trace}") from None
    return chia_resolve(trace)


def _materialise(binary: bytes) -> str:
    fd, path = tempfile.mkstemp(prefix=RUN_BINARY_PREFIX)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(binary)
        os.chmod(path, stat.S_IRWXU)
    except BaseException:
        os.unlink(path)
        raise
    return path


def build_replacement(champsim_root: str, module_name: str, sources: dict, *,
                      base_config: str = DEFAULT_BASE_CONFIG, cache_level: str = "LLC",
                      executable_name: str | None = None, jobs: int | None = None,
                      timeout_s: int = 1800) -> BuildResult:
    check_module_name(module_name)
    sources = without_source_id(check_sources(sources))
    config_path = base_config if os.path.isabs(base_config) else os.path.join(champsim_root, base_config)
    with open(config_path) as f:
        config = champsim_config(json.load(f), module_name, cache_level=cache_level,
                                 executable_name=executable_name)
    executable = config["executable_name"]
    sid = source_id(sources)
    marked = with_source_id(sources, sid)
    checkable = any(name.endswith(".cc") for name in sources)

    lock_fd = os.open(os.path.join(champsim_root, BUILD_LOCK), os.O_CREAT | os.O_RDWR)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=champsim_root, capture_output=True,
                             text=True, timeout=30)
        base_rev = rev.stdout.strip() if rev.returncode == 0 else ""

        module_dir = os.path.join(champsim_root, "replacement", module_name)
        os.makedirs(module_dir, exist_ok=True)
        for stale in os.listdir(module_dir):
            if stale not in marked and stale.endswith(SOURCE_SUFFIXES):
                os.unlink(os.path.join(module_dir, stale))
        for name, text in marked.items():
            with open(os.path.join(module_dir, name), "w") as f:
                f.write(text)

        stamp_path = os.path.join(champsim_root, BUILD_STAMP)
        stamp = build_stamp(config, base_rev)
        try:
            with open(stamp_path) as f:
                clean = f.read().split() != [stamp, generated_state(champsim_root)]
        except OSError:
            clean = True
        binary_path = os.path.join(champsim_root, "bin", executable)

        fd, generated = tempfile.mkstemp(suffix=".json", prefix="chia_champsim_config_")
        with os.fdopen(fd, "w") as f:
            json.dump(config, f)
        try:
            attempts = [clean] if clean else [False, True]
            wall_total, stdout, stderr, rc, timed_out = 0.0, "", "", -1, False
            binary, success, stale_retry, cleaned = b"", False, False, False
            for attempt, do_clean in enumerate(attempts):
                try:
                    os.unlink(binary_path)
                except OSError:
                    pass
                cleaned = cleaned or do_clean
                rc, stdout, stderr, timed_out, wall = _run_logged(
                    build_command(generated, jobs, clean=do_clean), champsim_root, timeout_s)
                wall_total += wall
                success = rc == 0 and not timed_out
                binary = b""
                if success:
                    try:
                        with open(binary_path, "rb") as f:
                            binary = f.read()
                    except OSError as exc:
                        success = False
                        stderr += f"\nthe build reported success but {binary_path} is not there: {exc}"
                if not success or not checkable or (SOURCE_ID + sid).encode() in binary:
                    break
                success, binary = False, b""
                stderr += (f"\nthe binary does not contain this candidate's source id {sid}"
                           + ("; rebuilding clean" if attempt + 1 < len(attempts) else ""))
                stale_retry = stale_retry or attempt + 1 < len(attempts)
        finally:
            try:
                os.unlink(generated)
            except OSError:
                pass

        finished = not timed_out and 0 <= rc < 128
        if success or (finished and rc != 0):
            with open(stamp_path, "w") as f:
                f.write(f"{stamp}\n{generated_state(champsim_root)}\n")
        elif not finished:
            try:
                os.unlink(stamp_path)
            except OSError:
                pass
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    return BuildResult(
        binary=binary,
        module_name=module_name,
        executable_name=executable,
        champsim_root=champsim_root,
        base_rev=base_rev,
        config=config,
        success=success,
        returncode=rc,
        build_duration_s=wall_total,
        stdout_tail=stdout[-3000:],
        build_diagnostics="" if success else (
            f"TIMEOUT after {wall_total:.0f}s (limit {timeout_s}s)" if timed_out
            else _diagnostics(stdout, stderr)),
        source_id=sid,
        cleaned=cleaned,
        stale_retry=stale_retry,
    )


def run_simulation(binary, trace: str, *, warmup_instructions: int, simulation_instructions: int,
                   timeout_s: int = 3600, opt_log: str | None = None, opt_sets: int | None = None,
                   opt_cache: str | None = None, json_path: str | None = None) -> RunResult:
    resolved = _resolve_trace(trace)
    owned = isinstance(binary, bytes)
    keep = json_path is not None
    if not keep:
        fd, json_path = tempfile.mkstemp(suffix=".json", prefix="chia_champsim_stats_")
        os.close(fd)
    binary_path = ""

    try:
        binary_path = _materialise(binary) if owned else str(binary)
        cmd = run_command(binary_path, resolved, warmup_instructions=warmup_instructions,
                          simulation_instructions=simulation_instructions, json_path=json_path,
                          opt_log=opt_log, opt_sets=opt_sets, opt_cache=opt_cache)
        rc, stdout, stderr, timed_out, wall = _run_logged(cmd, None, timeout_s)
        success = rc == 0 and not timed_out
        stats: list = []
        if success:
            try:
                with open(json_path) as f:
                    stats = json.load(f)
                totals = core_totals(stats)
            except (OSError, ValueError, KeyError) as exc:
                success = False
                stderr += f"\nthe run finished but its JSON could not be read: {exc}"
        if not success:
            return RunResult(ipc=0.0, instructions=0, cycles=0, command=cmd,
                             json_path=json_path if keep else "",
                             opt_log_path=opt_log or "", success=False, returncode=rc,
                             wall_s=wall, stdout_tail=(stdout + "\n" + stderr)[-3000:],
                             timed_out=timed_out)
        return RunResult(
            ipc=totals["ipc"],
            instructions=totals["instructions"],
            cycles=totals["cycles"],
            stats=stats,
            command=cmd,
            json_path=json_path if keep else "",
            opt_log_path=opt_log or "",
            opt_log_bytes=os.path.getsize(opt_log) if opt_log and os.path.isfile(opt_log) else 0,
            success=True,
            returncode=rc,
            wall_s=wall,
            stdout_tail=stdout[-3000:],
            timed_out=False,
        )
    finally:
        for path, remove in ((json_path, not keep), (binary_path, owned)):
            if remove and path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


if _HAS_CHIA:

    class DPC4ChampSimNode(ChampSimNode):
        def __init__(self, placement_group=None, require_colocated: bool = False, **kwargs):
            super().__init__(placement_group, require_colocated, **kwargs)

        @staticmethod
        @ChiaFunction(resources={"champsim": 1.0})
        def build_champsim(champsim_root: str, module_name: str, sources: dict, *,
                           base_config: str = DEFAULT_BASE_CONFIG, cache_level: str = "LLC",
                           executable_name: str | None = None, jobs: int | None = None,
                           timeout_s: int = 1800) -> BuildResult:
            return build_replacement(champsim_root, module_name, sources, base_config=base_config,
                                     cache_level=cache_level, executable_name=executable_name,
                                     jobs=jobs, timeout_s=timeout_s)

        @staticmethod
        @ChiaFunction(resources={"champsim": 1.0})
        def run_champsim(binary, trace: str, *, warmup_instructions: int,
                         simulation_instructions: int, timeout_s: int = 3600,
                         opt_log: str | None = None, opt_sets: int | None = None,
                         opt_cache: str | None = None, json_path: str | None = None) -> RunResult:
            return run_simulation(binary, trace, warmup_instructions=warmup_instructions,
                                  simulation_instructions=simulation_instructions,
                                  timeout_s=timeout_s, opt_log=opt_log, opt_sets=opt_sets,
                                  opt_cache=opt_cache, json_path=json_path)
