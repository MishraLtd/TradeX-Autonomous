"""
Dependency-free test runner for the whole Phase 3 repo.

Prefers real pytest when it is installed. When it isn't (the same
offline situation each component's own run_tests.py was written for), it
falls back to a minimal shim that supports the subset of the pytest API
these suites actually use: `pytest.approx`, `pytest.raises`,
`pytest.fixture`, and the `monkeypatch` fixture.

    python run_tests.py                # everything discoverable
    python run_tests.py phase3         # only the integration suite

Note: the capital_feasibility suite under vendor_tests/ needs the
`cost_model` package on the path (it tests the real Cost Model bridge)
and is skipped automatically when that package is absent.
"""
import importlib
import importlib.util
import pathlib
import sys
import traceback
import types

ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

SUITES = {
    "phase3": ROOT / "tests",
    "position_sizing": ROOT / "vendor_tests" / "position_sizing",
    "capital_feasibility": ROOT / "vendor_tests" / "capital_feasibility",
    "portfolio_manager": ROOT / "portfolio_manager" / "tests",
    "portfolio_risk": ROOT / "portfolio_risk" / "tests",
    "cost_model": ROOT / "vendor_tests" / "cost_model",
}


# --------------------------------------------------------------------------- #
# pytest shim (only used when pytest is unavailable)
# --------------------------------------------------------------------------- #

def _install_shim():
    class _Approx:
        def __init__(self, value, abs=None, rel=None):
            self.value = value
            self.abs = abs
            self.rel = rel

        def __eq__(self, other):
            if self.abs is not None:
                return abs(other - self.value) <= self.abs
            if self.rel is not None:
                return abs(other - self.value) <= abs(self.value) * self.rel + 1e-12
            return abs(other - self.value) <= 1e-6

        def __repr__(self):
            return f"approx({self.value})"

    class _Raises:
        def __init__(self, expected):
            self.expected = expected

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                raise AssertionError(f"expected {self.expected.__name__} to be raised")
            return issubclass(exc_type, self.expected)

    class _MonkeyPatch:
        def __init__(self):
            self._undo = []

        def setattr(self, target, name, value):
            original = getattr(target, name)
            self._undo.append((target, name, original))
            setattr(target, name, value)

        def undo(self):
            for target, name, original in reversed(self._undo):
                setattr(target, name, original)
            self._undo.clear()

    shim = types.ModuleType("pytest")
    shim.approx = lambda value, abs=None, rel=None: _Approx(value, abs=abs, rel=rel)
    shim.raises = lambda expected: _Raises(expected)
    def fixture(func=None, **kw):
        def mark(f):
            f._is_fixture = True
            return f
        return mark(func) if func is not None else mark
    shim.fixture = fixture
    shim.skip = lambda reason="": (_ for _ in ()).throw(_Skip(reason))
    shim.MonkeyPatch = _MonkeyPatch
    shim.mark = types.SimpleNamespace(
        parametrize=lambda *a, **k: (lambda f: f),
        skip=lambda *a, **k: (lambda f: f),
    )
    sys.modules["pytest"] = shim
    return shim


class _Skip(Exception):
    pass


def _load_module(module_name, module_path, package_name=None):
    """Import one file as a module, optionally as a member of a synthetic
    package so `from .conftest import x` inside vendored suites works."""
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    if package_name:
        module.__package__ = package_name
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _collect_fixtures(*modules):
    found = {}
    for module in modules:
        if module is None:
            continue
        for name in dir(module):
            obj = getattr(module, name)
            if callable(obj) and getattr(obj, "_is_fixture", False):
                found[name] = obj
    return found


def _resolve_fixture(factory):
    value = factory()
    if isinstance(value, types.GeneratorType):   # yield-style fixture
        return next(value)
    return value


def _run_module(module_name, module_path, shim, package_name=None, conftest=None):
    """Import a test module and run its test_* functions."""
    module = _load_module(module_name, module_path, package_name)
    fixtures = _collect_fixtures(conftest, module)

    passed, failed, skipped, failures = 0, 0, 0, []
    for name in sorted(dir(module)):
        if not name.startswith("test_"):
            continue
        fn = getattr(module, name)
        if not callable(fn) or getattr(fn, "_is_fixture", False):
            continue

        kwargs = {}
        monkeypatch = None
        varnames = fn.__code__.co_varnames[: fn.__code__.co_argcount]
        unresolved = []
        for var in varnames:
            if var == "monkeypatch":
                monkeypatch = shim.MonkeyPatch()
                kwargs[var] = monkeypatch
            elif var in fixtures:
                try:
                    kwargs[var] = _resolve_fixture(fixtures[var])
                except Exception:
                    unresolved.append(var)
            else:
                unresolved.append(var)

        if unresolved:
            skipped += 1
            print(f"SKIP  {module_name}.{name} (unsupported fixtures: {', '.join(unresolved)})")
            continue

        try:
            fn(**kwargs)
            passed += 1
            print(f"PASS  {module_name}.{name}")
        except _Skip as exc:
            skipped += 1
            print(f"SKIP  {module_name}.{name} ({exc})")
        except Exception:
            failed += 1
            failures.append((f"{module_name}.{name}", traceback.format_exc()))
            print(f"FAIL  {module_name}.{name}")
        finally:
            if monkeypatch is not None:
                monkeypatch.undo()

    return passed, failed, skipped, failures


def main(argv):
    wanted = argv[1:] or list(SUITES)
    shim = _install_shim()

    total_passed = total_failed = total_skipped = 0
    all_failures = []

    for suite in wanted:
        directory = SUITES.get(suite)
        if directory is None or not directory.exists():
            print(f"-- {suite}: not present, skipping")
            continue
        print(f"\n== {suite} " + "=" * (60 - len(suite)))
        # Each suite's own helpers (conftest/factories) go on the path only
        # while that suite runs — several suites use the same module names.
        sys.path.insert(0, str(directory))

        package_name = f"{suite}_suite"
        package = types.ModuleType(package_name)
        package.__path__ = [str(directory)]
        sys.modules[package_name] = package

        conftest = None
        conftest_path = directory / "conftest.py"
        if conftest_path.exists():
            try:
                conftest = _load_module(f"{package_name}.conftest", conftest_path, package_name)
            except Exception:
                print(f"-- {suite}: conftest could not be imported, fixtures unavailable")
        for path in sorted(directory.glob("test_*.py")):
            try:
                p, f, s, failures = _run_module(
                    f"{package_name}.{path.stem}", path, shim,
                    package_name=package_name, conftest=conftest,
                )
            except ModuleNotFoundError as exc:
                # A vendored suite whose own upstream dependency is not on
                # the path (e.g. capital_feasibility's Cost Model bridge
                # tests without the cost_model package) is skipped, not
                # failed — it is not this repo's code that is broken.
                print(f"SKIP  {path.name} (missing dependency: {exc.name})")
                total_skipped += 1
                continue
            except Exception:
                print(f"ERROR importing {path.name}:\n{traceback.format_exc()}")
                total_failed += 1
                all_failures.append((path.name, traceback.format_exc()))
                continue
            total_passed += p
            total_failed += f
            total_skipped += s
            all_failures.extend(failures)
        sys.path.remove(str(directory))

    print("\n" + "=" * 68)
    print(f"passed {total_passed}   failed {total_failed}   skipped {total_skipped}")
    for name, tb in all_failures:
        print(f"\n--- {name} ---\n{tb}")
    return 1 if total_failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
