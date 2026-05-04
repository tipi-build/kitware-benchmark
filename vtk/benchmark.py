#!/usr/bin/env python3
"""Benchmark configure and build times for CMake and cmake-re."""

import argparse
import csv
import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import resource
import time
from dataclasses import dataclass, field, asdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


@dataclass
class BenchmarkConfig:
    source_dir: Path
    image: str
    iterations: int
    toolchains: list
    output_dir: Path
    modified_file: str
    rbe_service: str
    jobs: int
    RBE_exec_strategy: str
    mtls_dir: str
    download_engflow_profiles: bool
    profile_error_patterns: list
    pending_profile_downloads: list = field(default_factory=list)


@dataclass
class BenchmarkResult:
    tool: str
    toolchain: str
    description: str
    iteration: int
    timings: dict = field(default_factory=dict)

    def record(self, step, elapsed):
        self.timings[step] = round(elapsed, 2)
        print(f"  [{step}] {self.timings[step]}s")


def clone_repo(url, branch):
    """Clone or reuse a git repo in /tmp with the given branch and init submodules. Returns the repo path."""
    name = url.rstrip("/").split("/")[-1]
    repo_name = name[:-4] if name.endswith(".git") else name
    repo_path = Path(tempfile.gettempdir()) / repo_name

    if repo_path.exists():
        print(f"Reusing existing clone at {repo_path}, resetting to origin/{branch}...")
        subprocess.run(["git", "fetch", "origin"], cwd=str(repo_path), check=True)
        subprocess.run(["git", "checkout", branch], cwd=str(repo_path), check=True)
        subprocess.run(["git", "reset", "--hard", f"origin/{branch}"], cwd=str(repo_path), check=True)
        subprocess.run(["git", "clean", "-fdx"], cwd=str(repo_path), check=True)
    else:
        print(f"Cloning {url} (branch: {branch})...")
        subprocess.run(["git", "clone", "--branch", branch, url, str(repo_path)], check=True)

    print("Initializing submodules...")
    subprocess.run(["git", "submodule", "update", "--init", "--recursive"], cwd=str(repo_path), check=True)

    print(f"Repo ready at {repo_path}")
    return repo_path


def pull_docker_image(image):
    """Pull a docker image. Returns the image name."""
    print(f"Pulling docker image {image}...")
    subprocess.run(["docker", "pull", "--platform", "linux/amd64", image], check=True)
    print(f"Image ready: {image}")
    return image


class DockerContainer:
    """Context manager for a docker container lifecycle."""

    def __init__(self, image, source_dir, log_dir, rbe_service, RBE_exec_strategy, mtls_dir):
        self.image = image
        self.source_dir = source_dir
        self.rbe_service = rbe_service
        self.RBE_exec_strategy = RBE_exec_strategy
        self.mtls_dir = mtls_dir
        self.name = str(uuid.uuid4())
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._run_counter = 0

    def __enter__(self):
        uid = os.getuid()
        gid = os.getgid()
        username = getpass.getuser()
        home = os.environ["HOME"]

        print(f"Starting container {self.name}...")

        rbe_env = []
        if self.RBE_exec_strategy == "racing":
            rbe_env = [
                "-e", "RBE_local_resource_fraction=0.3",
                "-e", "RBE_exec_strategy=racing",
                "-e", "RBE_racing_bias=5",
            ]

        subprocess.run([
            "docker", "run",
            "--platform", "linux/amd64",
            "--rm", "--init",
            "--name", self.name,
            f"-u{uid}:{gid}", "--group-add", "tipi",
            "--ulimit", "nofile=65535:65535",
            "-e", "TIPI_DISABLE_AR_RANLIB_DRIVER=ON",
            "-e", "TIPI_CACHE_CONSUME_ONLY=ON",
            "-e", "TIPI_CACHE_FORCE_ENABLE=OFF",
            *rbe_env,
            "-e", f"RBE_service={self.rbe_service}",
            "-e", f"RBE_tls_client_auth_key={home}/{self.mtls_dir}/engflow.key",
            "-e", f"RBE_tls_client_auth_cert={home}/{self.mtls_dir}/engflow.crt",
            "-e", f"RBE_proxy_log_dir=/tmp",
            "-v", f"{home}:{home}:rw",
            "-v", f"{self.source_dir}:{self.source_dir}:rw",
            "-w", str(self.source_dir),
            "-d",
            self.image,
            "sleep", "infinity",
        ], check=True)

        try:
            # Create the user inside the container
            subprocess.run([
                "docker", "exec", "-u", "0", self.name,
                "useradd", "-d", home, "-u", str(uid), username,
            ], check=False)

            print(f"Container running: {self.name}")
        except Exception:
            subprocess.run(["docker", "stop", "-t0", self.name], check=False)
            raise

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        print(f"Stopping container {self.name[:8]}...")
        subprocess.run(["docker", "stop", "-t0", self.name], check=False)
        print("Container stopped.")

    def run(self, cmd, step=None):
        """Run a command inside the container, stream output to console and log file, return elapsed time."""
        self._run_counter += 1
        label = step or f"step_{self._run_counter}"
        log_file = self.log_dir / f"{label}.log"

        print(f"  [{self.name[:8]}] Running: {cmd}")
        start = time.perf_counter()
        with open(log_file, "w") as f:
            f.write(f"$ {cmd}\n\n")
            proc = subprocess.Popen(
                ["docker", "exec", self.name, "bash", "-c", cmd],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            try:
                for line in proc.stdout:
                    sys.stdout.write(line)
                    f.write(line)
                proc.wait()
            except Exception:
                proc.kill()
                proc.wait()
                raise
            if proc.returncode != 0:
                f.write(f"\n[EXIT CODE: {proc.returncode}]\n")
        elapsed = time.perf_counter() - start

        print(f"  Done in {elapsed:.2f}s (log: {log_file})")
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, f"[{label}] {cmd}")
        return elapsed


