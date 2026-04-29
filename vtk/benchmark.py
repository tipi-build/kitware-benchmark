#!/usr/bin/env python3
"""Benchmark configure and build times for CMake and cmake-re."""

import argparse
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
from dataclasses import dataclass, field, asdict
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

    def __init__(self, image, source_dir, log_dir, rbe_service, RBE_exec_strategy):
        self.image = image
        self.source_dir = source_dir
        self.rbe_service = rbe_service
        self.RBE_exec_strategy = RBE_exec_strategy
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
            "-e", f"RBE_platform=linux-amd64",
            "-e", f"RBE_service={self.rbe_service}",
            "-e", f"RBE_tls_client_auth_key={home}/engflow-mTLS/engflow.key",
            "-e", f"RBE_tls_client_auth_cert={home}/engflow-mTLS/engflow.crt",
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
            for line in proc.stdout:
                sys.stdout.write(line)
                f.write(line)
            proc.wait()
            if proc.returncode != 0:
                f.write(f"\n[EXIT CODE: {proc.returncode}]\n")
        elapsed = time.perf_counter() - start

        print(f"  Done in {elapsed:.2f}s (log: {log_file})")
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, f"[{label}] {cmd}")
        return elapsed


def zip_tmp_excluding_repo(container, source_dir, log_dir):
    """Zip /tmp inside the container excluding the source repo folder, then copy it to the host log dir."""
    zip_path_in_container = "/tmp/tmp_snapshot.zip"

    subprocess.run(
        ["docker", "exec", "-u", "0", container.name, "bash", "-c", "apt update && apt -y install zip"],
        check=True,
    )
    container.run(
        f'zip -r {zip_path_in_container} /tmp -x "{source_dir}/*" "{source_dir}" "{zip_path_in_container}"',
        step="zip_tmp",
    )

    host_zip_path = Path(log_dir) / "tmp_snapshot.zip"
    subprocess.run(["docker", "cp", f"{container.name}:{zip_path_in_container}", str(host_zip_path)], check=True)
    print(f"  Saved tmp snapshot to {host_zip_path}")


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
            with DockerContainer(cfg.image, cfg.source_dir, log_dir, cfg.rbe_service, cfg.RBE_exec_strategy) as container:
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


def cmake_re_steps(container, result, toolchain, cfg):
    silo_key = str(uuid.uuid4())
    build_cmd = f'RBE_platform="cache-silo-key={silo_key}" cmake-re --build ./build --host --distributed -j{cfg.jobs}'

    result.record("configure", container.run(f'cmake-re -GNinja -S . -B ./build -DCMAKE_TOOLCHAIN_FILE="{toolchain}" --host --distributed', step="configure"))
    result.record("build_no_cache", container.run(build_cmd, step="build_no_cache"))

    # Clean build artifacts, keep RBE cache warm
    container.run("cmake-re --build ./build --target clean --host --distributed", step="clean")
    result.record("build_with_cache", container.run(build_cmd, step="build_with_cache"))

    modify_file_to_trigger_incremental_build(container, cfg.modified_file)
    result.record("modified_file_rebuild", container.run(build_cmd, step="modified_file_rebuild"))

    zip_tmp_excluding_repo(container, cfg.source_dir, container.log_dir)


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
  "RBE_exec_strategy":    "<RBE execution mode: 'remote' or 'racing' (required)>"
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
    )

    all_results = []
    suite_start = time.perf_counter()
    all_results.extend(run_benchmarks(cfg, "cmake", cmake_steps))
    all_results.extend(run_benchmarks(cfg, "cmake-re", cmake_re_steps))
    suite_elapsed = time.perf_counter() - suite_start

    results_file = output_dir / "benchmark-results.json"
    with open(results_file, "w") as f:
        json.dump(all_results, f, indent=2)

    minutes, seconds = divmod(int(suite_elapsed), 60)
    hours, minutes = divmod(minutes, 60)
    print(f"\nAll results written to {results_file}")
    print(f"Total benchmark time: {hours}h{minutes:02d}m{seconds:02d}s")


if __name__ == "__main__":
    main()
