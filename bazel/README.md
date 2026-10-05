# Bazel benchmark

Benchmarks distributed Bazel builds of [Lambourl/bazel-benchmark](https://github.com/Lambourl/bazel-benchmark),
a synthetic workspace of 20,000 deliberately-expensive translation units.

Unlike the CMake benchmarks, this suite is **distributed-only**: every build
offloads compilation to the EngFlow RBE cluster (`--remote_executor` +
`--remote_cache`). No local-execution pass is run, matching the project's design
(local Apple-clang builds are not supported, and the point of the workload is to
exercise remote execution).

The RBE endpoint, remote instance, mTLS cert/key, platform exec properties and a
per-run cache-silo key are written into a generated `build:remote` config in a
bazelrc inside the container at the start of each iteration (derived from
`rbe_service`, `bazel_remote_instance`, `mtls_dir` and `image`). The build
commands then just pass `--config=remote`, so the connection details are
configured on the fly rather than hard-coded in the repo's `.bazelrc`.

### Platform exec properties (required by EngFlow)

EngFlow's docker-based runners pick which runner/image to execute an action in
from the action's **platform exec properties**. If Bazel only sends a
`cache-silo-key`, the scheduler rejects every action with:

```
INVALID_ARGUMENT: ... No matching action runner found
(available: WorkerInDockerActionRunner, CachedDockerActionRunner),
client-provided platform proto: properties { name: "cache-silo-key" ... }
```

To avoid this the benchmark always sends the runner-selecting
`container-image=docker://<image>` property plus the per-run `cache-silo-key`.
The image used here is `bazel_remote_image` if set, otherwise `image` — the RBE
workers pull it themselves, so it can point at a registry co-located with the
cluster (e.g. ACR/GHCR) to avoid Docker Hub rate limits, while the local
benchmark container keeps using `image`. Per EngFlow's platform-options reference the value must
start with `docker://` and should include a digest. Machine-platform properties
like `OSFamily` are *not* defaulted (EngFlow requires `OSFamily` and `ISA` to be
set together) — add them via `bazel_exec_properties` only if your pool needs them.

### Build Event Protocol (BES)

BES is **on by default** for Bazel: the generated `build:remote` config streams
the Build Event Protocol to EngFlow's BES backend, giving each invocation a web
UI (timeline, action details, logs). The relevant lines mirror the standard
EngFlow pattern and reuse the same gRPC endpoint + mTLS credentials:

```
build:remote --remote_executor=grpcs://<rbe host:port>
build:remote --bes_backend=grpcs://<rbe host:port>
build:remote --bes_results_url=https://<rbe host>/invocation/
```

Override the invocation-link base URL with `bazel_bes_results_url`, or set
`bazel_enable_bes: false` to disable BES.

## Measured steps

Each iteration runs, inside the Docker container, against the RBE cluster:

- **build** — cold distributed build under a unique cache-silo key
- **modified_file_rebuild** — touches a fraction of the generated TUs
  (`modified_fraction`, default 15%) rather than the shared header, so only
  those units recompile — a realistic incremental change
- **rebuild** — `bazel clean` followed by a rebuild to measure remote-cache hits

## To run

An `engflow-mTLS` folder (with `engflow.crt` / `engflow.key`) must be present in
the machine's home directory. This config does **not** download EngFlow
profiles.

```bash
cd bazel/
python3 ../benchmark.py config.json
```

Results land in `<output_dir>/benchmark-results.json` and
`<output_dir>/benchmark-results.csv`.

## Config knobs

Bazel-specific keys in `config.json` (see `../benchmark.py --help` for the full list):

- `build_system`: must be `"bazel"`.
- `bazel_targets`: target pattern to build (default `//...`).
- `bazel_bin`: bazel binary invoked in the container (default `bazel`).
- `bazel_remote_instance`: RBE remote instance name (default `default`).
- `bazel_remote_image`: docker image the RBE workers execute in (the
  `container-image` exec property). Falls back to `image` when unset. Point this
  at a cluster-local registry to avoid Docker Hub pull throttling.
- `bazel_args`: extra `bazel build` flags.
- `jobs`: passed as `--jobs` (high values fan work out across the cluster).
- `modified_sources_glob`: glob of TU sources touched for the incremental step
  (default `generated/srcs/*.cpp`).
- `modified_fraction`: fraction (0–1) of those sources to modify (default `0.15`).
- `bazel_exec_properties`: dict of RBE platform exec properties, merged over the
  default `container-image=docker://<image>`.
- `bazel_enable_bes`: stream the Build Event Protocol to EngFlow's BES backend
  for a per-invocation web UI (default `true`).
- `bazel_bes_results_url`: base URL for BES invocation links (default
  `https://<rbe host>/invocation/`).