def archive_container_data_excluding_repo(container, source_dir, log_dir):
    """Tar /tmp inside the container excluding the source repo folder, then copy it to the host log dir."""
    archive_path_in_container = "/tmp/tmp_snapshot.tar"


    container.run(
        f'tar -cf {archive_path_in_container} --exclude="{source_dir}/*" -C /tmp .',
        step="archive_container_data",
    )

    host_archive_path = Path(log_dir) / "tmp_snapshot.tar"
    subprocess.run(["docker", "cp", f"{container.name}:{archive_path_in_container}", str(host_archive_path)], check=True)
    print(f"  Saved tmp snapshot to {host_archive_path}")


def download_engflow_profiles(log_dir, invocations, rbe_service, mtls_dir):
    """Download EngFlow profiling JSON for each invocation into a dedicated folder."""
    home = os.environ["HOME"]
    rbe_host = rbe_service.rsplit(":", 1)[0]
    profile_dir = Path(log_dir) / "engflow_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    for step_name, invocation_id in invocations.items():
        profile_path = profile_dir / f"{step_name}.json"
        curl_cmd = [
            "curl", "--fail",
            "--cert", f"{home}/{mtls_dir}/engflow.crt",
            "--key", f"{home}/{mtls_dir}/engflow.key",
            "-H", "Accept: application/json",
            "-o", str(profile_path),
            f"https://{rbe_host}/api/profiling/v1/instances/default/invocations/{invocation_id}",
        ]
        print(f"  Downloading EngFlow profile for {step_name} ({invocation_id})...")
        subprocess.run(curl_cmd, check=True)
        print(f"  Saved to {profile_path}")


def clean_test_repo(source_dir):
    """Reset the source tree to a clean state, undoing any modifications and untracked files."""
    print("  Cleaning test repo...")
    subprocess.run(["git", "checkout", "--", "."], cwd=str(source_dir), check=True)
    subprocess.run(["git", "clean", "-fd"], cwd=str(source_dir), check=True)


def modify_file_to_trigger_incremental_build(container, modified_file):
    """Inject a unique #define into a header to trigger a cascade rebuild."""
    touch_uuid = str(uuid.uuid4())
    container.run(f'sed -i "1i #define TIPI \\"{touch_uuid}\\"" "{modified_file}"')


def run_benchmarks(cfg, tool_name, run_steps):
    """Common loop: iterate toolchains x iterations, run tool-specific steps inside a container."""
    results = []

    for toolchain_path, description in cfg.toolchains:
        tc_name = Path(toolchain_path).stem
        print(f"\n=== {tool_name} benchmark: {description} ({tc_name}) ===")

        for i in range(1, cfg.iterations + 1):
            print(f"\n--- Iteration {i}/{cfg.iterations} ---")
            clean_test_repo(cfg.source_dir)

            log_dir = cfg.output_dir / "logs" / tool_name / tc_name / f"iter_{i}"
            with DockerContainer(cfg.image, cfg.source_dir, log_dir, cfg.rbe_service, cfg.RBE_exec_strategy, cfg.mtls_dir) as container:
                result = BenchmarkResult(tool=tool_name, toolchain=tc_name, description=description, iteration=i)
                run_steps(container, result, toolchain_path, cfg)
                results.append(asdict(result))

            build_path = cfg.source_dir / "build"
            if build_path.exists():
                shutil.rmtree(build_path)

    return results


