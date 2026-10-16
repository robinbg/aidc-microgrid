"""Record final test identities and ensure checks run against frozen sources."""
from pathlib import Path
from collections import Counter
import hashlib
import json
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).parent


def snapshot():
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for base, pattern in ((ROOT / "src", "*.py"), (ROOT / "tests", "*.py"), (ROOT / "configs", "*.yaml"))
            for path in sorted(base.rglob(pattern))}


before = snapshot()
xml_path = OUT / "final-full.xml"
command = [sys.executable, "-B", "-m", "pytest", "tests", "-p", "no:cacheprovider",
           "--basetemp", str(OUT / "pytest-temp-final"), "--tb=short", "-ra", "-o", "addopts=",
           "--junitxml", str(xml_path)]
start = time.perf_counter()
process = subprocess.run(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
(OUT / "final-full.txt").write_text(process.stdout, encoding="utf-8")
cases = []
if xml_path.exists():
    for case in ET.parse(xml_path).getroot().iter("testcase"):
        status = next((kind for kind in ("failure", "error", "skipped") if case.find(kind) is not None), "passed")
        cases.append({"module": case.get("classname"), "name": case.get("name"), "status": status, "seconds": float(case.get("time", 0))})
after = snapshot()
original = json.loads((ROOT / "outputs/m1_validation/protected_baseline.json").read_text(encoding="utf-8"))
config_same = {key: after[key] == value for key, value in original.items() if key.startswith("configs")}
result = {"command": command, "returncode": process.returncode, "elapsed_seconds": time.perf_counter() - start,
          "counts": dict(Counter(c["status"] for c in cases)), "total": len(cases),
          "by_module": dict(Counter(c["module"] for c in cases)),
          "skip_list": [c for c in cases if c["status"] == "skipped"],
          "source_before": before, "source_after": after, "sources_stable": before == after,
          "configs_unchanged_from_m1": config_same}
(OUT / "final-full-per-test.json").write_text(json.dumps(cases, indent=2), encoding="utf-8")
(OUT / "final-checks.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps({key: value for key, value in result.items() if key not in ("source_before", "source_after")}, indent=2))
sys.exit(process.returncode if process.returncode else 0 if before == after and all(config_same.values()) else 1)
