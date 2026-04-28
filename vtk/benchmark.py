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
    """Clone a git repo into /tmp with the given branch and init submodules. Returns the repo path."""
    repo_name = url.rstrip("/").split("/")[-1].replace(".git", "")
    repo_path = Path(tempfile.gettempdir()) / repo_name

    if repo_path.exists():
        print(f"  Removing existing {repo_path}")
        shutil.rmtree(repo_path)

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

    def __init__(self, image, source_dir, log_dir):
        self.image = image
        self.source_dir = source_dir
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
            "-e", "HOME",
            "-e", "RBE_service=kernite.cluster.engflow.com:443",
            "-e", f"RBE_tls_client_auth_key={home}/engflow-mTLS/engflow.key",
            "-e", f"RBE_tls_client_auth_cert={home}/engflow-mTLS/engflow.crt",
            "-v", f"{home}:{home}:rw",
            "-v", f"{self.source_dir}:{self.source_dir}:rw",
            "-w", str(self.source_dir),
            "-d",
            self.image,
            "sleep", "infinity",
        ], check=True)

        # Create the user inside the container
        subprocess.run([
            "docker", "exec", "-u", "0", self.name,
            "useradd", "-d", home, "-u", str(uid), username,
        ], check=False)

        print(f"Container running: {self.name}")
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
            raise subprocess.CalledProcessError(proc.returncode, cmd)
        return elapsed


def clean_test_repo(source_dir):
    """Reset the source tree to a clean state, undoing any modifications."""
    print("  Cleaning test repo...")
    subprocess.run(["git", "checkout", "--", "."], cwd=str(source_dir), check=True)


def modify_file_to_trigger_incremental_build(container):
    """Inject a unique #define into vtkObject.h to trigger a cascade rebuild."""
    touch_uuid = str(uuid.uuid4())
    container.run(f'sed -i "1i #define TIPI \\"{touch_uuid}\\"" Common/Core/vtkObject.h')


def run_benchmarks(source_dir, image, iterations, toolchains, tool_name, output_dir, run_steps):
    """Common loop: iterate toolchains x iterations, run tool-specific steps inside a container."""
    results = []

    for toolchain_path, description in toolchains:
        tc_name = Path(toolchain_path).stem
        print(f"\n=== {tool_name} benchmark: {description} ({tc_name}) ===")

        for i in range(1, iterations + 1):
            print(f"\n--- Iteration {i}/{iterations} ---")
            clean_test_repo(source_dir)

            log_dir = output_dir / "logs" / tool_name / tc_name / f"iter_{i}"
            with DockerContainer(image, source_dir, log_dir) as container:
                result = BenchmarkResult(tool=tool_name, toolchain=tc_name, description=description, iteration=i)
                run_steps(container, result, toolchain_path)
                results.append(asdict(result))

            build_path = source_dir / "build"
            if build_path.exists():
                shutil.rmtree(build_path)

    return results


def cmake_steps(container, result, toolchain):
    result.record("configure", container.run(f"tipi run cmake -GNinja -S . -B ./build -DCMAKE_TOOLCHAIN_FILE={toolchain}", step="configure"))
    result.record("build", container.run("tipi run cmake --build ./build", step="build"))

    # Clean then rebuild
    container.run("tipi run cmake --build ./build --target clean", step="clean")
    result.record("rebuild", container.run("tipi run cmake --build ./build", step="rebuild"))

    modify_file_to_trigger_incremental_build(container)
    result.record("touch_rebuild", container.run("tipi run cmake --build ./build", step="touch_rebuild"))


def make_cmake_re_steps(jobs):
    def cmake_re_steps(container, result, toolchain):
        silo_key = str(uuid.uuid4())
        build_cmd = f'RBE_platform="cache-silo-key={silo_key}" cmake-re --build ./build --host --distributed -j{jobs}'

        result.record("configure", container.run(f"cmake-re -GNinja -S . -B ./build -DCMAKE_TOOLCHAIN_FILE={toolchain} --host --distributed", step="configure"))
        result.record("build_no_cache", container.run(build_cmd, step="build_no_cache"))

        # Clean build artifacts, keep RBE cache warm
        container.run("cmake-re --build ./build --target clean --host", step="clean")
        result.record("build_with_cache", container.run(build_cmd, step="build_with_cache"))

        modify_file_to_trigger_incremental_build(container)
        result.record("touch_rebuild", container.run(build_cmd, step="touch_rebuild"))

    return cmake_re_steps


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
  "output_dir":  "<directory for logs and results (default: output)>"
}"""
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=example_config,
    )
    parser.add_argument("config", help="path to JSON configuration file")
    args = parser.parse_args()

    with open(args.config) as f:
        config = json.load(f)

    iterations = config.get("iterations", 1)
    jobs = config.get("jobs", 1500)
    output_dir = Path(config.get("output_dir", "output"))
    output_dir.mkdir(parents=True, exist_ok=True)

    toolchains = [(tc["path"], tc["description"]) for tc in config["toolchains"]]

    target = 65535
    resource.setrlimit(resource.RLIMIT_NOFILE, (target, target))

    source_dir = clone_repo(config["repo_url"], config["branch"])
    image = pull_docker_image(config["image"])

    all_results = []
    all_results.extend(run_benchmarks(source_dir, image, iterations, toolchains, "cmake", output_dir, cmake_steps))
    all_results.extend(run_benchmarks(source_dir, image, iterations, toolchains, "cmake-re", output_dir, make_cmake_re_steps(jobs)))

    results_file = output_dir / "benchmark-results.json"
    with open(results_file, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nAll results written to {results_file}")


if __name__ == "__main__":
    main()