def cmake_steps(container, result, toolchain, cfg):
    result.record("configure", container.run(f'tipi run cmake -GNinja -S . -B ./build -DCMAKE_TOOLCHAIN_FILE="{toolchain}"', step="configure"))
    result.record("build", container.run("tipi run cmake --build ./build", step="build"))

    # Clean then rebuild
    container.run("tipi run cmake --build ./build --target clean", step="clean")
    result.record("rebuild", container.run("tipi run cmake --build ./build", step="rebuild"))

    modify_file_to_trigger_incremental_build(container, cfg.modified_file)
    result.record("modified_file_rebuild", container.run("tipi run cmake --build ./build", step="modified_file_rebuild"))


def cmake_re_preheat(toolchain, cfg):
    
    start = time.perf_counter()
    
    tc_name = Path(toolchain).stem
    log_dir = cfg.output_dir / "logs" / "cmake-re" / tc_name / "preheat"
    
    
    def single_preheat_run(task_ix):
        silo_key = str(uuid.uuid4())
        with DockerContainer(cfg.image, cfg.source_dir, log_dir, cfg.rbe_service, cfg.RBE_exec_strategy, cfg.mtls_dir) as container:
            time.sleep(task_ix * 10) # staggered start to allow for ramp up
            
            print(f" - preheat task {task_ix} start")
            container.run(f'cmake-re -GNinja -S . -B ./build_preheat_{task_ix} -DCMAKE_TOOLCHAIN_FILE="{toolchain}" --host --distributed', step="configure")
            container.run(f'RBE_platform="cache-silo-key={silo_key}" cmake-re --build ./build_preheat_{task_ix} --host --distributed -j{cfg.jobs}', step="build")
            print(f" - preheat task {task_ix} done")
            
                
    with ThreadPoolExecutor(max_workers=4) as executor:
        indices = range(4)
        print(f"Running preheat tasks on the threadpool: {indices}")
        result = list(executor.map(single_preheat_run, indices))
    
    elapsed = time.perf_counter() - start
    print(f"All preheat tasks completed in {elapsed}")
    return elapsed

def cmake_re_steps(container, result, toolchain, cfg):
    silo_key = str(uuid.uuid4())
    build_invocation_id = str(uuid.uuid4())
    rebuild_invocation_id = str(uuid.uuid4())
    modified_rebuild_invocation_id = str(uuid.uuid4())
    
    result.record("preheat_cluster", cmake_re_preheat(toolchain, cfg))

    build_cmd = f'RBE_platform="cache-silo-key={silo_key}" cmake-re --build ./build --host --distributed -j{cfg.jobs}'

    result.record("configure", container.run(f'cmake-re -GNinja -S . -B ./build -DCMAKE_TOOLCHAIN_FILE="{toolchain}" --host --distributed', step="configure"))
    result.record("build", container.run(f'RBE_invocation_id={build_invocation_id} {build_cmd}', step="build"))

    modify_file_to_trigger_incremental_build(container, cfg.modified_file)
    result.record("modified_file_rebuild", container.run(f'RBE_invocation_id={modified_rebuild_invocation_id} {build_cmd}', step="modified_file_rebuild"))
    
    # Clean build artifacts, keep RBE cache warm
    container.run("cmake-re --build ./build --target clean --host --distributed", step="clean")
    result.record("rebuild", container.run(f'RBE_invocation_id={rebuild_invocation_id} {build_cmd}', step="rebuild"))

    print(f"  RBE invocation IDs — build: {build_invocation_id}, rebuild: {rebuild_invocation_id}, modified_file_rebuild: {modified_rebuild_invocation_id}")

    archive_container_data_excluding_repo(container, cfg.source_dir, container.log_dir)

    if cfg.download_engflow_profiles:
        cfg.pending_profile_downloads.append({
            "log_dir": container.log_dir,
            "invocations": {
                "build": build_invocation_id,
                "rebuild": rebuild_invocation_id,
                "modified_file_rebuild": modified_rebuild_invocation_id,
            },
        })
        print(f"  Queued EngFlow profile download ({len(cfg.pending_profile_downloads)} pending)")


def scan_engflow_profiles(output_dir, error_strings):
    """Scan EngFlow profile files for known error patterns."""
    profile_files = list(output_dir.rglob("engflow_profile/*.json")) + list(output_dir.rglob("engflow_profile/*.txt"))
    if not profile_files:
        return

    print(f"\nScanning {len(profile_files)} EngFlow profile(s) for connection errors...")
    found_any = False
    for pf in profile_files:
        content = pf.read_text()
        for err in error_strings:
            if err in content:
                print(f"  WARNING: \"{err}\" found in {pf}")
                found_any = True
    if not found_any:
        print("  No connection errors found in profiles.")


