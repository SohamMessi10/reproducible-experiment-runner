from exprun.environment import RECORDED_VARIABLES, capture, installed_packages


def test_the_environment_records_listed_variables_only(monkeypatch):
    monkeypatch.setenv("OMP_NUM_THREADS", "4")
    monkeypatch.setenv("EXPRUN_TEST_SECRET_TOKEN", "do-not-record")
    record = capture()
    assert record["variables"]["OMP_NUM_THREADS"] == "4"
    assert set(record["variables"]) <= set(RECORDED_VARIABLES)
    assert "do-not-record" not in repr(record)


def test_the_environment_uses_the_childs_variables_when_given():
    record = capture({"PYTHONHASHSEED": "0", "HOME": "/somewhere"})
    assert record["variables"] == {"PYTHONHASHSEED": "0"}


def test_the_environment_lists_installed_packages_and_platform():
    record = capture({})
    assert "pytest" in record["packages"] and record["packages"] == installed_packages()
    assert list(record["packages"]) == sorted(record["packages"])
    assert record["python"]["implementation"] == "CPython" and record["platform"]["cpu_count"] >= 1


def test_the_environment_summary_hash_is_stable_and_sensitive():
    assert capture({}) == capture({})
    assert capture({"OMP_NUM_THREADS": "1"})["summary_hash"] != capture({"OMP_NUM_THREADS": "2"})["summary_hash"]


def test_package_names_are_normalized_like_pip_does():
    packages = installed_packages()
    assert "pygments" in packages and "Pygments" not in packages      # its metadata says "Pygments"
    assert all(name == name.lower() and "_" not in name for name in packages)
