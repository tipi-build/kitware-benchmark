#!/usr/bin/env python3
"""Benchmark configure and build times for CMake and cmake-re."""

import argparse
import getpass
import os
import shutil
import subprocess
import tempfile
import time
import uuid
import resource
from pathlib import Path

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


def start_docker(image, source_dir, container_name=None):
    """Start a detached docker container with source_dir mounted. Returns the container name."""
    if container_name is None:
        container_name = str(uuid.uuid4())
    uid = os.getuid()
    gid = os.getgid()
    username = getpass.getuser()
    home = os.environ["HOME"]

    print(f"Starting container {container_name}...")
    subprocess.run([
        "docker", "run",
        "--platform", "linux/amd64",
        "--rm", "--init",
        "--name", container_name,
        f"-u{uid}:{gid}", "--group-add", "tipi",
        "--ulimit", "nofile=65535:65535",   #
        "-e", "TIPI_DISABLE_AR_RANLIB_DRIVER=ON",
        "-e", "TIPI_CACHE_CONSUME_ONLY=ON",
        "-e", "TIPI_CACHE_FORCE_ENABLE=OFF",
        "-e", "HOME",
        "-e", "RBE_service=kernite.cluster.engflow.com:443",
        "-e", f"RBE_tls_client_auth_key={home}/engflow-mTLS/engflow.key",
        "-e", f"RBE_tls_client_auth_cert={home}/engflow-mTLS/engflow.crt",
        "-v", f"{home}:{home}:rw",
        "-v", f"{source_dir}:{source_dir}:rw",
        "-w", str(source_dir),
        "-d",
        image,
        "sleep", "infinity",
    ], check=True)

    # Create the user inside the container
    subprocess.run([
        "docker", "exec", "-u", "0", container_name,
        "useradd", "-d", home, "-u", str(uid), username,
    ], check=False)

    print(f"Container running: {container_name}")
    return container_name


def stop_docker(container_name):
    """Stop a running docker container."""
    print(f"Stopping container {container_name[:8]}...")
    subprocess.run(["docker", "stop", "-t0", container_name], check=True)
    print("Container stopped.")


def docker_exec(container_name, cmd):
    """Run a command inside a running docker container and return elapsed time."""
    print(f"  [{container_name[:8]}] Running: {cmd}")
    start = time.perf_counter()
    subprocess.run(["docker", "exec", container_name, "bash", "-c", cmd], check=True)
    elapsed = time.perf_counter() - start
    print(f"  Done in {elapsed:.2f}s")
    return elapsed


def benchmark_vtk_project_cmake(source_dir, image, iterations, toolchains):
    """Benchmark cmake configure+build on VTK for each toolchain."""
    for toolchain, description in toolchains:
        tc_name = Path(toolchain).stem
        build_file = f"cmake-run_build_{tc_name}.txt"
        rebuild_file = f"cmake-run_rebuild_{tc_name}.txt"
        touch_file = f"cmake-run_touch_rebuild_{tc_name}.txt"
        print(f"\n=== CMake benchmark: {description} ({tc_name}) ===")
        with open(build_file, "w") as f_build, \
             open(rebuild_file, "w") as f_rebuild, \
             open(touch_file, "w") as f_touch:
            for i in range(1, iterations + 1):
                print(f"\n--- Iteration {i}/{iterations} ---")
                container_name = str(uuid.uuid4())
                container = start_docker(image, source_dir, container_name)

                docker_exec(container, f"tipi run cmake -GNinja -S . -B ./build -DCMAKE_TOOLCHAIN_FILE={toolchain}")
                build_time = docker_exec(container, "tipi run cmake --build ./build")

                f_build.write(f"{description} iteration {i} build {build_time:.2f}s\n")
                f_build.flush()
                print(f"  [build] Build: {build_time:.2f}s")

                # Clean then rebuild
                docker_exec(container, "tipi run cmake --build ./build --target clean")
                rebuild_time = docker_exec(container, "tipi run cmake --build ./build")

                f_rebuild.write(f"{description} iteration {i} rebuild {rebuild_time:.2f}s\n")
                f_rebuild.flush()
                print(f"  [rebuild] Build: {rebuild_time:.2f}s")

                # Touch vtkVersionQuick.h.in with a unique define to trigger cascade rebuild
                touch_uuid = str(uuid.uuid4())
                docker_exec(container, f'sed -i "1i #define TIPI \\"{touch_uuid}\\"" Common/Core/vtkVersionQuick.h.in')
                touch_rebuild_time = docker_exec(container, "tipi run cmake --build ./build")

                f_touch.write(f"{description} iteration {i} touch-rebuild {touch_rebuild_time:.2f}s\n")
                f_touch.flush()
                print(f"  [touch-rebuild] Build: {touch_rebuild_time:.2f}s")

                stop_docker(container_name)
                build_path = source_dir / "build"
                if build_path.exists():
                    shutil.rmtree(build_path)

        print(f"\nResults written to {build_file}, {rebuild_file} and {touch_file}")


