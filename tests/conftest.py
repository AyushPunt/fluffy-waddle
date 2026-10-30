import pytest

from docagent.writer import fill
from tests.fixtures import review
from tests.fixtures.csra import build_template


@pytest.fixture(scope="session")
def template() -> bytes:
    return build_template()


@pytest.fixture(scope="session")
def manifest(template):
    return review.approved_manifest(template)


@pytest.fixture(scope="session")
def filled(template, manifest):
    return fill(template, manifest, review.record(), review.plan(manifest), review.library())
