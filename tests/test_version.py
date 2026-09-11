"""The version comes from git tags via setuptools-scm, not a hardcoded string."""

import subprocess
import sys

import eve


def test_version_is_a_real_string():
    assert isinstance(eve.__version__, str)
    assert eve.__version__


def test_version_is_not_the_no_git_fallback():
    """A dev install has git available, so the fallback must not be in play."""
    assert eve.__version__ != "0.0.0+unknown"


def test_version_starts_from_a_tag():
    """setuptools-scm output begins with the tagged number, e.g. 0.1.0 or 0.1.1.dev3."""
    assert eve.__version__[0].isdigit()


def test_cli_reports_the_same_version():
    out = subprocess.run(
        [sys.executable, "-m", "eve", "--version"], capture_output=True, text=True, check=True
    ).stdout
    assert eve.__version__ in out