def write_results_csv(results, csv_path):
    """Write benchmark results list to a CSV file."""
    seen = set()
    timing_keys = []
    for r in results:
        for k in r["timings"]:
            if k not in seen:
                seen.add(k)
                timing_keys.append(k)

    fieldnames = ["tool", "toolchain", "description", "iteration"] + timing_keys

    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            row = {k: r[k] for k in ("tool", "toolchain", "description", "iteration")}
            row.update(r["timings"])
            writer.writerow(row)

    print(f"CSV results written to {csv_path}")


def main():
    example_config = """\
Expected JSON config format:
{
  "repo_url":    "<git URL of the repository to benchmark>",
  "branch":      "<git branch to checkout>",
  "image":       "<docker image (name or name@sha256:digest)>",
  "toolchains":  [
    {
      "path":        "<path to CMake toolchain file, relative to repo root>",
      "description": "<human-readable label for this toolchain>"
    }
  ],
  "iterations":  "<number of benchmark iterations per toolchain (default: 1)>",
  "jobs":        "<number of parallel jobs for cmake-re builds (default: 1500)>",
  "output_dir":  "<directory for logs and results (default: output)>",
  "modified_file":  "<header file to modify for incremental rebuild test, relative to repo root>",
  "rbe_service": "<RBE endpoint host:port (default: kernite.cluster.engflow.com:443)>",
  "RBE_exec_strategy":    "<RBE execution mode: 'remote' or 'racing' (required)>",
  "mtls_dir":             "<mTLS certificate directory name under $HOME (default: engflow-mTLS)>",
  "download_engflow_profiles": "<bool: download EngFlow profiling data after each cmake-re iteration (default: false)>",
  "profile_error_patterns":    "<list of strings to search for in downloaded profiles (default: [])>"
}"""
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=example_config,
    )
    parser.add_argument("config", help="path to JSON configuration file")
    args = parser.parse_args()

    try:
        with open(args.config) as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        parser.error(f"invalid JSON in {args.config}: {e}")
    except FileNotFoundError:
        parser.error(f"config file not found: {args.config}")

    required_keys = ["repo_url", "branch", "image", "toolchains", "modified_file", "RBE_exec_strategy"]
    missing = [k for k in required_keys if k not in config]
    if missing:
        parser.error(f"missing required config keys: {', '.join(missing)}")

    for i, tc in enumerate(config["toolchains"]):
        for key in ("path", "description"):
            if key not in tc:
                parser.error(f"toolchains[{i}] is missing required key: {key}")

    output_dir = Path(config.get("output_dir", "output"))
    output_dir.mkdir(parents=True, exist_ok=True)

    target = 65535
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, target))
    except (ValueError, OSError) as e:
        print(f"Warning: could not set NOFILE limit to {target}: {e}")

    cfg = BenchmarkConfig(
        source_dir=clone_repo(config["repo_url"], config["branch"]),
        image=pull_docker_image(config["image"]),
        iterations=config.get("iterations", 1),
        toolchains=[(tc["path"], tc["description"]) for tc in config["toolchains"]],
        output_dir=output_dir,
        modified_file=config["modified_file"],
        rbe_service=config.get("rbe_service", "kernite.cluster.engflow.com:443"),
        jobs=config.get("jobs", 1500),
        RBE_exec_strategy=config["RBE_exec_strategy"],
        mtls_dir=config.get("mtls_dir", "engflow-mTLS"),
        download_engflow_profiles=config.get("download_engflow_profiles", False),
        profile_error_patterns=config.get("profile_error_patterns", []),
    )

    all_results = []
    suite_start = time.perf_counter()
    all_results.extend(run_benchmarks(cfg, "cmake", cmake_steps))
    all_results.extend(run_benchmarks(cfg, "cmake-re", cmake_re_steps))
    suite_elapsed = time.perf_counter() - suite_start

    results_file = output_dir / "benchmark-results.json"
    with open(results_file, "w") as f:
        json.dump(all_results, f, indent=2)

    csv_file = output_dir / "benchmark-results.csv"
    write_results_csv(all_results, csv_file)

    minutes, seconds = divmod(int(suite_elapsed), 60)
    hours, minutes = divmod(minutes, 60)
    print(f"\nAll results written to {results_file}")
    print(f"Total benchmark time: {hours}h{minutes:02d}m{seconds:02d}s")

    if cfg.download_engflow_profiles and cfg.pending_profile_downloads:
        # EngFlow needs time to finalize profiling data after builds complete
        print(f"\nWaiting 60s for EngFlow to finalize profiling data before downloading {len(cfg.pending_profile_downloads)} profile(s)...")
        time.sleep(120)

        for dl in cfg.pending_profile_downloads:
            download_engflow_profiles(dl["log_dir"], dl["invocations"], cfg.rbe_service, cfg.mtls_dir)

        if cfg.profile_error_patterns:
            scan_engflow_profiles(output_dir, cfg.profile_error_patterns)


if __name__ == "__main__":
    main()