def benchmark_vtk_project_cmake_re(source_dir, image, iterations, toolchains, jobs):
    """Benchmark cmake-re: no-cache run (seed) then with-cache run, for each toolchain."""
    for toolchain, description in toolchains:
        tc_name = Path(toolchain).stem
        no_cache_file = f"cmake-re-run_no_cache_{tc_name}.txt"
        with_cache_file = f"cmake-re-run_with_cache_{tc_name}.txt"
        touch_file = f"cmake-re-run_touch_rebuild_{tc_name}.txt"
        print(f"\n=== cmake-re benchmark with toolchain: {tc_name} ===")
        with open(no_cache_file, "w") as f_no_cache, \
             open(with_cache_file, "w") as f_with_cache, \
             open(touch_file, "w") as f_touch:
            for i in range(1, iterations + 1):
                print(f"\n--- Iteration {i}/{iterations} ---")
                container_name = str(uuid.uuid4())
                container = start_docker(image, source_dir, container_name)
                silo_key = str(uuid.uuid4())

                # First run: no cache (seed the RBE cache)
                print("  [no-cache] cmake-re configure + build...")
                docker_exec(container, f"cmake-re -GNinja -S . -B ./build -DCMAKE_TOOLCHAIN_FILE={toolchain} --host --distributed")
                build_time_no = docker_exec(container, f'RBE_platform="cache-silo-key={silo_key}" cmake-re --build ./build --host --distributed -j{jobs}')

                f_no_cache.write(f"{description} no-cache iteration {i} build {build_time_no:.2f}s\n")
                f_no_cache.flush()
                print(f"  [no-cache] Build: {build_time_no:.2f}s")

                # Clean build artifacts, keep RBE cache warm
                docker_exec(container, f'cmake-re --build ./build --target clean --host')

                # Second run: with warm RBE cache
                print("  [with-cache] cmake-re configure + build...")
                build_time_cache = docker_exec(container, f'RBE_platform="cache-silo-key={silo_key}" cmake-re --build ./build --host --distributed -j{jobs}')

                f_with_cache.write(f"{description} with-cache iteration {i} build {build_time_cache:.2f}s\n")
                f_with_cache.flush()
                print(f"  [with-cache] Build: {build_time_cache:.2f}s")

                # Touch vtkVersionQuick.h.in with a unique define to trigger cascade rebuild
                touch_uuid = str(uuid.uuid4())
                docker_exec(container, f'sed -i "1i #define TIPI \\"{touch_uuid}\\"" Common/Core/vtkVersionQuick.h.in')
                print("  [touch-rebuild] cmake-re build after header touch...")
                touch_rebuild_time = docker_exec(container, f'RBE_platform="cache-silo-key={silo_key}" cmake-re --build ./build --host --distributed -j{jobs}')

                f_touch.write(f"{description} touch-rebuild iteration {i} build {touch_rebuild_time:.2f}s\n")
                f_touch.flush()
                print(f"  [touch-rebuild] Build: {touch_rebuild_time:.2f}s")

                stop_docker(container_name)
                build_path = source_dir / "build"
                if build_path.exists():
                    shutil.rmtree(build_path)

        print(f"\nResults written to {no_cache_file}, {with_cache_file} and {touch_file}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("-j", "--jobs", type=int, default=1500, help="number of parallel jobs for cmake-re builds")
    args = parser.parse_args()
    target = 65535
    resource.setrlimit(resource.RLIMIT_NOFILE, (target, target))

    toolchains = [
        ("toolchains/environments/linux-kitware-paraview-vtk-mini.cmake", "mini configuration"),
    ]

    source_dir = clone_repo("https://github.com/tipi-build/vtk", "feature/benchmark-branch")
    image = pull_docker_image("tipibuild/linux-kitware-paraview@sha256:e0417824c4d417eb4d363f08954d11b94f9e6eb4ec76cee391db72e1e281fb18")

    benchmark_vtk_project_cmake(source_dir, image, args.iterations, toolchains)
    benchmark_vtk_project_cmake_re(source_dir, image, args.iterations, toolchains, args.jobs)


if __name__ == "__main__":
    main()
