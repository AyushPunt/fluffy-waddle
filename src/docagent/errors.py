class DocAgentError(Exception):
    """Base class for every refusal the template engine raises."""


class PackageError(DocAgentError):
    """The .docx container is malformed or uses a feature we refuse to handle."""


class TemplateError(DocAgentError):
    """The template cannot be indexed or an anchor cannot be resolved."""


class ManifestError(DocAgentError):
    """The manifest is invalid, unapproved, or does not match the template."""


class PlanError(DocAgentError):
    """The fill plan, system record or boilerplate library is invalid."""


class GateError(DocAgentError):
    """The immutability gate rejected an output."""
