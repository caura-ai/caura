"""Prove the built collaboration distributions install together on their own.

Run after ``uv build --all-packages`` (it writes ``dist/``)::

    python scripts/verify_dist.py dist [--expect-version X.Y.Z]

The check refuses a release unless:

* ``dist/`` holds exactly one wheel and one sdist for each of the four packages
  and nothing else, all at one version (and at ``--expect-version`` when given);
* every intra-family dependency is pinned to that exact version, so a published
  ``caura-bus-mcp`` can never resolve against a different ``caura-bus-core``;
* the four wheels install into a fresh virtualenv created in an empty temporary
  directory, with no workspace, sibling checkout or ``PYTHONPATH`` in reach, and
  ``pip check`` is clean;
* each installed module is imported from that virtualenv's ``site-packages``
  (not an editable or source-tree path); and
* every console entrypoint answers ``--help`` and reports the version.

Third-party dependencies resolve from the configured package index; the caura
packages themselves come only from the wheel files passed to pip.
"""

from __future__ import annotations

import argparse
import email.parser
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

CORE = "caura-bus-core"
PACKAGES = {
    # distribution -> (import package, intra-family dependencies)
    CORE: ("caura_bus_core", ()),
    "caura-bus-mcp": ("caura_bus_mcp", (CORE,)),
    "caura-bus-cli": ("caura_bus_cli", (CORE,)),
    "caura-bus-adapter-sdk": ("caura_bus_adapter", (CORE,)),
}
# entrypoint -> (distribution whose version it must print, version flag or None)
ENTRYPOINTS = {
    "caura-bus": ("caura-bus-cli", "--version"),
    "caura-bus-mcp": ("caura-bus-mcp", "--version"),
    "caura-bus-adapter-echo": ("caura-bus-adapter-sdk", None),
}
WHEEL = re.compile(r"^(?P<name>[a-z0-9_]+)-(?P<version>[^-]+)-py3-none-any\.whl$")
SDIST = re.compile(r"^(?P<name>[a-z0-9_]+)-(?P<version>[^-]+)\.tar\.gz$")


def fail(message: str) -> None:
    raise SystemExit(f"verify_dist: {message}")


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def inventory(dist: Path) -> tuple[str, dict[str, Path]]:
    wheels: dict[str, Path] = {}
    sdists: dict[str, Path] = {}
    versions: set[str] = set()
    for path in sorted(dist.iterdir()):
        if path.name.startswith("."):
            continue
        for pattern, bucket in ((WHEEL, wheels), (SDIST, sdists)):
            match = pattern.match(path.name)
            if match:
                name = normalise(match["name"])
                if name not in PACKAGES or name in bucket:
                    fail(f"unexpected or duplicate artifact {path.name}")
                bucket[name] = path
                versions.add(match["version"])
                break
        else:
            fail(f"unexpected file in {dist}: {path.name}")
    for kind, bucket in (("wheel", wheels), ("sdist", sdists)):
        missing = sorted(set(PACKAGES) - set(bucket))
        if missing:
            fail(f"missing {kind}(s): {', '.join(missing)}")
    if len(versions) != 1:
        fail(f"artifacts disagree on version: {sorted(versions)}")
    return versions.pop(), wheels


def check_pins(version: str, wheels: dict[str, Path]) -> None:
    for name, wheel in wheels.items():
        with zipfile.ZipFile(wheel) as archive:
            (metadata_name,) = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
            metadata = email.parser.Parser().parsestr(archive.read(metadata_name).decode())
        if metadata["Version"] != version:
            fail(f"{wheel.name} metadata says {metadata['Version']}")
        requires = {}
        for requirement in metadata.get_all("Requires-Dist") or []:
            match = re.match(r"^([A-Za-z0-9_.-]+)\s*(.*)$", requirement)
            if match:
                requires[normalise(match[1])] = match[2].replace(" ", "")
        for dependency in PACKAGES[name][1]:
            if requires.get(dependency) != f"=={version}":
                fail(f"{name} must require {dependency}=={version}, found {requires.get(dependency)!r}")
        print(f"ok   {name}=={version} pins {list(PACKAGES[name][1]) or 'no family packages'}")


def run(command: list[str], cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        fail(f"{' '.join(command)} exited {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def install_and_exercise(version: str, wheels: dict[str, Path]) -> None:
    with tempfile.TemporaryDirectory(prefix="caura-bus-verify-") as scratch:
        root = Path(scratch)
        venv = root / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        bindir = venv / ("Scripts" if os.name == "nt" else "bin")
        python = bindir / "python"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV", "UV_"))}
        env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
        empty = root / "cwd"
        empty.mkdir()
        run(
            [str(python), "-m", "pip", "install", "--quiet", "--no-cache-dir"]
            + [str(w.resolve()) for w in wheels.values()],
            empty,
            env,
        )
        run([str(python), "-m", "pip", "check"], empty, env)
        probe = (
            "import importlib, importlib.metadata as m, json, sys\n"
            "out = {}\n"
            "for dist, module in json.loads(sys.argv[1]).items():\n"
            "    d = m.distribution(dist)\n"
            "    direct = d.read_text('direct_url.json')\n"
            "    out[dist] = {'version': d.version, 'file': importlib.import_module(module).__file__,\n"
            "                 'editable': bool(direct and json.loads(direct).get('dir_info', {}).get('editable'))}\n"
            "print(json.dumps({'site': [p for p in sys.path if p.endswith('site-packages')], 'dists': out}))\n"
        )
        modules = {name: module for name, (module, _) in PACKAGES.items()}
        report = json.loads(run([str(python), "-I", "-c", probe, json.dumps(modules)], empty, env))
        site = [Path(p).resolve() for p in report["site"]]
        for name, facts in report["dists"].items():
            location = Path(facts["file"]).resolve()
            if facts["version"] != version:
                fail(f"installed {name} is {facts['version']}, expected {version}")
            if facts["editable"] or not any(location.is_relative_to(s) for s in site):
                fail(f"{name} imported from {location}, not the clean virtualenv's site-packages")
            print(f"ok   {name}=={facts['version']} imported from site-packages")
        for entrypoint, (dist, version_flag) in ENTRYPOINTS.items():
            executable = bindir / entrypoint
            help_text = run([str(executable), "--help"], empty, env)
            if "usage" not in help_text.lower():
                fail(f"{entrypoint} --help printed no usage:\n{help_text}")
            print(f"ok   {entrypoint} --help ({help_text.strip().splitlines()[0].strip()})")
            if version_flag:
                reported = run([str(executable), version_flag], empty, env).strip()
                if not reported.endswith(version):
                    fail(f"{entrypoint} {version_flag} printed {reported!r}, expected {version} from {dist}")
                print(f"ok   {entrypoint} {version_flag} -> {reported}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dist", type=Path)
    parser.add_argument("--expect-version")
    args = parser.parse_args()
    version, wheels = inventory(args.dist)
    if args.expect_version and args.expect_version != version:
        fail(f"artifacts are {version}, release expects {args.expect_version}")
    check_pins(version, wheels)
    install_and_exercise(version, wheels)
    print(f"verified {len(PACKAGES)} distributions at {version} install together from artifacts alone")


if __name__ == "__main__":
    main()
