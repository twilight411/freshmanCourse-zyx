"""Reproduce Java flow execution with external private-repository settings, or --portable.

Credentials stay outside the repository; no model API calls.
"""
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import base64
import urllib.parse
import re

ROOT = Path(__file__).resolve().parents[1]
REVISION = "93cc223de5bc8f0510801796a46a8ae391725be9"
UPSTREAM = "https://github.com/ymxiong/rsi-flow.git"
MATERIALS = ROOT / "experiments/rsi-flow"
TESTS = "FlowLongAgentTest,PortableBuildTest,FlowStandardTest,FlowNestTest,FlowConditionTest,FlowMultiNodeTest,FlowSingleNodeTest"
BEIJING = timezone(timedelta(hours=8))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--portable", action="store_true", help="Public-only build without private scheduling")
    parser.add_argument("--settings", type=Path, help="External Maven settings containing authorized credentials")
    args = parser.parse_args()
    settings_path = args.settings or (Path(os.environ["RSI_MAVEN_SETTINGS"]) if os.environ.get("RSI_MAVEN_SETTINGS") else None)
    if not args.portable and (settings_path is None or not settings_path.is_file()):
        raise SystemExit("Pass --settings PATH to your authorized Maven settings.xml, or --portable for public-only validation.")
    patch = MATERIALS / ("portable-validation.patch" if args.portable else "authenticated-validation.patch")
    tests = TESTS if args.portable else TESTS.replace("PortableBuildTest", "AuthenticatedDependencyTest")
    mode = "portable" if args.portable else "authenticated"
    maven = shutil.which("mvn")
    if not maven or not shutil.which("java") or not shutil.which("git"):
        raise SystemExit("Install Git, a JDK (verified: 17), and Maven (verified: 3.8.9) first.")
    work = ROOT / ".work"
    project = work / ("rsi-flow" if args.portable else "rsi-flow-authenticated")
    cache = args.cache or work / ("maven-repository" if args.portable else "maven-repository-authenticated")
    work.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["MAVEN_OPTS"] = env.get("MAVEN_OPTS", "") + " -Dfile.encoding=UTF-8 -Duser.timezone=Asia/Shanghai"

    def capture(command, cwd=ROOT):
        return subprocess.check_output(command, cwd=cwd, env=env, stderr=subprocess.STDOUT).decode("utf-8", errors="replace").strip()

    patch_sha = hashlib.sha256(patch.read_bytes()).hexdigest()
    marker = work / (project.name + "-patch.sha256")
    if not project.exists():
        subprocess.run(["git", "clone", "--no-checkout", UPSTREAM, str(project)], check=True, env=env)
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=project, check=True, env=env)
        subprocess.run(["git", "checkout", "--detach", REVISION], cwd=project, check=True, env=env)
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=project, check=True, env=env)
        subprocess.run(["git", "apply", str(patch)], cwd=project, check=True, env=env)
        marker.write_text(patch_sha, encoding="ascii")
    else:
        if capture(["git", "rev-parse", "HEAD"], project) != REVISION:
            raise SystemExit("Existing validation checkout has a different revision; move .work/rsi-flow aside before retrying.")
        if not marker.exists() or marker.read_text(encoding="ascii") != patch_sha:
            raise SystemExit("Existing validation checkout has a different patch; move .work/rsi-flow aside before retrying.")
        subprocess.run(["git", "apply", "--reverse", "--check", str(patch)], cwd=project, check=True, env=env)
    settings = settings_path.resolve() if not args.portable else work / "maven-central-only.xml"
    secrets = set()
    if not args.portable:
        config = ET.parse(settings).getroot()
        for server in config.findall("{*}servers/{*}server"):
            def resolve(value):
                return re.sub(r"\$\{env\.([^}]+)\}", lambda match: os.environ.get(match.group(1), match.group(0)), value or "")
            user = resolve(server.findtext("{*}username"))
            password = resolve(server.findtext("{*}password"))
            for secret in (user, password, base64.b64encode(f"{user}:{password}".encode()).decode()):
                if len(secret) >= 3:
                    secrets.add(secret)
                    secrets.add(urllib.parse.quote(secret, safe=""))
    else:
        settings.write_text('<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0"><mirrors><mirror>'
                        '<id>public-central-only</id><mirrorOf>*</mirrorOf>'
                        '<url>https://repo.maven.apache.org/maven2</url>'
                        '</mirror></mirrors></settings>', encoding="utf-8")
    stamp = datetime.now(BEIJING).strftime("%Y%m%d_%H%M%S")
    output = ROOT / "experiments/rsi-flow/runs" / stamp
    output.mkdir(parents=True, exist_ok=False)
    summary = {"started_at_beijing": datetime.now(BEIJING).isoformat(), "mode": mode, "upstream": UPSTREAM,
               "upstream_commit": REVISION, "patch_sha256": patch_sha,
               "maven_version": capture([maven, "-version"]), "java_version": capture(["java", "-version"]),
               "repository_policy": ("Maven Central only; isolated local cache" if args.portable else "authorized external Maven settings; isolated local cache; credentials not archived"),
               "model_api_calls": 0, "status": "running"}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    commands = [
        [sys.executable, "flow-test/validate_long_agent.py"],
        [sys.executable, "-m", "unittest", "discover", "-s", "flow-test", "-p", "test_validate_long_agent.py", "-v"],
        [maven, "-B", "-ntp", "-s", os.path.relpath(settings, project),
         f"-Dmaven.repo.local={os.path.relpath(cache.resolve(), project)}",
         "-pl", "flow-test", "-am", "clean", "test", f"-Dtest={tests}", "-Dsurefire.failIfNoSpecifiedTests=false"],
    ]
    names = ["graph.log", "graph-tests.log", "maven.log"]
    if not args.portable:
        commands.append([maven, "-B", "-ntp", "-s", os.path.relpath(settings, project),
                         f"-Dmaven.repo.local={os.path.relpath(cache.resolve(), project)}", "-DskipTests", "package"])
        names.append("package.log")
    print(f"Running validation; evidence: {output}", flush=True)
    try:
        for name, command in zip(names, commands):
            print(f"Running {name}", flush=True)
            with (output / name).open("wb") as log:
                process = subprocess.Popen(command, cwd=project, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                for raw in process.stdout:
                    line = raw.decode("utf-8", errors="replace")
                    for secret in sorted(secrets, key=len, reverse=True):
                        line = line.replace(secret, "[REDACTED]")
                    log.write(line.encode("utf-8"))
                if process.wait() != 0:
                    raise RuntimeError(f"Validation step failed; inspect {name}")
        report = project / "flow-test/target/surefire-reports/testng-results.xml"
        tree = ET.parse(report)
        attrs = tree.getroot().attrib
        assert int(attrs["total"]) == 20 and int(attrs["passed"]) == 20
        assert int(attrs["failed"]) == 0 and int(attrs["skipped"]) == 0
        summary["java_tests"] = {k: int(attrs[k]) for k in ("total", "passed", "failed", "skipped")}
        summary["test_methods"] = [element.attrib["name"] for element in tree.iter("test-method")
                                   if element.attrib.get("is-config") != "true"]
        # Publish portable evidence, excluding raw XML with machine-specific JVM properties.
        summary["structure_traces"] = [line for line in (output / "maven.log").read_text(encoding="utf-8", errors="replace").splitlines()
                                       if line.startswith("STRUCTURE ")]
        summary["status"] = "passed"
        if not args.portable:
            summary["all_eight_modules_packaged"] = True
        (MATERIALS / ("latest-" + mode + ".json")).write_text(json.dumps({
            "mode": mode, "status": "passed", "run": str(output.relative_to(MATERIALS)).replace("\\", "/"),
            "summary": str((output / "summary.json").relative_to(MATERIALS)).replace("\\", "/"),
            "patch_sha256": patch_sha}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("PASS: 8 graph-validator tests and 20 Java engine tests; no skipped Java tests", flush=True)
    except Exception:
        summary["status"] = "failed"
        raise
    finally:
        summary["finished_at_beijing"] = datetime.now(BEIJING).isoformat()
        summary["evidence_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                       for p in output.glob("*.log")}
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
