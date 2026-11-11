import re

import exprun


def test_the_package_has_a_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+", exprun.__version__)
